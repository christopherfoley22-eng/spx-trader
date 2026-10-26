"""Synthetic held-stop evidence, fill fencing and overlapping close prevention."""
from dataclasses import replace
import unittest
import executor_portfolio_test as fixtures
from executor_execution_coordinator import UnifiedExecutionCoordinator,SyntheticStopEvidence,unresolved_protection_orders
from executor_liquidation import GlobalLiquidationCoordinator
from executor_adaptive_liquidity import AdaptiveExitPolicy
from executor_paper_types import PaperBrokerError


class ProtectionLifecycleTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own','restart'):locals()[_name]=getattr(fixtures.PortfolioTest,_name)
    def configure(self):
        GlobalLiquidationCoordinator(self.ledger).configure(AdaptiveExitPolicy('p10'))
        self.controller=UnifiedExecutionCoordinator(self.ledger);self.controller.configure_protection('strict-v1')
    def proof(self,c=None,**kwargs):
        c=c or self.contracts[0]
        return replace(SyntheticStopEvidence(self.ledger.approval.account,self.fake.generation,c,'stop:'+str(c.con_id),2000+c.con_id,3000+c.con_id,
            self.ledger.positions().get(c.con_id,0),'6.75','Submitted',self.fake.wall,self.fake.mono,True),**kwargs)

    def test_five_partial_fills_fence_acquisition_in_same_journal_event(self):
        self.configure();actions=self.schedule();ids=self.fake.dispatch(actions)
        self.fake.match(capacity=1);self.fake.reconcile()
        self.assertTrue(self.ledger.portfolio['halted']);self.assertEqual(sum(self.ledger.positions().values()),5)
        self.assertEqual(len(self.ledger.portfolio['protection_lifecycle']['recovery']),5)
        self.assertFalse(any(a.get('side')=='BUY' for a in self.schedule()))

    def test_synthetic_coverage_requires_exact_stop_and_never_claims_real_broker_protection(self):
        self.configure();self.own();status=self.controller.observe_stop(self.proof())
        self.assertEqual(status['status'],'SYNTHETIC_COVERED');self.assertFalse(status['real_broker_protection_verified'])
        self.assertTrue(self.ledger.portfolio['halted'])  # No automatic acquisition rearm.

    def test_rejected_or_unenforceable_stop_stays_unprotected_and_reserved(self):
        self.configure();self.own()
        status=self.controller.observe_stop(self.proof(status='Inactive',enforceable_in_simulator=False))
        self.assertEqual(status['status'],'UNVERIFIED_OR_GAP');self.assertEqual(status['reserved'],25)
        self.assertTrue(self.ledger.portfolio['halted'])

    def test_competing_tactical_exits_cannot_double_sell_and_cancel_requires_evidence(self):
        self.configure();self.own();self.controller.observe_stop(self.proof())
        self.ledger.trigger_exit('panic','EMERGENCY_EXIT_ALL',True)
        actions=self.controller.reduction_actions()
        self.assertEqual(sum(a.get('quantity',0) for a in actions),100)
        self.assertTrue(any(a['type']=='CANCEL_PROTECTION_RECONCILIATION_REQUIRED' for a in actions))
        self.fake.dispatch(actions);self.fake.match(side='SELL');self.fake.reconcile()
        self.assertEqual(sum(self.ledger.positions().values()),25)
        self.controller.observe_stop(self.proof(quantity=0,status='Cancelled',terminal_reconciled=True))
        actions=self.schedule();self.fake.dispatch(actions);self.fake.match(side='SELL');self.fake.reconcile()
        self.assertEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_missing_execution_cannot_make_filled_stop_release_coverage(self):
        self.configure();self.own();self.controller.observe_stop(self.proof())
        with self.assertRaises(PaperBrokerError):self.controller.observe_stop(self.proof(quantity=0,status='Filled',terminal_reconciled=True))
        self.assertTrue(self.ledger.portfolio['protection_lifecycle']['uncertain'])
        self.assertNotEqual(self.ledger.status()['phase'],'FLAT_CONFIRMED')

    def test_duplicate_id_contradiction_and_restart_require_reverification(self):
        self.configure();self.own();self.controller.observe_stop(self.proof())
        self.controller.observe_stop(self.proof())
        self.restart();self.controller=UnifiedExecutionCoordinator(self.ledger)
        self.assertEqual(self.controller.protection_status(self.contracts[0].con_id)['status'],'UNVERIFIED_OR_GAP')
        self.assertTrue(self.ledger.portfolio['halted']);self.assertGreater(unresolved_protection_orders(self.ledger),0)
        self.fake.reconcile()
        self.assertEqual(self.controller.observe_stop(self.proof())['status'],'SYNTHETIC_COVERED')

    def test_wrong_account_or_overlapping_stops_are_durable_uncertainty(self):
        self.configure();self.own()
        with self.assertRaises(Exception):self.controller.observe_stop(self.proof(account='DU9999999'))
        self.assertTrue(self.ledger.portfolio['protection_lifecycle']['uncertain'])
        self.assertTrue(self.ledger.portfolio['adaptive_exit']['external_invalid'])


if __name__=='__main__':unittest.main()
