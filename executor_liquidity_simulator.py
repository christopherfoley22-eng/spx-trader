"""Durable deterministic SYNTHETIC event book and delayed fake broker.

No socket/client, real data, execution permission or prediction. Economic broker
records reuse OfflineBrokerFixture. Prepared fill effects are replayed by stable
execution identity after a crash, never inferred from an in-memory position.
"""
from dataclasses import asdict,replace,dataclass
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
import math
import statistics
import time

from executor_offline_fixture import OfflineBrokerFixture,order_record
from executor_portfolio import PortfolioOrderRecord
from executor_paper_broker import money,utc
from executor_paper_types import TERMINAL
from mortificatio_store import encode
from mortificatio_models import ApplicationError
from simulation_evidence import VerifiedOptionContract


@dataclass(frozen=True)
class SimulationPolicy:
    seed:int
    submission_delay:float = .01
    acknowledgement_delay:float = .02
    cancel_delay:float = .03
    execution_report_delay:float = .01
    order_report_delay:float = 0.
    realization:object = '.65'
    reject_every:int = 0
    request_capacity:int = 100
    request_window:float = 1.
    commission_per_contract:object = None

    def normalized(self):
        if type(self.seed) is not int or type(self.reject_every) is not int or self.reject_every<0 or type(self.request_capacity) is not int or self.request_capacity<1:
            raise ValueError('Invalid synthetic execution policy')
        data=asdict(self)
        for name in ('submission_delay','acknowledgement_delay','cancel_delay','execution_report_delay','order_report_delay','request_window'):
            if not isinstance(data[name],(int,float)) or isinstance(data[name],bool) or not math.isfinite(data[name]) or data[name]<0:
                raise ValueError('Invalid synthetic timing')
        if self.request_window<=0 or not 0<=money(self.realization)<=1:raise ValueError('Invalid synthetic realization')
        data['realization']=str(money(self.realization))
        if self.commission_per_contract is None:data.pop('commission_per_contract')
        else:
            if money(self.commission_per_contract)<0:raise ValueError('Invalid synthetic commission')
            data['commission_per_contract']=str(money(self.commission_per_contract))
        return data


class VirtualMarketClock:
    def __init__(self,base,seconds=0.):self.base=utc(base);self.seconds=seconds
    def wall(self):return self.base+timedelta(seconds=self.seconds)
    def mono(self):return 100.+self.seconds


