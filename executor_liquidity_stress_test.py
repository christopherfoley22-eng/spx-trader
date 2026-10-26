"""125-contract synthetic adverse scenarios and seeded invariant checks."""
from dataclasses import replace
import random
import tempfile
import unittest
from executor_liquidity_benchmark import run_case,ExitSimulation,SCENARIOS
from executor_liquidity_simulator import SimulationPolicy
from simulation_evidence import FeedStatus


class LiquidityStressTest(unittest.TestCase):
    def test_full_portfolio_all_adverse_scenarios_never_false_flat(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                result=run_case(125,scenario=scenario,horizon=1.2,mode='EMERGENCY')
                self.assertEqual(result['filled']+result['residual'],125)
                self.assertEqual(result['safety_violations'],0)
                self.assertEqual(result['confirmed_flat'],result['residual']==0)
                self.assertFalse(result['execution_enabled'])
                if scenario in ('severe_volatility','disappearing'):
                    self.assertGreater(result['residual'],0)
                    self.assertFalse(result['confirmed_flat'])

    def test_seeded_sizes_and_policies_preserve_quantities(self):
        rng=random.Random(810)
        for _ in range(8):
            size=rng.randint(1,125);method=rng.choice(('SINGLE','FIXED','ADAPTIVE','ADAPTIVE_REPRICE'))
            result=run_case(size,method=method,seed=rng.randrange(100),horizon=.5)
            self.assertEqual(result['filled']+result['residual'],size)
            self.assertEqual(result['safety_violations'],0)

    def test_delayed_out_of_order_reports_and_rejected_pacing_reconcile(self):
        with tempfile.TemporaryDirectory() as root:
            policy=SimulationPolicy(92,order_report_delay=.08,execution_report_delay=.01,request_capacity=2)
            sim=ExitSimulation(root,125,simulation_policy=policy)
            try:
                sim.update_books();sim.trigger('EMERGENCY')
                for index in range(12):sim.step(index)
                result=sim.report();self.assertEqual(result['safety_violations'],0)
                self.assertGreater(result['rejections'],0)
                self.assertEqual(result['filled']+result['residual'],125)
            finally:sim.close()


if __name__=='__main__':unittest.main()
