"""Offline adversarial account/broker snapshot-cycle and restart tests."""

import json
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from executor_broker_snapshot import (BROKER_COMPLETION_COHERENCE_SECONDS,
                                      MAX_AGE_SECONDS, ReadOnlySnapshotMachine,
                                      SnapshotSafetyError, SUMMARY_TAGS)
from executor_dry_run import DryRunExecutor
from executor_live_observation_service import LocalReadOnlyObservationService
from executor_replay import ReplaySession
from executor_replay_process import DurableReplayRunner
from executor_replay_sessions import ACCOUNT, make_session


def blocked(action):
    try:
        action()
    except SnapshotSafetyError:
        return
    raise AssertionError("Unsafe snapshot transition accepted")


class SnapshotCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "executor.sqlite"
        controller = DryRunExecutor(self.local, ACCOUNT, lambda _: True)
        controller.close()
        self.path = self.root / "snapshot.json"
        self.machine = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(self.machine.close)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))

    def cycle(self, complete=("SUMMARY", "POSITIONS", "ORDERS"),
              times=(100.10, 100.12, 100.14), position=None, order=False):
        cycle_id = self.machine.begin_cycle(self.generation, 100.0)
        for name, finished in zip(("SUMMARY", "POSITIONS", "ORDERS"), times):
            self.machine.begin_component(self.generation, cycle_id, name, finished - 0.01)
            if name == "SUMMARY":
                for tag in SUMMARY_TAGS:
                    self.machine.observation(self.generation, cycle_id, name,
                                             ACCOUNT, tag, ("10000", "USD"), finished - 0.005)
            if name == "POSITIONS" and position:
                self.machine.observation(self.generation, cycle_id, name,
                                         ACCOUNT, position[0], position[1], finished - 0.005)
            if name == "ORDERS" and order:
                self.machine.observation(self.generation, cycle_id, name,
                                         ACCOUNT, 10, 101, finished - 0.005)
            if name in complete:
                self.machine.complete_component(self.generation, cycle_id, name, finished)
        return cycle_id

    def test_clean_cycle_repeat_and_read_only_status(self):
        first = self.cycle()
        result = self.machine.status(100.2)
        self.assertEqual(result["reconciliation"], "MATCHED_FLAT")
        self.assertFalse(result["block_new_entry"])
        self.assertEqual(result["order_visibility"], "API_VISIBLE_ONLY")
        self.assertEqual(result, self.machine.status(100.2))
        self.assertNotIn(ACCOUNT, json.dumps(result) + self.path.read_text())
        self.assertFalse(hasattr(self.machine, "placeOrder"))
        second = self.machine.begin_cycle(self.generation, 101.0)
        self.assertNotEqual(first, second)
        self.assertTrue(self.machine.status(101.0)["block_new_entry"])
        blocked(lambda: self.machine.complete_component(self.generation, first, "SUMMARY", 101.1))
        self.assertTrue(self.machine.status(101.1)["recovery_required"])
        service = LocalReadOnlyObservationService(snapshot_machine=self.machine)
        self.assertFalse(service.status()["ready"])
        self.assertFalse(service.status()["snapshot"]["executable"])

    def test_each_missing_completion_blocks(self):
        for missing in ("SUMMARY", "POSITIONS", "ORDERS"):
            with self.subTest(missing=missing):
                self.machine.disconnect(self.generation)
                self.generation = self.machine.connect()
                self.machine.managed_accounts(self.generation, (ACCOUNT,))
                self.cycle(complete=set(("SUMMARY", "POSITIONS", "ORDERS")) - {missing})
                result = self.machine.status(100.2)
                self.assertTrue(result["block_new_entry"])
                self.assertEqual(result["components"][missing], "STARTED")

    def test_generation_reuse_blocks_current_cycle(self):
        self.cycle()
        blocked(self.machine.connect)
        state = self.machine.status(100.2)
        self.assertTrue(state["block_new_entry"])
        self.assertTrue(state["recovery_required"])
        self.assertIn("GENERATION_UNVERIFIABLE", state["block_reasons"])

    def test_duplicates_reordering_and_callbacks_after_completion(self):
        cid = self.machine.begin_cycle(self.generation, 100.0)
        blocked(lambda: self.machine.complete_component(self.generation, cid, "POSITIONS", 100.1))
        self.assertTrue(self.machine.status(100.2)["recovery_required"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "SUMMARY", 100.0)
        self.machine.observation(self.generation, cid, "SUMMARY", ACCOUNT,
                                 SUMMARY_TAGS[0], ("10000", "USD"), 100.01)
        self.machine.observation(self.generation, cid, "SUMMARY", ACCOUNT,
                                 SUMMARY_TAGS[0], ("10000", "USD"), 100.01)
        blocked(lambda: self.machine.observation(self.generation, cid, "SUMMARY", ACCOUNT,
                                                 SUMMARY_TAGS[0], ("10001", "USD"), 100.02))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.cycle()
        blocked(lambda: self.machine.observation(self.generation, cid, "SUMMARY", ACCOUNT,
                                                 SUMMARY_TAGS[0], ("10000", "USD"), 100.2))

    def test_disconnect_each_phase_and_reconnect_generation(self):
        old = self.generation
        for phase in ("NONE", "BEGUN", "PARTIAL", "COMPLETE"):
            with self.subTest(phase=phase):
                if phase == "BEGUN":
                    self.machine.begin_cycle(self.generation, 100.0)
                elif phase == "PARTIAL":
                    cid = self.machine.begin_cycle(self.generation, 100.0)
                    self.machine.begin_component(self.generation, cid, "SUMMARY", 100.0)
                elif phase == "COMPLETE":
                    self.cycle()
                self.machine.disconnect(self.generation)
                self.assertTrue(self.machine.status(100.2)["block_new_entry"])
                self.generation = self.machine.connect()
                self.assertGreater(self.generation, old)
                old = self.generation
                self.machine.managed_accounts(self.generation, (ACCOUNT,))
                blocked(lambda: self.machine.begin_component(old - 1, "obsolete", "SUMMARY", 100.0))
                self.assertTrue(self.machine.status(100.2)["block_new_entry"])
                self.machine.disconnect(self.generation)
                self.generation = self.machine.connect()
                self.machine.managed_accounts(self.generation, (ACCOUNT,))

    def test_account_ambiguity_and_change(self):
        self.machine.disconnect(self.generation)
        other = ReadOnlySnapshotMachine(self.root / "other.json", self.local)
        self.addCleanup(other.close)
        gen = other.connect()
        other.managed_accounts(gen, (ACCOUNT, "SYNTHETIC_OTHER"))
        blocked(lambda: other.begin_cycle(gen, 100.0))
        self.assertIn("ACCOUNT_SELECTION_REQUIRED", other.status(100.1)["block_reasons"])
        explicit = ReadOnlySnapshotMachine(self.root / "explicit.json", self.local, ACCOUNT)
        self.addCleanup(explicit.close)
        explicit_gen = explicit.connect()
        explicit.managed_accounts(explicit_gen, (ACCOUNT, "SYNTHETIC_OTHER"))
        self.assertIsNotNone(explicit.begin_cycle(explicit_gen, 100.0))
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle()
        blocked(lambda: self.machine.managed_accounts(self.generation, ("SYNTHETIC_OTHER",)))
        self.assertTrue(self.machine.status(100.2)["recovery_required"])

    def test_staleness_incoherence_and_clock_rollback(self):
        self.cycle()
        self.assertIn("SNAPSHOT_EXPIRED", self.machine.status(101.11)["block_reasons"])
        self.assertEqual(json.loads(self.path.read_text())["block_reason"], "SNAPSHOT_EXPIRED")
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle(times=(100.1, 100.65, 101.11))
        self.assertIn("SNAPSHOT_INCOHERENT", self.machine.status(101.11)["block_reasons"])
        self.assertIn("MONOTONIC_CLOCK_ROLLBACK", self.machine.status(100.09)["block_reasons"])
        self.assertTrue(self.machine.status(float("nan"))["block_new_entry"])

    def test_component_duration_duplicate_begin_and_malformed_ordering(self):
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 100.0)
        blocked(lambda: self.machine.begin_component(self.generation, cid, "POSITIONS", 100.01))
        self.assertTrue(self.machine.status(100.02)["recovery_required"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 100.0)
        blocked(lambda: self.machine.complete_component(self.generation, cid, "POSITIONS", 101.01))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 100.0)
        blocked(lambda: self.machine.complete_component(self.generation, cid, "POSITIONS", 99.9))

    def test_broker_mismatches_orders_and_active_lifecycle(self):
        self.cycle(position=(101, 1))
        self.assertIn("BROKER_LOCAL_POSITION_DISAGREEMENT", self.machine.status(100.2)["block_reasons"])
        self.assertTrue(self.machine.status(100.2)["recovery_required"])
        self.assertTrue(self.machine.status(100.2)["block_new_entry"])
        blocked(lambda: self.machine.begin_cycle(self.generation, 100.3))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle(order=True)
        self.assertIn("API_VISIBLE_OPEN_ORDER_PRESENT", self.machine.status(100.2)["block_reasons"])

    def test_local_open_zero_wrong_conid_and_exiting(self):
        raw = make_session("snapshot-active", "CALL", [4.0, 1.25],
                           quantity=1, fill_mode="SCRIPTED")
        raw["events"][0]["fills"] = [{"event_id": "buy-one", "side": "ENTRY",
                                      "chunk_index": 0, "quantity": 1, "rejected": False}]
        session = ReplaySession.from_dict(raw)
        self.local.unlink()
        runner = DurableReplayRunner(session, self.local)
        runner.run(stop_index=0)
        runner.close()
        self.cycle(position=(101, 1))
        matched = self.machine.status(100.2)
        self.assertEqual(matched["reconciliation"], "MATCHED_ACTIVE")
        self.assertTrue(matched["block_new_entry"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle()
        self.assertIn("BROKER_LOCAL_POSITION_DISAGREEMENT", self.machine.status(100.2)["block_reasons"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle(position=(999, 1))
        self.assertIn("BROKER_LOCAL_POSITION_DISAGREEMENT", self.machine.status(100.2)["block_reasons"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        runner = DurableReplayRunner(session, self.local)
        runner.run(stop_index=2)
        self.assertEqual(runner.controller.status().phase, "EXITING")
        runner.close()
        self.cycle(position=(101, 0))
        self.assertIn("BROKER_LOCAL_POSITION_DISAGREEMENT", self.machine.status(100.2)["block_reasons"])

    def test_local_flat_requires_completion_journal(self):
        raw = make_session("snapshot-complete", "CALL", [5.0, 2.0], quantity=1)
        self.local.unlink()
        runner = DurableReplayRunner(ReplaySession.from_dict(raw), self.local)
        runner.run()
        self.assertEqual(runner.controller.status().phase, "FLAT")
        runner.close()
        with sqlite3.connect(self.local) as db:
            db.execute("DELETE FROM audit WHERE kind='CONFIRMED_FLAT'")
        self.cycle()
        result = self.machine.status(100.2)
        self.assertTrue(result["block_new_entry"])
        self.assertTrue(result["recovery_required"])
        self.assertIn("LOCAL_FLAT_AUDIT_MISSING", result["block_reasons"][0])

    def test_restart_complete_incomplete_corrupt_and_account_binding(self):
        self.cycle()
        self.assertEqual(self.machine.status(100.2)["reconciliation"], "MATCHED_FLAT")
        prior = self.generation
        self.machine.close()
        restart = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(restart.close)
        self.assertTrue(restart.status(100.2)["block_new_entry"])
        self.assertEqual(restart.status(100.2)["cycle_state"], "NONE")
        self.assertGreater(restart.connect(), prior)
        restart.managed_accounts(restart.generation, (ACCOUNT,))
        cid = restart.begin_cycle(restart.generation, 100.0)
        restart.begin_component(restart.generation, cid, "SUMMARY", 100.0)
        restart.close()
        restart = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(restart.close)
        self.assertTrue(restart.status(100.2)["block_new_entry"])
        restart.close()
        with self.path.open("w") as handle:
            handle.write("{")
        bad = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(bad.close)
        self.assertTrue(bad.status(100.2)["recovery_required"])
        blocked(bad.connect)

    def test_unknown_version_and_local_corruption(self):
        self.machine.close()
        raw = json.loads(self.path.read_text())
        raw["version"] = 99
        self.path.write_text(json.dumps(raw))
        invalid = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(invalid.close)
        self.assertTrue(invalid.status(100.0)["recovery_required"])
        invalid.close()
        self.path.write_text(json.dumps({"version": 1}))
        truncated = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(truncated.close)
        self.assertTrue(truncated.status(100.0)["block_new_entry"])
        truncated.close()
        self.machine = ReadOnlySnapshotMachine(self.root / "fresh.json", self.local, ACCOUNT)
        self.addCleanup(self.machine.close)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle()
        with sqlite3.connect(self.local) as db:
            db.execute("UPDATE controller SET broker_qty=1")
        self.assertTrue(self.machine.status(100.2)["recovery_required"])

    def test_persisted_generation_edit_and_writer_conflict_fail_closed(self):
        blocked(lambda: ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT))
        self.machine.close()
        raw = json.loads(self.path.read_text())
        raw["generation"] = 0  # Integrity digest no longer matches.
        self.path.write_text(json.dumps(raw))
        damaged = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(damaged.close)
        self.assertTrue(damaged.status(100.0)["recovery_required"])
        blocked(damaged.connect)

    def test_valid_old_state_file_cannot_reuse_generation(self):
        old_state = self.path.read_bytes()
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.close()
        self.path.write_bytes(old_state)  # Valid old digest, stale against independent high-water.
        rolled_back = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(rolled_back.close)
        self.assertTrue(rolled_back.status(100.0)["recovery_required"])
        blocked(rolled_back.connect)

    def test_exact_freshness_coherence_and_component_duration_boundaries(self):
        self.assertEqual(BROKER_COMPLETION_COHERENCE_SECONDS, MAX_AGE_SECONDS)
        # A sequential summary, positions, orders cycle can use the whole
        # existing one-second budget, including equality at each boundary.
        self.cycle(times=(100.10, 100.60, 101.10))
        result = self.machine.status(101.10)
        self.assertEqual(result["coherence_status"], "COHERENT")
        self.assertEqual(result["age_status"], "FRESH")
        self.assertEqual(result["reconciliation"], "MATCHED_FLAT")
        self.assertIn("SNAPSHOT_EXPIRED", self.machine.status(101.100001)["block_reasons"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 200.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 200.0)
        self.machine.complete_component(self.generation, cid, "POSITIONS", 201.0)
        self.assertEqual(self.machine.status(201.0)["components"]["POSITIONS"], "COMPLETE")
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 200.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 200.0)
        blocked(lambda: self.machine.complete_component(self.generation, cid, "POSITIONS", 201.000001))

    def test_bad_monotonic_values_future_and_decreasing_callbacks(self):
        for invalid in (True, -1, math.nan, math.inf, -math.inf, "100"):
            with self.subTest(invalid=invalid):
                self.machine.disconnect(self.generation)
                self.generation = self.machine.connect()
                self.machine.managed_accounts(self.generation, (ACCOUNT,))
                blocked(lambda: self.machine.begin_cycle(self.generation, invalid))
                self.assertTrue(self.machine.status(100.0)["block_new_entry"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 100.0)
        blocked(lambda: self.machine.observation(self.generation, cid, "POSITIONS", ACCOUNT,
                                                 101, 1, 99.99))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.cycle()
        self.assertIn("MONOTONIC_CLOCK_ROLLBACK", self.machine.status(100.139999)["block_reasons"])

    def test_account_change_midcycle_and_duplicate_position_order_callbacks(self):
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "POSITIONS", 100.0)
        self.machine.observation(self.generation, cid, "POSITIONS", ACCOUNT, 101, 1, 100.01)
        self.machine.observation(self.generation, cid, "POSITIONS", ACCOUNT, 101, 1, 100.01)
        blocked(lambda: self.machine.observation(self.generation, cid, "POSITIONS", ACCOUNT, 101, 2, 100.02))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        cid = self.machine.begin_cycle(self.generation, 100.0)
        self.machine.begin_component(self.generation, cid, "ORDERS", 100.0)
        self.machine.observation(self.generation, cid, "ORDERS", ACCOUNT, 10, 101, 100.01)
        self.machine.observation(self.generation, cid, "ORDERS", ACCOUNT, 10, 101, 100.01)
        blocked(lambda: self.machine.observation(self.generation, cid, "ORDERS", ACCOUNT, 10, 102, 100.02))
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        self.machine.begin_cycle(self.generation, 100.0)
        blocked(lambda: self.machine.managed_accounts(self.generation, ("SYNTHETIC_OTHER",)))
        self.assertTrue(self.machine.status(100.0)["recovery_required"])

    def test_atomic_diagnostic_write_failure_blocks_in_memory_and_on_restart(self):
        original = self.machine._atomic_json
        def fail_anchor(path, payload):
            if path == self.machine.anchor_path:
                raise OSError("synthetic write failure")
            return original(path, payload)
        with patch.object(self.machine, "_atomic_json", side_effect=fail_anchor):
            blocked(lambda: self.machine.begin_cycle(self.generation, 100.0))
        self.assertTrue(self.machine.status(100.0)["block_new_entry"])
        self.assertTrue(self.machine.status(100.0)["recovery_required"])
        self.machine.close()
        restart = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(restart.close)
        self.assertTrue(restart.status(100.0)["block_new_entry"])
        self.assertTrue(restart.status(100.0)["recovery_required"])

    def test_restoring_both_diagnostic_files_still_requires_new_cycle(self):
        self.cycle()
        self.assertEqual(self.machine.status(100.2)["reconciliation"], "MATCHED_FLAT")
        old_state, old_anchor = self.path.read_bytes(), self.machine.anchor_path.read_bytes()
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.close()
        self.path.write_bytes(old_state)
        self.machine.anchor_path.write_bytes(old_anchor)
        restart = ReadOnlySnapshotMachine(self.path, self.local, ACCOUNT)
        self.addCleanup(restart.close)
        result = restart.status(100.2)
        self.assertTrue(result["block_new_entry"])
        self.assertEqual(result["cycle_state"], "NONE")
        self.assertEqual(result["order_visibility"], "API_VISIBLE_ONLY")

    def test_status_transition_is_thread_serialized_and_idempotent(self):
        self.cycle()
        entered, release, callback_done = threading.Event(), threading.Event(), threading.Event()
        from executor_broker_snapshot import _local_state
        def slow_local(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return _local_state(*args)
        with patch("executor_broker_snapshot._local_state", side_effect=slow_local):
            reader = threading.Thread(target=lambda: self.machine.status(100.2))
            reader.start()
            self.assertTrue(entered.wait(5))
            callback = threading.Thread(target=lambda: (self.machine.disconnect(self.generation),
                                                       callback_done.set()))
            callback.start()
            self.assertFalse(callback_done.wait(0.05))
            release.set()
            reader.join(5)
            callback.join(5)
        self.assertTrue(callback_done.is_set())
        first = self.machine.status(100.2)
        before = self.path.read_bytes()
        self.assertEqual(first, self.machine.status(100.2))
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(first["block_new_entry"])

    def test_sqlite_reconciliation_uses_one_read_transaction(self):
        raw = make_session("snapshot-transaction", "CALL", [5.0, 2.0], quantity=1)
        self.local.unlink()
        runner = DurableReplayRunner(ReplaySession.from_dict(raw), self.local)
        runner.run()
        runner.close()
        with sqlite3.connect(self.local) as db:
            db.execute("PRAGMA journal_mode=WAL")
        self.cycle()
        original = DryRunExecutor.status
        wrote = []
        def update_between_reads(reader):
            result = original(reader)
            if not wrote:
                with sqlite3.connect(self.local) as writer:
                    writer.execute("DELETE FROM audit WHERE kind='CONFIRMED_FLAT'")
                wrote.append(True)
            return result
        with patch.object(DryRunExecutor, "status", update_between_reads):
            first = self.machine.status(100.2)
        self.assertEqual(first["reconciliation"], "MATCHED_FLAT")
        second = self.machine.status(100.2)
        self.assertTrue(second["block_new_entry"])
        self.assertIn("LOCAL_FLAT_AUDIT_MISSING", second["block_reasons"][0])

    def test_invalid_status_clocks_fail_closed(self):
        self.cycle()
        for value in (True, -1, math.nan, math.inf, -math.inf, "100"):
            with self.subTest(value=value):
                result = self.machine.status(value)
                self.assertTrue(result["block_new_entry"])
                self.assertTrue(result["recovery_required"])
        self.assertEqual(self.machine.status(100.2)["order_visibility"], "API_VISIBLE_ONLY")

    def test_unknown_controller_phase_and_missing_journal_schema_fail_closed(self):
        self.cycle()
        with sqlite3.connect(self.local) as db:
            db.execute("UPDATE controller SET phase='UNKNOWN_PHASE'")
        result = self.machine.status(100.2)
        self.assertTrue(result["block_new_entry"])
        self.assertTrue(result["recovery_required"])
        self.machine.disconnect(self.generation)
        self.generation = self.machine.connect()
        self.machine.managed_accounts(self.generation, (ACCOUNT,))
        with sqlite3.connect(self.local) as db:
            db.execute("UPDATE controller SET phase='FLAT'")
            db.execute("DROP TABLE audit")
        self.cycle()
        result = self.machine.status(100.2)
        self.assertTrue(result["block_new_entry"])
        self.assertTrue(result["recovery_required"])


if __name__ == "__main__":
    unittest.main()
