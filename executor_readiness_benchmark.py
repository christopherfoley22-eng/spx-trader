"""End-to-end OFFLINE readiness comparison; no actual broker latency measured."""
import argparse
import cProfile
import json
from pathlib import Path
import platform
import pstats
import sqlite3
import tempfile
from threading import Thread,Event
import time
from unittest.mock import patch
from executor_liquidity_benchmark import ExitSimulation,SIZES
from executor_liquidity_simulator import distribution
from executor_exit_readiness import enable_readiness
from executor_liquidation import ClosingOrderSafety
from simulation_evidence import SPXObservation,FeedStatus
import executor_liquidation


def dispatch_sample(path='READY',workload='WARM',quantity=125,seed=0,mode='NORMAL',right='C',profiler=None):
    with tempfile.TemporaryDirectory() as root:
        sim=ExitSimulation(root,quantity,method='ADAPTIVE',seed=seed,right=right)
        stamps={};durations={};thread=None;db=None
        try:
            sim.update_books();sim.sync()
            if mode=='NORMAL':
                sign=1 if right=='C' else -1
                sim.engine.observe_spx(sim.engine.state['generation'],'prearmed-before-measurement',
                    SPXObservation(6000+sign*4,FeedStatus.LIVE,sim.clock.wall(),sim.clock.mono()))
            cache=enable_readiness(sim.engine) if path=='READY' else None
            if cache and workload=='COLD':cache.invalidate()
            original_append=sim.engine._append;original_select=executor_liquidation.choose_slices;original_batch=ClosingOrderSafety.batch
            def append(event,kind,payload):
                if kind=='PORT_EXIT' or kind=='PORT_SPX' and payload.get('reason'):
                    stamps.setdefault('trigger',time.perf_counter_ns())
                out=original_append(event,kind,payload)
                if kind=='PORT_PLAN':stamps.setdefault('reservation',time.perf_counter_ns())
                if kind=='PORT_DISPATCH':stamps['binding']=time.perf_counter_ns()
                return out
            def select(*args,**kwargs):
                out=original_select(*args,**kwargs);stamps.setdefault('selected',time.perf_counter_ns());return out
            def validate(*args,**kwargs):
                before=time.perf_counter_ns();out=original_batch(*args,**kwargs)
                durations['independent_validation_ms']=(time.perf_counter_ns()-before)/1e6
                stamps['validated']=time.perf_counter_ns();return out
            original_hint=cache.select if cache else None
            def hint(*args,**kwargs):
                out=original_hint(*args,**kwargs)
                if out is not None:stamps.setdefault('selected',time.perf_counter_ns())
                return out
            if workload=='DB_CONTENTION':
                db=sqlite3.connect(str(sim.engine.path),isolation_level=None,check_same_thread=False)
                held=Event()
                def contend():
                    db.execute('BEGIN IMMEDIATE');held.set();time.sleep(.010);db.execute('COMMIT')
                thread=Thread(target=contend);thread.start();held.wait()
            with patch.object(sim.engine,'_append',append),patch.object(executor_liquidation,'choose_slices',select),patch.object(ClosingOrderSafety,'batch',validate):
                with patch.object(cache,'select',hint) if cache else _null():
                    if profiler:profiler.enable()
                    stamps['receipt']=time.perf_counter_ns()
                    if mode=='NORMAL':
                        sim.engine.observe_spx(sim.engine.state['generation'],'measured-single-trigger',
                            SPXObservation(6000+sign*1.25,FeedStatus.LIVE,sim.clock.wall(),sim.clock.mono()))
                    else:sim.trigger(mode)
                    actions=sim.engine.schedule(sim.engine.state['generation'])
                    stamps['prepared']=time.perf_counter_ns()
                    sim.sync()  # Required authoritative reconcile after reservation.
                    ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
                    if not ids:raise AssertionError('No valid closing plan in benchmark sample')
                    sim.engine.dispatch_batch(sim.engine.state['generation'],ids)
                    sim.source.bindings(sim.trade,sim.engine,ids)
                    stamps['adapter']=time.perf_counter_ns()
                    if profiler:profiler.disable()
            sim.check()
            assert sim.engine.status()['phase']!='FLAT_CONFIRMED'
            def elapsed(a,b):return (stamps[b]-stamps[a])/1e6
            durations.update(receipt_to_trigger_ms=elapsed('receipt','trigger'),trigger_to_selected_ms=elapsed('trigger','selected'),
                selected_to_reservation_ms=elapsed('selected','reservation'),reservation_to_final_validation_ms=elapsed('reservation','validated'),
                reservation_to_adapter_ms=elapsed('reservation','adapter'),trigger_to_prepared_ms=elapsed('trigger','prepared'),
                trigger_to_adapter_ms=elapsed('trigger','adapter'),receipt_to_adapter_ms=elapsed('receipt','adapter'))
            return dict(origin='SYNTHETIC_ONLY',path=path,workload=workload,quantity=quantity,seed=seed,mode=mode,right=right,
                metrics=durations,cache_hits=cache.hits if cache else 0,cache_misses=cache.misses if cache else 0,
                prepared_orders=len(ids),reserved=sum(sim.engine.reservations('SELL',c.con_id) for c in sim.contracts),
                actual_fills=0,safety_violations=0,execution_enabled=False)
        finally:
            if thread:thread.join()
            if db:db.close()
            sim.close()


