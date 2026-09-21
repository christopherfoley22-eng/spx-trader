"""Deterministic complete CALL/PUT dry-run flows; no IBKR imports."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

from executor_dry_run import DryRunError, DryRunExecutor
from simulation_evidence import (
    FeedStatus, OptionObservation, SPXObservation, VerifiedAccountSnapshot,
    VerifiedBrokerSnapshot, VerifiedOptionContract,
)

NY = ZoneInfo("America/New_York")
ACCOUNT = "SYNTHETIC_ONLY"


class Scenario:
    def __init__(self, directory, direction="CALL", exit_chunk=10):
        self.path = Path(directory) / "dry_run.db"
        self.direction = direction
        self.engine = DryRunExecutor(self.path, ACCOUNT, lambda _: True, exit_chunk)
        self.wall = datetime.now(NY)
        self.mono = time.monotonic()
        self.seq = 0

    def restart(self):
        self.engine.close()
        self.engine = DryRunExecutor(self.path, ACCOUNT, lambda _: True,
                                     self.engine.max_exit_chunk)
        return self.engine

    def step(self, price=None):
        self.seq += 1
        self.wall += timedelta(milliseconds=100)
        self.mono += 0.1
        return self.spx(price if price is not None else 5000.0)

    def spx(self, price=5000.0):
        return SPXObservation(price, FeedStatus.LIVE, self.wall, self.mono)

    def options(self, ask=10.0):
        right = "C" if self.direction == "CALL" else "P"
        contracts = [(5000.0, ask, 101), (5001.0, 9.0, 102), (4998.0, 8.0, 103)]
        return [OptionObservation(
            VerifiedOptionContract("SPX", "OPT", right,
                                   self.wall.strftime("%Y%m%d"), "SPXW", "100", "USD",
                                   strike, con_id), con_id, price-0.1, price,
            FeedStatus.LIVE, self.wall, self.mono)
            for strike, price, con_id in contracts]

    def account(self, funds):
        value = str(funds)
        return VerifiedAccountSnapshot(ACCOUNT, ACCOUNT, True, "USD", value,
                                       value, value, value, value, self.mono)

    def broker(self, **changes):
        status = self.engine.status()
        base = VerifiedBrokerSnapshot(ACCOUNT, ACCOUNT, (ACCOUNT,), True,
                                      status.owned, status.con_id, 0, self.mono)
        return replace(base, **changes)

    def start(self, qty, intent="first", funds=None, ask=10.0):
        if funds is None:
            funds = qty * ask * 100
        assert self.engine.press(intent, self.direction, self.broker(), self.wall, self.mono) == "ACCEPTED"
        plan = self.engine.authorize(intent, self.account(funds), self.spx(),
                                     self.options(ask), self.broker(), self.wall, self.mono)
        return plan

    def fill_entry(self):
        for index, (chunk, size) in enumerate(self.engine.entry_chunks()):
            assert self.engine.entry_event(
                "entry-fill-{}-{}".format(self.seq, index), chunk, size,
                fill_con_id=101,
                account=self.account(26000), spx=self.spx(), option=self.options()[0],
                snapshot=self.broker(), now_wall=self.wall, now_monotonic=self.mono,
            ) == "FILLED"
        assert self.engine.finish_entry(self.broker(), self.mono) == "OPEN"

    def tick(self, favorable, event=None):
        price = 5000.0 + favorable if self.direction == "CALL" else 5000.0 - favorable
        observation = self.step(price)
        return self.engine.observe(event or "tick-" + str(self.seq), observation,
                                   self.broker(), self.wall, self.mono)

    def exit_all(self):
        fills = []
        while self.engine.status().owned:
            chunk, size = self.engine.next_exit_chunk(self.broker(), self.mono)
            fills.append(size)
            self.engine.exit_event("exit-fill-" + str(len(fills)) + "-" + str(self.seq),
                                   chunk, size, fill_con_id=self.engine.status().con_id,
                                   snapshot=self.broker(),
                                   now_monotonic=self.mono)
        assert self.engine.confirm_flat(self.broker(), self.mono) == "CONFIRMED_FLAT"
        return fills


for direction in ("CALL", "PUT"):
    for qty in (1, 9, 10, 11, 19, 20, 21, 25, 26):
        for exit_chunk in (5, 10):
            with tempfile.TemporaryDirectory() as directory:
                s = Scenario(directory, direction, exit_chunk)
                plan = s.start(qty)
                expected = min(qty, 25)
                assert plan["quantity"] == expected
                assert all(1 <= x <= 10 for x in plan["chunks"])
                assert sum(plan["chunks"]) == expected
                s.fill_entry()
                assert s.engine.status().trades_today == 1
                assert s.tick(5.0) == "OPEN"
                assert s.engine.status().ride
                assert s.tick(2.01) == "OPEN"  # 2.99 reversal
                assert s.tick(2.0) == "EXITING"  # exactly 3.00 reversal
                assert s.engine.status().exit_reason == "LET_IT_RIDE_REVERSAL"
                fills = s.exit_all()
                assert fills == [min(exit_chunk, expected - i)
                                 for i in range(0, expected, exit_chunk)]
                assert s.engine.status().phase == "FLAT"
                assert s.engine.status().trades_today == 1
                completed = s.engine.completed_trades()
                assert len(completed) == 1
                assert completed[0][2:6] == (direction, 101, expected,
                                               "LET_IT_RIDE_REVERSAL")
                kinds = [kind for _, kind, _ in s.engine.journal()]
                for required in ("INTENT_ACCEPTED", "RECONCILED", "EVIDENCE_VALIDATED",
                                 "PLAN_AUTHORIZED", "ENTRY_CHUNK_PLANNED", "ENTRY_EVENT",
                                 "TRADE_COUNTED", "OPEN_CONFIRMED", "HIGH_WATER",
                                 "LET_IT_RIDE", "EXIT_TRIGGERED", "EXIT_CHUNK_PLANNED",
                                 "EXIT_EVENT", "CONFIRMED_FLAT"):
                    assert required in kinds, required
                s.engine.close()

for direction in ("CALL", "PUT"):
    with tempfile.TemporaryDirectory() as directory:
        s = Scenario(directory, direction)
        assert s.start(1)["quantity"] == 1
        s.fill_entry()
        assert s.tick(-3.24) == "OPEN"
        assert s.tick(-3.25) == "EXITING"
        assert s.engine.status().exit_reason == "INITIAL_STOP"
        s.exit_all()
        s.engine.close()
    with tempfile.TemporaryDirectory() as directory:
        s = Scenario(directory, direction)
        s.start(1)
        s.fill_entry()
        assert s.tick(4.80) == "OPEN"
        assert s.tick(4.90) == "OPEN"
        assert s.tick(4.80) == "OPEN"
        assert s.engine.status().peak == ("5004.9" if direction == "CALL" else "4995.1")
        assert s.tick(3.91) == "OPEN"
        assert s.tick(3.90) == "EXITING"
        assert s.engine.status().exit_reason == "NEAR_WINNER_REVERSAL"
        s.exit_all()
        s.engine.close()

print("COMPLETE VERIFIED DRY-RUN CALL/PUT LIFECYCLE PASS")
