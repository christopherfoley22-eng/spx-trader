"""OFFLINE five-strike portfolio coordination on the existing durable paper ledger.

No client, socket, order submission, modification, cancellation, or execution
permit. Returned actions are simulated work descriptions, never broker calls.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
import json
import hashlib
from pathlib import Path
import sqlite3

from executor_broker_snapshot import synchronized
from executor_emergency import EmergencyLatch
from executor_market_ingestion import IngestionError
from executor_dry_run import DryRunExecutor, DryRunError, spx_profit_decision
from executor_paper_broker import PaperBrokerLedger, canonical, integer, money, text_id, utc
from executor_paper_types import BrokerOrderRecord, PaperBrokerError
from executor_paper_reconciliation import _reconcile_snapshot
from executor_portfolio_liquidity import (ExecutionPolicy, LiquidityFixture, assess_liquidity,
    protected_limit, fixture_payload, fixture_from_payload, MAX_STRIKES, MAX_PORTFOLIO_CONTRACTS)
from simulation_evidence import (EvidenceClock, EvidenceError, VerifiedOptionContract,
    conservative_usable_funds, validate_contract)


@dataclass(frozen=True)
class PortfolioOrderRecord(BrokerOrderRecord):
    limit_price: object = None


class OfflinePortfolioLedger(PaperBrokerLedger):
    def __init__(self, path, approval, policy, wall_clock=None, monotonic_clock=None):
        if not isinstance(policy,ExecutionPolicy):
            raise PaperBrokerError('EXPLICIT_EXECUTION_POLICY_REQUIRED')
        normalized=policy.normalized()
        path=Path(path)
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                try: first=db.execute('SELECT kind,payload FROM events ORDER BY sequence LIMIT 1').fetchone()
                except sqlite3.DatabaseError: first=None
            if not first or first[0]!='PORT_INIT' or json.loads(first[1])!={'version':1,'policy':normalized}:
                raise PaperBrokerError('ISOLATED_PORTFOLIO_JOURNAL_AND_MATCHING_POLICY_REQUIRED')
        super().__init__(path,approval,wall_clock,monotonic_clock)
        self.emergency=EmergencyLatch(path.with_name(path.name+'.emergency.sqlite'),approval.account)
        if not self.sequence:
            self._append('portfolio-init','PORT_INIT',{'version':1,'policy':normalized})
        if self.portfolio.get('adaptive_exit',{}).get('readiness_enabled'):
            from executor_exit_readiness import ContinuousExitReadiness
            self.exit_readiness=ContinuousExitReadiness(self)

    @staticmethod
    def _empty():
        state=PaperBrokerLedger._empty()
        state['portfolio']=None
        return state

    @property
    def portfolio(self):
        return self.state['portfolio']

    def _rollback_snapshot(self,kind,data):
        # Opt-in Phase 9 only. Every object mutated by these specific reducers
        # is detached; immutable historical payloads can be shared. Unknown
        # events/operations retain the original whole-state deepcopy fallback.
        from executor_exit_readiness import rollback_snapshot
        snapshot=rollback_snapshot(self,kind,data) if getattr(self,'exit_readiness',None) is not None else None
        return snapshot if snapshot is not None else super()._rollback_snapshot(kind,data)

    def _append(self,event_id,kind,payload):
        if kind=='RECONCILED':
            payload=dict(payload,observed_at=utc(self.wall_clock()).isoformat())
        result=super()._append(event_id,kind,payload)
        readiness=getattr(self,'exit_readiness',None)
        if readiness is not None and result=='RECORDED':
            try:readiness.after_commit(kind,payload)
            except Exception:
                # A disposable optimization cannot mask an already committed
                # event or abandon durable protection. Fall back to Phase 8.
                readiness.invalidate();readiness.failure='READINESS_UNAVAILABLE_USE_DURABLE_SCHEDULER'
        return result

    def _reduce(self,kind,data):
        p=self.portfolio
        if kind=='PORT_NATIVE':
            from executor_native_protection import reduce_native
            return reduce_native(self,data)
        if kind=='PORT_PROTECTION':
            from executor_execution_coordinator import reduce_protection
            return reduce_protection(self,data)
        if kind=='PORT_EXIT_OPT':
            from executor_liquidation import reduce_adaptive
            return reduce_adaptive(self,data)
        if kind=='PORT_INIT':
            if p is not None: raise PaperBrokerError('PORTFOLIO_ALREADY_INITIALIZED')
            self.state['portfolio']={'policy':data['policy'],'targets':{},'quotes':{},'quote_history':{},
                'halted':False,'exit_reason':None,'emergency':False,'alerts':{},'cancel_requests':{},
                'entry_spx':None,'peak':None,'ride':False,'last_spx_source':None,'budget':None,
                'started_at':None,'entry_complete_at':None,'exit_started_at':None,'flat_at':None,
                'flat_revision':None,'execution_receipts':{},'arrival_quotes':{'BUY':{},'SELL':{}},'spx_watermarks':{},'last_pretrade':None}
            return
        if kind=='PORT_CONFIG':
            p.update(targets=data['targets'],entry_spx=data['entry_spx'],peak=data['entry_spx'],budget=data['budget'])
            if data.get('allocation'):
                p['allocation']=data['allocation']
                readiness=data['allocation'].get('exit_capacity_admission',{}).get('protection_plan')
                if readiness:
                    from executor_native_protection import reduce_native
                    reduce_native(self,dict(op='CONFIG',version=readiness['version']))
                p['arrival_quotes']['BUY']={str(line['contract']['con_id']):line['ask'] for line in data['allocation']['lines'] if line['quantity']}
            return
        if kind=='PORT_ALLOCATION_ID':
            if p.get('allocation_journal_id'):raise PaperBrokerError('ALLOCATION_JOURNAL_ALREADY_IDENTIFIED')
            p['allocation_journal_id']=data['journal_id']
            return
        if kind=='PORT_BROKER_CONTEXT':
            p['broker_context']=data
            return
        if kind=='PORT_FILL_SOURCE_TIME':
            old=p.setdefault('fill_source_times',{}).get(data['exec_id'])
            if old is not None and old!=data:raise PaperBrokerError('CHANGED_FILL_SOURCE_TIME')
            p['fill_source_times'][data['exec_id']]=data
            return
        if kind=='PORT_QUOTE':
            q=data['quote'];cid=str(q['option']['contract']['con_id'])
            old=p['quote_history'].get(q['quote_id'])
            if old is not None and old!=q: raise PaperBrokerError('CHANGED_LIQUIDITY_QUOTE_ID')
            previous=p['quotes'].get(cid)
            if previous and datetime.fromisoformat(q['option']['source_time'])<datetime.fromisoformat(previous['option']['source_time']):
                raise PaperBrokerError('BACKWARD_LIQUIDITY_SOURCE_TIME')
            if q['spx'] is not None:
                source=q['spx']['source_time']
                if cid in p['spx_watermarks'] and datetime.fromisoformat(source)<datetime.fromisoformat(p['spx_watermarks'][cid]):
                    raise PaperBrokerError('BACKWARD_UNDERLYING_SOURCE_TIME')
                p['spx_watermarks'][cid]=source
            if previous and (datetime.fromisoformat(q['sizes_source_time'])<datetime.fromisoformat(previous['sizes_source_time'])
                    or q['sizes_received_monotonic']<previous['sizes_received_monotonic']
                    or q['option']['received_monotonic']<previous['option']['received_monotonic']):
                raise PaperBrokerError('BACKWARD_SIZE_OR_RECEIPT_EVIDENCE')
            p['quotes'][cid]=q;p['quote_history'][q['quote_id']]=q
            return
        if kind=='PORT_MARKET_INVALID':
            p['quotes'].pop(str(data['contract_id']),None)
            p['halted']=True
            p['alerts'].setdefault('invalid-market:'+str(data['contract_id']),dict(reason='INVALID_MARKET_OBSERVATION',at=data['at']))
            return
        if kind=='PORT_VALIDATION':
            p['last_pretrade']=data
            return
        if kind=='PORT_HALT':
            p['halted']=True
            return
        if kind=='PORT_ALERT':
            p['alerts'].setdefault(data['key'],data)
            return
        if kind=='PORT_EXIT':
            p['halted']=True
            p['emergency']=p['emergency'] or data['emergency']
            p['exit_reason']='EMERGENCY_EXIT_ALL' if p['emergency'] else p['exit_reason'] or data['reason']
            p['exit_started_at']=p['exit_started_at'] or data['at']
            p['flat_revision']=None;p['flat_at']=None
            if p.get('adaptive_exit'):
                for cid,value in data.get('exit_arrivals',{}).items():p['adaptive_exit']['arrivals'].setdefault(cid,value)
            return
        if kind=='PORT_SPX':
            p.update(peak=data['peak'],ride=data['ride'],last_spx_source=data['source'])
            if data['reason']:
                self._reduce('PORT_EXIT',dict(reason=data['reason'],emergency=False,at=data['at'],exit_arrivals=data.get('exit_arrivals',{})))
            return
        if kind=='PORT_CANCEL':
            p['cancel_requests'].setdefault(data['intent_id'],data['at'])
            return
        if kind=='PORT_PLAN':
            for intent in data['intents']:
                p['arrival_quotes'][intent['side']].setdefault(str(intent['contract']['con_id']),intent['arrival_price'])
                super()._reduce('INTENT',intent)
            self._assert_reservations(enforce_cash=any(i['side']=='BUY' for i in data['intents']))
            return
        if kind=='PORT_DISPATCH':
            if data.get('exit_pacing'):
                from executor_liquidation import reduce_adaptive
                reduce_adaptive(self,dict(op='DISPATCH',**data['exit_pacing']))
            for binding in data['bindings']:
                super()._reduce('BIND',binding)
                self.state['intents'][binding['intent_id']]['submitted_at']=data['at']
                self.state['intents'][binding['intent_id']]['evidence_generation']=self.state['generation']
                self.state['intents'][binding['intent_id']]['order_revision']=self.state['intents'][binding['intent_id']].get('order_revision',0)+1
            p['started_at']=p['started_at'] or data['at']
            return
        if kind in {'PORT_ORDER','PORT_EXECUTION','PORT_COMMISSION'}:
            record=data['record'];base=kind[5:]
            if base=='EXECUTION' and record['exec_id'] in self.state['executions'] and self.state['executions'][record['exec_id']]['record']==record:
                return
            was_terminal=False
            if base=='EXECUTION': was_terminal=self._intent_for_ref(record['order_ref'])['terminal_proven']
            if base=='EXECUTION' and not record['bust']:
                item=self._intent_for_ref(record['order_ref'])
                if not item.get('native_managed') and (record['side']=='BUY' and money(record['price'])>money(item['limit_price'])
                        or record['side']=='SELL' and money(record['price'])<money(item['limit_price'])):
                    raise PaperBrokerError('FILL_OUTSIDE_PROTECTED_LIMIT')
            super()._reduce(base,record)
            if base=='ORDER':
                item=self._intent_for_ref(record['order_ref'])
                if record['perm_id'] and not item.get('ack_at'): item['ack_at']=data['at']
                if record['status']=='Inactive':
                    item['rejected_at']=item.get('rejected_at') or data['at']
                    p['alerts'].setdefault('rejected:'+item['intent_id'],dict(reason='BROKER_REJECTION_UNRESOLVED',at=data['at']))
                    if item['side']=='BUY': p['halted']=True
                if record['status'] in {'Cancelled','ApiCancelled'}: item['cancelled_at']=item.get('cancelled_at') or data['at']
            elif base=='EXECUTION':
                p['execution_receipts'].setdefault(record['exec_id'],data['at'])
                p['flat_at']=p['flat_revision']=None
                if record['side']=='BUY' and (p['exit_reason'] or was_terminal):
                    p['halted']=True
                    if was_terminal: p['alerts'].setdefault('late-fill',dict(reason='LATE_FILL_REQUIRES_RECONCILIATION',at=data['at']))
                for cid,qty in self.positions().items():
                    if str(cid) not in p['targets'] or qty>p['targets'][str(cid)]['quantity']:
                        raise PaperBrokerError('PORTFOLIO_POSITION_EXCEEDS_CONFIGURED_TARGET')
                    try: self._premium_stop(cid)
                    except DryRunError:
                        self._reduce('PORT_EXIT',dict(reason='INVALID_PREMIUM_STOP',emergency=True,at=data['at']))
                if any(self._bought(int(cid))+self.reservations('BUY',int(cid))>t['quantity'] for cid,t in p['targets'].items()):
                    p['halted']=True
                    p['alerts'].setdefault('late-fill-reservations',dict(reason='LATE_FILL_REQUIRES_ACQUISITION_CANCELLATION',at=data['at']))
                if not self._entry_filled(): p['entry_complete_at']=None
                from executor_execution_coordinator import protection_after_economic
                protection_after_economic(self,'EXECUTION')
                item=self._intent_for_ref(record['order_ref'])
                if item.get('native_managed') and record['side']=='SELL' and not record['bust'] and record['quantity']:
                    self._reduce('PORT_EXIT',dict(reason='INITIAL_STOP',emergency=False,at=data['at']))
            from executor_native_protection import refresh_native
            refresh_native(self,base)
            item=self._intent_for_ref(record['order_ref']) if base!='COMMISSION' else None
            if item is not None:item['order_revision']=item.get('order_revision',0)+1
            return
        super()._reduce(kind,data)
        if kind in {'RESTART','DISCONNECT','RECONCILED'} and p is not None:
            from executor_native_protection import refresh_native
            refresh_native(self,kind,data)
        if kind in {'RESTART','DISCONNECT'} and p is not None and p.get('protection_lifecycle'):
            from executor_execution_coordinator import protection_after_economic
            protection_after_economic(self,kind)
        if kind=='RECONCILED':
            if self._entry_filled() and not any(i['side']=='BUY' and not i['terminal_proven'] for i in self.state['intents'].values()):
                p['entry_complete_at']=p['entry_complete_at'] or data['observed_at']
            from executor_execution_coordinator import unresolved_protection_orders
            if p['exit_reason'] and not self.positions() and all(i['terminal_proven'] for i in self.state['intents'].values()) and not unresolved_protection_orders(self):
                p['flat_at']=p['flat_at'] or data['observed_at'];p['flat_revision']=self.state['revision']
                for obligation in p.get('protection_obligations',{}).values():
                    obligation['disposition']='DISCHARGED_BY_CONFIRMED_FLAT'
                    obligation['closed_reconciliation_at']=data['observed_at']

    def _fault(self,reason,payload=None):
        # Quarantine stops outstanding acquisition scheduling immediately too;
        # existing BUY reservations are retained until cancellation is proved.
        if self.portfolio is not None and not self.portfolio['halted']:
            self._append('fault-halt:'+str(self.sequence+1),'PORT_HALT',{'reason':'BROKER_OR_MARKET_EVIDENCE_FAULT'})
        return super()._fault(reason,payload)

    def _intent_for_ref(self,ref):
        matches=[i for i in self.state['intents'].values() if i['order_ref']==ref]
        if len(matches)!=1: raise PaperBrokerError('UNKNOWN_PORTFOLIO_ORDER_REF')
        return matches[0]

    def _identity(self,data):
        intent=super()._identity(data)
        if 'remaining' in data and not intent.get('native_managed') and (data.get('order_type')!='LMT' or money(data.get('limit_price'),True)!=money(intent['limit_price'])):
            raise PaperBrokerError('PRICE_PROTECTED_ORDER_ACK_MISMATCH')
        return intent

    def _record(self,generation,event_id,kind,payload):
        self._session(generation)
        if kind in {'ORDER','EXECUTION','COMMISSION'}:
            old=self.db.execute('SELECT kind,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
            if old:
                if old[0]!='PORT_'+kind or json.loads(old[1])['record']!=payload:
                    self._fault('CONFLICTING_DUPLICATE_EVENT')
                return 'DUPLICATE'
            return super()._record(generation,event_id,'PORT_'+kind,dict(record=payload,at=utc(self.wall_clock()).isoformat()))
        return super()._record(generation,event_id,kind,payload)

    @synchronized
    def observe_order(self,generation,event_id,record):
        if isinstance(record,BrokerOrderRecord):
            intent=next((i for i in self.state['intents'].values() if i['order_ref']==record.order_ref),None)
            if intent and intent.get('native_managed'):
                return super().observe_order(generation,event_id,record)
        if not isinstance(record,PortfolioOrderRecord): self._fault('PORTFOLIO_LIMIT_ACK_REQUIRED')
        return super().observe_order(generation,event_id,replace(record,limit_price=str(money(record.limit_price,True))))

    def prepare_intent(self,*args,**kwargs):
        raise PaperBrokerError('USE_ATOMIC_PORTFOLIO_SCHEDULER')

    def reserve_order_identity(self,*args,**kwargs):
        raise PaperBrokerError('USE_OFFLINE_BATCH_DISPATCH')

    def _entry_filled(self):
        return bool(self.portfolio['targets']) and all(self._bought(int(cid))==item['quantity'] for cid,item in self.portfolio['targets'].items())

    def _bought(self,cid):
        return sum(e['record']['quantity'] for e in self.state['executions'].values() if e['active'] and e['record']['side']=='BUY' and e['record']['contract']['con_id']==cid)

    def _spent(self):
        return sum((money(e['record']['price'])*100*e['record']['quantity'] for e in self.state['executions'].values()
                    if e['active'] and e['record']['side']=='BUY'),Decimal(0))+sum((max(Decimal(0),money(c['amount'])) for key,c in self.state['commissions'].items() if key in self.state['executions'] and self.state['executions'][key]['active']),Decimal(0))

    def _cash_reserved(self):
        return sum((money(i['cash_per_contract'])*(i['quantity']-self.filled(i)) for i in self.state['intents'].values()
                    if i['side']=='BUY' and not i['terminal_proven']),Decimal(0))

    def _assert_reservations(self,enforce_cash=True):
        p=self.portfolio
        for cid,target in p['targets'].items():
            if self._bought(int(cid))+self.reservations('BUY',int(cid))>target['quantity']:
                raise PaperBrokerError('PORTFOLIO_ACQUISITION_OVER_RESERVED')
            if self.reservations('SELL',int(cid))>self.positions().get(int(cid),0):
                raise PaperBrokerError('PORTFOLIO_EXIT_OVER_RESERVED')
            if p.get('adaptive_exit'):
                from executor_liquidation import external_reserved
                if self.reservations('SELL',int(cid))+external_reserved(self,int(cid))>self.positions().get(int(cid),0):
                    raise PaperBrokerError('COMBINED_PROTECTIVE_AND_EXIT_OVER_RESERVED')
        if sum(self.positions().values())>MAX_PORTFOLIO_CONTRACTS or len(self.positions())>MAX_STRIKES:
            raise PaperBrokerError('PORTFOLIO_CAP_EXCEEDED')
        if enforce_cash and self._spent()+self._cash_reserved()>money(p['budget']):
            raise PaperBrokerError('PORTFOLIO_CASH_OVER_RESERVED')

    def _premium_stop(self,cid):
        fills=[e['record'] for e in self.state['executions'].values() if e['active'] and e['record']['side']=='BUY' and e['record']['contract']['con_id']==cid]
        quantity=sum(e['quantity'] for e in fills)
        average=sum((money(e['price'])*e['quantity'] for e in fills),Decimal(0))/quantity if quantity else None
        return (average,DryRunExecutor._stop_price(average)) if quantity else (None,None)

    @synchronized
    def configure(self,generation,targets,entry_spx,account,session_has_expiration,allocation_plan=None):
        self._planning_gate(generation)
        if self.portfolio['targets'] or self.state['intents'] or self.portfolio['halted']:
            raise PaperBrokerError('PORTFOLIO_CONFIGURATION_ALREADY_BOUND')
        if not isinstance(targets,(tuple,list)) or not 1<=len(targets)<=MAX_STRIKES:
            raise PaperBrokerError('ONE_TO_FIVE_DISTINCT_STRIKES_REQUIRED')
        result={};strikes=set();rights=set()
        for contract,quantity in targets:
            if not isinstance(contract,VerifiedOptionContract) or not integer(quantity,1,25): raise PaperBrokerError('INVALID_STRIKE_TARGET')
            validate_contract(contract,'CALL' if contract.right=='C' else 'PUT',self.wall_clock(),session_has_expiration)
            if str(contract.con_id) in result or contract.strike in strikes: raise PaperBrokerError('DUPLICATE_PORTFOLIO_STRIKE')
            result[str(contract.con_id)]={'contract':asdict(contract),'quantity':quantity}
            strikes.add(contract.strike);rights.add(contract.right)
        if len(rights)!=1: raise PaperBrokerError('DIRECTION_ONLY_PORTFOLIO_REQUIRED')
        budget=min(conservative_usable_funds(account,self.approval.account,self.monotonic_clock()),money(self.portfolio['policy']['max_premium_cash']))
        allocation=None
        if allocation_plan is not None:
            from executor_allocation import AllocationPlan
            if not isinstance(allocation_plan,AllocationPlan):raise PaperBrokerError('IMMUTABLE_ALLOCATION_PLAN_REQUIRED')
            allocation=allocation_plan.report()
            expected={str(line['contract']['con_id']):{'contract':line['contract'],'quantity':line['quantity']} for line in allocation['lines'] if line['quantity']}
            if (allocation['binding']['account']!=self.approval.account or allocation['binding']['environment']!='PAPER'
                    or allocation['generation']!=self.portfolio.get('broker_context',{}).get('broker_generation',generation) or allocation['policy']['execution']!=self.portfolio['policy']
                    or expected!=result or money(allocation['reserved_capital'],True)>budget
                    or money(allocation['entry_spx'],True)!=money(entry_spx,True)):
                raise PaperBrokerError('FUNDED_ALLOCATION_BINDING_MISMATCH')
            budget=money(allocation['reserved_capital'],True)
            allocation=dict(allocation,plan_digest=allocation_plan.digest)
        return self._append('portfolio-config','PORT_CONFIG',dict(targets=result,entry_spx=str(money(entry_spx,True)),budget=str(budget),**({'allocation':allocation} if allocation else {})))

    @synchronized
    def observe_liquidity(self,generation,event_id,quote):
        self._session(generation)
        if not isinstance(quote,LiquidityFixture) or quote.origin!='OFFLINE_FIXTURE': raise PaperBrokerError('OFFLINE_FIXTURE_ONLY')
        target=self.portfolio['targets'].get(str(quote.option.contract.con_id))
        if not target or asdict(quote.option.contract)!=target['contract'] or quote.option.quote_con_id!=quote.option.contract.con_id: raise PaperBrokerError('WRONG_PORTFOLIO_CONTRACT')
        try:
            result=self._append(event_id,'PORT_QUOTE',{'quote':fixture_payload(quote)})
        except PaperBrokerError as exc: self._fault(str(exc))
        except (ValueError,TypeError,AttributeError):
            self._append('invalid-market:'+str(self.sequence+1),'PORT_MARKET_INVALID',dict(contract_id=quote.option.contract.con_id,at=utc(self.wall_clock()).isoformat()))
            raise PaperBrokerError('INVALID_LIQUIDITY_OBSERVATION')
        if self.positions().get(quote.option.contract.con_id,0):
            try:
                clock=EvidenceClock();clock.validate_option_risk('CALL' if quote.option.contract.right=='C' else 'PUT',quote.option,self.wall_clock(),self.monotonic_clock(),lambda _:True)
                _,stop=self._premium_stop(quote.option.contract.con_id)
                if money(quote.option.bid)<=stop: self.trigger_exit('premium:'+quote.quote_id,'INITIAL_STOP')
            except EvidenceError: pass
        return result

    @synchronized
    def observe_spx(self,generation,event_id,spx):
        self._session(generation)
        old=self.db.execute('SELECT kind,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
        if old:
            prior=json.loads(old[1])
            if old[0]!='PORT_SPX' or (prior['price'],prior['source'],prior['receipt'],prior['status'])!=(str(money(spx.price)),spx.source_time.isoformat(),spx.received_monotonic,spx.status.value):
                self._fault('CHANGED_SPX_EVENT_ID')
            return 'DUPLICATE'
        if not self.positions(): raise PaperBrokerError('NO_CONFIRMED_FILL_FOR_SPX_PROTECTION')
        p=self.portfolio;clock=EvidenceClock()
        if p['last_spx_source']: clock.last_source['SPX']=datetime.fromisoformat(p['last_spx_source'])
        clock.validate_spx(spx,self.wall_clock(),self.monotonic_clock())
        _,_,peak,ride,_,reason=spx_profit_decision('CALL' if next(iter(p['targets'].values()))['contract']['right']=='C' else 'PUT',
            money(p['entry_spx']),money(p['peak']),p['ride'],money(spx.price))
        extra={}
        if reason and p.get('adaptive_exit'):
            from executor_liquidation import capture_exit_arrivals
            extra['exit_arrivals']=capture_exit_arrivals(self)
        return self._append(event_id,'PORT_SPX',dict(peak=str(peak),ride=ride,reason=reason,source=spx.source_time.isoformat(),price=str(money(spx.price)),receipt=spx.received_monotonic,status=spx.status.value,at=utc(self.wall_clock()).isoformat(),**extra))

    def trigger_exit(self,event_id,reason,emergency=False):
        if not text_id(event_id) or not text_id(reason) or type(emergency) is not bool:
            raise PaperBrokerError('INVALID_PORTFOLIO_EXIT')
        # The independent durable latch bypasses the portfolio scheduling lock.
        if emergency:
            self.emergency.request(event_id)
        with self.lock:
            old=self.db.execute('SELECT kind,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
            if old:
                prior=json.loads(old[1])
                if old[0]!='PORT_EXIT' or (prior['reason'],prior['emergency'])!=(reason,emergency): raise PaperBrokerError('CONFLICTING_EXIT_REQUEST')
                return 'DUPLICATE'
            extra={}
            if self.portfolio.get('adaptive_exit'):
                from executor_liquidation import capture_exit_arrivals
                extra['exit_arrivals']=capture_exit_arrivals(self)
            return self._append(event_id,'PORT_EXIT',dict(reason=reason,emergency=emergency,at=utc(self.wall_clock()).isoformat(),**extra))

    def _sync_emergency(self):
        if self.portfolio['emergency'] and not self.emergency.blocked():
            self.emergency.request('derived-portfolio-emergency')
        if self.emergency.blocked() and not self.portfolio['emergency']:
            token=hashlib.sha256(self.emergency.status()['request_id'].encode()).hexdigest()[:32]
            self._append('latch:'+token,'PORT_EXIT',dict(reason='EMERGENCY_EXIT_ALL',emergency=True,at=utc(self.wall_clock()).isoformat()))

    def _alert(self,key,reason):
        if key not in self.portfolio['alerts']:
            self._append('alert:'+key,'PORT_ALERT',dict(key=key,reason=reason,at=utc(self.wall_clock()).isoformat()))
        return {'type':'RISK_ALERT','key':key,'reason':reason,'executable':False}

    def _cancel(self,item):
        if item['order_id'] is None:
            self._append('abandon:'+item['intent_id'],'LOCAL_ABANDON',{'intent_id':item['intent_id']})
            return {'type':'LOCAL_ABANDONED','intent_id':item['intent_id'],'executable':False}
        if item['intent_id'] not in self.portfolio['cancel_requests']:
            self._append('cancel:'+item['intent_id'],'PORT_CANCEL',dict(intent_id=item['intent_id'],at=utc(self.wall_clock()).isoformat()))
        return {'type':'CANCEL_REQUIRED','action_id':'cancel:'+item['intent_id'],'intent_id':item['intent_id'],'order_id':item['order_id'],'executable':False}

    def _order_policy(self,record,intent):
        if intent.get('native_managed'):
            if record.order_type!='STP' or money(record.stop_price,True)!=money(intent['stop_price'],True):
                raise PaperBrokerError('RECONCILED_NATIVE_STOP_POLICY_MISMATCH')
            return
        if not isinstance(record,PortfolioOrderRecord) or record.order_type!='LMT' or money(record.limit_price,True)!=money(intent['limit_price']):
            raise PaperBrokerError('RECONCILED_LIMIT_POLICY_MISMATCH')

    @synchronized
    def reconcile(self,snapshot):
        self._sync_emergency()
        result=_reconcile_snapshot(self,snapshot,max_positions=MAX_STRIKES,order_policy=self._order_policy,
            authoritative_order_proof=bool(self.portfolio.get('native_protection')))
        if result['matched'] and self.portfolio['flat_revision']==self.state['revision'] and self.portfolio['emergency']:
            self.emergency.confirm_flat('offline-portfolio:{}:{}'.format(snapshot.generation,snapshot.cycle_id))
        return result

    @synchronized
    def dispatch_batch(self,generation,intent_ids,account=None,first_order_id=None):
        self._sync_emergency()
        self._planning_gate(generation)
        if not intent_ids or len(set(intent_ids))!=len(intent_ids): raise PaperBrokerError('UNIQUE_BATCH_REQUIRED')
        if any(self.state['intents'][key].get('native_managed') for key in intent_ids):raise PaperBrokerError('USE_INDEPENDENT_NATIVE_REDUCTION_BOUNDARY')
        if any(i['uncertain'] for i in self.state['intents'].values()): raise PaperBrokerError('UNCERTAIN_SUBMISSION_NO_RETRY')
        exit_pacing=None
        if self.portfolio.get('adaptive_exit') and any(self.state['intents'][key]['side']=='SELL' for key in intent_ids):
            from executor_liquidation import ClosingOrderSafety
            ClosingOrderSafety.batch(self,[self.state['intents'][key] for key in intent_ids])
            exit_pacing=dict(at=utc(self.wall_clock()).isoformat(),count=len(intent_ids))
        if any(self.state['intents'][key]['side']=='BUY' for key in intent_ids):
            if self._cash_reserved()>conservative_usable_funds(account,self.approval.account,self.monotonic_clock()):
                raise PaperBrokerError('DISPATCH_BUYING_POWER_INADEQUATE')
        bindings=[]
        next_order_id=max((i['order_id'] or 0 for i in self.state['intents'].values()),default=999)+1
        if first_order_id is not None:
            if not integer(first_order_id,next_order_id):raise PaperBrokerError('GLOBAL_ORDER_ID_MUST_ADVANCE')
            next_order_id=first_order_id
        for key in intent_ids:
            item=self.state['intents'][key]
            if item['order_id'] is not None or item['terminal_proven']: raise PaperBrokerError('INTENT_ALREADY_DISPATCHED_OR_ABANDONED')
            if item['side']=='BUY' and (self.portfolio['halted'] or self.portfolio['exit_reason']): raise PaperBrokerError('ACQUISITION_BLOCKED')
            self._assess(item['contract']['con_id'],item['side'])
            current=fixture_from_payload(self.portfolio['quotes'][str(item['contract']['con_id'])]).option
            if item['side']=='BUY' and money(current.ask)>money(item['limit_price']) or item['side']=='SELL' and money(current.bid)<money(item['limit_price']):
                raise PaperBrokerError('PRICE_BOUND_NO_LONGER_MARKETABLE')
            bindings.append(dict(intent_id=key,order_id=next_order_id+len(bindings),client_id=self.session.client_id))
        if any(self.state['intents'][key]['side']=='BUY' for key in intent_ids) and self.emergency.blocked():
            raise PaperBrokerError('EMERGENCY_BLOCKS_DISPATCH')
        self._assert_reservations(enforce_cash=any(self.state['intents'][key]['side']=='BUY' for key in intent_ids))
        return self._append('dispatch:'+str(self.sequence+1),'PORT_DISPATCH',dict(bindings=bindings,at=utc(self.wall_clock()).isoformat(),**({'exit_pacing':exit_pacing} if exit_pacing else {})))

    def _assess(self,cid,side):
        data=self.portfolio['quotes'].get(str(cid))
        if data is None: raise PaperBrokerError('EXACT_LIQUIDITY_QUOTE_MISSING')
        quote=fixture_from_payload(data)
        direction='CALL' if quote.option.contract.right=='C' else 'PUT'
        result=assess_liquidity(quote,direction,self.wall_clock(),self.monotonic_clock(),lambda _:True,self.portfolio['policy'],side)
        if side=='BUY':
            try: DryRunExecutor._stop_price(result['ask'])
            except DryRunError as exc: raise PaperBrokerError('NONPOSITIVE_INITIAL_PREMIUM_STOP') from exc
        return quote,result

    def _halt(self,reason):
        if not self.portfolio['halted']:
            self._append('halt:'+str(self.sequence+1),'PORT_HALT',{'reason':reason})
        return self._alert('acquisition-halted',reason)

    @synchronized
    def schedule(self,generation,account=None):
        self._sync_emergency()
        self._session(generation)
        if self.portfolio.get('adaptive_exit') and self.portfolio['exit_reason']:
            from executor_liquidation import GlobalLiquidationCoordinator
            return GlobalLiquidationCoordinator(self).plan()
        p=self.portfolio;policy=p['policy'];actions=[]
        if not p['targets']: raise PaperBrokerError('PORTFOLIO_NOT_CONFIGURED')
        now=utc(self.wall_clock())
        if p['exit_started_at'] and (now-datetime.fromisoformat(p['exit_started_at'])).total_seconds()>float(policy['liquidation_deadline']):
            actions.append(self._alert('liquidation-deadline','UNFILLED_EXIT_DEADLINE_OPERATOR_REVIEW_REQUIRED'))
        if not p['halted'] and p['started_at'] and (now-datetime.fromisoformat(p['started_at'])).total_seconds()>float(policy['entry_deadline']):
            actions.append(self._halt('ENTRY_DEADLINE_EXCEEDED'))
        working=[i for i in self.state['intents'].values() if not i['terminal_proven']]
        for item in working:
            if item.get('native_managed'):
                cid=item['contract']['con_id']
                excess=self.reservations('SELL',cid)>self.positions().get(cid,0)
                if p['exit_reason'] or item['status']=='Inactive' or excess:actions.append(self._cancel(item))
                continue
            if item['order_id'] is None:
                quote=p['quotes'].get(str(item['contract']['con_id']))
                if quote and (item['side']=='BUY' and money(quote['option']['ask'])>money(item['limit_price'])
                              or item['side']=='SELL' and money(quote['option']['bid'])<money(item['limit_price'])):
                    actions.append(self._cancel(item))
                    continue
            timed=False
            if item.get('submitted_at'):
                age=(now-datetime.fromisoformat(item['submitted_at'])).total_seconds()
                timed=age>float(policy['working_timeout']) or not item.get('ack_at') and age>float(policy['acknowledgement_timeout'])
                if not item.get('ack_at') and age>float(policy['acknowledgement_timeout']):
                    actions.append(self._alert('ack:'+item['intent_id'],'UNCERTAIN_ACKNOWLEDGEMENT_RECONCILE_NO_RETRY'))
            excess=item['side']=='SELL' and self.reservations('SELL',item['contract']['con_id'])>self.positions().get(item['contract']['con_id'],0)
            if item['side']=='BUY' and p['halted'] or timed or item['status']=='Inactive' or excess:
                actions.append(self._cancel(item))
        try: self._planning_gate(generation)
        except PaperBrokerError:
            return actions+[{'type':'RECONCILE_REQUIRED','executable':False}]
        if self.state['faults']: return actions+[{'type':'RECOVERY_REQUIRED','executable':False}]
        side='SELL' if p['exit_reason'] else 'BUY'
        if side=='BUY' and p['halted']: return actions or [{'type':'ACQUISITION_HALTED','executable':False}]
        if side=='BUY' and self._entry_filled(): return actions or [{'type':'HOLDING_MONITOR_PROTECTION','executable':False}]
        assessments={}
        for cid,target in p['targets'].items():
            if side=='SELL' and not self.positions().get(int(cid),0): continue
            try: assessments[cid]=self._assess(int(cid),side)
            except (PaperBrokerError,EvidenceError,IngestionError,ValueError) as exc:
                if side=='BUY':
                    actions.append(self._halt(str(exc)))
                    return actions+[self._cancel(i) for i in working if i['side']=='BUY' and not i['terminal_proven']]
                actions.append(self._alert('exit-liquidity:'+cid,'EXIT_LIQUIDITY_UNAVAILABLE_RETAIN_EXPOSURE'))
        if side=='BUY':
            try:
                funds=conservative_usable_funds(account,self.approval.account,self.monotonic_clock())
                desired=self._spent()+self._cash_reserved();delta=Decimal(0)
                if len({canonical(fixture_payload(q)['spx']) for q,_ in assessments.values()})!=1:
                    raise PaperBrokerError('PORTFOLIO_UNDERLYING_EVIDENCE_DISAGREES')
                source_times=[utc(t) for q,_ in assessments.values() for t in (q.option.source_time,q.sizes_source_time,q.greeks_source_time)]
                receipts=[t for q,_ in assessments.values() for t in (q.option.received_monotonic,q.sizes_received_monotonic,q.greeks_received_monotonic)]
                from simulation_evidence import MAX_SYNC_SECONDS
                if (max(source_times)-min(source_times)).total_seconds()+2*max(float(q.source_uncertainty_seconds) for q,_ in assessments.values())>MAX_SYNC_SECONDS or max(receipts)-min(receipts)>MAX_SYNC_SECONDS:
                    raise PaperBrokerError('PORTFOLIO_SOURCE_COHERENCE_FAILED')
                for cid,target in p['targets'].items():
                    quote,a=assessments[cid]
                    pending=max(0,target['quantity']-self._bought(int(cid))-self.reservations('BUY',int(cid)))
                    _,cap=protected_limit('BUY',p['arrival_quotes']['BUY'].get(cid,a['ask']),a['ask'],policy['max_entry_slippage'],policy['reprice_step'],0,a['tick'])
                    desired+=pending*(cap*100+money(policy['fee_reserve_per_contract']))
                    delta+=target['quantity']*abs(money(quote.greeks.delta))*money(quote.spx.price)*100
                planned_risk=sum(t['quantity'] for t in p['targets'].values())*(Decimal('3.25')+money(policy['max_exit_slippage']))*100
                if (desired>money(p['budget']) or desired-self._spent()>funds or delta>money(policy['max_delta_notional']) or planned_risk>money(policy['max_planned_stop_risk'])):
                    raise PaperBrokerError('AGGREGATE_CASH_OR_RISK_LIMIT')
                self._append('pretrade:'+str(self.sequence+1),'PORT_VALIDATION',dict(
                    at=now.isoformat(),usable_funds=str(funds),projected_total_entry_cash=str(desired),
                    planned_stop_risk=str(planned_risk),projected_delta_notional=str(delta),
                    strikes={cid:assessments[cid][1] for cid in assessments},executable=False))
            except (PaperBrokerError,EvidenceError) as exc:
                return actions+[self._halt(str(exc))]+[self._cancel(i) for i in working if i['side']=='BUY' and not i['terminal_proven']]
        plans=[]
        for item in working:
            if item['order_id'] is None and not item['terminal_proven'] and item['side']==side:
                actions.append({'type':'SIMULATED_LIMIT_INTENT','intent_id':item['intent_id'],'contract_id':item['contract']['con_id'],
                    'side':side,'quantity':item['quantity'],'limit_price':item['limit_price'],'executable':False})
        for cid,target in p['targets'].items():
            if cid not in assessments: continue
            quote,a=assessments[cid]
            active=[i for i in self.state['intents'].values() if i['contract']['con_id']==int(cid) and not i['terminal_proven']]
            if side=='SELL' and any(i['side']=='BUY' for i in active): continue
            # A cancellation in flight retains its full residual reservation.
            if any(i['intent_id'] in p['cancel_requests'] or i['status']=='Inactive' for i in active): continue
            current_side=[i for i in active if i['side']==side]
            remaining=(target['quantity']-self._bought(int(cid))-self.reservations('BUY',int(cid)) if side=='BUY' else self.positions().get(int(cid),0)-self.reservations('SELL',int(cid)))
            used=sum(i['quantity'] for i in self.state['intents'].values() if i['contract']['con_id']==int(cid) and i['side']==side and i['quote_id']==quote.quote_id)
            other=sum(i['quantity']-self.filled(i) for i in current_side if i['quote_id']!=quote.quote_id)
            capacity=max(0,a['buy_capacity' if side=='BUY' else 'sell_capacity']-used-other)
            candidates=[i for i in self.state['intents'].values() if i['contract']['con_id']==int(cid) and i['side']==side and i['terminal_proven'] and self._route_remaining(i)>0]
            roots={i['route_id']:i for i in sorted(candidates,key=lambda i:i['attempt'])}
            roots=[i for route,i in roots.items() if not any(w['route_id']==route for w in active)]
            slots=policy['max_working_per_strike']-len(current_side)
            for _ in range(max(0,slots)):
                if remaining<=0 or capacity<=0: break
                previous=roots.pop(0) if roots else None
                attempt=previous['attempt']+1 if previous else 0
                if previous and attempt>policy['max_reprices']:
                    actions.append(self._alert('retry:'+previous['route_id'],'PRICE_PROTECTED_RETRY_BUDGET_EXHAUSTED'))
                    if side=='BUY': actions.append(self._halt('ENTRY_RETRY_BUDGET_EXHAUSTED'))
                    # Never create a fresh route to evade a depleted retry budget.
                    break
                arrival=previous['arrival_price'] if previous else p['arrival_quotes'][side].get(cid,a['ask' if side=='BUY' else 'bid'])
                limit,bound=protected_limit(side,arrival,a['ask' if side=='BUY' else 'bid'],policy['max_entry_slippage' if side=='BUY' else 'max_exit_slippage'],policy['reprice_step'],attempt,a['tick'])
                if limit is None:
                    actions.append(self._alert('price:'+cid+':'+side,'PROTECTED_PRICE_BOUND_NOT_MARKETABLE'))
                    if side=='BUY': actions.append(self._halt('ENTRY_PRICE_BOUND_EXHAUSTED'))
                    break
                quantity=min(remaining,capacity,policy['max_slice'],self._route_remaining(previous) if previous else 25)
                if quantity<policy['min_slice'] and quantity!=remaining: break
                namespace=p.get('allocation',{}).get('trade_id')
                key='pf:'+((namespace+':') if namespace else '')+str(self.sequence+1)+':'+str(len(plans))
                route=previous['route_id'] if previous else key
                plan=dict(intent_id=key,order_ref=key,contract=target['contract'],side=side,quantity=quantity,
                    created_at=now.isoformat(),limit_price=str(limit),price_bound=str(bound),arrival_price=str(arrival),
                    quote_id=quote.quote_id,route_id=route,route_quantity=previous['route_quantity'] if previous else quantity,
                    attempt=attempt,cash_per_contract=str(bound*100+money(policy['fee_reserve_per_contract'])) if side=='BUY' else '0')
                plans.append(plan);remaining-=quantity;capacity-=quantity
        if plans:
            # Emergency/halt may have been latched while evaluating another strike.
            if side=='BUY' and (p['halted'] or self.emergency.blocked()): return actions or [{'type':'EMERGENCY_PENDING','executable':False}]
            self._append('plan:'+str(self.sequence+1),'PORT_PLAN',{'intents':plans})
            actions.extend({'type':'SIMULATED_LIMIT_INTENT','intent_id':i['intent_id'],'contract_id':i['contract']['con_id'],
                'side':side,'quantity':i['quantity'],'limit_price':i['limit_price'],'executable':False} for i in plans)
        return actions or [{'type':'WAIT_BROKER_EVIDENCE','executable':False}]

    def _route_remaining(self,item):
        return item['route_quantity']-sum(self.filled(i) for i in self.state['intents'].values() if i['route_id']==item['route_id'])

    @synchronized
    def liquidity_report(self):
        rows=[];delta=Decimal(0);delta_known=True
        for cid,target in self.portfolio['targets'].items():
            row={'contract_id':int(cid),'contract':target['contract'],'target_quantity':target['quantity']}
            for side in ('BUY','SELL'):
                try:
                    quote,assessment=self._assess(int(cid),side)
                    row[side]=dict(assessment,quote_age_seconds=(utc(self.wall_clock())-utc(quote.option.source_time)).total_seconds(),
                        trading_status=quote.trading_status,quote_id=quote.quote_id)
                except (PaperBrokerError,EvidenceError,IngestionError,ValueError,TypeError) as exc:
                    row[side]={'block_reason':str(exc),'executable':False}
            amount=self.positions().get(int(cid),0)+self.reservations('BUY',int(cid))
            if amount:
                try:
                    quote,_=self._assess(int(cid),'BUY')
                    delta+=amount*abs(money(quote.greeks.delta))*money(quote.spx.price)*100
                except (PaperBrokerError,EvidenceError,IngestionError,ValueError,TypeError): delta_known=False
            rows.append(row)
        quantity=sum(self.positions().values())+sum(self.reservations('BUY',int(cid)) for cid in self.portfolio['targets'])
        return {'strikes':rows,'owned_contracts':sum(self.positions().values()),'owned_plus_working_acquisition':quantity,
            'entry_cash_spent_and_reserved':str(self._spent()+self._cash_reserved()),
            'planned_stop_risk_estimate':str(quantity*(Decimal('3.25')+money(self.portfolio['policy']['max_exit_slippage']))*100),
            'delta_notional_estimate':str(delta) if delta_known else None,'stop_risk_is_not_max_loss_guarantee':True,
            'displayed_liquidity_guaranteed':False,'executable':False}

    @synchronized
    def metrics(self):
        p=self.portfolio;rows=[]
        for cid,target in p['targets'].items():
            intents=[i for i in self.state['intents'].values() if i['contract']['con_id']==int(cid)]
            row={'contract_id':int(cid),'target_quantity':target['quantity'],'owned_quantity':self.positions().get(int(cid),0)}
            for side,label in (('BUY','entry'),('SELL','exit')):
                orders=[i for i in intents if i['side']==side]
                fills=[(key,e['record']) for key,e in self.state['executions'].items() if e['active'] and e['record']['contract']['con_id']==int(cid) and e['record']['side']==side and e['record']['quantity']]
                count=sum(e['quantity'] for _,e in fills)
                times=[p['execution_receipts'][key] for key,_ in fills]
                average=sum((money(e['price'])*e['quantity'] for _,e in fills),Decimal(0))/count if count else None
                slips=sum(((money(e['price'])-money(self._intent_for_ref(e['order_ref'])['arrival_price']))*(1 if side=='BUY' else -1)*e['quantity'] for _,e in fills),Decimal(0))/count if count else None
                goal=target['quantity'] if side=='BUY' else self._bought(int(cid))
                row[label]={'order_submission_timestamps':[i['submitted_at'] for i in orders if i.get('submitted_at')],
                    'broker_acknowledgment_timestamps':[i['ack_at'] for i in orders if i.get('ack_at')],
                    'first_fill_timestamp':min(times) if times else None,'final_fill_timestamp':max(times) if times and count==goal else None,
                    'average_fill_price':str(average) if average is not None else None,'filled_quantity':count,
                    'fill_percentage':str(Decimal(count)*100/goal) if goal else '0','slippage_vs_arrival_quote':str(slips) if slips is not None else None,
                    'rejections':sum(bool(i.get('rejected_at')) for i in orders),'cancellation_requests':sum(i['intent_id'] in p['cancel_requests'] for i in orders),
                    'cancellation_acknowledgments':sum(bool(i.get('cancelled_at')) for i in orders),
                    'cancellations_confirmed':sum(bool(i.get('cancelled_at')) and i['terminal_proven'] for i in orders),'repricing_attempts':sum(i['attempt']>0 for i in orders)}
            rows.append(row)
        def elapsed(start,end): return (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds() if start and end else None
        return {'mode':'OFFLINE_SIMULATED_RECEIPT_METRICS','strikes':rows,
            'portfolio_entry_seconds':elapsed(p['started_at'],p['entry_complete_at']),
            'portfolio_liquidation_seconds':elapsed(p['exit_started_at'],p['flat_at']),
            'executable':False}

    @synchronized
    def status(self):
        fresh=False
        if self.session:
            try: self._planning_gate(self.state['generation']);fresh=True
            except PaperBrokerError: pass
        p=self.portfolio
        external_emergency=self.emergency.blocked()
        flat=(not external_emergency or p['emergency']) and fresh and p['flat_at'] is not None and p['flat_revision']==self.state['revision'] and not self.positions() and all(i['terminal_proven'] for i in self.state['intents'].values())
        from executor_execution_coordinator import unresolved_protection_orders
        flat=flat and not unresolved_protection_orders(self)
        return {'mode':'OFFLINE MULTI-STRIKE PORTFOLIO / NO ORDERS','executable':False,'submission_enabled':False,
            'phase':'EMERGENCY_EXIT_PENDING' if external_emergency and not p['emergency'] else 'FLAT_CONFIRMED' if flat else 'RECOVERY_REQUIRED' if not fresh else 'EXITING' if p['exit_reason'] else 'ACQUISITION_HALTED' if p['halted'] else 'HOLDING' if self._entry_filled() else 'ACQUIRING',
            'positions':self.positions(),'entry_blocked':p['halted'] or external_emergency or not fresh,'emergency':p['emergency'] or external_emergency,
            'exit_reason':p['exit_reason'],'alerts':list(p['alerts'].values()),'faults':list(self.state['faults']),
            'premium_cash_spent':str(self._spent()),'premium_cash_reserved':str(self._cash_reserved()),
            'favorable_spx_peak':p['peak'],'trailing_active':p['ride'],
            'last_pretrade_validation':p['last_pretrade'],
            'protection':'OFFLINE_CONDITIONAL_MODEL_NOT_BROKER_HELD','displayed_liquidity_guaranteed':False}