from contextlib import nullcontext as _null


def quality_sample(ready,scenario,quantity=125,seed=0):
    with tempfile.TemporaryDirectory() as root:
        sim=ExitSimulation(root,quantity,method='ADAPTIVE',seed=seed,scenario=scenario)
        try:
            sim.update_books()
            if ready:enable_readiness(sim.engine)
            sim.trigger()
            for index in range(40):
                sim.step(index)
                if sim.flat_at is not None:break
            report=sim.report();report['path']='READY' if ready else 'PHASE8'
            return report
        finally:sim.close()


def summary(samples):
    groups={}
    for sample in samples:groups.setdefault(sample['path']+':'+sample['workload'],[]).append(sample)
    return {key:{metric:distribution([x['metrics'][metric] for x in rows]) for metric in rows[0]['metrics']} for key,rows in groups.items()}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);parser.add_argument('--repeats',type=int,default=50)
    args=parser.parse_args();samples=[];quality=[]
    result=dict(origin='SYNTHETIC_ONLY',runtime=platform.python_version(),platform=platform.platform(),
        hardware=platform.machine(),workload='ONE_BENCHMARK_PROCESS_PLUS_CONCURRENT_OFFLINE_REGRESSION',
        clock='perf_counter_ns',samples=samples,quality=quality,complete=False,real_ibkr_measured=False)
    def save():Path(args.output).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    for workload,n in (('WARM',args.repeats),('COLD',10),('DB_CONTENTION',10)):
        for seed in range(n):
            for path in ('PHASE8','READY'):samples.append(dispatch_sample(path,workload,seed=seed))
        save();print(workload,'completed',n,'paired samples',flush=True)
    for quantity in SIZES:
        for path in ('PHASE8','READY'):samples.append(dispatch_sample(path,'SIZE_'+str(quantity),quantity=quantity))
    for mode in ('PROTECTIVE','EMERGENCY'):
        for right in ('C','P'):samples.append(dispatch_sample('READY',mode,mode=mode,right=right))
    save()
    for scenario in ('moderate','partial','severe_volatility','disappearing','disconnect'):
        pair=[quality_sample(ready,scenario) for ready in (False,True)];quality.extend(pair)
        keys=('filled','residual','time_to_flat_seconds','realized_slippage_points','logical_submissions','cancel_requests','rejections')
        if any(pair[0][key]!=pair[1][key] for key in keys):raise AssertionError('Execution-quality mismatch: '+scenario)
        save();print('QUALITY',scenario,'identical economic outcomes',flush=True)
    profiler=cProfile.Profile();dispatch_sample('READY',profiler=profiler)
    stats=pstats.Stats(profiler)
    result['profile_top_cumulative_seconds']=[dict(file=Path(key[0]).name,line=key[1],function=key[2],calls=value[1],self_seconds=value[2],cumulative_seconds=value[3])
        for key,value in sorted(stats.stats.items(),key=lambda item:item[1][3],reverse=True)[:35]]
    result.update(summary=summary(samples),complete=True,safety_violations=sum(x['safety_violations'] for x in samples))
    p99=result['summary']['READY:WARM']['trigger_to_adapter_ms']['p99']
    result['targets']=dict(primary_ms=5,stretch_ms=2,exploratory_ms=1,primary_met=p99<=5,stretch_met=p99<=2,exploratory_met=p99<=1,
        performance_acceptance='NOT_PRODUCTION_ACCEPTANCE')
    save();print('COMPLETE, warm trigger-to-synthetic-adapter p99 ms',p99,flush=True)


if __name__=='__main__':main()