class EventDrivenBroker(OfflineBrokerFixture):
    def __init__(self,path,account,clock,policy):
        self.clock=clock;self.sim_policy=policy.normalized()
        super().__init__(path,account,clock.wall,clock.mono,clock.base-timedelta(days=1))
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sim_environment (singleton INTEGER PRIMARY KEY CHECK(singleton=1),policy TEXT NOT NULL,clock_base TEXT NOT NULL,state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS sim_events (event_id TEXT PRIMARY KEY,due REAL NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS sim_effects (effect_id TEXT PRIMARY KEY,payload TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS sim_applied (event_id TEXT PRIMARY KEY)')
            row=db.execute('SELECT policy,clock_base,state FROM sim_environment').fetchone()
            if row:
                if row[:2]!=(encode(self.sim_policy),clock.base.isoformat()):raise ApplicationError('SIMULATION_CONFIGURATION_CHANGED')
                applied=db.execute('SELECT MAX(e.due) FROM sim_events e JOIN sim_applied a ON a.event_id=e.event_id').fetchone()[0]
                clock.seconds=max(json.loads(row[2])['now'],applied or 0.)
            else:
                db.execute('INSERT INTO sim_environment VALUES (1,?,?,?)',(encode(self.sim_policy),clock.base.isoformat(),encode(dict(
                    now=clock.seconds,books={},requests=[],submissions=0,cancellations=0,rejections=0,sequence=0,metrics={},exposure=[]))))

    def state(self):
        with self.db() as db:return json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0])

    @staticmethod
    def _save(db,state):db.execute('UPDATE sim_environment SET state=?',(encode(state),))

    @staticmethod
    def _queue(db,event_id,due,kind,payload):
        old=db.execute('SELECT due,kind,payload FROM sim_events WHERE event_id=?',(event_id,)).fetchone()
        data=(due,kind,encode(payload))
        if old and old!=data:raise ApplicationError('SIMULATION_EVENT_ID_CHANGED')
        db.execute('INSERT OR IGNORE INTO sim_events(event_id,due,kind,payload) VALUES (?,?,?,?)',(event_id,*data))

    def _callback(self,db,trade,kind,record,stamp=None):
        raw=asdict(record);delay=self.sim_policy['execution_report_delay'] if kind in ('EXECUTION','COMMISSION') else self.sim_policy['order_report_delay']
        payload=dict(trade=trade,kind=kind,record=raw,stamp=stamp)
        identity='delivery:'+hashlib.sha256(encode(payload).encode()).hexdigest()
        # Duplicate economic evidence retains its first scheduled delivery time.
        if not db.execute('SELECT 1 FROM sim_events WHERE event_id=?',(identity,)).fetchone():
            self._queue(db,identity,self.clock.seconds+delay,'DELIVER',payload)

    def bindings(self,trade,engine,identities):
        self.require_connected()
        with self.db() as db:
            for key in identities:
                intent=engine.state['intents'][key]
                if intent['order_id'] is None:raise ApplicationError('DURABLE_MODEL_BINDING_REQUIRED')
                ref=intent['order_ref'];event='submit:'+ref
                if not db.execute('SELECT 1 FROM sim_events WHERE event_id=?',(event,)).fetchone():
                    self._queue(db,event,self.clock.seconds+self.sim_policy['submission_delay'],'SUBMIT',dict(trade=trade,intent=intent))

    def cancel_observations(self,trade,engine,actions):
        self.require_connected()
        with self.db() as db:
            for action in actions:
                if action['type']=='CANCEL_REQUIRED':
                    identity='cancel:'+action['action_id']
                    if not db.execute('SELECT 1 FROM sim_events WHERE event_id=?',(identity,)).fetchone():
                        self._queue(db,identity,self.clock.seconds+self.sim_policy['cancel_delay'],'CANCEL',
                            dict(trade=trade,order_ref=engine.state['intents'][action['intent_id']]['order_ref']))

    def set_book(self,contract,levels,observation_id,withdraw=False):
        """Record this observation only; no access to future scenario observations."""
        state=self.state();cid=str(contract.con_id)
        fraction=money(self.sim_policy['realization'])
        # Reproducible uncertainty differs per observed book, not per order.
        noise=int(hashlib.sha256((str(self.sim_policy['seed'])+':'+observation_id).encode()).hexdigest()[:8],16)%21
        realization=max(Decimal(0),min(Decimal(1),fraction-Decimal(noise)/100))
        book=dict(contract=asdict(contract),observation_id=observation_id,source_time=self.clock.wall().isoformat(),
            levels=[[str(money(p,True)),q,0 if withdraw else int(q*realization)] for p,q in levels])
        state['books'][cid]=book;state['now']=self.clock.seconds
        with self.db() as db:self._save(db,state)

    def _effect(self,effect_id,payload):
        with self.db() as db:
            old=db.execute('SELECT payload FROM sim_effects WHERE effect_id=?',(effect_id,)).fetchone()
            if old and old[0]!=encode(payload):raise ApplicationError('SIMULATION_EFFECT_CHANGED')
            db.execute('INSERT OR IGNORE INTO sim_effects(effect_id,payload) VALUES (?,?)',(effect_id,encode(payload)))

    def recover_effects(self):
        with self.db() as db:effects=db.execute('SELECT effect_id,payload FROM sim_effects WHERE done=0 ORDER BY rowid').fetchall()
        for identity,raw in effects:
            p=json.loads(raw)
            fee=self.sim_policy.get('commission_per_contract')
            kwargs={} if fee is None else dict(fee=str(money(fee)*p['quantity']))
            self.fill(p['order_ref'],p['quantity'],p['price'],datetime.fromisoformat(p['executed_at']),p['exec_id'],**kwargs)
            with self.db() as db:db.execute('UPDATE sim_effects SET done=1 WHERE effect_id=?',(identity,))

    def match(self):
        self.require_connected();self.recover_effects();state=self.state()
        with self.db() as db:
            orders=[json.loads(r[0]) for r in db.execute('SELECT record FROM fixture_orders ORDER BY rowid')]
            executions=[json.loads(r[0]) for r in db.execute('SELECT record FROM fixture_executions WHERE active=1')]
        owned={}
        for x in executions:
            if not x['bust']:owned[x['contract']['con_id']]=owned.get(x['contract']['con_id'],0)+x['quantity']*(1 if x['side']=='BUY' else -1)
        effects=[]
        for order in orders:
            if order['status'] not in {'Submitted','PendingCancel'}:continue
            book=state['books'].get(str(order['contract']['con_id']))
            if book is None:continue
            remaining=order['remaining'];cid=order['contract']['con_id']
            for level in book.get('asks',[]) if order['side']=='BUY' else book['levels']:
                price,_,capacity=level
                if (order['side']=='SELL' and money(price)<money(order['limit_price']) or
                    order['side']=='BUY' and money(price)>money(order['limit_price']) or not capacity or not remaining):continue
                qty=min(remaining,capacity)
                if order['side']=='SELL' and qty>owned.get(cid,0):raise AssertionError('SYNTHETIC_OVERSALE_INVARIANT')
                if order['side']=='BUY' and (owned.get(cid,0)+qty>25 or sum(owned.values())+qty>125):raise AssertionError('SYNTHETIC_ACQUISITION_CAP_INVARIANT')
                state['sequence']+=1
                identity='sim-fill:'+str(state['sequence'])+'.01'
                effects.append(dict(order_ref=order['order_ref'],quantity=qty,price=price,
                    executed_at=self.clock.wall().isoformat(),exec_id=identity))
                remaining-=qty;level[2]-=qty;owned[cid]=owned.get(cid,0)+(qty if order['side']=='BUY' else -qty)
        # Reserve actual synthetic liquidity and stable effects atomically BEFORE
        # applying idempotent broker executions. Recovery drains these first.
        with self.db() as db:
            self._save(db,state)
            for effect in effects:db.execute('INSERT INTO sim_effects(effect_id,payload) VALUES (?,?)',(effect['exec_id'],encode(effect)))
        self.recover_effects()

    def set_asks(self,contract,levels,observation_id,withdraw=False):
        """Explicit synthetic ask depth; no inference from displayed bid depth."""
        with self.db() as db:
            state=json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0]);book=state['books'].get(str(contract.con_id))
            if book is None:raise ApplicationError('BID_BOOK_REQUIRED')
            previous=None;asks=[]
            for price,size in levels:
                price=money(price,True)
                if type(size) is not int or size<0 or previous is not None and price<=previous:raise ApplicationError('INVALID_SYNTHETIC_ASK_DEPTH')
                actual=0 if withdraw else int(Decimal(size)*money(self.sim_policy['realization']))
                asks.append([str(price),size,actual]);previous=price
            if not asks or money(asks[0][0])<=money(book['levels'][0][0]):raise ApplicationError('CROSSED_SYNTHETIC_BOOK')
            book['asks']=asks;book['ask_observation_id']=observation_id;self._save(db,state)

    def _handle(self,event_id,kind,payload):
        if kind=='DELIVER':
            with self.db() as db:
                if db.execute('SELECT 1 FROM sim_applied WHERE event_id=?',(event_id,)).fetchone():return
                db.execute('INSERT INTO fixture_callbacks(trade_id,kind,record,executed_at) VALUES (?,?,?,?)',
                    (payload['trade'],payload['kind'],encode(payload['record']),payload['stamp']))
                db.execute('INSERT INTO sim_applied VALUES (?)',(event_id,))
            return
        if kind=='SUBMIT':
            i=payload['intent'];ref=i['order_ref']
            with self.db() as db:
                if db.execute('SELECT 1 FROM sim_applied WHERE event_id=?',(event_id,)).fetchone():return
                if not db.execute('SELECT 1 FROM fixture_orders WHERE order_ref=?',(ref,)).fetchone():
                    record=PortfolioOrderRecord(self.account,ref,i['order_id'],0,i['client_id'],VerifiedOptionContract(**i['contract']),
                        i['side'],i['quantity'],0,i['quantity'],'PendingSubmit','LMT',None,i['limit_price'])
                    db.execute('INSERT INTO fixture_orders VALUES (?,?,?)',(ref,payload['trade'],encode(asdict(record))))
                    self._callback(db,payload['trade'],'ORDER',record)
                    state=json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0]);state['submissions']+=1
                    state['metrics'].setdefault(ref,{})['broker_received']=self.clock.seconds;self._save(db,state)
                    state['metrics'][ref]['ordinal']=state['submissions'];self._save(db,state)
                if not db.execute('SELECT 1 FROM sim_events WHERE event_id=?',('ack:'+ref,)).fetchone():
                    self._queue(db,'ack:'+ref,self.clock.seconds+self.sim_policy['acknowledgement_delay'],'ACK',dict(order_ref=ref))
                db.execute('INSERT INTO sim_applied VALUES (?)',(event_id,))
            return
        with self.db() as db:
            if db.execute('SELECT 1 FROM sim_applied WHERE event_id=?',(event_id,)).fetchone():return
            row=db.execute('SELECT trade_id,record FROM fixture_orders WHERE order_ref=?',(payload['order_ref'],)).fetchone()
            if row is None:return  # Absence is not a cancellation or a fill proof.
            trade,raw=row[0],json.loads(row[1]);state=json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0])
            if kind=='ACK':
                recent=[t for t in state['requests'] if 0<=self.clock.seconds-t<self.sim_policy['request_window']]
                ordinal=state['metrics'].get(raw['order_ref'],{}).get('ordinal',0)
                reject=len(recent)>=self.sim_policy['request_capacity'] or bool(self.sim_policy['reject_every'] and ordinal%self.sim_policy['reject_every']==0)
                state['requests']=recent+[self.clock.seconds]
                raw['perm_id']=raw['order_id']+100000
                if raw['status']=='PendingSubmit':raw['status']='Inactive' if reject else 'Submitted'
                if reject:state['rejections']+=1
                state['metrics'].setdefault(raw['order_ref'],{})['acknowledged']=self.clock.seconds
            elif kind=='CANCEL':
                recent=[t for t in state['requests'] if 0<=self.clock.seconds-t<self.sim_policy['request_window']]
                if len(recent)>=self.sim_policy['request_capacity']:state['rejections']+=1
                elif raw['status'] not in TERMINAL:raw['status']='Cancelled'
                state['requests']=recent+[self.clock.seconds]
                state['cancellations']+=1
            else:raise ApplicationError('UNKNOWN_SIMULATION_EVENT')
            db.execute('UPDATE fixture_orders SET record=? WHERE order_ref=?',(encode(raw),raw['order_ref']))
            self._callback(db,trade,'ORDER',order_record(raw));self._save(db,state)
            db.execute('INSERT INTO sim_applied VALUES (?)',(event_id,))

    def advance(self,seconds):
        target=self.clock.seconds+seconds
        if seconds<0:raise ValueError('Synthetic time cannot reverse')
        if self.connected:self.recover_effects()
        while self.connected:
            with self.db() as db:row=db.execute('SELECT event_id,due,kind,payload FROM sim_events WHERE done=0 AND due<=? ORDER BY due,rowid LIMIT 1',(target,)).fetchone()
            if row is None:break
            identity,due,kind,raw=row;self.clock.seconds=max(self.clock.seconds,due)
            self._handle(identity,kind,json.loads(raw))
            with self.db() as db:
                db.execute('UPDATE sim_events SET done=1 WHERE event_id=?',(identity,))
                state=json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0]);state['now']=self.clock.seconds;self._save(db,state)
        self.clock.seconds=target
        with self.db() as db:
            state=json.loads(db.execute('SELECT state FROM sim_environment').fetchone()[0]);state['now']=target;self._save(db,state)


def distribution(values):
    values=sorted(values)
    if not values:return dict(samples=0,median=None,p95=None,p99=None,worst=None)
    def percentile(p):return values[min(len(values)-1,max(0,math.ceil(p*len(values))-1))]
    return dict(samples=len(values),median=statistics.median(values),p95=percentile(.95),p99=percentile(.99),worst=values[-1])
