"""Stage 1 premium-stop regressions; synthetic evidence only, no broker client."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from executor_dry_run import DryRunError, DryRunExecutor
from executor_replay import ReplayRunner, ReplaySession
from executor_replay_process import DurableReplayRunner
from executor_replay_sessions import ACCOUNT, make_session
from simulation_evidence import EvidenceError, FeedStatus, VerifiedBrokerSnapshot


class PremiumStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engines = []
        self.addCleanup(lambda: [engine.close() for engine in self.engines])

    def setup_trade(self, direction="CALL", qty=10, ask=10.0, name="trade"):
        self.path = self.root / (name + ".sqlite")
        self.session = ReplaySession.from_dict(make_session(name, direction, [0], quantity=qty))
        event = self.session.events[0]
        self.wall, self.mono = event.now_wall, event.monotonic
        self.spx = event.spx
        self.option = replace(event.options[0], ask=ask, bid=min(9.9, ask - 0.1))
        self.account = replace(event.account, available_funds="100000", buying_power="100000",
                               excess_liquidity="100000", total_cash_value="100000", settled_cash="100000")
        self.engine = DryRunExecutor(self.path, ACCOUNT, lambda _: True,
                                     initial_trading_day=self.wall.date().isoformat())
        self.engines.append(self.engine)
        self.engine.press(name, direction, self.snapshot(), self.wall, self.mono)
        # Exact quantity from supplied funds; refill checks use a larger reserve.
        funds = (str(Decimal(str(ask)) * qty * 100)
                 if isinstance(ask, (int, float)) and not isinstance(ask, bool)
                 and math.isfinite(ask) and ask > 0 else "100000")
        account = replace(self.account, available_funds=funds, excess_liquidity=funds,
                          total_cash_value=funds, settled_cash=funds)
        self.plan = self.engine.authorize(name, account, self.spx, [self.option],
                                          self.snapshot(), self.wall, self.mono)
        self.sequence = 0

    def snapshot(self, **changes):
        status = self.engine.status()
        return replace(VerifiedBrokerSnapshot(ACCOUNT, ACCOUNT, (ACCOUNT,), True,
                       status.owned, status.con_id, 0, self.mono), **changes)

    def fill(self, qty, price=None, event=None, rejected=False, **changes):
        self.sequence += 1
        chunk = self.engine.entry_chunks()[0][0]
        args = dict(fill_con_id=self.option.contract.con_id if qty else None,
                    simulated_price=price, account=self.account, spx=self.spx,
                    option=self.option, snapshot=self.snapshot(), now_wall=self.wall,
                    now_monotonic=self.mono)
        args.update(changes)
        return self.engine.entry_event(event or "fill:" + str(self.sequence), chunk,
                                        qty, rejected, **args)

    def open(self):
        for _, size in self.engine.entry_chunks():
            self.fill(size)
        self.engine.finish_entry(self.snapshot(), self.mono)

    def risk(self, bid, event=None, **changes):
        self.sequence += 1
        option = replace(self.option, bid=bid, **changes)
        return self.engine.observe_option(event or "risk:" + str(self.sequence), option,
                                          self.snapshot(), self.wall, self.mono)

    def spx_move(self, favorable):
        self.sequence += 1
        price = self.spx.price + (favorable if self.plan["direction"] == "CALL" else -favorable)
        return self.engine.observe("spx:" + str(self.sequence), replace(self.spx, price=price),
                                   self.snapshot(), self.wall, self.mono)

    def restart(self):
        self.engine.close()
        self.engines.remove(self.engine)
        self.engine = DryRunExecutor(self.path, ACCOUNT, lambda _: True)
        self.engines.append(self.engine)

    def test_call_and_put_exact_boundary_and_spx_independence(self):
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                self.setup_trade(direction, name=direction)
                self.open()
                self.assertEqual(self.engine.status().initial_stop_premium, "6.75")
                self.assertEqual(self.spx_move(-20), "OPEN")
                self.assertEqual(self.risk(6.750001), "OPEN")
                self.assertEqual(self.risk(6.75), "EXITING")
                trigger = next(json.loads(raw) for _, kind, raw in self.engine.journal()
                               if kind == "EXIT_TRIGGERED")
                self.assertEqual(trigger["basis"], "OPTION_PREMIUM")
                self.assertEqual(trigger["reason"], "INITIAL_STOP")

    def test_weighted_average_updates_up_down_and_survives_restart(self):
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                self.setup_trade(direction, name=direction)
                self.fill(4, 10)
                self.assertEqual(Decimal(self.engine.status().average_entry_premium), Decimal("10"))
                self.fill(2, 9)
                self.assertEqual(Decimal(self.engine.status().average_entry_premium), Decimal("58") / 6)
                self.fill(4, 10)
                self.assertEqual(Decimal(self.engine.status().average_entry_premium), Decimal("9.8"))
                self.assertEqual(Decimal(self.engine.status().initial_stop_premium), Decimal("6.55"))
                self.restart()
                self.assertEqual(Decimal(self.engine.status().initial_stop_premium), Decimal("6.55"))
                self.engine.finish_entry(self.snapshot(), self.mono)
                self.assertEqual(self.risk(6.56), "OPEN")
                self.assertEqual(self.risk(6.55), "EXITING")
                self.assertEqual(self.engine.status().trades_today, 1)

    def test_partial_entry_stop_halts_acquisition_and_exits_only_owned(self):
        for direction in ("CALL", "PUT"):
            with self.subTest(direction=direction):
                self.setup_trade(direction, qty=21, name=direction)
                self.fill(4)
                self.restart()
                self.assertEqual(self.risk(6.75), "EXITING")
                self.assertEqual(self.engine.status().owned, 4)
                self.assertEqual(self.engine.status().trades_today, 1)
                with self.assertRaises(DryRunError):
                    self.engine.entry_chunks()
                chunk, size = self.engine.next_exit_chunk(self.snapshot(), self.mono)
                self.assertEqual(size, 4)
                self.engine.exit_event("sell", chunk, 4, fill_con_id=self.option.contract.con_id,
                                        snapshot=self.snapshot(), now_monotonic=self.mono)
                self.restart()
                self.assertEqual(self.engine.confirm_flat(self.snapshot(), self.mono), "CONFIRMED_FLAT")
                self.assertEqual(self.engine.completed_trades()[0][4], 4)
                self.assertIsNone(self.engine.status().initial_stop_premium)

    def test_invalid_authorization_and_fill_stop_prices_are_atomic(self):
        for price in (3.25, 3.0, 0, -1, float("nan"), float("inf"), True):
            with self.subTest(price=price):
                with self.assertRaises((DryRunError, EvidenceError)):
                    self.setup_trade(ask=price, name="invalid-" + str(price))
                self.assertEqual(self.engine.status().phase, "INTENT")
                self.assertEqual(self.engine.status().owned, 0)
        self.setup_trade(name="invalid-fill")
        for price in (3.25, 0, -1, float("nan"), float("inf"), True, 10.01):
            with self.subTest(fill_price=price):
                before = self.engine.journal()
                with self.assertRaises((DryRunError, EvidenceError)):
                    self.fill(1, price)
                self.assertEqual(self.engine.journal(), before)
                self.assertEqual(self.engine.status().owned, 0)
                self.assertEqual(self.engine.status().trades_today, 0)
        self.fill(1, 3.250001)
        self.assertEqual(Decimal(self.engine.status().initial_stop_premium), Decimal("0.000001"))

    def test_duplicate_fill_cannot_recount_or_change_cost(self):
        self.setup_trade()
        self.fill(2, 9.5, event="same")
        before = self.engine.status()
        self.assertEqual(self.fill(2, 9.5, event="same"), "DUPLICATE")
        self.assertEqual(self.engine.status(), before)
        with self.assertRaises(DryRunError):
            self.fill(2, 9.6, event="same")
        self.assertEqual(self.engine.status(), before)

    def test_later_fill_with_nonpositive_projected_stop_is_rejected(self):
        self.setup_trade()
        self.fill(4, 3.3)
        before = self.engine.status(), self.engine.journal()
        with self.assertRaises(DryRunError):
            self.fill(1, 3.0)
        self.assertEqual((self.engine.status(), self.engine.journal()), before)
        self.assertEqual(Decimal(self.engine.status().initial_stop_premium), Decimal("0.05"))

    def test_first_fill_can_stop_atomically_and_block_later_fills(self):
        raw = make_session("wide-spread", "CALL", [], quantity=21, option_bids=[6.75])
        output = ReplayRunner(ReplaySession.from_dict(raw)).run(restart_after_actions=True)
        self.assertEqual(output["authorized_quantity"], 21)
        self.assertEqual(sum(fill["quantity"] for fill in output["entry_fills"]), 10)
        self.assertEqual(sum(fill["quantity"] for fill in output["exit_fills"]), 10)
        self.assertEqual(output["final_reconciliation"], "CONFIRMED_FLAT")
        self.assertEqual(output["exit_trigger"]["reason"], "INITIAL_STOP")

    def test_rejected_entry_never_creates_premium_or_trade_count(self):
        self.setup_trade(qty=1)
        self.fill(0, rejected=True)
        self.assertEqual(self.engine.finish_entry(self.snapshot(), self.mono), "FLAT")
        self.assertIsNone(self.engine.status().average_entry_premium)
        self.assertIsNone(self.engine.status().initial_stop_premium)
        self.assertEqual(self.engine.status().trades_today, 0)

    def test_invalid_risk_evidence_does_not_trigger_or_advance_watermark(self):
        self.setup_trade()
        self.open()
        changes = [dict(status=FeedStatus.DELAYED), dict(status=FeedStatus.FROZEN),
                   dict(status=FeedStatus.UNKNOWN), dict(source_time=self.wall - timedelta(seconds=2)),
                   dict(source_time=self.wall + timedelta(seconds=1)),
                   dict(received_monotonic=self.mono - 2), dict(quote_con_id=999),
                   dict(contract=replace(self.option.contract, strike=5005)),
                   dict(contract=replace(self.option.contract, right="P")),
                   dict(ask=6), dict(bid=-1), dict(bid=True), dict(bid=float("nan")),
                   dict(bid=float("inf"))]
        for change in changes:
            with self.subTest(change=change):
                before = self.engine.journal()
                option = replace(self.option, bid=6.75)
                with self.assertRaises(EvidenceError):
                    self.engine.observe_option("invalid", replace(option, **change), self.snapshot(),
                                               self.wall, self.mono)
                self.assertEqual(self.engine.journal(), before)
                self.assertEqual(self.engine.status().phase, "OPEN")
        self.assertEqual(self.risk(6.75), "EXITING")

    def test_zero_bid_and_gap_below_stop_trigger_without_spx(self):
        for bid in (0, 1):
            self.setup_trade(name="gap-" + str(bid))
            self.open()
            self.assertEqual(self.risk(bid), "EXITING")

    def test_missing_quote_and_broker_contradictions_fail_closed(self):
        self.setup_trade()
        self.open()
        with self.assertRaises(EvidenceError):
            self.engine.observe_option("missing", None, self.snapshot(), self.wall, self.mono)
        for change in (dict(complete=False), dict(open_order_count=1), dict(position_qty=9),
                       dict(con_id=999), dict(received_monotonic=self.mono-2)):
            with self.subTest(change=change), self.assertRaises((EvidenceError, DryRunError)):
                self.engine.observe_option("bad-broker", replace(self.option, bid=6.75),
                                           self.snapshot(**change), self.wall, self.mono)
        self.assertEqual(self.engine.status().phase, "OPEN")

    def test_option_event_replay_and_restart_are_idempotent(self):
        self.setup_trade()
        self.open()
        self.assertEqual(self.risk(6.75, "risk-id"), "EXITING")
        self.restart()
        before = self.engine.journal()
        self.assertEqual(self.risk(6.75, "risk-id"), "EXITING")
        self.assertEqual(self.engine.journal(), before)
        with self.assertRaises(DryRunError):
            self.risk(6.74, "risk-id")
        self.assertEqual(self.risk(9.9), "EXITING")
        self.assertEqual(sum(kind == "EXIT_TRIGGERED" for _, kind, _ in self.engine.journal()), 1)

    def test_backward_option_time_rejected_across_restart(self):
        self.setup_trade()
        self.open()
        self.risk(9.8, source_time=self.wall + timedelta(milliseconds=100))
        self.restart()
        with self.assertRaises(EvidenceError):
            self.risk(6.75)
        self.assertEqual(self.engine.status().phase, "OPEN")

    def test_profit_floor_and_trail_stay_spx_based(self):
        for direction in ("CALL", "PUT"):
            for peak, pullback, reason in ((4, 1.25, "PROFIT_PROTECTION_FLOOR"),
                                            (5, 2, "LET_IT_RIDE_REVERSAL")):
                self.setup_trade(direction, name=direction + str(peak))
                self.open()
                self.assertEqual(self.spx_move(peak), "OPEN")
                self.assertEqual(self.risk(9.9), "OPEN")
                self.assertEqual(self.spx_move(pullback), "EXITING")
                self.assertEqual(self.engine.status().exit_reason, reason)

    def test_premium_floor_remains_protective_after_profit_arms(self):
        for peak in (4, 5):
            self.setup_trade(name="armed-" + str(peak))
            self.open()
            self.spx_move(peak)
            self.assertEqual(self.risk(6.75), "EXITING")
            self.assertEqual(self.engine.status().exit_reason, "INITIAL_STOP")

    def test_partial_exit_does_not_change_entry_basis(self):
        self.setup_trade()
        self.fill(4, 9)
        self.fill(6, 10)
        self.engine.finish_entry(self.snapshot(), self.mono)
        self.risk(6.35)
        before = self.engine.status()
        chunk, _ = self.engine.next_exit_chunk(self.snapshot(), self.mono)
        self.engine.exit_event("partial-sell", chunk, 3, fill_con_id=self.option.contract.con_id,
                                snapshot=self.snapshot(), now_monotonic=self.mono)
        self.restart()
        self.assertEqual(self.engine.status().average_entry_premium, before.average_entry_premium)
        self.assertEqual(self.engine.status().initial_stop_premium, before.initial_stop_premium)
        self.assertEqual(self.engine.status().owned, 7)

    def test_fill_and_stop_audit_crashes_roll_back(self):
        self.setup_trade()
        original = self.engine._event
        def fail_count(event, kind, detail):
            if kind == "TRADE_COUNTED":
                raise RuntimeError("crash")
            return original(event, kind, detail)
        self.engine._event = fail_count
        with self.assertRaises(RuntimeError):
            self.fill(4, 9)
        self.restart()
        self.assertIsNone(self.engine.status().average_entry_premium)
        self.fill(4, 9)
        original = self.engine._event
        def fail_stop(event, kind, detail):
            if kind == "EXIT_TRIGGERED":
                raise RuntimeError("crash")
            return original(event, kind, detail)
        self.engine._event = fail_stop
        with self.assertRaises(RuntimeError):
            self.risk(5.75)
        self.restart()
        self.assertEqual(self.engine.status().phase, "ENTERING")
        self.assertEqual(self.engine.status().owned, 4)
        self.assertEqual(self.risk(5.75), "EXITING")

    def test_corrupt_or_legacy_active_cost_evidence_blocks_restart(self):
        for damage in ("price", "missing", "legacy"):
            self.setup_trade(name=damage)
            self.fill(4)
            if damage == "legacy":
                plan = dict(self.plan)
                del plan["premium_stop_version"]
                self.engine.db.execute("UPDATE controller SET plan=?", (json.dumps(plan),))
            else:
                raw = self.engine.db.execute("SELECT detail FROM audit WHERE kind='ENTRY_EVENT'").fetchone()[0]
                detail = json.loads(raw)
                if damage == "price":
                    detail["entry_premium"] = "NaN"
                else:
                    del detail["entry_premium"]
                self.engine.db.execute("UPDATE audit SET detail=? WHERE kind='ENTRY_EVENT'", (json.dumps(detail),))
            with self.assertRaises(DryRunError):
                self.engine.status()
            self.engine.close()
            self.engines.remove(self.engine)
            with self.assertRaises(DryRunError):
                DryRunExecutor(self.path, ACCOUNT, lambda _: True)

    def test_replay_premium_stop_with_flat_spx_and_process_restart(self):
        for direction in ("CALL", "PUT"):
            raw = make_session(direction, direction, [0, 0], option_bids=[9.9, 6.76, 6.75])
            session = ReplaySession.from_dict(raw)
            output = ReplayRunner(session).run(restart_after_actions=True)
            self.assertEqual(output["final_reconciliation"], "CONFIRMED_FLAT")
            self.assertEqual(output["exit_trigger"]["reason"], "INITIAL_STOP")
            self.assertEqual(output["maximum_adverse_spx_points"], "0")
            path = self.root / (direction + "-durable.sqlite")
            runner = DurableReplayRunner(session, path)
            runner.run(stop_index=1)
            runner.close()
            runner = DurableReplayRunner(session, path)
            try:
                runner.run()
                self.assertEqual(runner.controller.status().phase, "FLAT")
                self.assertEqual(runner.controller.completed_trades()[0][5], "INITIAL_STOP")
            finally:
                runner.close()

    def test_fresh_process_deaths_around_premium_stop(self):
        worker = Path(__file__).with_name("executor_replay_process_worker.py")
        for direction in ("CALL", "PUT"):
            raw = make_session(direction, direction, [0, 0], option_bids=[9.9, 6.76, 6.75])
            source = self.root / (direction + ".json")
            source.write_text(json.dumps(raw))
            for number, checkpoint in enumerate(("0:entry_committed:0", "2:before_option_risk",
                                                    "2:option_risk", "2:exit_committed:0")):
                with self.subTest(direction=direction, checkpoint=checkpoint):
                    db = self.root / (direction + str(number) + ".sqlite")
                    args = [sys.executable, "-B", str(worker), str(source), str(db)]
                    killed = subprocess.run(args + ["--stop", checkpoint], capture_output=True,
                                            text=True, timeout=20)
                    self.assertEqual(killed.returncode, 73, killed.stderr)
                    resumed = subprocess.run(args, capture_output=True, text=True, timeout=20)
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    result = json.loads(resumed.stdout)
                    self.assertEqual(result["phase"], "FLAT")
                    self.assertEqual(result["trade_count"], 1)
                    self.assertEqual(result["exit_reason"], "INITIAL_STOP")
                    self.assertEqual(sum(fill["quantity"] for fill in result["entry_fills"]), 11)
                    self.assertEqual(sum(fill["quantity"] for fill in result["exit_fills"]), 11)


if __name__ == "__main__":
    unittest.main()
