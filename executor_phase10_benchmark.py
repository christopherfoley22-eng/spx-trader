"""Offline diagnostic: real fixture authentication/admission and quality results.

Test fixtures deliberately remain outside application services. No broker client.
"""
import argparse
import json
from pathlib import Path
import time
import uuid
from unittest.mock import patch
from executor_phase10_lifecycle_test import Phase10LifecycleTest,UniqueProvider
from executor_readiness_benchmark import dispatch_sample
from executor_execution_twin import run_case,ADVERSE_SCENARIOS
from executor_liquidity_simulator import distribution
from mortificatio_models import TradeCommand
from simulation_evidence import SPXObservation,FeedStatus


def application_sample(prepared):
    fixture=Phase10LifecycleTest();fixture.setUp();markers={}
    try:
        fixture.provider=UniqueProvider(fixture.provider);fixture.allocation.provider=fixture.provider
        fixture.lifecycle.configure_partial_fill_protection(fixture.service_token,'benchmark-protection-v1')
        before=time.perf_counter_ns()
        if prepared:fixture.lifecycle.prepare_entry_candidates(fixture.service_token)
        preparation_ms=(time.perf_counter_ns()-before)/1e6
        fixture.authorize()
        original_prepare=fixture.allocation.prepare;original_admit=fixture.core.direction_command;original_bind=fixture.fixture.bindings
        def allocation(*args,**kwargs):
            markers['authenticated']=time.perf_counter_ns();out=original_prepare(*args,**kwargs);markers['plan_in_transaction']=time.perf_counter_ns();return out
        def admit(*args,**kwargs):
            out=original_admit(*args,**kwargs);markers['capital_commit']=time.perf_counter_ns();return out
        def bind(*args,**kwargs):
            out=original_bind(*args,**kwargs);markers.setdefault('adapter',time.perf_counter_ns());return out
        with patch.object(fixture.allocation,'prepare',allocation),patch.object(fixture.core,'direction_command',admit),patch.object(fixture.fixture,'bindings',bind):
            markers['click']=time.perf_counter_ns()
            result=fixture.lifecycle.direction_command(fixture.session.token,fixture.session.csrf_token,TradeCommand(str(uuid.uuid4()),'CALL',0))
            markers['returned']=time.perf_counter_ns()
        entry_adapter=markers['adapter']
        engine=fixture.engine(result);assert sum(i['quantity'] for i in fixture.orders(result))==125
        first=time.perf_counter_ns();fixture.fill(result,25);all_filled=time.perf_counter_ns()
        assert sum(engine.positions().values())==125
        fixture.lifecycle.observe_spx(fixture.service_token,result['trade_id'],'arm-before-timing',SPXObservation(6005,FeedStatus.LIVE,fixture.wall(),fixture.mono()))
        markers.pop('adapter',None)
        with patch.object(fixture.fixture,'bindings',bind):
            markers['exit_trigger']=time.perf_counter_ns()
            fixture.lifecycle.observe_spx(fixture.service_token,result['trade_id'],'three-point-reversal',SPXObservation(6002,FeedStatus.LIVE,fixture.wall(),fixture.mono()))
        markers['exit_adapter']=markers['adapter'];fixture.fill(result,25,side='SELL',price='9.9');flat=time.perf_counter_ns()
        assert fixture.lifecycle._trade_status(result['trade_id'])['reconciled_flat']
        def ms(a,b):return (markers[b]-markers[a])/1e6
        return dict(origin='SYNTHETIC_ONLY',prepared=prepared,quantity=125,background_dual_preparation_ms=preparation_ms,
            click_to_auth_and_broker_checks_ms=ms('click','authenticated'),auth_to_allocation_in_transaction_ms=ms('authenticated','plan_in_transaction'),
            click_to_capital_commit_ms=ms('click','capital_commit'),capital_commit_to_adapter_ms=(entry_adapter-markers['capital_commit'])/1e6,
            click_to_adapter_ms=(entry_adapter-markers['click'])/1e6,
            click_to_return_ms=ms('click','returned'),entry_fill_injection_to_confirmed_target_ms=(all_filled-first)/1e6,
            exit_event_to_adapter_ms=ms('exit_trigger','exit_adapter'),exit_event_to_confirmed_flat_ms=(flat-markers['exit_trigger'])/1e6,
            cache_builds=fixture.allocation.readiness.builds if prepared else 0,cache_hits=fixture.allocation.readiness.hits if prepared else 0,
            continuous_exit_readiness=hasattr(engine,'exit_readiness'),
            broker_acknowledgment='SYNCHRONOUS_FAKE_CALLBACKS_NOT_REAL_IBKR',safety_violations=0,execution_enabled=False)
    finally:fixture.doCleanups()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--quality',required=True);p.add_argument('--samples',type=int,default=10);args=p.parse_args()
    path=Path(args.quality);report=json.loads(path.read_text())
    if not report['complete']:raise RuntimeError('Complete quality run required')
    report['application_samples']=[]
    for index in range(args.samples):
        for ready in (False,True):report['application_samples'].append(application_sample(ready))
        print('Authenticated entry/exit pair',index,flush=True)
    report['application_summary']={str(ready):{key:distribution([x[key] for x in report['application_samples'] if x['prepared']==ready])
        for key in report['application_samples'][0] if key.endswith('_ms')} for ready in (False,True)}
    report['phase9_equivalent_exit_samples']=[dispatch_sample('READY',seed=seed) for seed in range(50)]
    report['phase9_equivalent_exit_summary']={key:distribution([x['metrics'][key] for x in report['phase9_equivalent_exit_samples']])
        for key in report['phase9_equivalent_exit_samples'][0]['metrics']}
    report['interruptions']=[run_case(125,scenario=scenario) for scenario in ADVERSE_SCENARIOS]
    report['tail_confidence']='APPLICATION_N10_DESCRIPTIVE_ONLY_NOT_P99_ACCEPTANCE; EXIT_N50_FINITE_SAMPLE'
    report['all_safety_violations']=sum(x['safety_violations'] for x in report['application_samples']+report['phase9_equivalent_exit_samples']+report['results']+report['interruptions'])
    path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')


if __name__=='__main__':main()
