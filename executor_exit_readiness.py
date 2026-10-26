"""Disposable incremental execution hints over authoritative durable state.

No order IDs, entry permissions, broker API or durable-state fallback. A hint
never authorizes dispatch. The original independent closing boundary still runs.
"""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from executor_adaptive_liquidity import choose_slices,depth_levels
from executor_paper_broker import money
from executor_paper_types import PaperBrokerError


def rollback_snapshot(engine,kind,data):
    """Rollback write sets for explicitly enumerated reducers; no durability change.

    These paths assign scalar fields, append new records, or replace bindings.
    Execution/correction/reconciliation reducers intentionally keep full copies.
    """
    if kind not in {'PORT_SPX','PORT_EXIT','PORT_PLAN','PORT_DISPATCH','PORT_EXIT_OPT'}:return None
    state=engine.state;old_p=state['portfolio'];s=old_p.get('adaptive_exit')
    if kind=='PORT_EXIT_OPT' and data['op'] not in {'METRIC','CONDITION','ARRIVALS','DISPATCH','CANCEL_PERMIT','DEPTH','EXTERNAL','EXTERNAL_INVALID','ENABLE_READINESS'}:
        return None
    saved=state.copy();p=old_p.copy();saved['portfolio']=p
    if s is not None:p['adaptive_exit']=s.copy()
    if kind in {'PORT_SPX','PORT_EXIT'} and s is not None:
        p['adaptive_exit']['arrivals']=s['arrivals'].copy()
    if kind=='PORT_PLAN':
        saved['intents']=state['intents'].copy()
        p['arrival_quotes']={side:values.copy() for side,values in old_p['arrival_quotes'].items()}
    if kind=='PORT_DISPATCH':
        saved['intents']=state['intents'].copy()
        for binding in data['bindings']:
            key=binding['intent_id'];saved['intents'][key]=state['intents'][key].copy()
        if s is not None:p['adaptive_exit']['requests']=s['requests'].copy()
    if kind=='PORT_EXIT_OPT':
        op=data['op'];a=p['adaptive_exit']
        if op=='METRIC':a['metrics']=s['metrics'][:]
        if op=='ARRIVALS':a['arrivals']=s['arrivals'].copy()
        if op in {'DISPATCH','CANCEL_PERMIT'}:a['requests']=s['requests'][:]
        if op=='CANCEL_PERMIT':a['cancellations']={key:values[:] for key,values in s['cancellations'].items()}
        if op=='DEPTH':a['depth']=s['depth'].copy();a['depth_ids']=s['depth_ids'].copy()
    return saved


@dataclass(frozen=True)
class PreparedExitTemplate:
    account:str
    generation:int
    contract_id:int
    contract_identity:tuple
    side:str
    order_type:str
    confirmed_quantity:int
    uncommitted_quantity:int
    quote_id:str
    source_timestamp:str
    receipt_monotonic:float
    risk_version:int
    depth_id:object
    policy_version:str
    slices:tuple
    floor:str
    arrival:str
    priority:str = 'REQUIRES_AUTHORIZED_EXIT'


