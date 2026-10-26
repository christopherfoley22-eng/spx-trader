"""Coordinated OFFLINE reduction plans over the existing durable portfolio ledger.

The optimizer proposes. Independent closing checks and the existing dispatch/
reconciliation boundary authorize simulated bindings. No broker API exists here.
"""
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import time

from executor_adaptive_liquidity import AdaptiveExitPolicy,DepthObservation,depth_levels,choose_slices
from executor_paper_broker import money,integer,utc,text_id
from executor_paper_types import PaperBrokerError
from simulation_evidence import validate_observation_time


def reduce_adaptive(ledger,data):
    p=ledger.portfolio;op=data['op']
    if op=='CONFIG':
        if p.get('adaptive_exit'):raise PaperBrokerError('EXIT_EXECUTION_POLICY_ALREADY_PINNED')
        p['adaptive_exit']=dict(policy=data['policy'],depth={},depth_ids={},external=None,external_invalid=False,
            requests=[],cancellations={},arrivals={},metrics=[],last_condition='WAIT_AUTHORIZED_EXIT')
        return
    s=p['adaptive_exit']
    if op=='ENABLE_READINESS':
        s['readiness_enabled']=True
        return
    if op=='DEPTH':
        value=data['value'];cid=str(value['contract']['con_id']);prior=s['depth'].get(cid)
        old=s['depth_ids'].get(value['evidence_id'])
        if old is not None and old!=value:raise PaperBrokerError('CHANGED_DEPTH_EVENT')
        if prior and (value['sequence']<=prior['sequence'] or value['source_time']<prior['source_time'] or value['received_monotonic']<prior['received_monotonic']) and old is None:
            raise PaperBrokerError('BACKWARD_DEPTH_SEQUENCE')
        s['depth'][cid]=value;s['depth_ids'][value['evidence_id']]=value
    elif op=='EXTERNAL':s.update(external=data['value'],external_invalid=False)
    elif op=='EXTERNAL_INVALID':s['external_invalid']=True
    elif op=='ARRIVALS':
        for cid,value in data['values'].items():s['arrivals'].setdefault(cid,value)
    elif op=='CANCEL_PERMIT':
        s['cancellations'].setdefault(data['intent_id'],[]).append(data)
        s['requests'].append(data['at'])
    elif op=='DISPATCH':s['requests'].extend([data['at']]*data['count'])
    elif op=='METRIC':s['metrics'].append(data['value'])
    elif op=='CONDITION':s['last_condition']=data['value']
    else:raise PaperBrokerError('UNKNOWN_ADAPTIVE_EXIT_EVENT')


def _event(engine,op,**data):
    return engine._append('exit-opt:'+str(engine.sequence+1),'PORT_EXIT_OPT',dict(op=op,**data))


def external_reserved(engine,cid):
    state=engine.portfolio.get('adaptive_exit')
    if not state or not state.get('external'):return 0
    return sum(x['remaining'] for x in state['external']['orders'] if x['contract']['con_id']==cid and not x['terminal_reconciled'])


def priority(engine):
    p=engine.portfolio
    if p['emergency']:return 'EMERGENCY'
    return 'PROTECTIVE' if p['exit_reason'] in {'INITIAL_STOP','INVALID_PREMIUM_STOP'} else 'NORMAL'


def capture_exit_arrivals(engine):
    """Capture only currently valid quotes at an existing strategy decision."""
    values={}
    if not engine.portfolio.get('adaptive_exit'):return values
    for cid in engine.positions():
        try:
            quote,a=engine._assess(cid,'SELL')
            if money(a['bid'])>=money(a['ask']):continue
            tick=money(a['tick']);raw=money(a['bid'])-money(engine.portfolio['policy']['max_exit_slippage'])
            from decimal import ROUND_CEILING
            floor=(raw/tick).to_integral_value(rounding=ROUND_CEILING)*tick
            if floor<=0:continue
            values[str(cid)]=dict(price=a['bid'],floor=str(floor),quote_id=quote.quote_id,
                source_time=quote.option.source_time.isoformat(),reference='EXIT_TRIGGER_QUOTE')
        except Exception:continue
    return values


