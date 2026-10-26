"""Reproducible SYNTHETIC exit comparisons. Not measured IBKR execution.

Fixture entry quantities are benchmark inputs, never a dashboard sizing API.
The actual entry allocator and its capital-first behavior are unchanged.
"""
import argparse
from dataclasses import replace,asdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import platform
import tempfile
import time
import uuid

from executor_adaptive_liquidity import AdaptiveExitPolicy,DepthObservation
from executor_liquidation import GlobalLiquidationCoordinator,ClosingOrderSafety
from executor_liquidity_simulator import EventDrivenBroker,VirtualMarketClock,SimulationPolicy,distribution
from executor_offline_fixture import OfflineBrokerFixture,order_record,execution_record
from executor_portfolio import OfflinePortfolioLedger
from executor_portfolio_liquidity import ExecutionPolicy,LiquidityFixture
from executor_paper_types import ApprovedPaperAccount,PaperSessionEvidence,CommissionRecord,PaperBrokerError
from executor_paper_broker import money
from executor_market_ingestion import OptionGreeks
from simulation_evidence import VerifiedOptionContract,VerifiedAccountSnapshot,OptionObservation,SPXObservation,FeedStatus

BASE=datetime(2026,10,23,14,30,tzinfo=timezone.utc)
METHODS=('SINGLE','FIXED','ADAPTIVE','ADAPTIVE_REPRICE')
SIZES=(1,9,25,35,50,60,75,85,100,125)
SCENARIOS=('deep','moderate','thin','withdrawal','volatile','delayed_ack','partial','disconnect','rejections','severe_volatility','disappearing')


def sequential_fixture_quantities(quantity):
    if type(quantity) is not int or not 1<=quantity<=125:raise ValueError('Synthetic size must be 1..125')
    return tuple(max(0,min(25,quantity-25*i)) for i in range(5))


def fixture_execution_policy():
    return ExecutionPolicy('.50','.20','.30','.50',1,25,3,2,'.05',2,3,30,20,2,200000,50000,50000000)


