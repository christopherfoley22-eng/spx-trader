"""Diagnostic only: actual offline auth/lifecycle/event worker, using test fixtures.

Never imported by application services. No broker transport or enabled execution.
"""
import argparse
import json
from pathlib import Path
import platform
from threading import Event
import time
from unittest.mock import patch
from executor_exit_stream_test import ExitStreamTest
from executor_exit_stream import OfflineProtectiveStream
from executor_liquidity_simulator import distribution


def sample():
    fixture=ExitStreamTest();fixture.setUp();stream=None
    try:
        result=fixture.enter();fixture.configure(result);fixture.fill(result,25)
        fixture.lifecycle.enable_exit_readiness(fixture.service_token,result['trade_id'])
        stream=OfflineProtectiveStream(fixture.lifecycle,fixture.service_token,result['trade_id'])
        engine=fixture.engine(result);peak_done=Event();dispatch_done=Event();times={}
        original_observe=fixture.lifecycle.observe_spx;original_append=engine._append;original_bind=fixture.fixture.bindings
        def observed(*args):
            out=original_observe(*args)
            if args[2]=='worker-peak':peak_done.set()
            return out
        def append(identity,kind,data):
            if kind=='PORT_SPX' and data.get('reason'):times.setdefault('recognized',time.perf_counter_ns())
            return original_append(identity,kind,data)
        def bind(*args):
            out=original_bind(*args);times['dispatch']=time.perf_counter_ns();dispatch_done.set();return out
        with patch.object(fixture.lifecycle,'observe_spx',observed),patch.object(engine,'_append',append),patch.object(fixture.fixture,'bindings',bind):
            stream.start_worker();stream.enqueue(fixture.event(6050,1,'worker-peak'))
            if not peak_done.wait(5):raise AssertionError('Worker peak processing unresolved')
            times['receipt']=time.perf_counter_ns();stream.enqueue(fixture.event(6047,2,'worker-trigger'))
            if not dispatch_done.wait(5):raise AssertionError('Worker dispatch unresolved')
        stream.stop_worker()
        assert sum(engine.positions().values())==125
        reserved=sum(engine.reservations('SELL',cid) for cid in engine.positions())
        assert reserved==125 and not fixture.lifecycle._trade_status(result['trade_id'])['reconciled_flat']
        return dict(origin='SYNTHETIC_ONLY',quantity=125,reserved=reserved,actual_fills=0,safety_violations=0,
            receipt_to_trigger_ms=(times['recognized']-times['receipt'])/1e6,
            trigger_to_adapter_ms=(times['dispatch']-times['recognized'])/1e6,
            receipt_to_adapter_ms=(times['dispatch']-times['receipt'])/1e6)
    finally:
        if stream:stream.stop_worker()
        fixture.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);parser.add_argument('--repeats',type=int,default=5)
    args=parser.parse_args();rows=[sample() for _ in range(args.repeats)]
    report=dict(origin='SYNTHETIC_ONLY',runtime=platform.python_version(),platform=platform.platform(),
        samples=rows,summary={key:distribution([r[key] for r in rows]) for key in ('receipt_to_trigger_ms','trigger_to_adapter_ms','receipt_to_adapter_ms')},
        real_ibkr_measured=False,execution_enabled=False,safety_violations=sum(r['safety_violations'] for r in rows))
    Path(args.output).write_text(json.dumps(report,sort_keys=True,indent=2)+'\n');print(json.dumps(report['summary']))


if __name__=='__main__':main()
