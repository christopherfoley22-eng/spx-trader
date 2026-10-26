"""Managed, OFFLINE native-stop coordination in the existing economic journal.

No broker client or mutation API. All bindings/coverage here are synthetic; a
supported plan is NOT an acknowledgment, working order or protection guarantee.
"""
from dataclasses import dataclass,asdict
from datetime import datetime
from enum import Enum
import hashlib,json
from executor_paper_broker import PaperBrokerLedger,money,integer,text_id,utc
from executor_paper_types import PaperBrokerError
from simulation_evidence import VerifiedOptionContract
from mortificatio_models import ApplicationError
from mortificatio_store import encode


class OrderPhase(str,Enum):
    PREPARED='PREPARED'
    SUBMISSION_PENDING='SUBMISSION_PENDING'
    ACKNOWLEDGED='BROKER_ACKNOWLEDGED'
    WORKING='WORKING'
    PARTIAL='PARTIALLY_FILLED'
    FILLED='FILLED'
    CANCEL_PENDING='CANCELLATION_PENDING'
    CANCELED='CANCELED'
    REJECTED='REJECTED'
    EXPIRED='EXPIRED'
    UNKNOWN='RECONCILIATION_REQUIRED'


@dataclass(frozen=True)
class ProtectionReadiness:
    account:str
    generation:int
    reference:str
    contracts_json:str
    version:str = 'OFFLINE_NATIVE_STOP_V1'
    order_type:str = 'STP'
    premium_offset:str = '3.25'
    partial_fill_policy:str = 'FENCE_RECONCILE_PROTECT'
    origin:str = 'OFFLINE_FIXTURE'

    def validate(self,account,generation,contracts):
        if (self.origin!='OFFLINE_FIXTURE' or self.account!=account or type(self.generation) is not int
            or self.generation!=generation or not text_id(self.reference) or not text_id(self.version)
            or self.order_type!='STP' or self.premium_offset!='3.25' or self.partial_fill_policy!='FENCE_RECONCILE_PROTECT'
            or encode(json.loads(self.contracts_json))!=self.contracts_json
            or json.loads(self.contracts_json)!=[asdict(c) for c in contracts]):
            raise ApplicationError('EXACT_SUPPORTED_PROTECTION_PLAN_REQUIRED')
        return dict(asdict(self),broker_working_confirmed=False,real_broker_verified=False)


def is_native(intent):return intent.get('native_managed') is True


def order_lifecycle(engine,intent):
    n=engine.filled(intent);status=intent['status'];terminal=intent['terminal_proven']
    try:engine._planning_gate(engine.state['generation']);fresh=True
    except Exception:fresh=False
    annotations=engine.portfolio.get('native_protection',{}).get('annotations',{}).get(intent['intent_id'],{})
    cancel=intent['intent_id'] in engine.portfolio['cancel_requests']
    if status=='LOCAL_ABANDONED':phase=OrderPhase.CANCELED
    elif terminal and annotations.get('expired'):phase=OrderPhase.EXPIRED
    elif annotations.get('rejection'):phase=OrderPhase.REJECTED
    elif status=='Inactive':phase=OrderPhase.UNKNOWN
    elif terminal and status in ('Cancelled','ApiCancelled'):phase=OrderPhase.CANCELED
    elif intent['order_id'] is None:phase=OrderPhase.PREPARED
    elif engine.session is None or intent.get('evidence_generation',engine.state['generation'])!=engine.state['generation']:phase=OrderPhase.UNKNOWN
    elif cancel or status=='PendingCancel':phase=OrderPhase.CANCEL_PENDING
    elif n==intent['quantity']:phase=OrderPhase.FILLED
    elif n:phase=OrderPhase.PARTIAL
    elif status=='RESERVED_UNCONFIRMED':phase=OrderPhase.SUBMISSION_PENDING
    elif intent['perm_id'] and (intent['uncertain'] or status=='PreSubmitted' or not fresh):phase=OrderPhase.ACKNOWLEDGED
    elif fresh and not intent['uncertain'] and status=='Submitted':phase=OrderPhase.WORKING
    else:phase=OrderPhase.UNKNOWN
    return dict(phase=phase.value,order_ref=intent['order_ref'],order_id=intent['order_id'],perm_id=intent['perm_id'],client_id=intent['client_id'],
        contract=intent['contract'].copy(),side=intent['side'],quantity=intent['quantity'],executed_quantity=n,
        remaining_quantity=max(0,intent['quantity']-n),reported_filled=intent['reported_filled'],
        reported_remaining=intent['quantity']-intent['reported_filled'],broker_reported_status=status,
        terminal_reconciled=terminal,reconciliation_required=not fresh or intent['uncertain'] or cancel or status=='PendingCancel'
            or not terminal and (status in ('Filled','Inactive') or n==intent['quantity']),
        cancel_requested=cancel,replacement_requested=bool(annotations.get('replacement')),
        order_revision=intent.get('order_revision',0),native_stop=is_native(intent),real_broker_verified=False)