class ExitSimulation:
    def __init__(self,root,quantity,method='ADAPTIVE_REPRICE',seed=1,scenario='moderate',right='C',simulation_policy=None,seed_entries=True,execution_policy=None,listed_strikes=None):
        self.root=Path(root);self.clock=VirtualMarketClock(BASE);self.account='DU0000001';self.trade='synthetic-exit'
        self.approval=ApprovedPaperAccount(self.account,'SYNTHETIC_PAPER_APPROVAL',BASE+timedelta(days=1))
        self.execution=execution_policy or fixture_execution_policy();self.scenario=scenario;self.serial=0;self.cursor=0
        self.host=dict(receipt_to_decision=[],decision_to_prepare=[],prepare_to_dispatch=[],coordinator_cycle=[])
        self.synthetic=dict(dispatch_to_ack=[],ack_to_first_fill=[],ack_to_final_fill=[]);self.residual=[]
        sim=SimulationPolicy(seed,acknowledgement_delay=.25 if scenario=='delayed_ack' else .02,
            realization='.35' if scenario=='partial' else '.65',reject_every=7 if scenario=='rejections' else 0)
        self.source=EventDrivenBroker(self.root/'broker.sqlite',self.account,self.clock,simulation_policy or sim)
        self.engine=OfflinePortfolioLedger(self.root/'portfolio.sqlite',self.approval,self.execution,self.clock.wall,self.clock.mono)
        self.verify()
        if listed_strikes is not None and len(listed_strikes)!=5:raise ValueError('Five explicit synthetic listed strikes required')
        self.contracts=[VerifiedOptionContract(symbol='SPX',sec_type='OPT',right=right,expiration='20261023',trading_class='SPXW',
            multiplier='100',currency='USD',strike=listed_strikes[i] if listed_strikes is not None else 6000.+(5*i if right=='C' else -5*i),con_id=1001+i) for i in range(5)]
        self.all_contracts=tuple(self.contracts)
        quantities=sequential_fixture_quantities(quantity)
        self.engine.configure(self.engine.state['generation'],[(c,q) for c,q in zip(self.contracts,quantities) if q],6000,self.funds(),lambda _:True)
        self.contracts=[c for c,q in zip(self.contracts,quantities) if q]
        self.update_books('seed')
        if seed_entries:
            plans=self.engine.schedule(self.engine.state['generation'],self.funds())
            keys=[a['intent_id'] for a in plans if a['type']=='SIMULATED_LIMIT_INTENT']
            self.sync();self.engine.dispatch_batch(self.engine.state['generation'],keys,self.funds())
            # Seed actual synthetic acquisition evidence through the existing fixture.
            OfflineBrokerFixture.bindings(self.source,self.trade,self.engine,keys)
            self.source.advance(.001);self.sync()
            for key in keys:
                item=self.engine.state['intents'][key];price=str(Decimal('10')-Decimal(item['contract']['con_id']-1001)*Decimal('.5'))
                self.source.fill(item['order_ref'],item['quantity'],price,self.clock.wall(),'seed:'+str(item['contract']['con_id'])+'.01')
            self.source.advance(max(.02,self.source.sim_policy['order_report_delay']+.001,self.source.sim_policy['execution_report_delay']+.001));self.sync()
            if sum(self.engine.positions().values())!=quantity:raise AssertionError('Synthetic seed fill failed')
        self.coordinator=GlobalLiquidationCoordinator(self.engine)
        self.coordinator.configure(AdaptiveExitPolicy('synthetic-phase8-v1',method=method))
        self.start=None;self.flat_at=None;self.market_host_budget_ms=250.
        self.decision_host_ns=None;self.first_preparation_measured=False

    def close(self):self.engine.close()

    def verify(self):
        self.engine.verify_session(PaperSessionEvidence(self.account,(self.account,),self.approval.verification_ref,str(uuid.uuid4()),
            self.clock.wall(),self.clock.wall()+timedelta(hours=1),3))
        self.source.connected=True
        self.sync()

    def funds(self):return VerifiedAccountSnapshot(self.account,self.account,True,'USD','200000','200000','200000','200000','200000',self.clock.mono())

    def sync(self):
        if not self.source.connected:return False
        for sequence,trade,kind,raw,stamp in self.source.callbacks_after(self.cursor):
            data=json.loads(raw);generation=self.engine.state['generation'];identity='sim-callback:'+str(sequence)
            if kind=='ORDER':self.engine.observe_order(generation,identity,order_record(data))
            elif kind=='EXECUTION':
                record=execution_record(data)
                self.engine._append('sim-source:'+record.exec_id,'PORT_FILL_SOURCE_TIME',dict(exec_id=record.exec_id,record_digest='SYNTHETIC_SOURCE',executed_at=stamp))
                self.engine.observe_execution(generation,identity,record)
            elif kind=='COMMISSION':self.engine.observe_commission(generation,identity,CommissionRecord(**data))
            self.cursor=sequence
        snapshot=self.source.snapshot(self.engine.state['generation']);result=self.engine.reconcile(snapshot)
        if not result['matched']:
            # A completed delayed/out-of-order stream can be repaired only by
            # an explicit current authoritative broker-order snapshot, not guesses.
            with self.source.db() as db:pending=db.execute("SELECT 1 FROM sim_events WHERE kind='DELIVER' AND done=0").fetchone()
            if not pending:
                for order in (*snapshot.open_orders,*snapshot.completed_orders):
                    self.engine.observe_order(self.engine.state['generation'],'snapshot-order:'+str(snapshot.cycle_id)+':'+str(order.order_id),order)
                result=self.engine.reconcile(self.source.snapshot(self.engine.state['generation']))
        return result['matched']

    def update_books(self,phase='exit',step=0):
        for c in self.contracts:
            self.serial+=1
            bid=Decimal('9.90')-Decimal(c.con_id-1001)*Decimal('.5')
            if phase!='seed' and self.scenario=='volatile':bid-=Decimal('.05')*(step%5)
            if phase!='seed' and self.scenario=='severe_volatility':bid-=Decimal('.15')*min(step,10)
            size=200 if phase=='seed' or self.scenario=='deep' else 4 if self.scenario in ('thin','partial','severe_volatility','disappearing') else 15
            levels=((bid,size),(bid-Decimal('.05'),size),(bid-Decimal('.10'),size))
            identity='book:'+str(self.serial)
            self.source.set_book(c,levels,identity,withdraw=phase!='seed' and
                (self.scenario=='withdrawal' and step%5 in (1,2,3) or self.scenario=='disappearing' and step>=1))
            q=LiquidityFixture('quote:'+str(self.serial),OptionObservation(c,c.con_id,float(bid),float(bid+Decimal('.10')),FeedStatus.LIVE,self.clock.wall(),self.clock.mono()),
                SPXObservation(6000,FeedStatus.LIVE,self.clock.wall(),self.clock.mono()),size,200,self.clock.wall(),self.clock.mono(),
                '.05',200,200,'.05','.05',OptionGreeks(.48 if c.right=='C' else -.48,.01,.2,-.5,.25,float(bid),6000.,0.),self.clock.wall(),self.clock.mono())
            self.engine.observe_liquidity(self.engine.state['generation'],'quote-event:'+str(self.serial),q)
            if self.engine.portfolio.get('adaptive_exit'):
                self.coordinator.observe_depth(DepthObservation(identity,c,self.clock.wall(),self.clock.mono(),self.serial,levels,False))

    def trigger(self,mode='NORMAL'):
        self.start=self.clock.seconds
        begin=time.perf_counter_ns()
        if mode=='EMERGENCY':self.engine.trigger_exit('synthetic-emergency','EMERGENCY_EXIT_ALL',True)
        elif mode=='PROTECTIVE':
            c=self.contracts[0];q=self.engine.portfolio['quotes'][str(c.con_id)]
            from executor_portfolio_liquidity import fixture_from_payload
            quote=fixture_from_payload(q);_,stop=self.engine._premium_stop(c.con_id)
            self.engine.observe_liquidity(self.engine.state['generation'],'synthetic-premium-stop',replace(quote,quote_id='hard-stop',option=replace(quote.option,bid=float(stop),ask=float(stop+Decimal('.1')))))
        else:
            sign=1 if self.contracts[0].right=='C' else -1
            for offset in (4,1.25):
                begin=time.perf_counter_ns()
                self.engine.observe_spx(self.engine.state['generation'],'synthetic-spx:'+str(offset),SPXObservation(6000+sign*offset,FeedStatus.LIVE,self.clock.wall(),self.clock.mono()))
        self.host['receipt_to_decision'].append((time.perf_counter_ns()-begin)/1e6)
        self.decision_host_ns=time.perf_counter_ns()
        if not self.engine.portfolio['exit_reason']:raise AssertionError('Existing exit strategy did not authorize reduction')

    def step(self,index,step=.1):
        if self.scenario=='disconnect' and index==2:
            self.source.connected=False;self.engine.disconnect(self.engine.state['generation'])
        if self.scenario=='disconnect' and index==4:self.verify()
        if not self.source.connected:
            self.source.advance(step);self.residual.append([self.clock.seconds-self.start,sum(self.engine.positions().values())]);return
        self.source.advance(0);self.sync();self.update_books(step=index)
        previous=set(self.engine.state['intents']);before=time.perf_counter_ns();actions=self.engine.schedule(self.engine.state['generation'])
        prepared_ns=time.perf_counter_ns()
        elapsed=(prepared_ns-before)/1e6;self.host['coordinator_cycle'].append(elapsed)
        if set(self.engine.state['intents'])-previous and not self.first_preparation_measured:
            self.host['decision_to_prepare'].append((prepared_ns-self.decision_host_ns)/1e6)
            self.first_preparation_measured=True
        self.source.cancel_observations(self.trade,self.engine,actions)
        self.sync()
        keys=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT' and self.engine.state['intents'][a['intent_id']]['order_id'] is None]
        if keys:
            self.engine.dispatch_batch(self.engine.state['generation'],keys)
            self.source.bindings(self.trade,self.engine,keys)
            self.host['prepare_to_dispatch'].append((time.perf_counter_ns()-prepared_ns)/1e6)
        self.source.advance(step);self.source.match();self.source.advance(0);self.sync()
        self.check()
        count=sum(self.engine.positions().values());self.residual.append([self.clock.seconds-self.start,count])
        if self.engine.status()['phase']=='FLAT_CONFIRMED':self.flat_at=self.clock.seconds

    def check(self):
        if any(q<0 or q>25 for q in self.engine.positions().values()) or sum(self.engine.positions().values())>125:raise AssertionError('Position-size invariant')
        for c in self.contracts:
            if self.engine.reservations('SELL',c.con_id)>self.engine.positions().get(c.con_id,0):raise AssertionError('Closing-reservation invariant')
        keys=[(i['client_id'],i['order_id']) for i in self.engine.state['intents'].values() if i['order_id'] is not None]
        if len(keys)!=len(set(keys)):raise AssertionError('Duplicate order identity')

    def report(self):
        state=self.source.state();fills=[e['record'] for e in self.engine.state['executions'].values() if e['active'] and e['record']['side']=='SELL']
        qty=sum(e['quantity'] for e in fills);s=self.engine.portfolio['adaptive_exit'];cost=Decimal(0);worst=Decimal(0)
        per=[]
        for c in self.contracts:
            selected=[e for e in fills if e['contract']['con_id']==c.con_id];count=sum(e['quantity'] for e in selected)
            arrival=s['arrivals'].get(str(c.con_id),{}).get('price')
            slip=sum(((money(arrival)-money(e['price']))*e['quantity'] for e in selected),Decimal(0)) if arrival else Decimal(0)
            cost+=slip
            worst=max([worst]+[money(arrival)-money(e['price']) for e in selected] if arrival else [worst])
            per.append(dict(contract_id=c.con_id,filled=count,realized_slippage_points=str(slip/count) if count else None))
        for ref,m in state['metrics'].items():
            intent=next((i for i in self.engine.state['intents'].values() if i['order_ref']==ref),None)
            if intent and intent.get('ack_at'):
                ack=datetime.fromisoformat(intent['ack_at'])
                self.synthetic['dispatch_to_ack'].append((ack-datetime.fromisoformat(intent['submitted_at'])).total_seconds())
                times=[datetime.fromisoformat(self.engine.portfolio['execution_receipts'][e['exec_id']]) for e in fills if e['order_ref']==ref]
                if times:
                    self.synthetic['ack_to_first_fill'].append((min(times)-ack).total_seconds())
                    if self.engine.filled(intent)==intent['quantity']:
                        self.synthetic['ack_to_final_fill'].append((max(times)-ack).total_seconds())
        estimates=[i for i in self.engine.state['intents'].values() if i['side']=='SELL' and i.get('estimated_slippage_points') is not None]
        estimated_qty=sum(i['quantity'] for i in estimates)
        return dict(origin='SYNTHETIC_ONLY',real_ibkr_measured=False,method=s['policy']['method'],scenario=self.scenario,
            seed=self.source.sim_policy['seed'],target=sum(t['quantity'] for t in self.engine.portfolio['targets'].values()),
            filled=qty,confirmed_flat=self.flat_at is not None,time_to_flat_seconds=self.flat_at-self.start if self.flat_at is not None else None,
            residual=sum(self.engine.positions().values()),realized_slippage_points=str(cost/qty) if qty else None,
            estimated_intent_weighted_slippage_points=str(sum((money(i['estimated_slippage_points'])*i['quantity'] for i in estimates),Decimal(0))/estimated_qty) if estimated_qty else None,
            estimate_basis='DISPLAYED_INTENT_ESTIMATE_NOT_GUARANTEED_FINAL_LIQUIDATION_COST',
            realized_slippage_dollars=str(cost*100),worst_fill_slippage_points=str(worst),per_strike=per,
            host_internal_latency_ms={key:distribution(values) for key,values in self.host.items()},
            host_internal_latency_samples_ms=self.host,
            synthetic_latency_seconds={key:distribution(values) for key,values in self.synthetic.items()},
            logical_submissions=state['submissions'],cancel_requests=state['cancellations'],rejections=state['rejections'],
            logical_order_requests=state['submissions']+state['cancellations'],
            partial_fill_executions=sum(e['quantity']<next(i['quantity'] for i in self.engine.state['intents'].values() if i['order_ref']==e['order_ref']) for e in fills),
            residual_over_time=self.residual,safety_violations=0,execution_enabled=False)


