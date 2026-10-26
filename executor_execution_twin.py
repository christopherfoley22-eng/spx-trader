"""Deterministic synthetic acquisition/liquidation comparisons, never real fills.

Reuses durable portfolio/simulator boundaries. All strategies see identical
source books; varying policies never changes funded ATM-first target quantities.
"""
import argparse
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import platform
import tempfile
import time
from executor_liquidity_benchmark import ExitSimulation,fixture_execution_policy
from executor_liquidity_simulator import SimulationPolicy,distribution
from executor_execution_coordinator import UnifiedExecutionCoordinator
from executor_exit_readiness import enable_readiness
from executor_portfolio_liquidity import fixture_from_payload
from executor_market_state import MarketDatum,MarketState
from executor_paper_broker import money

TACTICS=('SINGLE','CONSERVATIVE_BOUND','FIXED_CHILD_5','LIQUIDITY_CHILD','ADAPTIVE_QUANTITY','PHASE8_ADAPTIVE','URGENCY_REPRICE_RESEARCH')
SCENARIOS=('deep','thin','spread_expansion','disappearing','price_acceleration','improvement_offered')
ADVERSE_SCENARIOS=('halt','session_end')


class ExecutionTwin:
    def __init__(self,root,quantity,tactic='PHASE8_ADAPTIVE',scenario='deep',seed=0,realization='.65',right='C',broker_policy=None):
        if tactic not in TACTICS or scenario not in SCENARIOS+ADVERSE_SCENARIOS:raise ValueError('Explicit research tactic/scenario required')
        self.tactic=tactic;self.scenario=scenario;self.seed=seed;self.quantity=quantity
        policy=replace(fixture_execution_policy(),max_slice=5 if tactic=='FIXED_CHILD_5' else 25,
            max_working_per_strike=1 if tactic in {'SINGLE','CONSERVATIVE_BOUND'} else 3,
            entry_deadline='.40' if tactic=='CONSERVATIVE_BOUND' else '.80')
        method='SINGLE' if tactic in {'SINGLE','CONSERVATIVE_BOUND'} else 'FIXED' if tactic=='FIXED_CHILD_5' else 'ADAPTIVE_REPRICE' if tactic=='URGENCY_REPRICE_RESEARCH' else 'ADAPTIVE'
        listed=(6000.,6005.,6012.5,6025.,6050.) if right=='C' else (6000.,5997.5,5990.,5980.,5970.)
        self.sim=ExitSimulation(root,quantity,method=method,scenario='moderate',seed=seed,right=right,seed_entries=False,
            execution_policy=policy,simulation_policy=broker_policy or SimulationPolicy(seed,realization=realization,commission_per_contract='.65'),listed_strikes=listed)
        self.coordinator=UnifiedExecutionCoordinator(self.sim.engine);self.coordinator.configure_protection('offline-gap-fence-v1')
        enable_readiness(self.sim.engine)
        self.market=MarketState();self.serial=0;self.residual=[];self.gap_contract_seconds=Decimal(0);self.entry_latency=[];self.exit_latency=[]
        self.first_fill=None;self.target_fill=None;self.flat=None;self.arrivals={};self.entry_start=self.sim.clock.seconds
        # These opposite-direction identities are synthetic candidate observations,
        # not held contracts or automatic directional decisions.
        other=(6000.,5997.5,5990.,5980.,5970.) if right=='C' else (6000.,6005.,6012.5,6025.,6050.)
        self.candidates=self.sim.all_contracts+tuple(replace(c,con_id=c.con_id+10000,right='P' if right=='C' else 'C',strike=other[i]) for i,c in enumerate(self.sim.all_contracts))

    def market_update(self,index):
        s=self.sim;e=s.engine
        size=200 if self.scenario=='deep' else 6 if self.scenario in {'thin','disappearing'} else 20
        shift=Decimal('.05')*min(index,6) if self.scenario=='price_acceleration' else Decimal(0)
        spread=Decimal('.30') if self.scenario=='spread_expansion' and index>0 else Decimal('.10')
        closed=self.scenario in ADVERSE_SCENARIOS and index>=2
        for c in s.contracts:
            self.serial+=1;bid=Decimal('9.90')-Decimal(c.con_id-1001)*Decimal('.5')+shift;ask=bid+spread
            q=fixture_from_payload(e.portfolio['quotes'][str(c.con_id)])
            q=replace(q,quote_id='twin:'+str(self.serial),option=replace(q.option,bid=float(bid),ask=float(ask),source_time=s.clock.wall(),received_monotonic=s.clock.mono()),
                spx=replace(q.spx,source_time=s.clock.wall(),received_monotonic=s.clock.mono()),bid_size=size,ask_size=size,
                sizes_source_time=s.clock.wall(),sizes_received_monotonic=s.clock.mono(),greeks_source_time=s.clock.wall(),greeks_received_monotonic=s.clock.mono())
            if closed:
                previous=fixture_from_payload(e.portfolio['quotes'][str(c.con_id)])
                q=replace(q,trading_status='HALTED' if self.scenario=='halt' else 'CLOSED',option=replace(q.option,source_time=previous.option.source_time))
            e.observe_liquidity(e.state['generation'],'twin-quote:'+str(self.serial),q)
            self.arrivals[q.quote_id]=dict(bid=bid,ask=ask)
            withdraw=closed or self.scenario=='disappearing' and index>=1
            s.source.set_book(c,((bid,size),(bid-Decimal('.05'),size),(bid-Decimal('.10'),size)),'twin-bid:'+str(self.serial),withdraw=withdraw)
            actual_ask=ask-Decimal('.05') if self.scenario=='improvement_offered' else ask
            s.source.set_asks(c,((actual_ask,size),(ask+Decimal('.05'),size)),'twin-ask:'+str(self.serial),withdraw=withdraw)
        for rank,c in enumerate(self.candidates):
            self.serial+=1
            candidate_bid=Decimal('9.90')-Decimal(rank%5)*Decimal('.5')+shift
            self.market.ingest(MarketDatum('SYNTHETIC_BOOK',str(c.con_id),e.state['generation'],self.serial,
                dict(right=c.right,strike=c.strike,bid=str(candidate_bid),ask=str(candidate_bid+spread),bid_size=size,ask_size=size,
                    displayed_bid_depth=[(str(candidate_bid),size)],displayed_ask_depth=[(str(candidate_bid+spread),size)]),
                None if closed else s.clock.wall(),s.clock.wall(),s.clock.mono(),'CLOSED' if closed else 'LIVE',
                ('executable_quotes',) if closed else ()),s.clock.wall(),s.clock.mono())
        self.serial+=1
        self.market.ingest(MarketDatum('SYNTHETIC_INDEX','SPX',e.state['generation'],self.serial,'6000',s.clock.wall(),s.clock.wall(),s.clock.mono(),'LIVE'),s.clock.wall(),s.clock.mono())

    def cycle(self,index,exiting=False):
        s=self.sim;e=s.engine;self.market_update(index);s.source.advance(0);s.sync()
        before=time.perf_counter_ns();actions=e.schedule(e.state['generation'],s.funds())
        s.source.cancel_observations(s.trade,e,actions);s.sync()
        keys=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT' and e.state['intents'][a['intent_id']]['order_id'] is None]
        if keys:
            e.dispatch_batch(e.state['generation'],keys,None if exiting else s.funds());s.source.bindings(s.trade,e,keys)
            (self.exit_latency if exiting else self.entry_latency).append((time.perf_counter_ns()-before)/1e6)
        s.source.advance(.10);s.source.match();s.source.advance(.02);s.sync();s.check()
        fills=[v['record'] for v in e.state['executions'].values() if v['active']]
        bought=sum(x['quantity'] for x in fills if x['side']=='BUY')
        if bought and self.first_fill is None:self.first_fill=s.clock.seconds-self.entry_start
        if bought==self.quantity and self.target_fill is None:self.target_fill=s.clock.seconds-self.entry_start
        owned=sum(e.positions().values());self.gap_contract_seconds+=Decimal(owned)*Decimal('.12')
        self.residual.append(dict(virtual_seconds=s.clock.seconds,owned=owned,working_acquisition=sum(e.reservations('BUY',c.con_id) for c in s.contracts),
            working_reduction=sum(e.reservations('SELL',c.con_id) for c in s.contracts),entry_fenced=e.portfolio['halted']))
        if exiting and e.status()['phase']=='FLAT_CONFIRMED':self.flat=s.clock.seconds

    def run(self):
        s=self.sim
        for index in range(8):self.cycle(index)
        exit_at=s.clock.seconds
        # The same explicit synthetic operator EXIT ALL event for every tactic;
        # execution tactics do not decide strategy exit thresholds or direction.
        s.trigger('EMERGENCY')
        for index in range(8,20):
            self.cycle(index,True)
            if self.flat is not None:break
        return self.report(exit_at)

    def report(self,exit_at):
        s=self.sim;e=s.engine;active=[v['record'] for v in e.state['executions'].values() if v['active']]
        entries=[x for x in active if x['side']=='BUY'];exits=[x for x in active if x['side']=='SELL']
        buy_qty=sum(x['quantity'] for x in entries);sell_qty=sum(x['quantity'] for x in exits)
        buy_cost=sum((money(x['price'])*x['quantity']*100 for x in entries),Decimal(0));proceeds=sum((money(x['price'])*x['quantity']*100 for x in exits),Decimal(0))
        entry_slip=sum(((money(x['price'])-money(e._intent_for_ref(x['order_ref'])['arrival_price']))*x['quantity']*100 for x in entries),Decimal(0))
        exit_slip=sum(((money(e._intent_for_ref(x['order_ref'])['arrival_price'])-money(x['price']))*x['quantity']*100 for x in exits),Decimal(0))
        active_ids={x['exec_id'] for x in active}
        commissions=sum((money(x['amount']) for x in e.state['commissions'].values() if x['exec_id'] in active_ids),Decimal(0))
        spread_cost=Decimal(0)
        for x in active:
            q=self.arrivals.get(e._intent_for_ref(x['order_ref'])['quote_id'])
            if q:
                mid=(q['bid']+q['ask'])/2
                spread_cost+=2*(money(x['price'])-mid)*(1 if x['side']=='BUY' else -1)*x['quantity']*100
        state=s.source.state()
        return dict(origin='SYNTHETIC_ONLY',tactic=self.tactic,scenario=self.scenario,seed=self.seed,target=self.quantity,
            candidate_count=len(self.candidates),acquired=buy_qty,closed=sell_qty,residual=sum(e.positions().values()),
            first_fill_seconds=self.first_fill,target_fill_seconds=self.target_fill,confirmed_flat=self.flat is not None,
            exit_to_confirmed_flat_seconds=self.flat-exit_at if self.flat is not None else None,
            fill_completion_rate=str(Decimal(buy_qty)/self.quantity),entry_premium_dollars=str(buy_cost),exit_proceeds_dollars=str(proceeds),
            realized_net_pnl_dollars=str(proceeds-buy_cost-commissions) if self.flat is not None else None,
            entry_slippage_dollars=str(entry_slip),exit_slippage_dollars=str(exit_slip),commissions_dollars=str(commissions),
            effective_spread_dollars=str(spread_cost),
            price_movement_while_waiting_dollars=str(sum(((money(e.portfolio['quotes'][str(c.con_id)]['option']['ask'])-Decimal('10')+Decimal(c.con_id-1001)*Decimal('.5'))*e.portfolio['targets'][str(c.con_id)]['quantity']*100 for c in s.contracts),Decimal(0))),
            entry_average_premium=str(buy_cost/100/buy_qty) if buy_qty else None,
            exit_average_premium=str(proceeds/100/sell_qty) if sell_qty else None,
            worst_entry_premium=str(max((money(x['price']) for x in entries),default=Decimal(0))),
            worst_exit_premium=str(min((money(x['price']) for x in exits),default=Decimal(0))),
            protection_state='UNVERIFIED_NATIVE_BROKER_PROTECTION',
            protection_gap_contract_seconds_discrete_estimate=str(self.gap_contract_seconds),
            protection_gap_estimate_basis='END_OF_STEP_OWNED_TIMES_0_12_SECONDS_NOT_EXACT_GAP_OR_BOUND',
            protection_gap_resolution='FLAT_RECONCILED' if self.flat is not None else 'UNRESOLVED',
            submissions=state['submissions'],cancellations=state['cancellations'],rejections=state['rejections'],
            logical_broker_messages=state['submissions']+state['cancellations'],
            entry_scheduler_to_adapter_ms=distribution(self.entry_latency),exit_scheduler_to_adapter_ms=distribution(self.exit_latency),
            residual_over_time=self.residual,safety_violations=0,execution_enabled=False,
            assumptions=dict(realization=s.source.sim_policy['realization'],queue_position_known=False,depth_guaranteed=False,
                virtual_step_seconds='.12',native_protection_implemented=False,real_ibkr_measured=False))


def run_case(quantity=125,tactic='PHASE8_ADAPTIVE',scenario='deep',seed=0,realization='.65'):
    with tempfile.TemporaryDirectory() as root:
        twin=ExecutionTwin(root,quantity,tactic,scenario,seed,realization)
        try:return twin.run()
        finally:twin.sim.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);args=p.parse_args();rows=[]
    result=dict(origin='SYNTHETIC_ONLY',platform=platform.platform(),python=platform.python_version(),results=rows,complete=False)
    for quantity in (25,125):
        for scenario in SCENARIOS:
            for tactic in TACTICS:
                rows.append(run_case(quantity,tactic,scenario));Path(args.output).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
                print(quantity,scenario,tactic,rows[-1]['acquired'],rows[-1]['residual'],flush=True)
    result['sensitivity']=[run_case(125,'PHASE8_ADAPTIVE','thin',realization=value) for value in ('.25','.85')]
    result['interruptions']=[run_case(125,scenario=scenario) for scenario in ADVERSE_SCENARIOS]
    result.update(complete=True,safety_violations=sum(x['safety_violations'] for x in rows))
    Path(args.output).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')


if __name__=='__main__':main()