def reduce_native(e,data):
    p=e.portfolio;op=data['op'];s=p.get('native_protection')
    if op=='CONFIG':
        if s:raise PaperBrokerError('NATIVE_PROTECTION_ALREADY_CONFIGURED')
        p['native_protection']=dict(version=data['version'],annotations={},uncertain=False,recovery={})
        return
    if not s:raise PaperBrokerError('NATIVE_PROTECTION_PLAN_REQUIRED')
    if op=='PLAN':
        PaperBrokerLedger._reduce(e,'INTENT',data['intent']);e._assert_reservations(enforce_cash=False)
    elif op=='BIND':
        PaperBrokerLedger._reduce(e,'BIND',data['binding'])
        i=e.state['intents'][data['binding']['intent_id']];i['submitted_at']=data['at'];i['evidence_generation']=e.state['generation'];i['order_revision']=i.get('order_revision',0)+1
    elif op=='ANNOTATE':
        s['annotations'].setdefault(data['intent_id'],{}).update(data['values'])
        if data['values'].get('rejection'):
            i=e.state['intents'][data['intent_id']];i['uncertain']=True;i['terminal_proven']=False
            p['halted']=True;e.state['revision']+=1
    elif op=='ORDER_PROOF':
        # Called only after the complete snapshot has passed identity, position,
        # execution/fee, status, remaining-order and freshness checks.
        record=data['record'];i=e._identity(record)
        i.update(status=record['status'],reported_filled=record['filled'],terminal_proven=False,
            evidence_generation=e.state['generation'],order_revision=i.get('order_revision',0)+1)
        e.state['revision']+=1
    else:raise PaperBrokerError('UNKNOWN_NATIVE_PROTECTION_EVENT')
    p['flat_at']=p['flat_revision']=None


def refresh_native(e,reason,evidence=None):
    p=e.portfolio;s=p.get('native_protection')
    if not s:return
    positions=e.positions()
    # Corrections are economic events even if they restore a formerly flat trade.
    # Keep all historical obligations; recompute current required quantity/WAP.
    s['recovery']={str(cid):dict(quantity=q,stop_price=str(e._premium_stop(cid)[1]),reason=reason)
        for cid,q in positions.items()}
    excess=any(e.reservations('SELL',int(cid))>positions.get(int(cid),0) for cid in p['targets'])
    s['uncertain']=excess
    if reason=='RECONCILED':
        coverage={}
        for cid,quantity in positions.items():
            stop=e._premium_stop(cid)[1]
            working=sum(max(0,i['quantity']-e.filled(i)) for i in e.state['intents'].values() if is_native(i)
                and i['contract']['con_id']==cid and i['status']=='Submitted' and not i['uncertain'] and not i['terminal_proven']
                and i.get('evidence_generation')==e.state['generation'] and money(i['stop_price'])==stop
                and not s['annotations'].get(i['intent_id'],{}).get('rejection'))
            coverage[str(cid)]=dict(required_quantity=quantity,working_quantity=working,cycle=e.state['last_cycle'],
                snapshot_digest=(evidence or {}).get('snapshot_digest'),real_broker_verified=False)
        s['coverage_as_of_reconciliation']=coverage
        for obligation in p.get('protection_obligations',{}).values():
            proof=coverage.get(str(obligation['contract']['con_id']))
            if proof and obligation['active']:
                obligation['coverage_as_of_reconciliation']=proof.copy()
    else:s['coverage_as_of_reconciliation']={}
    if positions or excess:
        p['halted']=True
        p['alerts'].setdefault('native-protection',dict(reason='NATIVE_PROTECTION_RECOVERY_REQUIRED',at='DURABLE_ECONOMIC_EVENT'))
    if reason in ('RESTART','DISCONNECT'):
        for i in e.state['intents'].values():
            if is_native(i):i['evidence_generation']=None