def run_case(quantity=125,method='ADAPTIVE_REPRICE',seed=1,scenario='moderate',horizon=4.,mode='NORMAL',right='C'):
    with tempfile.TemporaryDirectory() as root:
        simulation=ExitSimulation(root,quantity,method,seed,scenario,right)
        try:
            simulation.update_books();simulation.trigger(mode)
            for index in range(int(horizon/.1)):
                simulation.step(index)
                if simulation.flat_at is not None:break
            return simulation.report()
        finally:simulation.close()


def summarize_results(results):
    """Do not hide unresolved cases or average percentiles of percentiles."""
    groups={}
    for method in METHODS:
        rows=[r for r in results if r['method']==method];filled=sum(r['filled'] for r in rows)
        groups[method]=dict(cases=len(rows),confirmed_flat=sum(r['confirmed_flat'] for r in rows),
            residual=sum(r['residual'] for r in rows),logical_order_requests=sum(r['logical_order_requests'] for r in rows),
            cancellations=sum(r['cancel_requests'] for r in rows),rejections=sum(r['rejections'] for r in rows),
            filled_contracts=filled,
            realized_slippage_points=str(sum((money(r['realized_slippage_dollars'])/100 for r in rows),Decimal(0))/filled) if filled else None,
            confirmed_flat_latency_seconds=distribution([r['time_to_flat_seconds'] for r in rows if r['confirmed_flat']]),
            latency_population='CONFIRMED_FLAT_CASES_ONLY_PENDING_CASES_REPORTED_SEPARATELY')
    keys=('receipt_to_decision','decision_to_prepare','prepare_to_dispatch','coordinator_cycle')
    host={key:distribution([sample for row in results for sample in row['host_internal_latency_samples_ms'][key]]) for key in keys}
    return dict(origin='SYNTHETIC_ONLY',methods=groups,host_internal_latency_ms=host,
        safety_violations=sum(r['safety_violations'] for r in results),
        latency_budget=dict(milliseconds=250,scope='EACH_INTERNAL_HANDLER_STAGE_P99_ON_RECORDED_HOST_ONLY',
            passed=all(v['p99'] is not None and v['p99']<=250 for v in host.values())),
        broker_latency_measured=False,forecasting_active=False,entry_gate_changed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);parser.add_argument('--seeds',type=int,default=3)
    args=parser.parse_args();results=[]
    report=dict(origin='SYNTHETIC_ONLY',hardware=dict(platform=platform.platform(),processor=platform.machine()),
        policy='synthetic-phase8-v1',horizon_seconds=4,results=results,complete=False,real_ibkr_measured=False)
    for scenario in SCENARIOS:
        for method in METHODS:
            for seed in range(args.seeds):
                results.append(run_case(method=method,seed=seed,scenario=scenario))
                Path(args.output).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
                print('SYNTHETIC',scenario,method,seed,'residual',results[-1]['residual'],flush=True)
    report.update(summary=summarize_results(results),complete=True)
    Path(args.output).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print('Synthetic benchmark cases:',len(results),'Safety violations:',sum(r['safety_violations'] for r in results))


if __name__=='__main__':main()
