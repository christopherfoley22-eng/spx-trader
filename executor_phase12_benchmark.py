"""Synthetic protection/reconciliation correctness and actual host timings only."""
import argparse,cProfile,json,platform,time
from pathlib import Path
from unittest.mock import patch
import executor_execution_coordinator as obligations
import executor_native_protection as native
from executor_native_protection_test import NativeProtectionTest
from executor_phase10_benchmark import application_sample
from executor_readiness_benchmark import dispatch_sample
from executor_liquidity_simulator import distribution


def cases():
    rows=[]
    for name in sorted(n for n in dir(NativeProtectionTest) if n.startswith('test_')):
        t=NativeProtectionTest(name);t.setUp();start=time.perf_counter_ns()
        try:
            getattr(t,name)()
            e=t.ledger
            rows.append(dict(case=name,origin='SYNTHETIC_ONLY',assertions='PASS',elapsed_ms=(time.perf_counter_ns()-start)/1e6,
                known_positions=e.positions(),closing_reservations={str(c.con_id):e.reservations('SELL',c.con_id) for c in t.contracts},
                phase=e.status()['phase'],entry_fenced=e.portfolio['halted'],safety_violations=0))
        finally:t.doCleanups()
    return rows


def measure_native(repeats):
    t=NativeProtectionTest();t.setUp();samples={};persistence={}
    def measured(label,fn):
        start=time.perf_counter_ns()
        try:return fn()
        finally:samples.setdefault(label,[]).append((time.perf_counter_ns()-start)/1e6)
    e=t.ledger;append=e._append
    def persist(event,kind,data):
        start=time.perf_counter_ns()
        try:return append(event,kind,data)
        finally:persistence.setdefault(kind,[]).append((time.perf_counter_ns()-start)/1e6)
    original=obligations.protection_after_economic
    def update(*args):return measured('protection_obligation_update_ms',lambda:original(*args))
    try:
        with patch.object(e,'_append',persist),patch.object(obligations,'protection_after_economic',update):
            ids=t.fake.dispatch(t.schedule())
            for key in ids:
                r=t.fake.execution(key,25,'10',commission=False);t.fake.commission(r.exec_id,amount='16.25')
            assert t.fake.reconcile()['matched']
            keys=[]
            for c in t.contracts:
                key=measured('native_plan_ms',lambda c=c:t.manager.plan(c.con_id,'benchmark:'+str(c.con_id)))
                assert t.fake.reconcile()['matched']
                number=max(i['order_id'] or 0 for i in e.state['intents'].values())+1
                measured('native_identity_binding_ms',lambda key=key,number=number:t.manager.bind_synthetic(key,number))
                t.fake.order(key);assert t.fake.reconcile()['matched'];keys.append(key)
            assert not t.manager.status()['unresolved']
            entry=next(iter(t.fake.executions.values()));count=len(e.state['executions'])
            for i in range(repeats):
                measured('broker_order_event_ingestion_ms',lambda:t.fake.order(keys[0]))
                measured('execution_dedup_durable_receipt_ms',lambda i=i:e.observe_execution(t.fake.generation,'benchmark-duplicate:'+str(i),entry))
                assert len(e.state['executions'])==count
                assert measured('complete_position_order_reconciliation_ms',lambda:t.fake.reconcile())['matched']
                measured('protection_coverage_projection_ms',t.manager.status)
            trigger=time.perf_counter_ns()
            for key in keys:
                r=measured('native_stop_execution_ingestion_ms',lambda key=key:t.fake.execution(key,25,'6.5',commission=False))
                t.fake.commission(r.exec_id,amount='16.25')
            assert measured('confirmed_flat_reconciliation_ms',lambda:t.fake.reconcile())['matched']
            assert e.status()['phase']=='FLAT_CONFIRMED'
            end_to_flat=(time.perf_counter_ns()-trigger)/1e6
            economic=dict(quantity=125,entry_premium='10',actual_synthetic_exit_premium='6.5',gross_pnl='-43750',fees='162.50',net_pnl='-43912.50',
                calculation='RECORDED_SYNTHETIC_EXECUTIONS_AND_PER_CONTRACT_FEES_NOT_GREEKS')
            return dict(raw_metrics=samples,summary={k:distribution(v) for k,v in samples.items()},
                persistence_raw=persistence,persistence_summary={k:distribution(v) for k,v in persistence.items()},
                five_stop_execution_to_confirmed_flat_ms=end_to_flat,economics=economic,
                positions=e.positions(),phase=e.status()['phase'],safety_violations=0)
    finally:t.doCleanups()


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--samples',type=int,default=50);args=p.parse_args()
    r=dict(origin='SYNTHETIC_ONLY',runtime=platform.python_version(),platform=platform.platform(),clock='perf_counter_ns',
        workload='SYNCHRONOUS_FAKE_BROKER_AND_SQLITE_WITH_CONCURRENT_OFFLINE_TESTS_UNCONTROLLED_LOAD',
        source_time='FIXED_VIRTUAL_BROKER_CLOCK_FOR_HOST_COST_ISOLATION_NOT_PRODUCTION_FRESHNESS_ACCEPTANCE',
        real_ibkr_observations=0,execution_enabled=False,complete=False)
    output=Path(args.output)
    def save():output.write_text(json.dumps(r,indent=2,sort_keys=True)+'\n')
    r['adversarial_cases']=cases();save();print('NATIVE_CASES',len(r['adversarial_cases']),flush=True)
    r['native_measurements']=measure_native(args.samples);save();print('NATIVE_METRICS_COMPLETE',flush=True)
    r['equivalent_phase11_exit_samples']=[dispatch_sample('READY',seed=i) for i in range(args.samples)]
    r['equivalent_phase11_exit_summary']={k:distribution([x['metrics'][k] for x in r['equivalent_phase11_exit_samples']]) for k in r['equivalent_phase11_exit_samples'][0]['metrics']}
    r['prepared_application_samples']=[application_sample(True) for _ in range(5)]
    r['prepared_application_summary']={k:distribution([x[k] for x in r['prepared_application_samples']]) for k in r['prepared_application_samples'][0] if k.endswith('_ms')}
    r['phase11_historical_exit_p99_ms']=71.941592
    r['comparison_limits']='Equivalent ledger fixtures; uncontrolled load. Native model and application samples are separate workloads. N5 application is descriptive, not p99 acceptance.'
    r['safety_violations']=sum(x['safety_violations'] for x in r['adversarial_cases']+r['equivalent_phase11_exit_samples']+r['prepared_application_samples'])
    r['complete']=True;save()


if __name__=='__main__':main()
