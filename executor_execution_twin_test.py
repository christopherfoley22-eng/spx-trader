"""Deterministic economic results and no invented protection/fills."""
from decimal import Decimal
import tempfile
import unittest
from executor_execution_twin import run_case,ExecutionTwin


class ExecutionTwinTest(unittest.TestCase):
    def test_25_and_125_entry_to_reconciled_flat_and_actual_commissions(self):
        for quantity in (25,125):
            result=run_case(quantity,scenario='deep')
            self.assertEqual(result['candidate_count'],10);self.assertEqual(result['acquired'],quantity)
            self.assertTrue(result['confirmed_flat']);self.assertEqual(result['residual'],0)
            self.assertEqual(Decimal(result['commissions_dollars']),Decimal(quantity)*Decimal('1.30'))
            self.assertEqual(Decimal(result['realized_net_pnl_dollars']),Decimal(result['exit_proceeds_dollars'])-Decimal(result['entry_premium_dollars'])-Decimal(result['commissions_dollars']))

    def test_disappearing_liquidity_never_claims_flat_or_native_protection(self):
        result=run_case(125,scenario='disappearing')
        self.assertGreater(result['residual'],0);self.assertFalse(result['confirmed_flat'])
        self.assertIsNone(result['realized_net_pnl_dollars'])
        self.assertFalse(result['assumptions']['native_protection_implemented']);self.assertFalse(result['execution_enabled'])

    def test_seeded_economic_results_and_simple_baseline_comparison(self):
        first=run_case(25,tactic='SINGLE',scenario='thin',seed=3)
        second=run_case(25,tactic='SINGLE',scenario='thin',seed=3)
        for key in ('acquired','closed','residual','entry_premium_dollars','exit_proceeds_dollars','commissions_dollars'):
            self.assertEqual(first[key],second[key])
        self.assertEqual(first['safety_violations'],0)

    def test_halt_and_session_end_retain_residual_exposure(self):
        for scenario in ('halt','session_end'):
            result=run_case(125,scenario=scenario)
            self.assertGreater(result['residual'],0);self.assertFalse(result['confirmed_flat'])
            self.assertIsNone(result['realized_net_pnl_dollars'])

    def test_lost_execution_callback_keeps_order_uncertainty_and_blocks_false_flat(self):
        with tempfile.TemporaryDirectory() as root:
            twin=ExecutionTwin(root,125)
            try:
                s=twin.sim;e=s.engine;twin.market_update(0)
                actions=e.schedule(e.state['generation'],s.funds());s.sync()
                ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
                e.dispatch_batch(e.state['generation'],ids,s.funds());s.source.bindings(s.trade,e,ids)
                s.source.advance(.1);s.source.match()
                with s.source.db() as db:
                    db.execute("DELETE FROM sim_events WHERE kind='DELIVER' AND payload LIKE '%\"kind\":\"EXECUTION\"%'")
                s.source.advance(.02);self.assertFalse(s.sync())
                self.assertFalse(e.positions());self.assertTrue(e.status()['entry_blocked'])
                self.assertNotEqual(e.status()['phase'],'FLAT_CONFIRMED')
                self.assertEqual(sum(e.reservations('BUY',c.con_id) for c in s.contracts),125)
            finally:twin.sim.close()


if __name__=='__main__':unittest.main()