def pacing_room(engine):
    s=engine.portfolio['adaptive_exit'];policy=s['policy'];now=utc(engine.wall_clock())
    # A backward wall-clock jump must not reset a durable pacing budget.
    used=sum((now-datetime.fromisoformat(stamp)).total_seconds()<float(policy['window_seconds']) for stamp in s['requests'])
    bound=policy['requests_per_window']-(0 if priority(engine)=='EMERGENCY' else policy['emergency_reserve'])
    return max(0,bound-used)


class ClosingOrderSafety:
    """Independent quantities, identities, timestamps and current broker proof."""
    @staticmethod
    def gate(engine):
        engine._planning_gate(engine.state['generation'])
        p=engine.portfolio;s=p.get('adaptive_exit')
        if not s or not p['exit_reason'] or not p['halted']:raise PaperBrokerError('AUTHORIZED_REDUCTION_EXIT_REQUIRED')
        if p.get('protection_lifecycle',{}).get('uncertain'):raise PaperBrokerError('UNRESOLVED_PROTECTION_SCOPE')
        if any(i['uncertain'] for i in engine.state['intents'].values()):raise PaperBrokerError('UNCERTAIN_SUBMISSION_NO_RETRY')
        if s['external_invalid']:raise PaperBrokerError('EXTERNAL_CLOSING_SCOPE_UNRESOLVED')
        if s['external'] is not None:
            proof=s['external']
            validate_observation_time(datetime.fromisoformat(proof['observed_at']),proof['received_monotonic'],engine.wall_clock(),engine.monotonic_clock())
            if proof['generation']!=engine.state['generation'] or proof['positions']!={str(cid):qty for cid,qty in engine.positions().items()}:
                raise PaperBrokerError('EXTERNAL_CLOSING_POSITION_PROOF_CHANGED')
            if any(engine.reservations('SELL',cid)+external_reserved(engine,cid)>owned for cid,owned in engine.positions().items()):
                raise PaperBrokerError('PROTECTIVE_AND_ADAPTIVE_CLOSING_CONFLICT')

    @staticmethod
    def available(engine,cid):
        active=[i for i in engine.state['intents'].values() if i['contract']['con_id']==cid and not i['terminal_proven']]
        if any(i['side']=='BUY' for i in active):return 0
        return max(0,engine.positions().get(cid,0)-engine.reservations('SELL',cid)-external_reserved(engine,cid))

    @staticmethod
    def batch(engine,intents):
        ClosingOrderSafety.gate(engine);p=engine.portfolio;s=p['adaptive_exit']
        if not intents or len(set(i['intent_id'] for i in intents))!=len(intents):raise PaperBrokerError('UNIQUE_REDUCTION_BATCH_REQUIRED')
        if len(intents)>pacing_room(engine):raise PaperBrokerError('EXIT_PACING_BACKPRESSURE')
        per={}
        for i in intents:
            cid=i['contract']['con_id'];target=p['targets'].get(str(cid))
            if (i['side']!='SELL' or not target or i['contract']!=target['contract'] or i.get('adaptive_version')!=s['policy']['version']
                    or not integer(i['quantity'],1,25) or i['order_ref']!=i['intent_id']):raise PaperBrokerError('REDUCTION_IDENTITY_OR_DIRECTION_MISMATCH')
            if i.get('order_id') is not None or i.get('terminal_proven'):raise PaperBrokerError('REDUCTION_ALREADY_BOUND_OR_TERMINAL')
            quote,_=engine._assess(cid,'SELL')
            levels=[(money(quote.option.bid),quote.bid_size)]
            if money(quote.option.bid)>=money(quote.option.ask):raise PaperBrokerError('LOCKED_OR_CROSSED_EXIT_QUOTE')
            if i['quantity']>p['policy']['max_slice'] or money(i['limit_price'])%money(quote.min_tick,True) or i['attempt']>p['policy']['max_reprices']:
                raise PaperBrokerError('INDEPENDENT_SLICE_PRICE_OR_REPRICE_LIMIT')
            if quote.quote_id!=i['quote_id']:raise PaperBrokerError('EXIT_QUOTE_CHANGED_REPLAN_REQUIRED')
            if i.get('depth_evidence_id'):
                book=s['depth'].get(str(cid))
                if book is None or book['evidence_id']!=i['depth_evidence_id']:raise PaperBrokerError('DEPTH_EVIDENCE_CHANGED')
                levels=depth_levels(book,quote,engine.wall_clock(),engine.monotonic_clock())
            if (money(i['limit_price'],True)>money(quote.option.bid) or money(i['limit_price'],True)<money(i['price_bound'],True)
                    or money(i['price_bound'])!=money(s['arrivals'][str(cid)]['floor'])):
                raise PaperBrokerError('EXIT_PRICE_PROTECTION_VIOLATION')
            if any(x['side']=='BUY' and x['contract']['con_id']==cid and not x['terminal_proven'] for x in engine.state['intents'].values()):
                raise PaperBrokerError('ENTRY_CANCELLATION_NOT_RECONCILED')
            per[cid]=per.get(cid,0)+i['quantity']
            capacity=min(quote.estimated_exit_capacity,sum(int(Decimal(size)*money(p['policy']['displayed_participation']))
                for price,size in levels if price>=money(i['price_bound'])))
            batch_ids={x['intent_id'] for x in intents}
            other=sum(x['quantity'] if x['quote_id']==quote.quote_id else max(0,x['quantity']-engine.filled(x))
                for x in engine.state['intents'].values() if x['side']=='SELL' and x['contract']['con_id']==cid
                and x['intent_id'] not in batch_ids and (x['quote_id']==quote.quote_id or not x['terminal_proven']))
            if per[cid]+other>capacity:raise PaperBrokerError('INDEPENDENT_LIQUIDITY_CAPACITY_EXCEEDED')
        # Plans already reserve their full quantities. Count ALL residual closing
        # intents, including cancel/replace-pending and external protective orders.
        for cid in per:
            if engine.reservations('SELL',cid)+external_reserved(engine,cid)>engine.positions().get(cid,0):
                raise PaperBrokerError('CLOSING_RESERVATIONS_EXCEED_OWNERSHIP')


