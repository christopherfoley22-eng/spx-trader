"""Final active Mortificatio rules, synthetic observations and fake broker only."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import unittest

import executor_premium_stop_test as premium
import executor_protective_exits_test as protective
from executor_dry_run import DryRunError
from executor_paper_broker import PaperBrokerLedger
from executor_paper_broker_test import approval
from executor_protective_exits import ProtectiveExitManager
from executor_protective_service import LocalProtectiveExitService
from simulation_evidence import EvidenceError, FeedStatus


class ActiveControllerTest(unittest.TestCase):
    # Reuse evidence factories without collecting their separate test suites.
    for _name in ('setUp', 'setup_trade', 'snapshot', 'fill', 'open', 'risk', 'spx_move', 'restart'):
        locals()[_name] = getattr(premium.PremiumStopTest, _name)

    def test_early_protection_boundaries_symmetrical(self):
        for direction in ('CALL', 'PUT'):
            for peak in (4, 4.9):
                with self.subTest(direction=direction, peak=peak):
                    self.setup_trade(direction, name=direction+str(peak))
                    self.open()
                    self.assertEqual(self.spx_move(peak), 'OPEN')
                    self.assertTrue(self.engine.status().profit_protection)
                    self.assertEqual(self.spx_move(1.25001), 'OPEN')
                    self.assertEqual(self.spx_move(1.25), 'EXITING')
                    self.assertEqual(self.engine.status().exit_reason, 'PROFIT_PROTECTION_FLOOR')

    def test_unlimited_peaks_and_exact_three_point_reversal(self):
        for direction in ('CALL', 'PUT'):
            for final_peak in (5, 10, 20, 50):
                with self.subTest(direction=direction, peak=final_peak):
                    self.setup_trade(direction, name=direction+str(final_peak))
                    self.open()
                    for peak in (x for x in (4, 4.9, 5, 10, 20, 50) if x <= final_peak):
                        self.assertEqual(self.spx_move(peak), 'OPEN')
                        if peak >= 5:
                            self.assertTrue(self.engine.status().ride)
                            self.assertFalse(self.engine.status().profit_protection)
                            self.assertEqual(self.spx_move(peak-2.999), 'OPEN')
                    self.assertEqual(self.spx_move(final_peak-3), 'EXITING')
                    self.assertEqual(self.engine.status().exit_reason, 'LET_IT_RIDE_REVERSAL')
                    self.assertEqual(self.engine.status().owned, 10)

    def test_partial_peak_persists_through_restart_and_completion(self):
        for direction in ('CALL', 'PUT'):
            self.setup_trade(direction, name=direction)
            self.fill(4, price=10)
            self.assertEqual(self.spx_move(20), 'ENTERING')
            expected = self.engine.status().peak
            self.restart()
            self.assertEqual(self.engine.status().peak, expected)
            self.assertTrue(self.engine.status().ride)
            self.fill(6, price=9)
            self.engine.finish_entry(self.snapshot(), self.mono)
            self.assertEqual(self.engine.status().peak, expected)
            self.assertTrue(self.engine.status().ride)
            self.assertEqual(Decimal(self.engine.status().initial_stop_premium), Decimal('6.15'))
            self.assertEqual(self.spx_move(17), 'EXITING')

    def test_partial_entry_profit_exit_halts_all_acquisition(self):
        for direction in ('CALL', 'PUT'):
            for peak, reversal in ((4, 1.25), (5, 2), (50, 47)):
                self.setup_trade(direction, name=direction+str(peak))
                self.fill(4)
                self.spx_move(peak)
                self.restart()
                self.assertEqual(self.spx_move(reversal), 'EXITING')
                self.assertEqual(self.engine.db.execute("SELECT count(*) FROM chunks WHERE side='ENTRY' AND terminal=0").fetchone()[0], 0)
                self.assertEqual(self.engine.status().owned, 4)
                with self.assertRaises((DryRunError, IndexError)):
                    self.fill(1)

    def test_premium_stop_stays_active_after_trailing_and_first_exit_is_latched(self):
        for first in ('premium', 'spx'):
            self.setup_trade(name=first)
            self.fill(4)
            self.spx_move(20)
            if first == 'premium':
                self.risk(6.75)
                self.spx_move(17)
                expected = 'INITIAL_STOP'
            else:
                self.spx_move(17)
                self.risk(6.75)
                expected = 'LET_IT_RIDE_REVERSAL'
            self.assertEqual(self.engine.status().exit_reason, expected)
            self.assertEqual(sum(kind == 'EXIT_TRIGGERED' for _, kind, _ in self.engine.journal()), 1)
            self.assertEqual(self.engine.status().owned, 4)

    def test_duplicate_partial_spx_is_idempotent_changed_duplicate_rejected(self):
        self.setup_trade()
        self.fill(4)
        observation = replace(self.spx, price=self.spx.price+20)
        args = (self.snapshot(), self.wall, self.mono)
        self.engine.observe('same', observation, *args)
        self.restart()
        before = self.engine.journal()
        self.engine.observe('same', observation, *args)
        self.assertEqual(self.engine.journal(), before)
        with self.assertRaises(DryRunError):
            self.engine.observe('same', replace(observation, price=observation.price-3), *args)

    def test_partial_entry_replay_and_process_crash_retain_peak(self):
        worker = Path(__file__).with_name('executor_replay_process_worker.py')
        for direction in ('CALL', 'PUT'):
            raw = premium.make_session('partial-'+direction, direction, [20, 17],
                                       quantity=10, fill_mode='SCRIPTED')
            for index, side in ((0, 'ENTRY'), (2, 'EXIT')):
                raw['events'][index]['fills'].append({
                    'event_id': side+direction, 'side': side, 'chunk_index': 0,
                    'quantity': 4, 'rejected': False})
            session = premium.ReplaySession.from_dict(raw)
            replay = premium.ReplayRunner(session).run(restart_after_actions=True)
            self.assertEqual(replay['exit_trigger']['reason'], 'LET_IT_RIDE_REVERSAL')
            self.assertEqual(sum(f['quantity'] for f in replay['entry_fills']), 4)
            source = self.root / (direction+'.json')
            source.write_text(json.dumps(raw))
            for checkpoint in ('0:entry_committed:0', '1:option_risk', '2:exit_committed:0'):
                db = self.root / (direction+checkpoint.replace(':', '-')+'.sqlite')
                args = [sys.executable, '-B', str(worker), str(source), str(db)]
                killed = subprocess.run(args+['--stop', checkpoint], capture_output=True, text=True, timeout=20)
                self.assertEqual(killed.returncode, 73, killed.stderr)
                resumed = subprocess.run(args, capture_output=True, text=True, timeout=20)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                result = json.loads(resumed.stdout)
                self.assertEqual(result['phase'], 'FLAT')
                self.assertEqual(result['exit_reason'], 'LET_IT_RIDE_REVERSAL')
                self.assertEqual(sum(f['quantity'] for f in result['entry_fills']), 4)
                self.assertEqual(sum(f['quantity'] for f in result['exit_fills']), 4)

    def test_invalid_partial_market_cannot_advance_peak(self):
        self.setup_trade()
        self.fill(4)
        for changes in ({'status': FeedStatus.DELAYED}, {'price': float('nan')}, {'received_monotonic': self.mono-2}):
            with self.assertRaises(EvidenceError):
                self.engine.observe('bad', replace(self.spx, price=5020, **changes) if 'price' not in changes else replace(self.spx, **changes),
                                    self.snapshot(), self.wall, self.mono)
        self.assertFalse(self.engine.status().ride)


class ActiveBrokerModelTest(unittest.TestCase):
    for _name in ('setUp', 'fresh', 'filled', 'protected', 'spx'):
        locals()[_name] = getattr(protective.ProtectiveExitTest, _name)

    def test_partial_trail_recovery_late_fill_and_cancel_before_liquidate(self):
        for right in ('C', 'P'):
            self.fresh(right)
            self.filled(4)
            stop = self.protected()
            self.spx(50)
            peak = self.manager.state['peak']
            self.ledger.close()
            self.ledgers.remove(self.ledger)
            self.ledger = PaperBrokerLedger(self.path, approval())
            self.ledgers.append(self.ledger)
            self.fake.ledger = self.ledger
            self.ledger.wall_clock, self.ledger.monotonic_clock = lambda: self.fake.wall, lambda: self.fake.mono
            self.manager = ProtectiveExitManager(self.ledger, self.latch)
            self.fake.start()
            self.assertTrue(self.fake.reconcile()['matched'])
            self.assertEqual(self.manager.state['peak'], peak)
            self.assertTrue(self.manager.state['ride'])
            self.spx(47)
            actions = self.manager.advance()
            self.assertEqual([a['intent_id'] for a in actions], ['buy'])
            self.assertEqual(self.fake.orders[stop].status, 'Submitted')
            self.filled(2)  # Actual late broker evidence, never a new authorization.
            self.fake.cancellations(actions)
            self.fake.cancellations(self.manager.advance())
            full = self.fake.acknowledge(self.manager.advance()[0])
            self.assertEqual(self.ledger.state['intents'][full]['quantity'], 6)
            self.fake.execution(full, 6)
            self.assertNotEqual(self.manager.status()['phase'], 'FLAT CONFIRMED')
            self.assertTrue(self.fake.reconcile()['matched'])
            self.assertEqual(self.manager.advance()[0]['type'], 'FLAT_CONFIRMED')
            self.assertEqual(self.ledger.positions(), {})

    def test_ui_reports_only_evidenced_strategy_protection(self):
        self.filled(4)
        service = LocalProtectiveExitService(self.manager)
        position = service.status()['position']
        self.assertEqual(Decimal(position['initial_stop_premium']), Decimal('6.75'))
        self.assertFalse(position['early_protection_active'])
        self.spx(4)
        self.assertTrue(service.status()['position']['early_protection_active'])
        self.spx(5)
        position = service.status()['position']
        self.assertFalse(position['early_protection_active'])
        self.assertTrue(position['trailing_active'])
        self.assertEqual(position['trailing_threshold_spx'], '6002')
        self.spx(50)
        self.assertEqual(service.status()['position']['trailing_threshold_spx'], '6047')

    def test_recovered_due_research_target_cannot_sell_position(self):
        self.fresh(targets=({'id': 'legacy', 'spx_points': 2, 'quantity': 3},))
        self.filled()
        stop = self.protected()
        self.manager.state['targets'][0]['due'] = True
        self.spx(20)
        self.assertEqual(self.manager.advance()[0]['intent_id'], stop)
        self.assertFalse(any(i.get('purpose') == 'PROFIT_PARTIAL' for i in self.ledger.state['intents'].values()))


if __name__ == '__main__':
    unittest.main()
