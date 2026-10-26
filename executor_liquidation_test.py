"""Independent closing boundary and concurrent five-strike plans; offline only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace,asdict
from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch
import executor_portfolio_test as fixtures
from executor_adaptive_liquidity import AdaptiveExitPolicy,DepthObservation
from executor_liquidation import GlobalLiquidationCoordinator,ClosingOrderSafety,pacing_room
from executor_paper_types import PaperBrokerError
from executor_portfolio_liquidity import fixture_from_payload


class CoordinatorTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own'):locals()[_name]=getattr(fixtures.PortfolioTest,_name)

    def enable(self,**changes):
        self.own();self.coordinator=GlobalLiquidationCoordinator(self.ledger)
        self.coordinator.configure(AdaptiveExitPolicy('test-v1',**changes))
        self.ledger.trigger_exit('exit','PROFIT_PROTECTION_FLOOR')

    def intents(self,actions):return [a for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']

    def test_five_strikes_coordinate_in_one_batch_and_two_workers_cannot_oversell(self):
        self.enable()
        with ThreadPoolExecutor(max_workers=2) as pool:batches=list(pool.map(lambda _:self.schedule(),range(2)))
        actions=self.intents(batches[0]);self.assertEqual(len(actions),5)
        self.assertEqual(len({a['contract_id'] for a in actions}),5)
        self.assertEqual(len({a['intent_id'] for b in batches for a in self.intents(b)}),5)
        self.fake.dispatch(actions)
        with self.assertRaises(PaperBrokerError):self.ledger.dispatch_batch(self.fake.generation,[a['intent_id'] for a in actions])

    def test_no_optimizer_authority_before_exit_or_for_buy_requests(self):
        self.own();c=GlobalLiquidationCoordinator(self.ledger);c.configure(AdaptiveExitPolicy('v'))
        self.assertFalse(any(a.get('side')=='SELL' for a in c.plan()))
        self.ledger.trigger_exit('exit','INITIAL_STOP');actions=self.intents(c.plan())
        candidate=dict(self.ledger.state['intents'][actions[0]['intent_id']],side='BUY')
        with self.assertRaises(PaperBrokerError):ClosingOrderSafety.batch(self.ledger,[candidate])

    def test_pending_cancel_keeps_full_reservation_and_replacement_is_bounded(self):
        self.enable(method='ADAPTIVE_REPRICE');actions=self.schedule();self.fake.dispatch(actions)
        self.fake.advance(.6);self.quotes();actions=self.schedule()
        self.assertEqual(sum(a['type']=='CANCEL_REQUIRED' for a in actions),5)
        self.assertFalse(self.intents(actions))
        for c in self.contracts:self.assertEqual(self.ledger.reservations('SELL',c.con_id),25)
        self.fake.cancel_actions(actions)
        self.assertEqual(len(self.intents(self.schedule())),5)

    def test_protective_orders_block_conflicting_exits_without_cancellation(self):
        self.enable();engine=self.ledger
        proof=dict(origin='OFFLINE_FIXTURE',complete=True,account=engine.approval.account,generation=self.fake.generation,
            observed_at=self.fake.wall.isoformat(),received_monotonic=self.fake.mono,
            positions={str(k):v for k,v in engine.positions().items()},orders=[dict(order_ref='external:'+str(c.con_id),contract=asdict(c),side='SELL',remaining=25,terminal_reconciled=False) for c in self.contracts])
        self.coordinator.observe_external_closings(proof)
        self.assertFalse(self.intents(self.schedule()))
        self.assertFalse(any(a['type']=='CANCEL_REQUIRED' for a in self.schedule()))
        with self.assertRaises(Exception):self.coordinator.observe_external_closings(dict(proof,account='DU9999999'))
        self.assertFalse(self.intents(self.schedule()))

    def test_stale_depth_falls_back_to_real_bbo_and_greeks_not_invented(self):
        self.enable()
        for c in self.contracts:
            self.coordinator.observe_depth(DepthObservation('depth:'+str(c.con_id),c,self.fake.wall,self.fake.mono,1,(('9.9',100),('9.85',100))))
        self.fake.advance(.5);self.quotes(greeks=None,greeks_source_time=None,greeks_received_monotonic=None)
        actions=self.intents(self.schedule());self.assertEqual(len(actions),5)
        self.assertTrue(all(self.ledger.state['intents'][a['intent_id']]['depth_evidence_id'] is None for a in actions))

    def test_partial_external_protection_reserves_coverage_and_changed_position_requires_new_proof(self):
        self.enable();engine=self.ledger;c=self.contracts[0]
        proof=dict(origin='OFFLINE_FIXTURE',complete=True,account=engine.approval.account,generation=self.fake.generation,
            observed_at=self.fake.wall.isoformat(),received_monotonic=self.fake.mono,
            positions={str(k):v for k,v in engine.positions().items()},orders=[dict(order_ref='covered-ten',
                contract=asdict(c),side='SELL',remaining=10,terminal_reconciled=False)])
        self.coordinator.observe_external_closings(proof)
        actions=self.intents(self.schedule())
        self.assertEqual(sum(a['quantity'] for a in actions),115)
        self.fake.dispatch(actions);self.fake.match(side='SELL');self.fake.reconcile()
        self.assertEqual(engine.positions(),{c.con_id:10})
        self.assertFalse(self.intents(self.schedule()))
        self.assertEqual(engine.portfolio['adaptive_exit']['last_condition'],'CRITICAL_RECONCILIATION_REQUIRED')

    def test_dispatch_rechecks_quotes_identities_and_independent_quantity_limits(self):
        self.enable();actions=self.intents(self.schedule());keys=[a['intent_id'] for a in actions]
        self.quotes()
        with self.assertRaises(PaperBrokerError):self.ledger.dispatch_batch(self.fake.generation,keys)
        self.assertFalse(any(self.ledger.state['intents'][k]['order_id'] is not None for k in keys))
        # A fresh schedule abandons the obsolete plan, retaining owned exposure.
        self.schedule();self.fake.reconcile()
        self.assertTrue(self.intents(self.schedule()))

    def test_emergency_has_reserved_pacing_capacity_without_resetting_budget(self):
        self.enable(requests_per_window=6,emergency_reserve=2)
        normal=self.intents(self.schedule());self.assertEqual(len(normal),4)
        self.fake.dispatch(normal);self.assertEqual(pacing_room(self.ledger),0)
        self.fake.advance(-.1);self.assertEqual(pacing_room(self.ledger),0);self.fake.advance(.1)
        self.ledger.trigger_exit('panic','EMERGENCY_EXIT_ALL',True)
        self.assertEqual(pacing_room(self.ledger),2)
        self.assertEqual(len(self.intents(self.schedule())),1)

    def test_disk_failure_prevents_unjournaled_plans(self):
        self.enable()
        with patch.object(self.ledger,'_append',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.schedule()
        self.assertFalse(any(i['side']=='SELL' for i in self.ledger.state['intents'].values()))

    def test_independent_boundary_rejects_optimizer_overstating_liquidity(self):
        self.enable();self.quotes(bid_size=10)
        actions=self.intents(self.schedule());self.assertTrue(actions)
        self.fake.reconcile()
        candidate=dict(self.ledger.state['intents'][actions[0]['intent_id']],quantity=10)
        with self.assertRaisesRegex(PaperBrokerError,'LIQUIDITY_CAPACITY'):
            ClosingOrderSafety.batch(self.ledger,[candidate])


if __name__=='__main__':unittest.main()
