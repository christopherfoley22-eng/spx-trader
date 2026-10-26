"""Actual durable boundary tests, never a microbenchmark acceptance shortcut."""
import unittest
from executor_readiness_benchmark import dispatch_sample,quality_sample,summary
from executor_liquidity_benchmark import SIZES


class BenchmarkTest(unittest.TestCase):
    def test_every_required_size_uses_bounded_safe_dispatch_without_claiming_fills(self):
        for quantity in SIZES:
            with self.subTest(quantity=quantity):
                row=dispatch_sample(quantity=quantity)
                self.assertLessEqual(row['reserved'],quantity);self.assertGreater(row['reserved'],0)
                self.assertEqual(row['actual_fills'],0);self.assertFalse(row['execution_enabled'])
                self.assertEqual(row['safety_violations'],0);self.assertEqual(row['prepared_orders'],(quantity+24)//25)
                self.assertGreater(row['metrics']['trigger_to_adapter_ms'],row['metrics']['trigger_to_selected_ms'])

    def test_cold_and_contended_samples_are_kept_and_reported_separately(self):
        rows=[dispatch_sample(workload=x,quantity=35) for x in ('WARM','COLD','DB_CONTENTION')]
        report=summary(rows);self.assertEqual(len(report),3)
        self.assertGreater(rows[0]['cache_hits'],0);self.assertEqual(rows[1]['cache_hits'],0)
        self.assertGreaterEqual(rows[2]['metrics']['trigger_to_adapter_ms'],5)

    def test_identical_synthetic_events_keep_execution_quality(self):
        rows=[quality_sample(ready,'moderate',35) for ready in (False,True)]
        for key in ('filled','residual','time_to_flat_seconds','realized_slippage_points','logical_submissions','cancel_requests'):
            self.assertEqual(rows[0][key],rows[1][key])


if __name__=='__main__':unittest.main()
