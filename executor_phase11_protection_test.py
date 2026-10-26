"""Protection obligations are durable; incomplete coverage cannot release exposure."""
from dataclasses import replace
import unittest
import executor_protection_lifecycle_test as f
from executor_execution_coordinator import UnifiedExecutionCoordinator
from executor_paper_types import PaperBrokerError


class Phase11ProtectionTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own','restart','configure','proof'):
        locals()[_name]=getattr(f.ProtectionLifecycleTest,_name)
    def test_undersized_native_fixture_is_a_gap_with_retained_obligations(self):
        self.configure();self.own()
        r=self.controller.observe_stop(self.proof(quantity=1))
        self.assertEqual(r['status'],'UNVERIFIED_OR_GAP');self.assertEqual(r['reserved'],1)
        self.assertEqual(len(self.ledger.portfolio['protection_obligations']),5)
        self.assertTrue(self.ledger.portfolio['halted'])
    def test_rejected_protection_and_restart_preserve_every_fill_obligation(self):
        self.configure();self.own()
        before=self.ledger.portfolio['protection_obligations'].copy()
        self.controller.observe_stop(self.proof(status='Inactive',enforceable_in_simulator=False))
        self.restart();self.assertEqual(self.ledger.portfolio['protection_obligations'],before)
        self.assertTrue(self.ledger.portfolio['halted']);self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_exit_all_during_pending_stop_cancel_does_not_oversell(self):
        self.configure();self.own();self.controller.observe_stop(self.proof(status='PendingCancel'))
        self.ledger.trigger_exit('eall-cancel','EMERGENCY_EXIT_ALL',True)
        actions=self.controller.reduction_actions();self.assertEqual(sum(a.get('quantity',0) for a in actions),100)
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')
    def test_conflicting_native_identity_fails_closed_without_erasing_owned(self):
        self.configure();self.own();self.controller.observe_stop(self.proof())
        before=self.ledger.positions().copy()
        with self.assertRaises(PaperBrokerError):self.controller.observe_stop(self.proof(perm_id=999))
        self.assertEqual(self.ledger.positions(),before);self.assertTrue(self.ledger.portfolio['protection_lifecycle']['uncertain'])


if __name__=='__main__':unittest.main()
