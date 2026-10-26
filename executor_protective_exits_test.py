"""Stage 4A fake-broker coverage, quantity races, profit exits, and recovery."""

from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import uuid

from executor_emergency import EmergencyLatch
from executor_paper_broker import PaperBrokerLedger
from executor_paper_broker_test import FakeBroker, approval, CONTRACT, NOW
from executor_paper_types import PaperBrokerError
from executor_protective_exits import ProtectiveExitManager
from simulation_evidence import EvidenceError, FeedStatus, SPXObservation, MAX_SYNC_SECONDS


class ProtectiveFakeBroker(FakeBroker):
    def order(self, intent="buy", status="Submitted", **changes):
        item = self.ledger.state["intents"][intent]
        if item.get("purpose") == "PROTECTIVE_STOP":
            changes.setdefault("order_type", "STP")
            changes.setdefault("stop_price", item["stop_price"])
        return super().order(intent, status, **changes)

    def acknowledge(self, action):
        intent_id = action["intent_id"]
        assert self.reconcile()["matched"]
        order_id = max((item["order_id"] or 0 for item in self.ledger.state["intents"].values()), default=10)+1
        self.ledger.reserve_order_identity(self.generation, intent_id, order_id)
        self.order(intent_id)
        assert self.reconcile()["matched"]
        return intent_id

    def cancellations(self, actions):
        for action in actions:
            if action["type"] == "CANCEL_REQUIRED":
                self.order(action["intent_id"], "Cancelled")
        assert self.reconcile()["matched"]

    def option_bid(self, bid):
        """Simplified fake stop trigger, independent of controller/SPX ticks."""
        fired = []
        for intent_id, record in list(self.orders.items()):
            if (record.order_type == "STP" and record.status in {"Submitted", "PreSubmitted"}
                    and Decimal(str(bid)) <= Decimal(str(record.stop_price)) and Decimal(str(bid)) > 0):
                remaining = record.quantity-sum(item.quantity for item in self.active.values() if item.order_ref == record.order_ref)
                self.execution(intent_id, remaining, price=str(bid))
                fired.append(intent_id)
        return fired


class ProtectiveExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledgers = []
        self.addCleanup(lambda: [ledger.close() for ledger in self.ledgers])
        self.fresh()

    def fresh(self, right="C", targets=(), quantity=10, bind=True):
        number = len(self.ledgers)
        self.path = self.root / (str(number)+".sqlite")
        self.ledger = PaperBrokerLedger(self.path, approval())
        self.ledgers.append(self.ledger)
        self.fake = ProtectiveFakeBroker(self.ledger)
        self.fake.start()
        assert self.fake.reconcile()["matched"]
        self.latch = EmergencyLatch(self.root / (str(number)+"-emergency.sqlite"), approval().account)
        self.manager = ProtectiveExitManager(self.ledger, self.latch)
        self.contract = replace(CONTRACT, right=right)
        self.fake.prepare(qty=quantity, contract=self.contract, bind=bind)
        self.manager.configure_trade("trade", ["buy"], 6000, targets)
        if bind:
            self.fake.order()

    def filled(self, quantity=10, price="10.00"):
        self.fake.execution(qty=quantity, price=price)
        self.assertTrue(self.fake.reconcile()["matched"])

    def protected(self):
        actions = self.manager.advance()
        self.assertEqual(actions[0]["type"], "BIND_REQUIRED", actions)
        stop = self.fake.acknowledge(actions[0])
        self.assertEqual(self.manager.status()["coverage"], "BROKER_HELD_MODEL_ACKNOWLEDGED")
        return stop

    def spx(self, favorable, event=None):
        price = 6000 + favorable if self.contract.right == "C" else 6000-favorable
        return self.manager.observe_spx(event or self.fake.event(), SPXObservation(price, FeedStatus.LIVE,
                                                                                self.fake.wall, self.fake.mono))

    def test_call_and_put_initial_premium_stop_and_fake_broker_ack_required(self):
        for right in ("C", "P"):
            self.fresh(right)
            self.filled()
            model = self.manager.status()
            self.assertEqual(Decimal(model["stop_price"]), Decimal("6.75"))
            self.assertNotEqual(model["coverage"], "BROKER_HELD_MODEL_ACKNOWLEDGED")
            stop = self.protected()
            self.assertEqual(self.ledger.state["intents"][stop]["quantity"], 10)
            self.assertFalse(self.manager.status()["executable"])

    def test_partial_entry_quantity_and_average_changes_cancel_before_replace(self):
        self.filled(4)
        first = self.protected()
        self.filled(3, "9.50")
        self.assertEqual(self.manager.status()["quantity"], 7)
        self.assertNotEqual(self.manager.status()["coverage"], "BROKER_HELD_MODEL_ACKNOWLEDGED")
        actions = self.manager.advance()
        self.assertEqual(actions[0]["intent_id"], first)
        self.assertEqual(actions[0]["type"], "CANCEL_REQUIRED")
        self.filled(3)
        self.fake.cancellations(actions)
        replacement = self.protected()
        self.assertNotEqual(first, replacement)
        self.assertEqual(Decimal(self.ledger.state["intents"][replacement]["stop_price"]), Decimal("6.60"))
        self.assertEqual(self.ledger.reservations("SELL", CONTRACT.con_id), 10)

    def test_fake_broker_stop_fires_on_premium_without_any_spx_observation(self):
        for right in ("C", "P"):
            self.fresh(right)
            self.filled()
            stop = self.protected()
            self.assertEqual(self.fake.option_bid("6.76"), [])
            self.assertEqual(self.fake.option_bid("6.75"), [stop])
            self.assertEqual(self.manager.state["exit_reason"], "INITIAL_STOP")
            self.assertNotEqual(self.manager.status()["phase"], "FLAT CONFIRMED")
            assert self.fake.reconcile()["matched"]
            self.assertEqual(self.manager.advance()[0]["type"], "FLAT_CONFIRMED")

    def test_partial_entry_can_be_protected_while_acquisition_remains_working(self):
        self.filled(2)
        self.protected()
        self.assertEqual(self.ledger.reservations("BUY", CONTRACT.con_id), 8)
        self.assertEqual(self.ledger.reservations("SELL", CONTRACT.con_id), 2)
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("unsafe-profit", "SELL", 2)

    def test_stop_metadata_mismatch_is_rejected(self):
        self.filled()
        action = self.manager.advance()[0]
        assert self.fake.reconcile()["matched"]
        self.ledger.reserve_order_identity(self.fake.generation, action["intent_id"], 50)
        with self.assertRaises(PaperBrokerError):
            self.fake.order(action["intent_id"], stop_price="7.00")
        self.assertNotEqual(self.manager.status()["coverage"], "BROKER_HELD_MODEL_ACKNOWLEDGED")

    def test_stop_rejection_latches_emergency_and_blocks_new_entry(self):
        self.filled()
        stop = self.protected()
        self.fake.order(stop, "Inactive")
        status = self.manager.status()
        self.assertTrue(status["emergency"])
        self.assertTrue(self.latch.blocked())
        self.assertNotEqual(status["coverage"], "BROKER_HELD_MODEL_ACKNOWLEDGED")
        actions = self.manager.advance()
        self.assertEqual(actions[0]["type"], "RECONCILE_REQUIRED")
        self.fake.order(stop, "Cancelled")
        assert self.fake.reconcile()["matched"]
        action = self.manager.advance()[0]
        self.assertEqual(self.ledger.state["intents"][action["intent_id"]]["purpose"], "EMERGENCY_EXIT")

    def test_invalid_nonpositive_filled_stop_requires_emergency_not_fake_protection(self):
        self.filled(price="3.00")
        model = self.manager.status()
        self.assertEqual(model["coverage"], "INVALID_PREMIUM_STOP")
        self.assertTrue(model["emergency"])
        action = self.manager.advance()[0]
        self.assertEqual(self.ledger.state["intents"][action["intent_id"]]["purpose"], "EMERGENCY_EXIT")

    def test_defaults_do_not_invent_partial_profit_thresholds(self):
        self.filled()
        self.protected()
        self.spx(3)
        self.assertEqual(self.manager.state["targets"], [])
        self.assertEqual(self.manager.advance()[0]["type"], "WAIT_BROKER_EVIDENCE")

    def test_research_targets_never_activate_automatic_scale_outs(self):
        self.fresh(targets=({"id": "user-target", "spx_points": 2, "quantity": 3},))
        self.filled()
        stop = self.protected()
        for movement in (2, 4, 5, 10, 20, 50):
            self.spx(movement)
            self.assertIsNone(self.manager.state["exit_reason"])
            self.assertEqual(self.manager.advance()[0]["intent_id"], stop)
            self.assertEqual(self.ledger.positions()[self.contract.con_id], 10)
        self.assertFalse(self.manager.status()["automatic_profit_scale_outs"])

    def test_profit_floor_and_trail_rules_preserved_for_both_rights(self):
        for right in ("C", "P"):
            for peak, pullback, reason in ((4, 1.25, "PROFIT_PROTECTION_FLOOR"), (5, 2, "LET_IT_RIDE_REVERSAL")):
                self.fresh(right)
                self.filled()
                self.protected()
                self.spx(peak)
                self.spx(pullback)
                self.assertEqual(self.manager.state["exit_reason"], reason)
                self.assertEqual(self.manager.advance()[0]["type"], "CANCEL_REQUIRED")

    def test_full_exit_takes_precedence_over_configured_partial_exit(self):
        self.fresh(targets=({"id": "given", "spx_points": 4, "quantity": 2},))
        self.filled()
        self.protected()
        self.spx(4)
        self.spx(1.25)
        self.fake.cancellations(self.manager.advance())
        action = self.manager.advance()[0]
        self.assertEqual(self.ledger.state["intents"][action["intent_id"]]["quantity"], 10)
        self.assertEqual(self.ledger.state["intents"][action["intent_id"]]["purpose"], "STRATEGY_EXIT")

    def test_emergency_partial_entry_cancel_late_fill_and_repeated_clicks(self):
        self.filled(4)
        stop = self.protected()
        request = str(uuid.uuid4())
        self.manager.exit_all(request)
        self.manager.exit_all(request)
        self.manager.exit_all(str(uuid.uuid4()))
        actions = self.manager.advance()
        self.assertEqual({action["intent_id"] for action in actions}, {"buy"})
        self.assertEqual(self.ledger.state["intents"][stop]["status"], "Submitted")
        self.filled(2)
        self.fake.cancellations(actions)
        self.fake.cancellations(self.manager.advance())
        action = self.manager.advance()[0]
        exit_id = self.fake.acknowledge(action)
        self.assertEqual(self.ledger.state["intents"][exit_id]["quantity"], 6)
        self.fake.execution(exit_id, 6)
        self.assertNotEqual(self.manager.status()["phase"], "FLAT CONFIRMED")
        assert self.fake.reconcile()["matched"]
        self.assertEqual(self.manager.advance()[0]["type"], "FLAT_CONFIRMED")
        self.assertEqual(self.manager.status()["phase"], "FLAT CONFIRMED")
        self.assertTrue(self.latch.blocked())
        with self.assertRaises(PaperBrokerError):
            self.fake.prepare("new-entry", qty=1)

    def test_zero_position_with_unresolved_entry_is_not_flat(self):
        self.manager.exit_all(str(uuid.uuid4()))
        self.assertNotEqual(self.manager.status()["phase"], "FLAT CONFIRMED")
        assert self.fake.reconcile()["matched"]
        action = self.manager.advance()[0]
        self.assertEqual(action["type"], "CANCEL_REQUIRED")
        self.fake.cancellations([action])
        self.assertEqual(self.manager.advance()[0]["type"], "FLAT_CONFIRMED")

    def test_emergency_blocks_binding_a_previously_prepared_but_unsent_entry(self):
        self.fresh(bind=False)
        assert self.fake.reconcile()["matched"]
        self.manager.exit_all(str(uuid.uuid4()))
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, "buy", 75)
        self.assertEqual(self.manager.advance()[0]["type"], "LOCAL_ABANDONED")
        assert self.fake.reconcile()["matched"]
        self.assertEqual(self.manager.advance()[0]["type"], "FLAT_CONFIRMED")

    def test_cancel_pending_stop_still_reserves_quantity(self):
        self.filled()
        stop = self.protected()
        self.manager.exit_all(str(uuid.uuid4()))
        self.fake.order(stop, "PendingCancel")
        assert self.fake.reconcile()["matched"]
        self.assertEqual(self.manager.advance()[0]["type"], "CANCEL_REQUIRED")
        self.assertEqual(self.ledger.reservations("SELL", CONTRACT.con_id), 10)

    def test_reconnect_restart_retains_stop_and_emergency_without_duplicate_exit(self):
        self.filled()
        stop = self.protected()
        self.manager.exit_all(str(uuid.uuid4()))
        self.ledger.disconnect(self.fake.generation)
        self.assertEqual(self.manager.advance()[0]["type"], "RECONCILE_REQUIRED")
        self.ledger.close()
        self.ledgers.remove(self.ledger)
        self.ledger = PaperBrokerLedger(self.path, approval())
        self.ledgers.append(self.ledger)
        self.fake.ledger = self.ledger
        self.ledger.wall_clock, self.ledger.monotonic_clock = lambda: self.fake.wall, lambda: self.fake.mono
        self.manager = ProtectiveExitManager(self.ledger, self.latch)
        self.fake.start()
        self.assertEqual(self.manager.advance()[0]["type"], "RECONCILE_REQUIRED")
        assert self.fake.reconcile()["matched"]
        self.assertEqual(self.manager.advance()[0]["intent_id"], stop)
        self.fake.cancellations(self.manager.advance())
        first = self.manager.advance()[0]
        assert self.fake.reconcile()["matched"]
        second = self.manager.advance()[0]
        self.assertEqual(first["intent_id"], second["intent_id"])

    def test_late_stop_fill_after_cancel_blocks_competing_exit_and_never_models_short(self):
        self.filled()
        stop = self.protected()
        self.manager.exit_all(str(uuid.uuid4()))
        self.fake.cancellations(self.manager.advance())
        exit_id = self.fake.acknowledge(self.manager.advance()[0])
        self.fake.execution(stop, 2, update_order=False)
        self.assertFalse(self.fake.reconcile()["matched"])
        self.assertEqual(self.manager.advance()[0]["type"], "RECONCILE_REQUIRED")
        with self.assertRaises(PaperBrokerError):
            self.fake.execution(exit_id, 10)
        self.assertGreaterEqual(self.ledger.positions().get(CONTRACT.con_id, 0), 0)

    def test_price_corrections_invalidate_stop_until_cancel_and_reconcile(self):
        self.fake.execution(qty=10, exec_id="entry.01")
        assert self.fake.reconcile()["matched"]
        old_stop = self.protected()
        self.fake.execution(qty=10, price="9.00", exec_id="entry.02", correction_of="entry.01")
        assert self.fake.reconcile()["matched"]
        self.assertEqual(self.manager.advance()[0]["intent_id"], old_stop)
        self.assertEqual(Decimal(self.manager.status()["stop_price"]), Decimal("5.75"))

    def test_never_bound_stale_stop_can_be_abandoned_without_assuming_broker_cancel(self):
        self.fake.execution(qty=10, exec_id="entry.01")
        assert self.fake.reconcile()["matched"]
        stale = self.manager.advance()[0]["intent_id"]
        self.fake.execution(qty=5, exec_id="entry.02", correction_of="entry.01", update_order=False)
        self.fake.order(status="Cancelled")
        self.assertFalse(self.fake.reconcile()["matched"])
        self.assertEqual(self.manager.advance()[0]["type"], "LOCAL_ABANDONED")
        assert self.fake.reconcile()["matched"]
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, stale, 90)
        replacement = self.protected()
        self.assertEqual(self.ledger.state["intents"][replacement]["quantity"], 5)

    def test_old_stop_price_cannot_be_bound_after_a_same_quantity_price_correction(self):
        self.fake.execution(qty=10, exec_id="entry.01")
        assert self.fake.reconcile()["matched"]
        stale = self.manager.advance()[0]["intent_id"]
        self.fake.execution(qty=10, price="9.00", exec_id="entry.02", correction_of="entry.01")
        assert self.fake.reconcile()["matched"]
        with self.assertRaises(PaperBrokerError):
            self.ledger.reserve_order_identity(self.fake.generation, stale, 90)
        self.assertEqual(self.manager.advance()[0]["type"], "LOCAL_ABANDONED")

    def test_invalid_user_partial_configuration_is_rejected(self):
        for target in ({"id": "bad", "spx_points": 0, "quantity": 2},
                       {"id": "bad", "spx_points": 2, "quantity": 11},
                       {"id": "bad", "spx_points": 2, "quantity": True}):
            with self.assertRaises(PaperBrokerError):
                self.fresh(targets=(target,))
        self.assertEqual(MAX_SYNC_SECONDS, .25)

    def test_stale_or_delayed_spx_does_not_invent_profit_exit(self):
        self.filled()
        self.protected()
        for status in (FeedStatus.DELAYED, FeedStatus.FROZEN):
            with self.assertRaises(EvidenceError):
                self.manager.observe_spx("invalid", SPXObservation(6005, status, NOW, self.fake.mono))
        self.assertIsNone(self.manager.state["exit_reason"])

    def test_profit_observations_before_any_fill_cannot_arm_a_trade(self):
        with self.assertRaises(PaperBrokerError):
            self.spx(5)
        self.assertFalse(self.manager.state["ride"])
        self.assertIsNone(self.manager.state["exit_reason"])

    def test_new_stop_fields_preserve_stage3a_standard_callback_payloads(self):
        record = self.fake.orders["buy"]
        payload = asdict(record)
        payload.pop("order_type")
        payload.pop("stop_price")
        self.ledger._append("legacy-order-callback", "ORDER", payload)
        self.assertEqual(self.ledger.observe_order(self.fake.generation, "legacy-order-callback", record), "DUPLICATE")


if __name__ == "__main__":
    unittest.main()
