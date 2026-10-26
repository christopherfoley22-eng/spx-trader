"""Event-driven synthetic fills, sizes and durable failure replay. No network."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from executor_liquidity_benchmark import ExitSimulation,run_case,SIZES,sequential_fixture_quantities
from executor_liquidity_simulator import SimulationPolicy,EventDrivenBroker
from executor_portfolio import OfflinePortfolioLedger
from executor_liquidation import GlobalLiquidationCoordinator


class SimulationTest(unittest.TestCase):
    def make(self,quantity=35,**kwargs):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        simulation=ExitSimulation(tmp.name,quantity,**kwargs);self.addCleanup(lambda:simulation.engine.close())
        return simulation

    def test_every_required_size_uses_sequential_targets_and_reconciles(self):
        for size in SIZES:
            with self.subTest(size=size):
                result=run_case(size,scenario='deep',horizon=1.)
                self.assertEqual(result['filled'],size,result)
                self.assertTrue(result['confirmed_flat'],result)
                quantities=sequential_fixture_quantities(size)
                self.assertEqual(sum(quantities),size);self.assertTrue(all(0<=q<=25 for q in quantities))
                for a,b in zip(quantities,quantities[1:]):self.assertTrue(a==25 or b==0)

    def test_same_seed_same_economic_results_without_host_clock_claims(self):
        first=run_case(60,seed=18);second=run_case(60,seed=18)
        for key in ('filled','residual','time_to_flat_seconds','realized_slippage_points','logical_submissions','cancel_requests'):
            self.assertEqual(first[key],second[key],key)
        self.assertEqual(first['origin'],'SYNTHETIC_ONLY');self.assertFalse(first['real_ibkr_measured'])

    def test_put_and_call_equivalent_owned_exposure_is_closed(self):
        for right in ('C','P'):
            result=run_case(35,right=right,horizon=1.,mode='EMERGENCY')
            self.assertTrue(result['confirmed_flat']);self.assertEqual(result['filled'],35)

    def test_displayed_liquidity_can_be_zero_actual_execution(self):
        sim=self.make(25);sim.update_books();sim.trigger()
        actions=sim.engine.schedule(sim.engine.state['generation']);sim.sync()
        keys=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        sim.engine.dispatch_batch(sim.engine.state['generation'],keys);sim.source.bindings(sim.trade,sim.engine,keys)
        sim.source.advance(.1);sim.sync()
        sim.source.sim_policy['realization']='0'  # Explicit independent synthetic matching condition.
        for c in sim.contracts:sim.source.set_book(c,(('9.9',200),),'ghost:'+str(c.con_id),withdraw=True)
        before=sum(sim.engine.positions().values());sim.source.match();sim.source.advance(.05);sim.sync()
        self.assertEqual(sum(sim.engine.positions().values()),before)
        self.assertIsNone(sim.flat_at)

    def test_delayed_ack_does_not_authorize_duplicate_submission(self):
        sim=self.make(125,scenario='delayed_ack');sim.update_books();sim.trigger()
        for index in range(3):sim.step(index)
        with sim.source.db() as db:refs=db.execute("SELECT event_id FROM sim_events WHERE kind='SUBMIT'").fetchall()
        self.assertEqual(len(refs),5)
        self.assertTrue(all(i['side']=='BUY' or i['quantity']<=25 for i in sim.engine.state['intents'].values()))

    def test_restart_with_unresolved_orders_uses_durable_book_not_resubmission(self):
        sim=self.make(35,scenario='delayed_ack');sim.update_books();sim.trigger();sim.step(0)
        refs=[i['order_ref'] for i in sim.engine.state['intents'].values() if i['side']=='SELL']
        sim.engine.close();sim.engine=OfflinePortfolioLedger(sim.root/'portfolio.sqlite',sim.approval,sim.execution,sim.clock.wall,sim.clock.mono)
        sim.source=EventDrivenBroker(sim.root/'broker.sqlite',sim.account,sim.clock,SimulationPolicy(**sim.source.sim_policy))
        sim.coordinator=GlobalLiquidationCoordinator(sim.engine);sim.verify()
        for index in range(1,12):
            sim.step(index)
            if sim.flat_at is not None:break
        self.assertTrue(sim.flat_at is not None)
        for ref in refs:
            with sim.source.db() as db:
                submitted=[json.loads(row[0]) for row in db.execute("SELECT payload FROM sim_events WHERE kind='SUBMIT'")]
            self.assertEqual(sum(x['intent']['order_ref']==ref for x in submitted),1)
        sim.check()  # Additional slices may close genuinely uncommitted residuals.

    def test_market_effect_disk_failure_replays_stable_fill_ids_once(self):
        sim=self.make(25);sim.update_books();sim.trigger()
        # Prepare and acknowledge a close without matching yet.
        actions=sim.engine.schedule(sim.engine.state['generation']);sim.sync()
        ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        sim.engine.dispatch_batch(sim.engine.state['generation'],ids);sim.source.bindings(sim.trade,sim.engine,ids)
        sim.source.advance(.1);sim.sync()
        with patch.object(sim.source,'fill',side_effect=OSError('broker store unavailable')):
            with self.assertRaises(OSError):sim.source.match()
        with sim.source.db() as db:self.assertTrue(db.execute('SELECT 1 FROM sim_effects WHERE done=0').fetchone())
        sim.source.recover_effects();sim.source.recover_effects();sim.source.advance(.1);sim.sync();sim.check()
        with sim.source.db() as db:
            ids=db.execute('SELECT exec_id FROM fixture_executions').fetchall()
            self.assertEqual(len(ids),len(set(ids)))

    def test_callback_event_commit_gap_is_idempotent(self):
        sim=self.make(1);before=sim.source.state()['submissions'];sim.update_books();sim.trigger()
        actions=sim.engine.schedule(sim.engine.state['generation']);sim.sync()
        ids=[a['intent_id'] for a in actions if a['type']=='SIMULATED_LIMIT_INTENT']
        sim.engine.dispatch_batch(sim.engine.state['generation'],ids);sim.source.bindings(sim.trade,sim.engine,ids)
        original=sim.source._handle
        def crash(*args):original(*args);raise OSError('after broker commit before event acknowledgment')
        with patch.object(sim.source,'_handle',side_effect=crash):
            with self.assertRaises(OSError):sim.source.advance(.02)
        sim.source.advance(.1);sim.sync()
        self.assertEqual(sim.source.state()['submissions'],before+1)


if __name__=='__main__':unittest.main()
