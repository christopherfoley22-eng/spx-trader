"""Bounded local synthetic stream into the independent protective lifecycle.

Reuses the existing read-only subscription manifest when supplied. Owns no
transport, broker credentials, entry authority or browser endpoint.
"""
from collections import OrderedDict
from dataclasses import asdict,dataclass
import hashlib
import heapq
import json
from threading import RLock,Condition,Thread,current_thread
from executor_paper_broker import integer,text_id
from executor_paper_types import PaperBrokerError


@dataclass(frozen=True)
class ProtectiveStreamEvent:
    kind:str
    event_id:str
    instrument:str
    sequence:int
    payload:object = None
    source:str = 'SYNTHETIC_FIXTURE'
    origin:str = 'OFFLINE_FIXTURE'


class OfflineProtectiveStream:
    def __init__(self,lifecycle,token,trade,capacity=64):
        lifecycle.app.protection.require(token,'OBSERVE_RISK')
        if not integer(capacity,1,4096):raise PaperBrokerError('BOUNDED_STREAM_CAPACITY_REQUIRED')
        self.lifecycle=lifecycle;self.token=token;self.trade=trade;self.capacity=capacity
        self.queue=[];self.serial=0;self.lock=RLock();self.processing=RLock();self.sequences={};self.seen=OrderedDict();self.pending={}
        self.status='SYNTHETIC_SUBSCRIBED';self.failure=None
        self.condition=Condition(self.lock);self.worker=None;self.running=False

    @property
    def engine(self):return self.lifecycle.engines[self.trade]

    def manifest(self,market_stream=None,generation=None):
        self.lifecycle.app.protection.require(self.token,'OBSERVE_RISK')
        if market_stream is not None:
            from executor_market_stream import ReadOnlyMarketStream
            if not isinstance(market_stream,ReadOnlyMarketStream):raise PaperBrokerError('READ_ONLY_MARKET_STREAM_REQUIRED')
            return market_stream.subscription_manifest(generation)
        return tuple([dict(instrument='SPX',origin='SYNTHETIC_ONLY',transport_enabled=False)]+
            [dict(instrument=str(cid),contract=target['contract'].copy(),origin='SYNTHETIC_ONLY',transport_enabled=False)
             for cid,target in self.engine.portfolio['targets'].items()])

    def _fail(self,reason):
        self.failure=reason;self.status='FAILED_ENTRY_FENCED';self.engine.exit_readiness.invalidate() if getattr(self.engine,'exit_readiness',None) else None
        with self.lifecycle.lock:self.lifecycle._fence(reason,critical=True)

    def enqueue(self,event):
        self.lifecycle.app.protection.require(self.token,'EXIT_ALL' if getattr(event,'kind',None)=='EMERGENCY' else 'OBSERVE_RISK')
        if (not isinstance(event,ProtectiveStreamEvent) or event.kind not in {'SPX','QUOTE','DEPTH','EMERGENCY'} or
            not text_id(event.event_id) or not text_id(event.instrument) or not integer(event.sequence,1) or
            event.origin!='OFFLINE_FIXTURE' or not text_id(event.source)):
            raise PaperBrokerError('INVALID_PROTECTIVE_STREAM_EVENT')
        # Existing independent global fence is durable before queue admission.
        if event.kind=='EMERGENCY':self.lifecycle.app.protection.exit_all(self.token,event.event_id)
        try:digest=hashlib.sha256(json.dumps(asdict(event),sort_keys=True,default=str,allow_nan=False).encode()).hexdigest()
        except Exception:
            self._fail('INVALID_STREAM_PAYLOAD');raise PaperBrokerError('INVALID_STREAM_PAYLOAD')
        with self.lock:
            old=self.pending.get(event.event_id,self.seen.get(event.event_id))
            if old is not None:
                if old!=digest:self._fail('CHANGED_STREAM_EVENT');raise PaperBrokerError('CHANGED_STREAM_EVENT')
                return 'DUPLICATE'
            if len(self.queue)>=self.capacity:
                self._fail('STREAM_BACKPRESSURE_UNRESOLVED');raise PaperBrokerError('STREAM_BACKPRESSURE_UNRESOLVED')
            self.serial+=1;heapq.heappush(self.queue,(0 if event.kind=='EMERGENCY' else 1,self.serial,event,digest))
            self.pending[event.event_id]=digest
            self.condition.notify()
        return 'QUEUED'

    def start_worker(self):
        """Opt-in local synthetic consumer; wakes on events, never price polls."""
        self.lifecycle.app.protection.require(self.token,'OBSERVE_RISK')
        with self.condition:
            if self.running:return
            if self.failure:raise PaperBrokerError('FAILED_STREAM_REQUIRES_RECOVERY')
            self.running=True;self.worker=Thread(target=self._run,name='offline-protective-stream',daemon=True)
            self.worker.start()

    def _run(self):
        while True:
            with self.condition:
                while self.running and not self.queue:self.condition.wait()
                if not self.running:return
            try:self.drain_one()
            except Exception:
                with self.condition:self.running=False
                return  # Durable fence remains; no automatic unsafe rearm.

    def stop_worker(self):
        with self.condition:self.running=False;self.condition.notify_all()
        if self.worker and self.worker is not current_thread():
            self.worker.join(timeout=2)
            if self.worker.is_alive():raise PaperBrokerError('STREAM_WORKER_SHUTDOWN_UNRESOLVED')
        if self.worker and self.status!='STOPPED_ENTRY_FENCED':
            self._fail('READINESS_STREAM_WORKER_STOPPED');self.status='STOPPED_ENTRY_FENCED'

    def disconnect(self):
        self.lifecycle.app.protection.require(self.token,'OBSERVE_RISK')
        self._fail('READINESS_STREAM_DISCONNECTED')
        self.status='DISCONNECTED_ENTRY_FENCED'

    def drain_one(self):
        # One event worker; emergency can still latch independently of this lock.
        with self.processing:
            with self.lock:
                if not self.queue:return None
                _,_,event,digest=heapq.heappop(self.queue)
            try:
                self.lifecycle.app.protection.require(self.token,'EXIT_ALL' if event.kind=='EMERGENCY' else 'OBSERVE_RISK')
                key=(event.kind,event.instrument)
                if event.sequence<=self.sequences.get(key,0):raise PaperBrokerError('OUT_OF_ORDER_STREAM_EVENT')
                if event.kind=='EMERGENCY':result=self.lifecycle.supervise(self.token)
                elif event.kind=='SPX':
                    if event.instrument!='SPX':raise PaperBrokerError('WRONG_SPX_STREAM_IDENTITY')
                    result=self.lifecycle.observe_spx(self.token,self.trade,event.event_id,event.payload)
                else:
                    cid=event.payload.option.contract.con_id if event.kind=='QUOTE' else event.payload.contract.con_id
                    if str(cid)!=event.instrument:raise PaperBrokerError('WRONG_OPTION_STREAM_IDENTITY')
                    if event.kind=='QUOTE':result=self.lifecycle.observe_liquidity(self.token,self.trade,event.event_id,event.payload)
                    else:result=self.lifecycle.observe_exit_depth(self.token,self.trade,event.payload)
                self.sequences[key]=event.sequence
                with self.lock:
                    self.seen[event.event_id]=digest
                    if len(self.seen)>4096:self.seen.popitem(last=False)
                return result
            except Exception:
                self._fail('PROTECTIVE_STREAM_EVENT_UNRESOLVED');raise
            finally:
                with self.lock:self.pending.pop(event.event_id,None)
