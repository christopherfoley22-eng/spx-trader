"""Deterministic hard-admission cases and honest host timing; no broker client."""
import argparse,cProfile,json,platform,pstats,time
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from executor_exit_capacity_test import make_plan,capacity_policy
import executor_allocation_test as f
import executor_exit_capacity as capacity
from executor_phase10_benchmark import application_sample
from executor_readiness_benchmark import dispatch_sample
from executor_liquidity_simulator import distribution
from mortificatio_models import ApplicationError


def admission_cases():
    data=f.evidence(funds='700000');base=capacity_policy()
    scenarios=[('capital_700000_full_125',data,base),('capital_700000_risk_supports_50',data,capacity_policy('31565')),
        ('risk_supports_75',data,capacity_policy('47347.50')),
        ('atm_capacity_9',replace(data,quotes=tuple(replace(q,bid_size=18) if q.option.contract.strike==6000 else q for q in data.quotes)),base),
        ('entry_capacity_8',replace(data,quotes=tuple(replace(q,ask_size=10) if q.option.contract.strike==6000 else q for q in data.quotes)),base),
        ('all_exit_liquidity_gone',replace(data,quotes=tuple(replace(q,bid_size=0) for q in data.quotes)),base),
        ('protection_unverified',replace(data,capacity_context=replace(data.capacity_context,protection_feasibility='UNVERIFIED')),base),
        ('reconciliation_missing',replace(data,capacity_context=replace(data.capacity_context,reconciled=False)),base)]
    rows=[]
    for name,e,p in scenarios:
        before=time.perf_counter_ns()
        try:
            report=make_plan(e,p);decision=report['exit_capacity_admission'];reservation=report['reserved_capital']
        except ApplicationError as exc:
            decision=getattr(exc,'admission_decision',dict(status='REJECTED',reason=str(exc),admitted_quantities=[0]*5));reservation='0'
        rows.append(dict(case=name,origin='SYNTHETIC_ASSUMPTIONS_NOT_ACCOUNT_APPROVAL',buying_power='700000',
            decision=decision,reserved_capital=reservation,host_elapsed_ms=(time.perf_counter_ns()-before)/1e6,safety_violations=0))
    return rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--samples',type=int,default=5);p.add_argument('--exit-samples',type=int,default=50);args=p.parse_args()
    result=dict(origin='OFFLINE_SYNTHETIC_ONLY',execution_enabled=False,real_broker_observations=0,
        runtime=platform.python_version(),platform=platform.platform(),clock='perf_counter_ns',
        workload='BENCHMARK_PROCESS_WITH_CONCURRENT_OFFLINE_SUITE_NOT_CONTROLLED_LOAD',complete=False,
        admission_cases=admission_cases(),application_samples=[],exit_samples=[],capacity_checks_ms=[])
    original=capacity.require_capacity
    def timed(*a,**kw):
        start=time.perf_counter_ns()
        try:return original(*a,**kw)
        finally:result['capacity_checks_ms'].append((time.perf_counter_ns()-start)/1e6)
    def save():Path(args.output).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    with patch.object(capacity,'require_capacity',timed):
        for i in range(args.samples):
            for ready in (False,True):result['application_samples'].append(application_sample(ready))
            print('AUTHENTICATED_PAIR',i,flush=True);save()
    profiler=cProfile.Profile()
    for i in range(args.exit_samples):result['exit_samples'].append(dispatch_sample('READY',seed=i,profiler=profiler if i==0 else None))
    result['application_summary']={str(ready):{key:distribution([x[key] for x in result['application_samples'] if x['prepared']==ready])
        for key in result['application_samples'][0] if key.endswith('_ms')} for ready in (False,True)}
    result['exit_summary']={key:distribution([x['metrics'][key] for x in result['exit_samples']]) for key in result['exit_samples'][0]['metrics']}
    result['capacity_check_summary']=distribution(result['capacity_checks_ms'])
    stats=pstats.Stats(profiler)
    result['exit_profile_top_cumulative_seconds']=[dict(file=Path(k[0]).name,line=k[1],function=k[2],calls=v[1],self_seconds=v[2],cumulative_seconds=v[3]) for k,v in sorted(stats.stats.items(),key=lambda x:x[1][3],reverse=True)[:30]]
    result['historical_phase10_exit_p99_ms']=177.901919
    result['comparison_limit']='Equivalent ledger fixtures, different uncontrolled load; application N5/path descriptive only; one exit sample profiled and retained.'
    result['economic_findings']='Admission is a conditional downside model, not realized P&L or guaranteed liquidation. No order-slicing advantage established.'
    result['safety_violations']=sum(x['safety_violations'] for x in result['application_samples']+result['exit_samples']+result['admission_cases'])
    result['complete']=True;save()


if __name__=='__main__':main()
