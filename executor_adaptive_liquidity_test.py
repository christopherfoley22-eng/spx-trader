"""Pure exit-policy evidence tests; no market/broker connection."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest
from executor_adaptive_liquidity import AdaptiveExitPolicy,DepthObservation,depth_levels,choose_slices,liquidity_trend,entry_exit_capacity_review,calendar_review
from executor_paper_broker_test import CONTRACT,NOW
from executor_portfolio_test import policy
from executor_portfolio_liquidity import LiquidityFixture
from executor_market_ingestion import OptionGreeks
from simulation_evidence import OptionObservation,SPXObservation,FeedStatus


class AdaptiveTest(unittest.TestCase):
    def setUp(self):
        self.q=LiquidityFixture('q',OptionObservation(CONTRACT,CONTRACT.con_id,12.5,12.6,FeedStatus.LIVE,NOW,100.),
            SPXObservation(6000,FeedStatus.LIVE,NOW,100.),15,100,NOW,100.,'.05',100,100,'.05','.05',None,None,None)
        self.depth=DepthObservation('d',CONTRACT,NOW,100.,1,(('12.50',15),('12.45',10),('12.40',20)))
        self.execution=dict(policy(displayed_participation='1').normalized(),tick='.05',estimated_capacity=100)

    def test_single_sweep_25_uses_second_level_without_assuming_fills(self):
        levels=depth_levels(self.depth.payload(),self.q,NOW,100.)
        result=choose_slices(25,levels,'12.50',self.execution,AdaptiveExitPolicy('v1').normalized())
        self.assertEqual(result['slices'][0]['quantity'],25)
        self.assertEqual(Decimal(result['slices'][0]['limit_price']),Decimal('12.45'))
        self.assertFalse(result['displayed_liquidity_guaranteed'])

    def test_fixed_slices_and_participation_are_explicit(self):
        result=choose_slices(25,self.depth.bids,'12.5',self.execution,AdaptiveExitPolicy('fixed',method='FIXED').normalized())
        self.assertEqual([s['quantity'] for s in result['slices']],[5]*5)
        self.execution['displayed_participation']='.5'
        result=choose_slices(25,self.depth.bids,'12.5',self.execution,AdaptiveExitPolicy('a').normalized())
        self.assertEqual(sum(s['quantity'] for s in result['slices']),22)

    def test_immutable_price_floor_and_zero_unbounded_chasing(self):
        result=choose_slices(25,(('11.00',100),),'12.5',self.execution,AdaptiveExitPolicy('a').normalized(),20)
        self.assertFalse(result['slices']);self.assertEqual(Decimal(result['floor']),Decimal('12.20'))

    def test_source_skew_missing_time_wrong_contract_depth_and_locked_evidence(self):
        for change in (dict(source_time=NOW-timedelta(seconds=.251)),dict(source_time=NOW-timedelta(seconds=2)),
                       dict(contract=replace(CONTRACT,con_id=999)),dict(bids=(('12.45',15),)),dict(bids=(('12.5',15),('12.5',10)))):
            with self.assertRaises(Exception):depth_levels(replace(self.depth,**change).payload(),self.q,NOW,100.)

    def test_forecasting_and_new_entry_gates_remain_disabled(self):
        with self.assertRaises(Exception):AdaptiveExitPolicy('bad',forecasting_enabled=True).normalized()
        a=self.depth.payload();b=replace(self.depth,evidence_id='b',sequence=2,bids=(('12.5',2),)).payload()
        self.assertEqual(liquidity_trend([a,b])['status'],'DECREASING')
        self.assertFalse(liquidity_trend([a,b])['forecast_enabled'])
        review=entry_exit_capacity_review(125,(('12.5',10),))
        self.assertFalse(review['entry_gate_active']);self.assertFalse(review['redistribution_authorized'])

    def test_calendar_requires_exact_verified_evidence_and_is_advisory(self):
        self.assertIn('UNAVAILABLE',calendar_review(CONTRACT,NOW)['status'])
        evidence=dict(contract=self.depth.payload()['contract'],verified=True,source='SYNTHETIC_CALENDAR',reference='fixture',
            open=(NOW-timedelta(hours=1)).isoformat(),close=(NOW+timedelta(minutes=5)).isoformat(),early_close=True)
        self.assertFalse(calendar_review(CONTRACT,NOW,evidence)['automatic_policy_active'])
        self.assertIn('CLOSED',calendar_review(CONTRACT,NOW+timedelta(minutes=6),evidence)['status'])

    def test_benchmark_never_counts_unresolved_cases_as_flat_or_averages_percentiles(self):
        from executor_liquidity_benchmark import summarize_results
        row=dict(method='SINGLE',filled=5,confirmed_flat=False,residual=20,logical_order_requests=1,
            cancel_requests=0,rejections=0,realized_slippage_dollars='10',time_to_flat_seconds=None,safety_violations=0,
            host_internal_latency_samples_ms={key:[1.,300.] for key in ('receipt_to_decision','decision_to_prepare','prepare_to_dispatch','coordinator_cycle')})
        report=summarize_results([row]);single=report['methods']['SINGLE']
        self.assertEqual(single['confirmed_flat'],0);self.assertEqual(single['residual'],20)
        self.assertEqual(single['realized_slippage_points'],'0.02')
        self.assertIsNone(single['confirmed_flat_latency_seconds']['median'])
        self.assertFalse(report['latency_budget']['passed']);self.assertFalse(report['broker_latency_measured'])


if __name__=='__main__':unittest.main()