class NativeProtectionCoordinator:
    def __init__(self,engine):self.engine=engine

    def _scope(self):
        p=self.engine.portfolio
        if (p.get('native_protection',{}).get('uncertain') or p.get('protection_lifecycle',{}).get('uncertain')
            or p.get('adaptive_exit',{}).get('external_invalid')):raise PaperBrokerError('UNCERTAIN_PROTECTIVE_SCOPE_REQUIRES_RECONCILIATION')

    def configure(self,version='OFFLINE_NATIVE_STOP_V1'):
        e=self.engine
        with e.lock:
            e._planning_gate(e.state['generation'])
            if not text_id(version) or e.state['intents']:raise PaperBrokerError('CONFIGURE_NATIVE_BEFORE_ACQUISITION')
            return e._append('native-config','PORT_NATIVE',dict(op='CONFIG',version=version))

    def plan(self,cid,request_id,replaces=None):
        e=self.engine
        with e.lock:
            e._planning_gate(e.state['generation'])
            self._scope()
            if not e.portfolio.get('native_protection') or not text_id(request_id):raise PaperBrokerError('NATIVE_PLAN_AND_IDEMPOTENCY_REQUIRED')
            namespace=e.portfolio.get('allocation_journal_id') or str(e.path.resolve())
            key='ns:'+hashlib.sha256((namespace+':'+request_id).encode()).hexdigest()[:48]
            old=e.state['intents'].get(key)
            if old:
                if old['contract']['con_id']!=cid or old.get('replaces')!=replaces:raise PaperBrokerError('CONFLICTING_NATIVE_INTENTION')
                return key
            if e.portfolio['exit_reason']:raise PaperBrokerError('EXIT_PRIORITY_BLOCKS_NEW_PROTECTIVE_PLAN')
            if replaces:
                prior=e.state['intents'].get(replaces)
                if not prior or not is_native(prior) or not prior['terminal_proven'] or prior['contract']['con_id']!=cid:
                    raise PaperBrokerError('OLD_PROTECTION_CANCELLATION_NOT_RECONCILED')
            owned=e.positions().get(cid,0)
            from executor_liquidation import external_reserved
            available=owned-e.reservations('SELL',cid)-external_reserved(e,cid)
            if not integer(available,1,25):raise PaperBrokerError('NO_UNCOMMITTED_OWNED_PROTECTION_QUANTITY')
            _,stop=e._premium_stop(cid)
            if stop is None or stop<=0:raise PaperBrokerError('VALID_PREMIUM_STOP_REQUIRED')
            contract=e.portfolio['targets'][str(cid)]['contract'];now=utc(e.wall_clock()).isoformat()
            intent=dict(intent_id=key,order_ref=key,contract=contract.copy(),side='SELL',quantity=available,created_at=now,
                purpose='PROTECTIVE_STOP',native_managed=True,stop_price=str(stop),limit_price=None,arrival_price=str(stop),
                cash_per_contract='0',quote_id='NATIVE_STOP_NOT_AN_EXECUTABLE_QUOTE',price_bound=str(stop),
                attempt=0,route_id=key,route_quantity=available,replaces=replaces,order_revision=0)
            e._append('native-plan:'+hashlib.sha256(request_id.encode()).hexdigest(),'PORT_NATIVE',dict(op='PLAN',intent=intent))
            return key

    def bind_synthetic(self,intent_id,order_id):
        """Durable identity reservation only. NO transport/submission is performed."""
        e=self.engine
        with e.lock:
            e._planning_gate(e.state['generation']);i=e.state['intents'].get(intent_id)
            self._scope()
            if not i or not is_native(i) or not integer(order_id,1):raise PaperBrokerError('MANAGED_NATIVE_INTENT_REQUIRED')
            if e.portfolio['exit_reason']:raise PaperBrokerError('EXIT_PRIORITY_BLOCKS_NEW_PROTECTIVE_BINDING')
            if i['order_id'] is not None:raise PaperBrokerError('UNCERTAIN_NATIVE_SUBMISSION_NO_RETRY')
            if any(x['uncertain'] for x in e.state['intents'].values()):raise PaperBrokerError('RECONCILE_UNCERTAIN_ORDERS_FIRST')
            if i['terminal_proven'] or e.reservations('SELL',i['contract']['con_id'])>e.positions().get(i['contract']['con_id'],0):
                raise PaperBrokerError('NATIVE_REDUCTION_EXCEEDS_OWNERSHIP')
            if money(i['stop_price'])!=e._premium_stop(i['contract']['con_id'])[1]:raise PaperBrokerError('STALE_NATIVE_STOP_PREMIUM')
            e._assert_reservations(enforce_cash=False)
            return e._append('native-bind:'+intent_id,'PORT_NATIVE',dict(op='BIND',binding=dict(intent_id=intent_id,
                order_id=order_id,client_id=e.session.client_id),at=utc(e.wall_clock()).isoformat()))

    def request_replacement(self,intent_id,request_id):
        e=self.engine
        with e.lock:
            i=e.state['intents'][intent_id]
            if not is_native(i) or not text_id(request_id):raise PaperBrokerError('MANAGED_PROTECTION_REPLACEMENT_REQUIRED')
            e._append('native-replace:'+hashlib.sha256(request_id.encode()).hexdigest(),'PORT_NATIVE',dict(op='ANNOTATE',intent_id=intent_id,values=dict(replacement=request_id)))
            return e._cancel(i)  # Descriptor only; old quantity remains reserved.

    def annotate_terminal(self,intent_id,reason,proof_ref):
        e=self.engine
        with e.lock:
            e._planning_gate(e.state['generation']);i=e.state['intents'][intent_id]
            if not i['terminal_proven'] or reason not in ('rejection','expired') or not text_id(proof_ref):
                raise PaperBrokerError('RECONCILED_TERMINAL_REASON_REQUIRED')
            return e._append('native-terminal:'+proof_ref,'PORT_NATIVE',dict(op='ANNOTATE',intent_id=intent_id,values={reason:proof_ref}))

    def record_rejection(self,intent_id,code,proof_ref):
        e=self.engine
        with e.lock:
            e._session(e.state['generation']);i=e.state['intents'][intent_id]
            if not is_native(i) or i['order_id'] is None or code!=201 or not text_id(proof_ref):
                raise PaperBrokerError('CORRELATED_NATIVE_REJECTION_EVIDENCE_REQUIRED')
            return e._append('native-reject:'+hashlib.sha256(proof_ref.encode()).hexdigest(),'PORT_NATIVE',dict(op='ANNOTATE',
                intent_id=intent_id,values=dict(rejection=dict(code=code,proof_ref=proof_ref))))

    def status(self):
        e=self.engine
        with e.lock:
            fresh=True
            try:e._planning_gate(e.state['generation'])
            except Exception:fresh=False
            scope_invalid=bool(e.portfolio.get('native_protection',{}).get('uncertain') or
                e.portfolio.get('protection_lifecycle',{}).get('uncertain') or e.portfolio.get('adaptive_exit',{}).get('external_invalid'))
            rows=[]
            for cid,owned in e.positions().items():
                _,stop=e._premium_stop(cid);covered=0
                for i in e.state['intents'].values():
                    if is_native(i) and i['contract']['con_id']==cid and i['status']=='Submitted' and not i['uncertain'] and not i['terminal_proven'] and fresh and i.get('evidence_generation')==e.state['generation'] and money(i['stop_price'])==stop and not e.portfolio['native_protection']['annotations'].get(i['intent_id'],{}).get('rejection'):
                        covered+=max(0,i['quantity']-e.filled(i))
                rows.append(dict(contract_id=cid,owned=owned,working_synthetic_protective_quantity=covered,
                    required_stop_price=str(stop),covered=covered==owned and not scope_invalid,real_broker_verified=False))
            unresolved=scope_invalid or bool(e.state['faults']) or any(not r['covered'] for r in rows) or (
                not e.positions() and any(i['side']=='SELL' and not i['terminal_proven'] for i in e.state['intents'].values()))
            return dict(condition='CRITICAL_UNRESOLVED_PROTECTION' if unresolved else 'SYNTHETIC_PROTECTION_RECONCILED' if rows else 'AWAIT_RECONCILIATION' if not fresh else 'NO_OWNED_EXPOSURE',
                unresolved=unresolved,strikes=rows,orders=[order_lifecycle(e,i) for i in e.state['intents'].values()],
                real_broker_verified=False,submission_enabled=False)
