"""Strict synthetic entry gateway around Mortificatio's offline lifecycle."""

from datetime import datetime
from dataclasses import asdict
import json
from typing import Callable, Sequence

from mortificatio_v01 import MortificatioV01, OptionQuote, SimulatedBroker
from simulation_evidence import (
    EvidenceClock, EvidenceError, OptionObservation, SPXObservation,
    VerifiedBrokerSnapshot, VerifiedOptionContract, validate_broker_snapshot,
)


class VerifiedDryRun:
    def __init__(self, lifecycle_db, strategy_db, broker: SimulatedBroker,
                 selected_account: str, session_has_expiration: Callable):
        if not callable(session_has_expiration):
            raise EvidenceError("Session calendar validator required")
        self.engine = MortificatioV01(lifecycle_db, strategy_db, broker)
        self.engine.lifecycle.bind_selected_account(selected_account)
        self.selected_account = selected_account
        self.session_has_expiration = session_has_expiration
        self.clock = EvidenceClock()
        self.authorized_contract = None
        self.authorized_bid = None
        self.authorized_ask = None
        self.engine.lifecycle.conn.execute(
            "CREATE TABLE IF NOT EXISTS verified_contract (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL)"
        )
        self.engine.lifecycle.conn.execute(
            "CREATE TABLE IF NOT EXISTS verified_market_watermark (key TEXT PRIMARY KEY, source_time TEXT NOT NULL)"
        )
        row = self.engine.lifecycle.conn.execute(
            "SELECT payload FROM verified_contract WHERE singleton=1"
        ).fetchone()
        if row is not None:
            try:
                saved = json.loads(row[0])
                self.authorized_contract = VerifiedOptionContract(**saved["contract"])
                self.authorized_bid = saved["bid"]
                self.authorized_ask = saved["ask"]
            except (KeyError, TypeError, ValueError):
                raise EvidenceError("Persisted verified contract corrupted")
        for key, source_time in self.engine.lifecycle.conn.execute(
            "SELECT key, source_time FROM verified_market_watermark"
        ):
            try:
                parsed_key = ("OPTION", int(key.split(":", 1)[1])) if key.startswith("OPTION:") else key
                self.clock.last_source[parsed_key] = datetime.fromisoformat(source_time)
            except (ValueError, IndexError):
                raise EvidenceError("Persisted market watermark corrupted")

    def close(self):
        self.engine.close()

    def reconcile(self, snapshot: VerifiedBrokerSnapshot, now_monotonic: float):
        validate_broker_snapshot(snapshot, self.selected_account, now_monotonic)
        state_decision = self.engine.lifecycle.reconcile_verified_broker(
            snapshot, self.selected_account, now_monotonic
        )
        simulated = self.engine.broker.snapshot()
        if (snapshot.position_qty != simulated.position_qty
                or snapshot.con_id != simulated.con_id
                or snapshot.open_order_count != simulated.open_order_count
                or snapshot.complete != simulated.complete):
            raise EvidenceError("Snapshot disagrees with simulated broker")
        local = self.engine.lifecycle.status()
        strategy = self.engine.strategy.status()
        if local.state == "FLAT":
            return "ENTRY_ELIGIBLE" if state_decision == "ENTRY_ELIGIBLE" and not strategy.active else "BLOCK_ENTRY"
        if local.state == "OPEN" and state_decision == "MANAGE_EXISTING":
            if (not strategy.active or strategy.con_id != local.con_id
                    or strategy.direction != local.direction
                    or self.authorized_contract is None
                    or self.authorized_contract.con_id != local.con_id
                    or snapshot.position_qty != local.quantity
                    or snapshot.con_id != local.con_id
                    or snapshot.open_order_count):
                return "BLOCK_ENTRY"
            return "MANAGE_EXISTING"
        return "BLOCK_ENTRY"

    def _validate_market(self, direction: str, spx: SPXObservation,
                         options: Sequence[OptionObservation],
                         now_wall: datetime, now_monotonic: float):
        if not options:
            raise EvidenceError("Exact option quotes missing")
        seen = set()
        for option in options:
            self.clock.validate_pair(direction, spx, option, now_wall,
                                     now_monotonic, self.session_has_expiration)
            if option.contract.con_id in seen:
                raise EvidenceError("Duplicate contract conId")
            seen.add(option.contract.con_id)
        for key, source_time in self.clock.last_source.items():
            serial_key = key if isinstance(key, str) else "OPTION:" + str(key[1])
            self.engine.lifecycle.conn.execute(
                "INSERT INTO verified_market_watermark(key, source_time) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET source_time=excluded.source_time",
                (serial_key, source_time.isoformat()),
            )

    def authorize_entry(self, direction: str, spx: SPXObservation,
                        options: Sequence[OptionObservation], usable_funds,
                        snapshot: VerifiedBrokerSnapshot, now_wall: datetime,
                        now_monotonic: float):
        if self.reconcile(snapshot, now_monotonic) != "ENTRY_ELIGIBLE":
            raise EvidenceError("New entry blocked by reconciliation")
        self._validate_market(direction, spx, options, now_wall, now_monotonic)
        candidates = [OptionQuote(o.contract.con_id, o.contract.strike, o.ask)
                      for o in options]
        plan = self.engine._authorize_entry_unverified(
            direction, spx.price, usable_funds, candidates
        )
        chosen = next(o for o in options if o.contract.con_id == plan.con_id)
        self.authorized_contract = chosen.contract
        self.authorized_bid = chosen.bid
        self.authorized_ask = chosen.ask
        payload = json.dumps({"contract": asdict(chosen.contract),
                              "bid": chosen.bid, "ask": chosen.ask})
        self.engine.lifecycle.conn.execute(
            "INSERT INTO verified_contract(singleton, payload) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
            (payload,),
        )
        return plan

    def simulate_entry(self, plan, spx: SPXObservation,
                       option: OptionObservation, snapshot: VerifiedBrokerSnapshot,
                       now_wall: datetime, now_monotonic: float,
                       fill_quantities=None):
        validate_broker_snapshot(snapshot, self.selected_account, now_monotonic)
        if self.reconcile(snapshot, now_monotonic) != "BLOCK_ENTRY":
            raise EvidenceError("Entry reservation missing")
        if self.engine.lifecycle.status().state != "ENTERING":
            raise EvidenceError("Entry reservation missing")
        if snapshot.position_qty != 0 or snapshot.open_order_count != 0:
            raise EvidenceError("Broker changed during entry reservation")
        self._validate_market(plan.direction, spx, [option], now_wall, now_monotonic)
        if (option.contract != self.authorized_contract
                or option.bid != self.authorized_bid
                or option.ask != self.authorized_ask
                or option.contract.con_id != plan.con_id
                or option.contract.strike != plan.strike):
            raise EvidenceError("Authorized exact contract or quote changed")
        if spx.price != plan.spx_price:
            raise EvidenceError("Authorized SPX price changed")
        return self.engine.simulate_entry(plan, fill_quantities)

    def process_market(self, spx: SPXObservation, option: OptionObservation,
                       snapshot: VerifiedBrokerSnapshot, now_wall: datetime,
                       now_monotonic: float):
        direction = self.engine.lifecycle.status().direction
        self._validate_market(direction, spx, [option], now_wall, now_monotonic)
        if option.contract != self.authorized_contract:
            raise EvidenceError("Managed contract metadata changed")
        if self.reconcile(snapshot, now_monotonic) != "MANAGE_EXISTING":
            raise EvidenceError("Existing position requires reconciliation")
        return self.engine.process_spx(spx.price)

    def process_spx_risk(self, spx: SPXObservation,
                         snapshot: VerifiedBrokerSnapshot,
                         now_wall: datetime, now_monotonic: float):
        """Evaluate an owned position when its option quote is unavailable."""
        if self.reconcile(snapshot, now_monotonic) != "MANAGE_EXISTING":
            raise EvidenceError("Existing position requires reconciliation")
        self.clock.validate_spx(spx, now_wall, now_monotonic)
        self.engine.lifecycle.conn.execute(
            "INSERT INTO verified_market_watermark(key, source_time) VALUES ('SPX', ?) "
            "ON CONFLICT(key) DO UPDATE SET source_time=excluded.source_time",
            (spx.source_time.isoformat(),),
        )
        return self.engine.process_spx(spx.price)
