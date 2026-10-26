"""Durable fake SDK observations, not a verified broker connection."""
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import patch
import json,unittest
import executor_ibkr_read_only_test as f
from executor_broker_lifecycle_capture import BrokerLifecycleValidation
from ibkr_phase12_read_only import LifecycleReadOnlyProbe,run_lifecycle_read_only
from simulation_evidence import EvidenceError


class LifecycleCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'capture.sqlite'
        a,s=f.config();self.v=BrokerLifecycleValidation(a,s,lambda:f.NOW,lambda:100.,capture_path=self.path)
        self.p=LifecycleReadOnlyProbe(self.v);self.p.managedAccounts(f.ACCOUNT+',');self.p.nextValidId(7)
    def execution(self,id='fill.01',quantity=1):
        c=Obj(conId=101,symbol='SPX',secType='OPT',right='C',lastTradeDateOrContractMonth='20261023',tradingClass='SPXW',strike=6000.,multiplier='100',currency='USD')
        e=Obj(execId=id,acctNumber=f.ACCOUNT,orderRef='fixture-ref',orderId=1000,permId=2000,clientId=3,side='BOT',shares=quantity,price=10.,time='20261023  14:30:00 UTC',pendingPriceRevision=False)
        return c,e
    def test_duplicate_exec_callback_has_one_durable_fact(self):
        c,e=self.execution();self.p.execDetails(1,c,e);self.p.execDetails(1,c,e)
        rows=self.v.report()['lifecycle_capture']['observations'];self.assertEqual(len(rows),1)
        self.assertFalse(json.loads(rows[0]['payload'])['economic_application_permitted'])
        c,e=self.execution(quantity=2)
        with self.assertRaises(EvidenceError):self.v.execution_details(1,c,e)
        self.assertEqual(self.v.report()['lifecycle_capture']['unresolved_conflicts'],1)
    def test_out_of_order_correction_families_are_retained_without_guessing_economics(self):
        for id,quantity in (('fill.02',2),('fill.01',1)):
            c,e=self.execution(id,quantity);self.p.execDetails(1,c,e)
        report=self.v.report()['lifecycle_capture'];self.assertEqual(report['execution_families']['fill'],['fill.02','fill.01'])
        self.assertFalse(report['covers_all_local_intents'])
    def test_order_status_before_open_order_does_not_invent_source_account(self):
        self.p.orderStatus(1000,'Submitted',0,1,0,2000,0,0,3,'')
        row=json.loads(self.v.report()['lifecycle_capture']['observations'][0]['payload'])
        self.assertIsNone(row['source_account']);self.assertIsNone(row['source_timestamp'])
    def test_wrong_account_never_enters_capture(self):
        c,e=self.execution();e.acctNumber='U999999'
        with self.assertRaises(EvidenceError):self.v.execution_details(1,c,e)
        self.assertFalse(self.v.report()['lifecycle_capture']['observations'])
    def test_restart_retains_private_capture_and_detects_tampering(self):
        c,e=self.execution();self.v.execution_details(1,c,e)
        a,s=f.config();v=BrokerLifecycleValidation(a,s,lambda:f.NOW,lambda:100.,capture_path=self.path)
        v.managed_accounts(f.ACCOUNT+',');v.handshake(7)
        self.assertEqual(len(v.report()['lifecycle_capture']['observations']),1)
        with v.capture.database() as db:db.execute("UPDATE broker_observations SET payload='{}'")
        with self.assertRaises(EvidenceError):v.report()
    def test_history_request_requires_separate_opt_in_and_account_bound_filter(self):
        with patch.object(self.p,'reqExecutions') as request:
            with self.assertRaises(EvidenceError):self.p.request_execution_history(31000)
            request.assert_not_called()
            self.v.started=True
            self.p.request_execution_history(31000,authorized=True)
            self.assertEqual(request.call_args[0][1].acctCode,f.ACCOUNT)
            self.p.execDetailsEnd(31000)
            self.assertFalse(self.v.report()['lifecycle_capture']['covers_all_local_intents'])
    def test_mutations_and_unapproved_connection_remain_disabled(self):
        for name in ('placeOrder','cancelOrder','reqGlobalCancel','exerciseOptions','reqAutoOpenOrders'):
            with self.assertRaises(RuntimeError):getattr(self.p,name)()
        with patch.object(f.transport.ReadOnlyProbe,'connect') as connect:
            with self.assertRaises(EvidenceError):run_lifecycle_read_only(self.v)
            connect.assert_not_called()


if __name__=='__main__':unittest.main()