class GlobalLiquidationCoordinator:
    def __init__(self,engine):self.engine=engine

    def _condition(self,value):
        if self.engine.portfolio['adaptive_exit']['last_condition']!=value:_event(self.engine,'CONDITION',value=value)

    def configure(self,policy):
        if not isinstance(policy,AdaptiveExitPolicy):raise PaperBrokerError('EXPLICIT_OFFLINE_EXIT_POLICY_REQUIRED')
        engine=self.engine
        with engine.lock:
            engine._planning_gate(engine.state['generation'])
            if not engine.portfolio['targets'] or any(i['side']=='SELL' for i in engine.state['intents'].values()):
                raise PaperBrokerError('CONFIGURE_BEFORE_CLOSING_INTENTS')
            return _event(engine,'CONFIG',policy=policy.normalized())

    def observe_depth(self,observation):
        engine=self.engine
        with engine.lock:
            engine._session(engine.state['generation'])
            if not isinstance(observation,DepthObservation):raise PaperBrokerError('EXPLICIT_DEPTH_OBSERVATION_REQUIRED')
            value=observation.payload();target=engine.portfolio['targets'].get(str(observation.contract.con_id))
            if not target or target['contract']!=value['contract']:raise PaperBrokerError('WRONG_DEPTH_CONTRACT')
            validate_observation_time(observation.source_time,observation.received_monotonic,engine.wall_clock(),engine.monotonic_clock())
            state=engine.portfolio['adaptive_exit']
            old=state['depth_ids'].get(observation.evidence_id)
            if old is not None:
                if old!=value:engine._fault('CHANGED_DEPTH_EVENT')
                return 'DUPLICATE'
            return _event(engine,'DEPTH',value=value)

    def observe_external_closings(self,evidence):
        """Trusted backend fixture proof, never browser/optimizer-created scope."""
        engine=self.engine
        with engine.lock:
            try:
                engine._planning_gate(engine.state['generation'])
                if (evidence.get('origin')!='OFFLINE_FIXTURE' or evidence.get('complete') is not True
                        or evidence.get('account')!=engine.approval.account or evidence.get('generation')!=engine.state['generation']
                        or evidence.get('positions')!={str(c):q for c,q in engine.positions().items()}):
                    raise PaperBrokerError('COMPLETE_EXTERNAL_CLOSING_PROOF_REQUIRED')
                validate_observation_time(datetime.fromisoformat(evidence['observed_at']),evidence['received_monotonic'],engine.wall_clock(),engine.monotonic_clock())
                seen=set()
                for item in evidence['orders']:
                    target=engine.portfolio['targets'].get(str(item['contract']['con_id']))
                    if (not target or item['contract']!=target['contract'] or item['side']!='SELL'
                            or not integer(item['remaining'],0,25) or type(item['terminal_reconciled']) is not bool
                            or not text_id(item['order_ref']) or item['order_ref'] in seen
                            or any(i['order_ref']==item['order_ref'] for i in engine.state['intents'].values())):
                        raise PaperBrokerError('EXTERNAL_PROTECTIVE_IDENTITY_INVALID')
                    seen.add(item['order_ref'])
            except Exception:
                _event(engine,'EXTERNAL_INVALID');raise
            return _event(engine,'EXTERNAL',value=evidence)

    def _cancellation(self,item,actions):
        engine=self.engine;s=engine.portfolio['adaptive_exit'];p=s['policy'];prior=s['cancellations'].get(item['intent_id'],[])
        if item['order_id'] is None:
            actions.append(engine._cancel(item));return
        if pacing_room(engine)<=0:return
        if prior and (prior[-1]['cycle']==engine.state['last_cycle'] or len(prior)>=p['cancel_attempts']):return
        if prior and (utc(engine.wall_clock())-datetime.fromisoformat(prior[-1]['at'])).total_seconds()<float(p['window_seconds']):return
        action=engine._cancel(item)
        _event(engine,'CANCEL_PERMIT',intent_id=item['intent_id'],cycle=engine.state['last_cycle'],at=utc(engine.wall_clock()).isoformat())
        actions.append(dict(action,action_id=action.get('action_id','cancel')+':'+str(len(prior))))

    def plan(self):
        engine=self.engine;start=time.perf_counter_ns()
        with engine.lock:
            engine._sync_emergency();p=engine.portfolio;s=p['adaptive_exit'];policy=s['policy'];now=utc(engine.wall_clock())
            if not p['exit_reason']:return [{'type':'MONITOR_PROTECTION','executable':False}]
            actions=[]
            # Cancellation is itself a bounded, durable request, not proof of success.
            for item in list(engine.state['intents'].values()):
                if item['terminal_proven']:continue
                if item.get('native_managed'):
                    self._cancellation(item,actions)
                    continue
                submitted=item.get('submitted_at');age=(now-datetime.fromisoformat(submitted)).total_seconds() if submitted else 0
                interval=float(policy[{'NORMAL':'normal','PROTECTIVE':'protective','EMERGENCY':'emergency'}[priority(engine)]+'_reprice_seconds'])
                fills=[p['execution_receipts'][key] for key,e in engine.state['executions'].items() if e['active'] and e['record']['order_ref']==item['order_ref'] and key in p['execution_receipts']]
                progress=max(fills+[item.get('ack_at') or submitted]) if submitted else None
                quiet=(now-datetime.fromisoformat(progress)).total_seconds() if progress else 0
                reprice=(item['side']=='SELL' and item.get('ack_at') and policy['method']=='ADAPTIVE_REPRICE' and quiet>=interval)
                stale_prepared=item['order_id'] is None and item['quote_id']!=p['quotes'].get(str(item['contract']['con_id']),{}).get('quote_id')
                if item['side']=='BUY' or item['status']=='Inactive' or reprice or stale_prepared or age>float(p['policy']['working_timeout']):
                    self._cancellation(item,actions)
            if p['exit_started_at'] and (now-datetime.fromisoformat(p['exit_started_at'])).total_seconds()>float(p['policy']['liquidation_deadline']):
                actions.append(engine._alert('adaptive-deadline','UNFILLED_EXIT_DEADLINE_OPERATOR_REVIEW_REQUIRED'))
            try:ClosingOrderSafety.gate(engine)
            except Exception:
                self._condition('CRITICAL_RECONCILIATION_REQUIRED')
                return actions+[{'type':'RECONCILIATION_REQUIRED','executable':False}]
            if not engine.positions():
                self._condition('CONFIRMED_FLAT' if engine.status()['phase']=='FLAT_CONFIRMED' else 'AWAIT_FULL_RECONCILIATION')
                return actions+[{'type':'AWAIT_BROKER_CONFIRMED_FLAT','executable':False}]
            # Prepared orders reserve ownership too. Return them once, up to the
            # current pacing budget; dispatch rechecks every condition separately.
            prepared=[i for i in engine.state['intents'].values() if i['side']=='SELL' and i['order_id'] is None and not i['terminal_proven'] and not i.get('native_managed')]
            room=pacing_room(engine)
            for item in prepared[:room]:
                actions.append(dict(type='SIMULATED_LIMIT_INTENT',intent_id=item['intent_id'],contract_id=item['contract']['con_id'],
                    side='SELL',quantity=item['quantity'],limit_price=item['limit_price'],executable=False))
            room-=min(room,len(prepared));plans=[]
            slots=max(0,policy['max_queue']-sum(not i['terminal_proven'] for i in engine.state['intents'].values()))
            for cid,target in p['targets'].items():
                available=ClosingOrderSafety.available(engine,int(cid))
                if not available:continue
                try:
                    quote,a=engine._assess(int(cid),'SELL')
                    if money(a['bid'])>=money(a['ask']):raise PaperBrokerError('LOCKED_OR_CROSSED_EXIT_QUOTE')
                    levels=[(money(a['bid']),a['displayed_bid_size'])];depth=s['depth'].get(cid)
                    if depth is not None:
                        try:levels=depth_levels(depth,quote,now,engine.monotonic_clock())
                        except Exception:pass  # Explicit fresh-BBO fallback; no invented depth.
                    active=[i for i in engine.state['intents'].values() if i['contract']['con_id']==int(cid) and not i['terminal_proven']]
                    if any(i['intent_id'] in p['cancel_requests'] for i in active):continue
                    prior=[i for i in engine.state['intents'].values() if i['side']=='SELL' and i['contract']['con_id']==int(cid) and i['terminal_proven'] and not i.get('native_managed') and engine._route_remaining(i)>0]
                    # Latest terminal attempt per route; never evade a depleted budget.
                    roots={i['route_id']:i for i in sorted(prior,key=lambda i:i['attempt'])}
                    previous=next((i for i in roots.values() if not any(x['route_id']==i['route_id'] for x in active)),None)
                    attempt=previous['attempt']+1 if previous else 0
                    if attempt>p['policy']['max_reprices']:
                        actions.append(engine._alert('adaptive-retry:'+cid,'PRICE_PROTECTED_RETRY_BUDGET_EXHAUSTED'));continue
                    arrival=s['arrivals'].get(cid,{}).get('price',a['bid'])
                    readiness=getattr(engine,'exit_readiness',None)
                    result=readiness.select(int(cid),available,quote,str(arrival),
                        depth['evidence_id'] if depth is not None and len(levels)>1 else None,attempt,previous) if readiness else None
                    if result is None:
                        result=choose_slices(min(available,engine._route_remaining(previous) if previous else available),levels,arrival,
                            dict(p['policy'],tick=a['tick'],estimated_capacity=quote.estimated_exit_capacity),policy,attempt)
                    if cid not in s['arrivals']:
                        _event(engine,'ARRIVALS',values={cid:dict(price=str(arrival),floor=result['floor'],quote_id=quote.quote_id,source_time=quote.option.source_time.isoformat(),reference='FIRST_FRESH_QUOTE_TRIGGER_QUOTE_UNAVAILABLE')})
                    used=sum(i['quantity'] for i in engine.state['intents'].values() if i['side']=='SELL' and i['contract']['con_id']==int(cid) and i['quote_id']==quote.quote_id)
                    other=sum(i['quantity']-engine.filled(i) for i in active if i['side']=='SELL' and i['quote_id']!=quote.quote_id)
                    visible_capacity=min(quote.estimated_exit_capacity,sum(int(Decimal(size)*money(p['policy']['displayed_participation']))
                        for price,size in levels if price>=money(result['floor'])))
                    capacity=max(0,min(available,visible_capacity-used-other))
                    max_slots=p['policy']['max_working_per_strike']-sum(i['side']=='SELL' for i in active)
                    for choice in result['slices'][:min(room,slots,max_slots)]:
                        quantity=min(choice['quantity'],capacity)
                        if not quantity:break
                        key='ae:'+hashlib.sha256((str(engine.path)+':'+str(engine.sequence+1)+':'+str(len(plans))).encode()).hexdigest()[:48]
                        plans.append(dict(intent_id=key,order_ref=key,contract=target['contract'],side='SELL',quantity=quantity,
                            created_at=now.isoformat(),limit_price=choice['limit_price'],price_bound=result['floor'],arrival_price=str(arrival),
                            quote_id=quote.quote_id,route_id=previous['route_id'] if previous else key,
                            route_quantity=previous['route_quantity'] if previous else quantity,attempt=attempt,cash_per_contract='0',
                            adaptive_version=policy['version'],estimated_slippage_points=choice['estimated_slippage_points'],depth_evidence_id=depth['evidence_id'] if depth is not None and len(levels)>1 else None))
                        capacity-=quantity;room-=1;slots-=1
                except Exception:
                    actions.append(engine._alert('adaptive-data:'+cid,'EXIT_DATA_UNAVAILABLE_EXPOSURE_RETAINED'))
            if plans:
                engine._append('adaptive-plan:'+str(engine.sequence+1),'PORT_PLAN',{'intents':plans})
                _event(engine,'METRIC',value=dict(host_plan_ms=(time.perf_counter_ns()-start)/1e6,at=now.isoformat(),plans=len(plans)))
                actions.extend(dict(type='SIMULATED_LIMIT_INTENT',intent_id=i['intent_id'],contract_id=i['contract']['con_id'],
                    side='SELL',quantity=i['quantity'],limit_price=i['limit_price'],executable=False) for i in plans)
            if any(a['type']=='RISK_ALERT' for a in actions):self._condition('CRITICAL_EXIT_UNRESOLVED')
            elif any(external_reserved(engine,cid) for cid in engine.positions()):self._condition('PROTECTED_EXIT_COORDINATION_REQUIRED')
            elif plans or any(i['side']=='SELL' and not i['terminal_proven'] for i in engine.state['intents'].values()):self._condition('EXIT_WORKING')
            else:self._condition('EXIT_PACING_OR_LIQUIDITY_WAIT')
            return actions or [{'type':'EXIT_UNRESOLVED_WAIT_EVIDENCE_OR_PACING','executable':False}]

    def status(self):
        engine=self.engine
        with engine.lock:
            phases=[]
            for i in engine.state['intents'].values():
                if i['side']!='SELL':continue
                filled=engine.filled(i)
                phase='RECONCILIATION_REQUIRED' if i['uncertain'] else 'FILLED' if i['terminal_proven'] and filled==i['quantity'] else 'REJECTED' if i['status']=='Inactive' else 'REPLACEMENT_PENDING' if i['terminal_proven'] else 'CANCEL_PENDING' if i['status']=='PendingCancel' else 'CANCEL_REQUESTED' if i['intent_id'] in engine.portfolio['cancel_requests'] else 'PARTIALLY_FILLED' if filled else 'BROKER_ACKNOWLEDGED' if i.get('ack_at') else 'SUBMISSION_PENDING' if i['order_id'] is not None else 'ORDER_PLANNED'
                phases.append(dict(intent_id=i['intent_id'],phase=phase,remaining=max(0,i['quantity']-filled)))
            return dict(phase=engine.status()['phase'],orders=phases,uncommitted={str(cid):ClosingOrderSafety.available(engine,cid) for cid in engine.positions()},
                mode=priority(engine),submission_enabled=False)
