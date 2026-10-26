"""Unified OFFLINE view/protection lifecycle over the existing order journal.

Supplemental synthetic stop evidence is never a claim of real IBKR protection.
No submit/cancel/modify API exists; reduction actions retain the original boundary.
"""
from dataclasses import dataclass,asdict
from datetime import datetime
from decimal import Decimal
from copy import deepcopy
from executor_paper_broker import money,integer,text_id,utc
from executor_paper_types import PaperBrokerError
from simulation_evidence import validate_observation_time


def reduce_protection(engine,data):
    p=engine.portfolio;op=data['op']
    p['flat_at']=p['flat_revision']=None
    if op=='CONFIG':
        if p.get('protection_lifecycle'):raise PaperBrokerError('PROTECTION_POLICY_ALREADY_PINNED')
        p['protection_lifecycle']=dict(version=data['version'],orders={},recovery={},history=[],uncertain=False)
    elif op=='EVIDENCE':
        p['protection_lifecycle']['orders'][data['value']['order_ref']]=data['value']
    elif op=='RECOVERY':
        p['halted']=True;p['protection_lifecycle']['recovery'][data['contract_id']]=data['reason']
        if data['reason']=='STOP_EVIDENCE_REJECTED' and p.get('adaptive_exit'):
            p['adaptive_exit']['external_invalid']=True
            p['protection_lifecycle']['uncertain']=True
    else:raise PaperBrokerError('UNKNOWN_PROTECTION_EVENT')


def protection_after_economic(engine,reason):
    p=engine.portfolio;s=p.get('protection_lifecycle')
    # Derived inside the SAME economic journal transaction, including corrections
    # and restart replay. A receipt is an obligation, never native-stop evidence.
    obligations=p.setdefault('protection_obligations',{})
    for exec_id,event in engine.state['executions'].items():
        record=event['record']
        if record['side']!='BUY':continue
        cid=record['contract']['con_id']
        obligations[exec_id]=dict(exec_id=exec_id,order_ref=record['order_ref'],
            contract=record['contract'].copy(),quantity=record['quantity'],premium=record['price'],
            active=event['active'],observed_receipt=p.get('execution_receipts',{}).get(exec_id),
            owned_quantity=engine.positions().get(cid,0),native_protection_verified=False,
            disposition='REQUIRES_PROTECTION_RECONCILIATION' if event['active'] else 'CORRECTED_OR_BUSTED_RETAINED_FOR_AUDIT')
    if not s:return
    # Same durable event as the fill/disconnect. No window to authorize more
    # acquisition between observing ownership and setting the recovery fence.
    for cid,quantity in engine.positions().items():
        s['recovery'][str(cid)]='REVERIFY_AFTER_'+reason
        p['halted']=True
        p['alerts'].setdefault('protection:'+str(cid),dict(reason='PROTECTION_RECOVERY_REQUIRED',at='JOURNALED_ECONOMIC_EVENT'))


def unresolved_protection_orders(engine):
    s=engine.portfolio.get('protection_lifecycle',{})
    return int(s.get('uncertain',False))+sum(not x['terminal_reconciled'] for x in s.get('orders',{}).values())


@dataclass(frozen=True)
class SyntheticStopEvidence:
    account:str
    generation:int
    contract:object
    order_ref:str
    order_id:int
    perm_id:int
    quantity:int
    stop_price:object
    status:str
    observed_at:datetime
    received_monotonic:float
    enforceable_in_simulator:bool
    terminal_reconciled:bool = False
    origin:str = 'OFFLINE_FIXTURE'
    client_id:int = 3


