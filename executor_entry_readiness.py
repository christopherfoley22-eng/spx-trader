"""Immutable CALL/PUT allocation hints; original admission remains authoritative.

No network, broker credentials, order IDs or entry permissions. Evidence changes
invalidate hints; a click never silently rebuilds an expired/changed candidate.
"""
from dataclasses import dataclass,asdict,replace
from datetime import datetime
import hashlib
import json
from threading import RLock,Condition,Thread
import uuid
from zoneinfo import ZoneInfo
from executor_allocation import build_allocation,AllocationPlan,financial_room,amount
from executor_portfolio_liquidity import assess_liquidity
from executor_paper_broker import utc,integer
from mortificatio_models import TradeCommand,ApplicationError
from mortificatio_store import encode


def evidence_fingerprint(evidence,policy,held):
    data=asdict(evidence);data.pop('now_monotonic',None)
    raw=json.dumps(dict(evidence=data,policy=policy.normalized(),held=[str(amount(x)) for x in held]),
        sort_keys=True,default=str,allow_nan=False,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class PreparedEntryCandidate:
    direction:str
    generation:int
    sequence:int
    fingerprint:str
    plan_id:str
    payload_json:str
    assessment_json:str

    def report(self):return json.loads(self.assessment_json)


class PreparedEntries:
    def __init__(self,binding,policy):
        self.binding=binding;self.policy=policy;self.candidates={};self.last_valid={};self.invalid={};self.sequences={};self.lock=RLock()
        self.builds=0;self.hits=0

    def invalidate(self,direction,reason):
        with self.lock:self.candidates.pop(direction,None);self.invalid[direction]=reason

    def publish(self,direction,evidence,now_wall,sequence,held=('0','0','0')):
        if direction not in {'CALL','PUT'} or not integer(sequence,1):raise ApplicationError('INVALID_ENTRY_EVENT')
        with self.lock:
            if sequence<=self.sequences.get(direction,0):
                self.invalidate(direction,'OUT_OF_ORDER_ENTRY_EVIDENCE');raise ApplicationError('OUT_OF_ORDER_ENTRY_EVIDENCE')
            self.sequences[direction]=sequence
            try:
                fingerprint=evidence_fingerprint(evidence,self.policy,held)
                previous=self.last_valid.get(direction)
                if previous and evidence.generation<previous.generation:raise ApplicationError('ENTRY_GENERATION_ROLLBACK')
                if previous and evidence.generation==previous.generation:
                    old={q['option']['contract']['con_id']:q for q in json.loads(previous.payload_json)['market_evidence']['quotes']}
                    for q in evidence.quotes:
                        prior=old.get(q.option.contract.con_id)
                        if prior and (utc(q.option.source_time)<datetime.fromisoformat(prior['option']['source_time']) or
                            q.option.received_monotonic<prior['option']['received_monotonic'] or
                            utc(q.sizes_source_time)<datetime.fromisoformat(prior['sizes_source_time']) or
                            utc(q.greeks_source_time)<datetime.fromisoformat(prior['greeks_source_time']) or
                            utc(q.spx.source_time)<datetime.fromisoformat(prior['spx']['source_time'])):
                            raise ApplicationError('BACKWARD_ENTRY_MARKET_EVIDENCE')
                # Original allocator validates actual listed intervals, Greeks,
                # 0.25s coherence, funds, fees, reserves and ATM-first quantities.
                # Placeholder only; the actual authenticated UUID4 command is
                # bound at admission. This deterministic ID grants no authority.
                command=TradeCommand(str(uuid.UUID(fingerprint[:32],version=4)),direction,0)
                plan=build_allocation(command,'prepared:'+fingerprint,self.binding,self.policy,evidence,now_wall,*held)
                report=plan.report();plan_id=hashlib.sha256((direction+fingerprint+plan.payload_json).encode()).hexdigest()
                for other_direction,other in list(self.candidates.items()):
                    if other_direction==direction:continue
                    identities={q['option']['contract']['con_id']:q['option']['contract'] for q in json.loads(other.payload_json)['market_evidence']['quotes']}
                    if any(q['option']['contract']['con_id'] in identities and q['option']['contract']!=identities[q['option']['contract']['con_id']]
                           for q in report['market_evidence']['quotes']):
                        self.invalidate(other_direction,'DIRECTION_CONTRACT_ID_COLLISION')
                        raise ApplicationError('DIRECTION_CONTRACT_ID_COLLISION')
                assessment=dict(origin='OFFLINE_FIXTURE',direction=direction,generation=evidence.generation,sequence=sequence,
                    plan_id=plan_id,expiration=report['expiration'],total_contracts=report['total_contracts'],
                    maximum_permitted_cost=report['reserved_capital'],fees=report['estimated_fees'],
                    market_evidence=report['market_evidence'],account_evidence=report['account_evidence'],
                    reservations=report['prior_local_reservations'],risk=report['planned_stop_risk'],lines=report['lines'],
                    liquidity_confidence='DISPLAYED_ONLY_NOT_GUARANTEED',execution_difficulty='UNVERIFIED_WITHOUT_ACTUAL_FILLS',
                    invalidation_reason=None,submission_enabled=False)
                candidate=PreparedEntryCandidate(direction,evidence.generation,sequence,fingerprint,plan_id,plan.payload_json,encode(assessment))
                self.candidates[direction]=candidate;self.last_valid[direction]=candidate;self.invalid.pop(direction,None);self.builds+=1
                return candidate
            except Exception:
                self.invalidate(direction,'ENTRY_EVIDENCE_REJECTED');raise

    def confirm(self,command,trade_id,evidence,now_wall,held=('0','0','0')):
        with self.lock:
            c=self.candidates.get(command.direction)
            if c is None:raise ApplicationError('PREPARED_ENTRY_UNAVAILABLE')
            try:
                if (c.fingerprint!=evidence_fingerprint(evidence,self.policy,held) or c.generation!=evidence.generation
                        or evidence.binding!=self.binding or c.plan_id!=hashlib.sha256((c.direction+c.fingerprint+c.payload_json).encode()).hexdigest()):
                    raise ApplicationError('PREPARED_ENTRY_CHANGED')
                report=json.loads(c.payload_json)
                today=utc(now_wall).astimezone(ZoneInfo('America/New_York')).strftime('%Y%m%d')
                if today!=report['expiration']:raise ApplicationError('PREPARED_EXPIRATION_CHANGED')
                # Final checks use CURRENT clocks and authoritative source times.
                # No ranking/sizing rebuild or receipt-time substitution occurs.
                for quote in evidence.quotes:
                    assess_liquidity(quote,command.direction,now_wall,evidence.now_monotonic,
                        lambda t:t.strftime('%Y%m%d')==today,self.policy.normalized()['execution'])
                cap,cash,risk,delta=financial_room(self.policy.normalized(),evidence,self.binding,now_wall,*held)
                if (amount(report['reserved_capital'])>min(cap,cash) or amount(report['planned_stop_risk'])>risk
                        or amount(report['delta_notional'])>delta):raise ApplicationError('PREPARED_FINANCIAL_ROOM_CHANGED')
                from executor_exit_capacity import require_capacity
                report['exit_capacity_revalidation']=require_capacity(report,evidence,now_wall)
                report.update(trade_id=trade_id,command_id=command.command_id,created_at=utc(now_wall).isoformat(),
                    prepared_entry=dict(plan_id=c.plan_id,sequence=c.sequence,generation=c.generation))
                self.hits+=1;return AllocationPlan(encode(report))
            except Exception:
                self.invalidate(command.direction,'FINAL_ENTRY_CHECK_REJECTED');raise


class StreamingEntryEvidence:
    """Bounded event-driven fixture provider; snapshot never advances source time."""
    def __init__(self,monotonic_clock):self.clock=monotonic_clock;self.latest={};self.lock=RLock();self.listeners=[]
    def publish(self,direction,evidence):
        if direction not in {'CALL','PUT'}:raise ApplicationError('DIRECTION_REQUIRED')
        with self.lock:self.latest[direction]=evidence;listeners=self.listeners[:]
        for listener in listeners:listener(direction)
    def snapshot(self,direction):
        with self.lock:
            if direction not in self.latest:raise ApplicationError('ENTRY_STREAM_UNAVAILABLE')
            return replace(self.latest[direction],now_monotonic=self.clock())


class PreparedEntryWorker:
    """Local event-driven worker. Pending work is bounded to the two directions."""
    def __init__(self,lifecycle,token,provider):
        lifecycle.app.protection.require(token,'OBSERVE_RISK')
        if not isinstance(provider,StreamingEntryEvidence):raise ApplicationError('OFFLINE_STREAMING_PROVIDER_REQUIRED')
        self.lifecycle=lifecycle;self.token=token;self.provider=provider;self.pending=set();self.condition=Condition();self.running=True;self.failure=None
        with lifecycle.lock:
            if lifecycle.engines:raise ApplicationError('CONFIGURE_ENTRY_PROVIDER_BEFORE_TRADES')
            lifecycle.allocation.provider=provider
            if lifecycle.allocation.readiness is None:lifecycle.allocation.readiness=PreparedEntries(lifecycle.app.policy.binding,lifecycle.allocation.policy)
        with provider.lock:provider.listeners.append(self.notify)
        self.thread=Thread(target=self.run,name='offline-entry-readiness',daemon=True);self.thread.start()
        for direction in list(provider.latest):self.notify(direction)

    def notify(self,direction):
        self.lifecycle.allocation.readiness.invalidate(direction,'ENTRY_UPDATE_PENDING')
        with self.condition:self.pending.add(direction);self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                while self.running and not self.pending:self.condition.wait()
                if not self.running:return
                direction=self.pending.pop()
            try:self.lifecycle.prepare_entry_candidates(self.token,(direction,))
            except Exception:
                self.failure='ENTRY_CANDIDATE_UNAVAILABLE'
                self.lifecycle.allocation.readiness.invalidate(direction,self.failure)

    def close(self):
        with self.provider.lock:
            if self.notify in self.provider.listeners:self.provider.listeners.remove(self.notify)
        with self.condition:self.running=False;self.condition.notify_all()
        self.thread.join(timeout=2)
        if self.thread.is_alive():raise ApplicationError('ENTRY_READINESS_WORKER_UNRESOLVED')
        for direction in ('CALL','PUT'):self.lifecycle.allocation.readiness.invalidate(direction,'ENTRY_WORKER_STOPPED')
