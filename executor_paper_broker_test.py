"""Offline Stage 3A adversarial fake-broker tests. Never constructs an IBKR client."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from executor_paper_broker import PaperBrokerLedger
from executor_paper_types import (
    ApprovedPaperAccount, BrokerOrderRecord, CommissionRecord, COMPONENTS,
    ExecutionRecord, PaperBrokerError, PaperReconciliationSnapshot, PaperSessionEvidence,
    PositionRecord, TERMINAL,
)
from simulation_evidence import EvidenceError, MAX_SYNC_SECONDS, VerifiedOptionContract

NOW = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
ACCOUNT = "DU1234567"
CONTRACT = VerifiedOptionContract("SPX", "OPT", "C", "20261023", "SPXW", "100", "USD", 6000., 101)


def approval():
    return ApprovedPaperAccount(ACCOUNT, "synthetic-paper-approval", NOW + timedelta(days=1))


class FakeBroker:
    """Independent order/execution history; all operations deliver observations."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.wall, self.mono = NOW, 100.
        ledger.wall_clock = lambda: self.wall
        ledger.monotonic_clock = lambda: self.mono
        self.nonce = self.serial = self.cycle = 0
        self.orders, self.executions, self.active = {}, {}, {}

    def start(self, **changes):
        self.nonce += 1
        evidence = PaperSessionEvidence(ACCOUNT, (ACCOUNT,), "synthetic-paper-approval",
                                        "nonce:" + str(self.nonce), self.wall,
                                        self.wall+timedelta(hours=1), 3)
        self.generation = self.ledger.verify_session(replace(evidence, **changes))
        return self.generation

    def event(self):
        self.serial += 1
        return "fake:" + str(self.serial)

    def snapshot(self, **changes):
        self.cycle += 1
        positions = {}
        for execution in self.active.values():
            contract = execution.contract
            positions[contract] = positions.get(contract, 0) + execution.quantity * (1 if execution.side == "BUY" else -1)
        snapshot = PaperReconciliationSnapshot(
            self.generation, self.cycle, ACCOUNT,
            tuple(PositionRecord(ACCOUNT, contract, qty) for contract, qty in positions.items() if qty),
            tuple(record for record in self.orders.values() if record.status not in TERMINAL),
            tuple(record for record in self.orders.values() if record.status in TERMINAL),
            tuple(self.executions), tuple((name, self.mono) for name in COMPONENTS),
            NOW-timedelta(days=1), COMPONENTS)
        return replace(snapshot, **changes)

    def reconcile(self, **changes):
        return self.ledger.reconcile(self.snapshot(**changes))

    def prepare(self, intent="buy", side="BUY", qty=10, contract=CONTRACT, bind=True):
        self.ledger.prepare_intent(self.generation, intent, "mort:"+intent, contract, side, qty, lambda _: True)
        if bind:
            assert self.reconcile()["matched"]
            order_id = len(self.ledger.state["intents"])+10
            self.ledger.reserve_order_identity(self.generation, intent, order_id)
        return self.ledger.state["intents"][intent]

    def order(self, intent="buy", status="Submitted", **changes):
        item = self.ledger.state["intents"][intent]
        filled = sum(execution.quantity for execution in self.active.values() if execution.order_ref == item["order_ref"])
        record = BrokerOrderRecord(ACCOUNT, item["order_ref"], item["order_id"], item["order_id"]+1000,
                                    item["client_id"], VerifiedOptionContract(**item["contract"]), item["side"],
                                    item["quantity"], filled, item["quantity"]-filled, status)
        record = replace(record, **changes)
        self.orders[intent] = record
        self.ledger.observe_order(self.generation, self.event(), record)
        return record

    def execution(self, intent="buy", qty=10, price="10.00", exec_id=None,
                  correction_of=None, bust=False, commission=True, update_order=True):
        item = self.ledger.state["intents"][intent]
        record = ExecutionRecord(ACCOUNT, exec_id or self.event()+".01", item["order_ref"], item["order_id"],
                                 item["order_id"]+1000, item["client_id"], VerifiedOptionContract(**item["contract"]),
                                 item["side"], qty, price, correction_of, bust)
        self.executions[record.exec_id] = record
        if correction_of:
            self.active.pop(correction_of, None)
        self.active[record.exec_id] = record
        self.ledger.observe_execution(self.generation, self.event(), record)
        if commission:
            self.commission(record.exec_id)
        if update_order:
            filled = sum(execution.quantity for execution in self.active.values() if execution.order_ref == item["order_ref"])
            self.order(intent, "Filled" if filled == item["quantity"] else "Submitted")
        return record

    def commission(self, exec_id, amount="0.65", report_id=None, correction_of=None):
        report = CommissionRecord(report_id or self.event(), exec_id, amount, "USD", correction_of)
        self.ledger.observe_commission(self.generation, self.event(), report)
        return report

    def own(self, qty=10, contract=CONTRACT):
        self.prepare(qty=qty, contract=contract)
        self.order()
        self.execution(qty=qty)
        assert self.reconcile()["matched"]


class PaperBrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.fresh()

    def fresh(self, name=None, start=True):
        self.path = self.root / ((name or str(len(self.ledgers))) + ".sqlite")
        self.ledger = PaperBrokerLedger(self.path, approval())
        self.ledgers.append(self.ledger)
        self.fake = FakeBroker(self.ledger)
        if start:
            self.fake.start()
            self.assertTrue(self.fake.reconcile()["matched"])

    def restart(self):
        self.ledger.close()
        self.ledgers.remove(self.ledger)
        self.ledger = PaperBrokerLedger(self.path, approval())
        self.ledgers.append(self.ledger)
        self.fake.ledger = self.ledger
        self.ledger.wall_clock = lambda: self.fake.wall
        self.ledger.monotonic_clock = lambda: self.fake.mono

    def blocked(self, result):
        self.assertFalse(result["matched"], result)
        self.assertFalse(result["executable"])
        self.assertTrue(result["block_reasons"])

    def test_approved_paper_identity_is_exact_and_all_submission_is_disabled(self):
        status = self.ledger.status()
        self.assertTrue(status["paper_session_verified"])
        self.assertFalse(status["submission_enabled"])
        self.assertFalse(status["executable"])
        for method in ("connect", "placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions", "submit", "execute"):
            self.assertFalse(hasattr(self.ledger, method))
        with patch("socket.socket", side_effect=AssertionError("No socket allowed")):
            self.fake.own()
            self.assertFalse(self.ledger.status()["executable"])

    def test_live_mixed_missing_wrong_and_unverified_sessions_are_rejected(self):
        for index, change in enumerate((dict(account="U1234567"), dict(managed_accounts=(ACCOUNT, "U1111111")),
                                      dict(managed_accounts=()), dict(mode="LIVE"), dict(account="DU9999999"),
                                      dict(read_only=False), dict(origin="IBKR_UNVERIFIED"),
                                      dict(verification_ref="unknown"), dict(host="remote"), dict(port=7496),
                                      dict(verified_at=NOW-timedelta(seconds=2)), dict(expires_at=NOW),
                                      dict(client_id=True))):
            self.fresh("identity-"+str(index), start=False)
            with self.assertRaises(PaperBrokerError):
                self.fake.start(**change)
            self.assertFalse(self.ledger.status()["paper_session_verified"])

    def test_prefix_or_paper_port_alone_never_approves_an_account(self):
        for config in (replace(approval(), account="U1234567"), replace(approval(), verification_ref=""),
                       replace(approval(), port=7496), replace(approval(), host="0.0.0.0")):
            with self.assertRaises(PaperBrokerError):
                PaperBrokerLedger(self.root/"invalid.sqlite", config)

    def test_intention_and_ids_are_durable_before_any_broker_observation(self):
        self.fake.prepare()
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM events WHERE kind='INTENT'").fetchone()[0], 1)
        self.restart()
        item = self.ledger.status()["intents"]["buy"]
        self.assertEqual(item["order_ref"], "mort:buy")
        self.assertIsNotNone(item["order_id"])
        self.assertTrue(item["uncertain"])
        self.assertFalse(self.ledger.status()["paper_session_verified"])

    def test_missing_intent_cannot_receive_an_order_or_execution(self):
        record = BrokerOrderRecord(ACCOUNT, "unknown", 11, 1011, 3, CONTRACT, "BUY", 10, 0, 10, "Submitted")
        with self.assertRaises(PaperBrokerError):
            self.ledger.observe_order(self.fake.generation, "unknown", record)
        self.assertTrue(self.ledger.status()["recovery_required"])
        self.assertEqual(self.ledger.positions(), {})

    def test_partial_buy_cancellation_then_partial_exit(self):
        self.fake.prepare()
        self.fake.order()
        self.fake.execution(qty=4)
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("sell", "SELL", 4)
        self.fake.order(status="PendingCancel")
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("sell", "SELL", 4)
        self.fake.order(status="Cancelled")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.prepare("sell", "SELL", 4)
        self.fake.order("sell")
        self.fake.execution("sell", 1)
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertEqual(self.ledger.positions(), {101: 3})
        self.fake.execution("sell", 3)
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertEqual(self.ledger.positions(), {})

    def test_pending_zero_perm_id_is_not_confirmation_and_later_assignment_is_tracked(self):
        self.fake.prepare()
        self.fake.order(status="PendingSubmit", perm_id=0)
        self.assertIsNone(self.ledger.state["intents"]["buy"]["perm_id"])
        self.assertTrue(self.ledger.state["intents"]["buy"]["uncertain"])
        self.blocked(self.fake.reconcile())
        self.fake.order()
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertGreater(self.ledger.state["intents"]["buy"]["perm_id"], 0)

    def test_duplicate_execution_ids_do_not_double_count_with_new_callback_ids(self):
        self.fake.prepare()
        self.fake.order()
        execution = self.fake.execution(qty=4)
        for _ in range(3):
            self.ledger.observe_execution(self.fake.generation, self.fake.event(), execution)
        self.assertEqual(self.ledger.positions(), {101: 4})
        self.assertEqual(self.ledger.filled(self.ledger.state["intents"]["buy"]), 4)
        self.assertTrue(self.fake.reconcile()["matched"])

    def test_identical_callback_is_idempotent_and_conflicting_id_is_quarantined(self):
        self.fake.prepare()
        record = self.fake.order()
        self.ledger.observe_order(self.fake.generation, "duplicate", record)
        self.assertEqual(self.ledger.observe_order(self.fake.generation, "duplicate", record), "DUPLICATE")
        with self.assertRaises(PaperBrokerError):
            self.ledger.observe_order(self.fake.generation, "duplicate", replace(record, status="PendingCancel"))
        self.assertEqual(self.ledger.status()["quarantined_evidence_count"], 1)

    def test_duplicate_order_status_does_not_create_executions(self):
        self.fake.prepare()
        self.fake.order(status="Filled", filled=10, remaining=0)
        self.fake.order(status="Filled", filled=10, remaining=0)
        self.assertEqual(self.ledger.positions(), {})
        self.blocked(self.fake.reconcile())

    def test_correction_replaces_fill_and_commission_instead_of_adding(self):
        self.fake.prepare()
        self.fake.order()
        original = self.fake.execution(qty=6, exec_id="trade.01")
        self.fake.execution(qty=4, exec_id="trade.02", correction_of=original.exec_id)
        self.ledger.observe_execution(self.fake.generation, self.fake.event(), original)
        self.assertEqual(self.ledger.positions(), {101: 4})
        self.assertEqual(self.ledger.status()["commission_total"], "0.65")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.restart()
        self.assertEqual(self.ledger.positions(), {101: 4})

    def test_price_only_correction_and_explicit_bust(self):
        self.fake.prepare()
        self.fake.order()
        self.fake.execution(qty=4, exec_id="trade.01")
        self.fake.execution(qty=4, price="9.50", exec_id="trade.02", correction_of="trade.01")
        self.assertEqual(self.ledger.positions(), {101: 4})
        self.fake.execution(qty=0, price="0", exec_id="trade.03", correction_of="trade.02", bust=True)
        self.assertEqual(self.ledger.positions(), {})
        self.assertTrue(self.fake.reconcile()["matched"])

    def test_unlinked_or_out_of_order_corrections_are_quarantined(self):
        for suffix, correction in (("trade.02", None), ("trade.03", "trade.02")):
            self.fresh("correction-"+suffix)
            self.fake.prepare()
            self.fake.order()
            self.fake.execution(qty=4, exec_id="trade.01")
            with self.assertRaises(PaperBrokerError):
                self.fake.execution(qty=2, exec_id=suffix, correction_of=correction)
            self.assertEqual(self.ledger.positions(), {101: 4})
            self.assertTrue(self.ledger.status()["recovery_required"])

    def test_commissions_before_execution_duplicates_rebates_and_corrections(self):
        self.fake.prepare()
        self.fake.order()
        first = self.fake.commission("trade.01", "-0.05", "report1")
        self.blocked(self.fake.reconcile())
        self.fake.execution(qty=4, exec_id="trade.01", commission=False)
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertEqual(self.ledger.status()["commission_total"], "-0.05")
        self.ledger.observe_commission(self.fake.generation, self.fake.event(), first)
        corrected = self.fake.commission("trade.01", "0.60", "report2", "report1")
        self.ledger.observe_commission(self.fake.generation, self.fake.event(), corrected)
        self.fake.commission("trade.01", "0.60", "report2-alias")
        corrected = self.fake.commission("trade.01", "0.60", "report3", "report2-alias")
        self.ledger.observe_commission(self.fake.generation, self.fake.event(), corrected)
        self.assertEqual(self.ledger.status()["commission_total"], "0.60")
        self.assertTrue(self.fake.reconcile()["matched"])

    def test_missing_commissions_and_history_are_not_complete(self):
        self.fake.prepare()
        self.fake.order()
        execution = self.fake.execution(qty=4, commission=False)
        self.blocked(self.fake.reconcile())
        self.fake.commission(execution.exec_id)
        self.blocked(self.fake.reconcile(execution_ids=()))
        self.blocked(self.fake.reconcile(history_start=NOW+timedelta(seconds=1)))
        self.assertTrue(self.fake.reconcile()["matched"])

    def test_disconnect_uncertain_absence_and_reconnect_require_matching_broker_truth(self):
        self.fake.prepare()
        old_generation = self.fake.generation
        self.ledger.disconnect(old_generation)
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(old_generation, "buy", 99)
        self.fake.start()
        self.blocked(self.fake.reconcile())
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, "buy", 99)
        self.fake.order()
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertFalse(self.ledger.state["intents"]["buy"]["uncertain"])
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, "buy", 99)

    def test_restart_requires_new_nonce_session_and_full_reconciliation(self):
        self.fake.own()
        generation = self.fake.generation
        self.restart()
        with self.assertRaises(PaperBrokerError):
            self.ledger.observe_order(generation, "old", self.fake.orders["buy"])
        with self.assertRaises(PaperBrokerError):
            self.fake.start(nonce="nonce:1")
        self.fake.start()
        self.assertTrue(self.fake.reconcile()["matched"])
        self.assertEqual(self.ledger.positions(), {101: 10})

    def test_exit_reservations_prevent_double_sell_and_short_positions(self):
        self.fake.own()
        self.fake.prepare("exit1", "SELL", 6, bind=False)
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("exit2", "SELL", 5, bind=False)
        self.fake.prepare("exit2", "SELL", 4, bind=False)
        self.assertEqual(self.ledger.reservations("SELL", 101), 10)

    def test_concurrent_exit_intentions_cannot_over_reserve_owned_contracts(self):
        self.fake.own()
        def prepare(name):
            try:
                return self.ledger.prepare_intent(self.fake.generation, name, "mort:"+name,
                                                  CONTRACT, "SELL", 7, lambda _: True)
            except PaperBrokerError:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, ("exit-a", "exit-b")))
        self.assertEqual(sorted(results), ["BLOCKED", "RECORDED"])
        self.assertEqual(self.ledger.reservations("SELL", 101), 7)

    def test_correction_reducing_ownership_below_working_exit_is_blocked(self):
        self.fake.prepare()
        self.fake.order()
        self.fake.execution(qty=10, exec_id="buy.01")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.prepare("exit", "SELL", 8)
        self.fake.order("exit")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.execution(qty=5, exec_id="buy.02", correction_of="buy.01", update_order=False)
        self.fake.order(status="Cancelled")
        result = self.fake.reconcile()
        self.blocked(result)
        self.assertIn("EXIT_RESERVATIONS", str(result["block_reasons"]))
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("extra-exit", "SELL", 1)

    def test_global_cap_covers_unfilled_entries_and_other_contracts(self):
        self.fake.prepare(qty=20, bind=False)
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("other", qty=10, contract=replace(CONTRACT, con_id=102, strike=6005.))
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("extra", qty=6)

    def test_order_id_and_perm_id_cannot_be_reused_for_a_different_intent(self):
        self.fake.own()
        old = self.ledger.state["intents"]["buy"]
        self.fake.prepare("next", qty=1, bind=False)
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, "next", old["order_id"])
        self.ledger.reserve_order_identity(self.fake.generation, "next", 50)
        with self.assertRaises(PaperBrokerError):
            self.fake.order("next", perm_id=old["perm_id"])

    def test_overselling_execution_and_short_snapshot_are_quarantined(self):
        self.fake.own(qty=4)
        self.fake.prepare("exit", "SELL", 4)
        self.fake.order("exit")
        # Broker reports more than was authorized; never apply a short position.
        with self.assertRaises(PaperBrokerError):
            self.fake.execution("exit", 5)
        self.assertEqual(self.ledger.positions(), {101: 4})
        self.assertTrue(self.ledger.status()["recovery_required"])
        self.fresh("negative-snapshot")
        self.fake.own()
        self.blocked(self.fake.reconcile(positions=(PositionRecord(ACCOUNT, CONTRACT, -1),)))
        self.assertTrue(self.ledger.state["faults"])

    def test_buy_correction_cannot_create_short_after_exit(self):
        self.fake.prepare(qty=10)
        self.fake.order()
        self.fake.execution(qty=10, exec_id="buy.01")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.prepare("exit", "SELL", 8)
        self.fake.order("exit")
        self.fake.execution("exit", 8)
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.execution(qty=5, exec_id="buy.02", correction_of="buy.01")
        self.assertEqual(self.ledger.positions(), {101: 2})
        self.assertTrue(self.ledger.status()["recovery_required"])

    def test_pending_cancel_does_not_release_reservations(self):
        self.fake.own()
        self.fake.prepare("exit1", "SELL", 10)
        self.fake.order("exit1", "PendingCancel")
        self.assertTrue(self.fake.reconcile()["matched"])
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("exit2", "SELL", 1)
        self.fake.order("exit1", "Cancelled")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.prepare("exit2", "SELL", 10)

    def test_cancelled_order_late_fill_is_accounted_without_claiming_old_snapshot_is_current(self):
        self.fake.own()
        self.fake.prepare("exit1", "SELL", 10)
        self.fake.order("exit1", "Cancelled")
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.prepare("exit2", "SELL", 10, bind=False)
        self.fake.execution("exit1", 2, update_order=False)
        self.assertEqual(self.ledger.positions(), {101: 8})
        self.blocked(self.fake.reconcile())
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, "exit2", 50)

    def test_stale_incomplete_old_or_api_visible_only_snapshots_fail_closed(self):
        for changes in (dict(complete_components=("POSITIONS",)), dict(component_receipts=()),
                        dict(component_receipts=tuple((name, 98.) for name in COMPONENTS)),
                        dict(scope="API_VISIBLE_ONLY"), dict(generation=0), dict(account="U1234567")):
            self.blocked(self.fake.reconcile(**changes))
        snapshot = self.fake.snapshot()
        self.assertTrue(self.ledger.reconcile(snapshot)["matched"])
        self.blocked(self.ledger.reconcile(snapshot))

    def test_freshness_and_clock_expiry_never_leave_a_cached_permit(self):
        self.fake.mono += 2
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare()
        self.assertTrue(self.fake.reconcile()["matched"])
        self.fake.wall += timedelta(hours=2)
        self.assertFalse(self.ledger.status()["paper_session_verified"])
        self.assertIsNone(self.ledger.session)
        self.fake.start()
        self.assertTrue(self.fake.reconcile()["matched"])

    def test_identity_mismatches_and_conflicting_execution_are_quarantined(self):
        for index, changes in enumerate((dict(account="U1234567"), dict(order_ref="wrong"),
                                        dict(order_id=999), dict(perm_id=999), dict(client_id=7),
                                        dict(contract=replace(CONTRACT, con_id=999)))):
            self.fresh("mismatch"+str(index))
            self.fake.prepare()
            self.fake.order()
            execution = self.fake.execution(qty=4)
            with self.assertRaises(PaperBrokerError):
                self.ledger.observe_execution(self.fake.generation, self.fake.event(), replace(execution, **changes))
            self.assertEqual(self.ledger.positions(), {101: 4})

    def test_account_binding_writer_lock_and_journal_tampering_fail_closed(self):
        with self.assertRaises(PaperBrokerError):
            PaperBrokerLedger(self.path, approval())
        self.fake.prepare()
        self.ledger.close()
        self.ledgers.remove(self.ledger)
        with self.assertRaises(PaperBrokerError):
            PaperBrokerLedger(self.path, replace(approval(), account="DU9999999"))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE events SET payload='{}' WHERE kind='INTENT'")
        with self.assertRaises(PaperBrokerError):
            PaperBrokerLedger(self.path, approval())

    def test_transaction_failure_rolls_back_identity_and_intention(self):
        original = self.ledger._reduce
        def crash(kind, payload):
            original(kind, payload)
            if kind == "INTENT":
                raise RuntimeError("simulated crash")
        self.ledger._reduce = crash
        with self.assertRaises(RuntimeError):
            self.fake.prepare()
        self.restart()
        self.assertEqual(self.ledger.state["intents"], {})

    def test_fresh_process_crash_preserves_reserved_uncertainty_without_sending(self):
        path = self.root / "crashed.sqlite"
        code = ("import os,sys; from executor_paper_broker_test import FakeBroker,approval; "
                "from executor_paper_broker import PaperBrokerLedger; "
                "ledger=PaperBrokerLedger(sys.argv[1],approval()); fake=FakeBroker(ledger); "
                "fake.start(); assert fake.reconcile()['matched']; fake.prepare(); os._exit(73)")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stderr)
        ledger = PaperBrokerLedger(path, approval())
        self.ledgers.append(ledger)
        self.assertTrue(ledger.state["intents"]["buy"]["uncertain"])
        self.assertFalse(ledger.status()["submission_enabled"])

    def test_call_put_contracts_cap_and_original_market_gate_are_preserved(self):
        for index, contract in enumerate((CONTRACT, replace(CONTRACT, right="P"))):
            self.fresh("right"+str(index))
            with self.assertRaises(PaperBrokerError):
                self.fake.prepare(qty=26, contract=contract)
            self.fake.own(qty=25, contract=contract)
            with self.assertRaises(PaperBrokerError):
                self.fake.prepare("extra", qty=1, contract=contract)
        self.assertEqual(MAX_SYNC_SECONDS, .25)
        self.assertFalse(self.ledger.status()["executable"])


if __name__ == "__main__":
    unittest.main()