class UnifiedExecutionCoordinator:
    def __init__(self,engine):self.engine=engine

    def configure_protection(self,version):
        e=self.engine
        with e.lock:
            e._planning_gate(e.state['generation'])
            if not text_id(version):raise PaperBrokerError('VERSIONED_PROTECTION_POLICY_REQUIRED')
            if not e.portfolio.get('adaptive_exit'):raise PaperBrokerError('COORDINATED_EXIT_POLICY_REQUIRED')
            if any(i['side']=='BUY' for i in e.state['intents'].values()):raise PaperBrokerError('CONFIGURE_PROTECTION_BEFORE_ACQUISITION')
            return e._append('protection-config','PORT_PROTECTION',dict(op='CONFIG',version=version))

    def observe_stop(self,value):
        e=self.engine
        with e.lock:
            if not isinstance(value,SyntheticStopEvidence) or value.origin!='OFFLINE_FIXTURE':raise PaperBrokerError('SYNTHETIC_STOP_EVIDENCE_REQUIRED')
            cid=value.contract.con_id;key=str(cid);s=e.portfolio.get('protection_lifecycle')
            if not s:raise PaperBrokerError('PROTECTION_POLICY_REQUIRED')
            try:
                e._planning_gate(e.state['generation'])
                if (value.account!=e.approval.account or value.generation!=e.state['generation'] or
                    asdict(value.contract)!=e.portfolio['targets'][key]['contract'] or
                    not text_id(value.order_ref) or not integer(value.order_id,1) or not integer(value.perm_id,1) or
                    not integer(value.client_id,0) or
                    not integer(value.quantity,0,25) or type(value.terminal_reconciled) is not bool or
                    type(value.enforceable_in_simulator) is not bool or value.status not in {'Submitted','PreSubmitted','Inactive','Cancelled','Filled','PendingCancel'}):
                    raise PaperBrokerError('STOP_IDENTITY_OR_STATUS_INVALID')
                validate_observation_time(value.observed_at,value.received_monotonic,e.wall_clock(),e.monotonic_clock())
                old=s['orders'].get(value.order_ref)
                if old and (any(old[k]!=getattr(value,k) for k in ('order_id','perm_id','account','client_id')) or old['contract']!=asdict(value.contract)):
                    raise PaperBrokerError('STOP_IDENTITY_CHANGED')
                if any(x['order_ref']!=value.order_ref and (x['order_id']==value.order_id or x['perm_id']==value.perm_id) for x in s['orders'].values()):
                    raise PaperBrokerError('DUPLICATE_STOP_IDENTITY')
                if any((x['order_id']==value.order_id and x['client_id']==value.client_id) or x.get('perm_id')==value.perm_id for x in e.state['intents'].values()):
                    raise PaperBrokerError('STOP_IDENTITY_CONFLICTS_WITH_MANAGED_ORDER')
                if value.terminal_reconciled and value.status not in {'Cancelled','Filled'}:raise PaperBrokerError('TERMINAL_STOP_RECONCILIATION_REQUIRED')
                if value.terminal_reconciled and value.status=='Filled' and e.positions().get(cid,0):
                    raise PaperBrokerError('STOP_EXECUTIONS_AND_POSITION_RECONCILIATION_REQUIRED')
                if not value.terminal_reconciled and value.quantity<=0:raise PaperBrokerError('NONZERO_WORKING_STOP_REQUIRED')
                record=asdict(value);record['observed_at']=utc(value.observed_at).isoformat();record['stop_price']=str(money(value.stop_price,True))
                all_orders=dict(s['orders']);all_orders[value.order_ref]=record
                closing=sum(x['quantity'] for x in all_orders.values() if x['contract']['con_id']==cid and not x['terminal_reconciled'])
                if closing+e.reservations('SELL',cid)>e.positions().get(cid,0):raise PaperBrokerError('STOP_AND_TACTICAL_ORDERS_EXCEED_OWNERSHIP')
                e._append('protection-evidence:'+str(e.sequence+1),'PORT_PROTECTION',dict(op='EVIDENCE',value=record))
                self._scope()
                state=self.protection_status(cid)
                if state['status']!='SYNTHETIC_COVERED' and e.positions().get(cid,0):self._recover(cid,state['status'])
                return state
            except Exception:
                self._recover(cid,'STOP_EVIDENCE_REJECTED');raise

    def _recover(self,cid,reason):
        return self.engine._append('protection-recovery:'+str(self.engine.sequence+1),'PORT_PROTECTION',
            dict(op='RECOVERY',contract_id=str(cid),reason=reason))

    def _scope(self):
        from executor_liquidation import GlobalLiquidationCoordinator
        e=self.engine
        if not e.portfolio.get('adaptive_exit'):return
        records=list(e.portfolio['protection_lifecycle']['orders'].values())
        for x in records:
            if not x['terminal_reconciled']:
                if x['generation']!=e.state['generation']:return False
                try:validate_observation_time(datetime.fromisoformat(x['observed_at']),x['received_monotonic'],e.wall_clock(),e.monotonic_clock())
                except Exception:return False
        orders=[dict(order_ref=x['order_ref'],contract=x['contract'],side='SELL',remaining=x['quantity'],terminal_reconciled=x['terminal_reconciled']) for x in records]
        return GlobalLiquidationCoordinator(e).observe_external_closings(dict(origin='OFFLINE_FIXTURE',complete=True,
            account=e.approval.account,generation=e.state['generation'],observed_at=min(x['observed_at'] for x in records),
            received_monotonic=min(x['received_monotonic'] for x in records),positions={str(k):v for k,v in e.positions().items()},orders=orders))

    def protection_status(self,cid):
        e=self.engine;owned=e.positions().get(cid,0);covered=0;reserved=0
        s=e.portfolio.get('protection_lifecycle',{})
        try:
            e._planning_gate(e.state['generation']);_,desired=e._premium_stop(cid)
            for x in s.get('orders',{}).values():
                if x['contract']['con_id']!=cid or x['terminal_reconciled']:continue
                reserved+=x['quantity']
                validate_observation_time(datetime.fromisoformat(x['observed_at']),x['received_monotonic'],e.wall_clock(),e.monotonic_clock())
                if (x['generation']==e.state['generation'] and x['status'] in {'Submitted','PreSubmitted'} and
                    x['enforceable_in_simulator'] and money(x['stop_price'])==desired):covered+=x['quantity']
            status='SYNTHETIC_COVERED' if owned>0 and covered==owned else 'UNVERIFIED_OR_GAP'
        except Exception:status='UNVERIFIED_OR_GAP'
        return dict(contract_id=cid,owned=owned,covered=covered if status=='SYNTHETIC_COVERED' else 0,
            reserved=reserved,status=status,real_broker_protection_verified=False)

    def reduction_actions(self):
        e=self.engine
        with e.lock:
            if not e.portfolio['exit_reason']:return []
            actions=e.schedule(e.state['generation'])
            for x in e.portfolio.get('protection_lifecycle',{}).get('orders',{}).values():
                if not x['terminal_reconciled']:
                    actions.append(dict(type='CANCEL_PROTECTION_RECONCILIATION_REQUIRED',order_ref=x['order_ref'],executable=False))
            return actions

    def status(self):
        e=self.engine
        with e.lock:
            rows=[]
            for cid,t in e.portfolio['targets'].items():
                orders=[i for i in e.state['intents'].values() if i['contract']['con_id']==int(cid)]
                rows.append(dict(contract=t['contract'].copy(),desired=t['quantity'],confirmed=e.positions().get(int(cid),0),
                    pending_acquisition=e.reservations('BUY',int(cid)),pending_reduction=e.reservations('SELL',int(cid)),
                    uncertain=sum(i['uncertain'] for i in orders),cancel_pending=sum(i['intent_id'] in e.portfolio['cancel_requests'] and not i['terminal_proven'] for i in orders),
                    protection=self.protection_status(int(cid)),orders=[{k:i.get(k) for k in ('order_id','perm_id','order_ref','side','quantity','status','terminal_proven')} for i in orders]))
            return dict(strikes=rows,execution_ids=list(e.state['executions']),reserved_capital=str(e._cash_reserved()),
                phase=e.status()['phase'],entry_fenced=e.portfolio['halted'],submission_enabled=False)