class ContinuousExitReadiness:
    def __init__(self,engine):
        self.engine=engine;self.templates={};self.versions={};self.refreshes={};self.hits=0;self.misses=0;self.failure=None
        self.premium_stops={};self.strategy={}

    def invalidate(self,cid=None):
        ids=[cid] if cid is not None else list(self.engine.portfolio['targets'])
        for key in ids:
            key=int(key);self.templates.pop(key,None);self.versions[key]=self.versions.get(key,0)+1

    def refresh(self,cid):
        from executor_liquidation import ClosingOrderSafety
        e=self.engine;p=e.portfolio;s=p.get('adaptive_exit');cid=int(cid)
        self.invalidate(cid);self.refreshes[cid]=self.refreshes.get(cid,0)+1
        if e.positions().get(cid,0):
            average,stop=e._premium_stop(cid);self.premium_stops[cid]=(str(average),str(stop))
        else:self.premium_stops.pop(cid,None)
        if not s:return
        try:
            e._session(e.state['generation'])
            available=ClosingOrderSafety.available(e,cid)
            # Retries/cancellation races use the complete original scheduler.
            if not available or any(i['side']=='SELL' and i['contract']['con_id']==cid for i in e.state['intents'].values()):return
            q,a=e._assess(cid,'SELL')
            if money(a['bid'])>=money(a['ask']):return
            levels=[(money(a['bid']),a['displayed_bid_size'])];book=s['depth'].get(str(cid));depth_id=None
            if book is not None:
                try:
                    levels=depth_levels(book,q,e.wall_clock(),e.monotonic_clock())
                    if len(levels)>1:depth_id=book['evidence_id']
                except Exception:pass  # Same explicit fresh-BBO fallback as Phase 8.
            arrival=s['arrivals'].get(str(cid),{}).get('price',a['bid'])
            result=choose_slices(available,levels,arrival,dict(p['policy'],tick=a['tick'],estimated_capacity=q.estimated_exit_capacity),s['policy'])
            self.templates[cid]=PreparedExitTemplate(e.approval.account,e.state['generation'],cid,
                tuple(sorted(p['targets'][str(cid)]['contract'].items())),'SELL','LMT',e.positions()[cid],available,q.quote_id,
                q.option.source_time.isoformat(),q.option.received_monotonic,self.versions[cid],depth_id,s['policy']['version'],
                tuple(tuple(sorted(x.items())) for x in result['slices']),result['floor'],str(arrival))
        except Exception:
            self.templates.pop(cid,None)  # No unsafe in-memory persistence fallback.

    def refresh_all(self):
        for cid in self.engine.portfolio['targets']:self.refresh(cid)
        self._strategy()

    def _strategy(self):
        p=self.engine.portfolio
        self.strategy=dict(entry_spx=p['entry_spx'],peak=p['peak'],trailing=p['ride'],
            exit_reason=p['exit_reason'],emergency=p['emergency'])

    def after_commit(self,kind,data):
        if kind in {'PORT_SPX','PORT_EXIT'}:self._strategy()
        if kind=='PORT_QUOTE':self.refresh(data['quote']['option']['contract']['con_id'])
        elif kind=='PORT_EXIT_OPT' and data['op']=='DEPTH':self.refresh(data['value']['contract']['con_id'])
        elif kind in {'PORT_EXECUTION','PORT_ORDER'}:self.refresh(data['record']['contract']['con_id'])
        elif kind in {'SESSION','RECONCILED','PORT_CONFIG','PORT_PLAN','PORT_DISPATCH','PORT_CANCEL','LOCAL_ABANDON','PORT_HALT','FAULT','RESTART','DISCONNECT'} or kind=='PORT_EXIT_OPT' and data['op'] in {'CONFIG','EXTERNAL','EXTERNAL_INVALID'}:
            if kind in {'DISCONNECT','RESTART','FAULT'}:self.invalidate()
            else:self.refresh_all()
        # SPX/exit events change strategy/urgency, not the cached sizing inputs.
        # The controller owns their durable peaks/thresholds and final authority.

    def select(self,cid,available,quote,arrival,depth_id,attempt,previous):
        e=self.engine;t=self.templates.get(int(cid));self.failure=None
        if t is not None and not previous and attempt==0:
            try:
                # Revalidate time and current quantities, never a cached grant.
                current,_=e._assess(int(cid),'SELL')
                from executor_liquidation import ClosingOrderSafety
                if (t.account==e.approval.account and t.generation==e.state['generation'] and
                    t.contract_identity==tuple(sorted(e.portfolio['targets'][str(cid)]['contract'].items())) and
                    t.risk_version==self.versions[int(cid)] and t.quote_id==quote.quote_id==current.quote_id and
                    t.confirmed_quantity==e.positions().get(int(cid),0) and
                    t.uncommitted_quantity==available==ClosingOrderSafety.available(e,int(cid)) and
                    t.arrival==str(arrival) and t.depth_id==depth_id and
                    t.policy_version==e.portfolio['adaptive_exit']['policy']['version']):
                    self.hits+=1
                    return dict(slices=[dict(x) for x in t.slices],floor=t.floor,cache_hit=True)
            except Exception:pass
        self.misses+=1
        return None


def enable_readiness(engine):
    with engine.lock:
        engine._planning_gate(engine.state['generation'])
        if not engine.portfolio.get('adaptive_exit'):raise PaperBrokerError('ADAPTIVE_POLICY_REQUIRED')
        if not engine.portfolio['adaptive_exit'].get('readiness_enabled'):
            from executor_liquidation import _event
            _event(engine,'ENABLE_READINESS')
        if getattr(engine,'exit_readiness',None) is None:engine.exit_readiness=ContinuousExitReadiness(engine)
        engine.exit_readiness.refresh_all()
        return engine.exit_readiness
