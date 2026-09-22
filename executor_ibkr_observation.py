"""Offline-testable normalization of read-only IBKR callbacks.

This module owns no socket or EClient. A future, separately validated read-only
transport may feed callbacks into it. It cannot issue broker requests or orders.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
from threading import RLock

from simulation_evidence import (
    EvidenceClock, EvidenceError, FeedStatus, OptionObservation, SPXObservation,
    VerifiedAccountSnapshot, VerifiedBrokerSnapshot, VerifiedOptionContract,
    conservative_usable_funds, validate_broker_snapshot, validate_contract,
)

SIZING_TAGS = ("AvailableFunds", "BuyingPower", "ExcessLiquidity",
               "TotalCashValue", "SettledCash")
MARKET_TYPES = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}


@dataclass(frozen=True)
class ObservationResult:
    state: str
    reasons: tuple
    account: object
    broker: object
    spx: object
    option: object
    contract: object
    order_scope: str = "API_VISIBLE_ONLY"
    executable: bool = False


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise EvidenceError("Malformed numeric callback")
    if not result.is_finite():
        raise EvidenceError("Non-finite numeric callback")
    return result


class ReadOnlyIBKREvidenceAdapter:
    """A single session-scoped callback reducer with no broker client reference."""

    def __init__(self, selected_account=None):
        if selected_account is not None and (not isinstance(selected_account, str) or not selected_account.strip()):
            raise EvidenceError("Invalid runtime account selection")
        self.requested_account = selected_account
        self.lock = RLock()
        self.generation = 0
        self.reset()

    def reset(self):
        with self.lock:
            self.generation += 1
            self.connected = False
            self.accounts = None
            self.selected = None
            self.summary = {}
            self.summary_done = False
            self.positions = {}
            self.positions_done = False
            self.positions_received = float("nan")
            self.orders = {}
            self.orders_done = False
            self.orders_received = float("nan")
            self.underlying = None
            self.underlying_done = False
            self.chains = []
            self.chain_done = False
            self.exact = {}
            self.contract_done = False
            self.market = {"SPX": {}, "OPTION": {}}
            self.clock = EvidenceClock()
            self.invalid = None

    def _current(self, generation):
        if generation != self.generation or not self.connected:
            raise EvidenceError("Disconnected or old callback generation")
        if self.invalid:
            raise EvidenceError("Contradictory callback evidence")

    def handshake(self, generation):
        with self.lock:
            if generation != self.generation:
                raise EvidenceError("Old connection handshake")
            self.connected = True

    def managed_accounts(self, generation, accounts):
        with self.lock:
            self._current(generation)
            if (not isinstance(accounts, (tuple, list)) or not accounts
                    or any(not isinstance(x, str) or not x.strip() for x in accounts)
                    or len(set(accounts)) != len(accounts)):
                self.invalid = "ACCOUNT EVIDENCE INCOMPLETE"
                raise EvidenceError(self.invalid)
            accounts = tuple(accounts)
            if self.accounts is not None and self.accounts != accounts:
                self.invalid = "CONTRADICTORY ACCOUNT EVIDENCE"
                raise EvidenceError(self.invalid)
            self.accounts = accounts
            self.selected = self.requested_account or (accounts[0] if len(accounts) == 1 else None)
            if self.selected not in accounts:
                self.selected = None

    def account_value(self, generation, account, tag, currency, value, received):
        with self.lock:
            self._current(generation)
            if self.summary_done or not self.accounts or account not in self.accounts:
                self.invalid = "CONTRADICTORY ACCOUNT EVIDENCE"
                raise EvidenceError(self.invalid)
            if tag not in SIZING_TAGS:
                return
            if (currency != "USD" or not isinstance(received, (int, float))
                    or not math.isfinite(received)):
                self.invalid = "ACCOUNT EVIDENCE INCOMPLETE"
                raise EvidenceError(self.invalid)
            try:
                numeric = _decimal(value)
            except EvidenceError:
                self.invalid = "ACCOUNT EVIDENCE INCOMPLETE"
                raise
            key = (account, tag)
            if key in self.summary and self.summary[key][0] != numeric:
                self.invalid = "CONTRADICTORY ACCOUNT EVIDENCE"
                raise EvidenceError(self.invalid)
            self.summary[key] = (numeric, received)

    def account_end(self, generation):
        with self.lock:
            self._current(generation)
            self.summary_done = True

    def position(self, generation, account, con_id, quantity):
        with self.lock:
            self._current(generation)
            if self.positions_done or not self.accounts or account not in self.accounts:
                self.invalid = "CONTRADICTORY BROKER EVIDENCE"
                raise EvidenceError(self.invalid)
            qty = _decimal(quantity)
            if qty != int(qty) or qty < 0 or (qty and not _positive_int(con_id)):
                self.invalid = "INVALID BROKER POSITION"
                raise EvidenceError(self.invalid)
            key = (account, con_id)
            if key in self.positions and self.positions[key] != int(qty):
                self.invalid = "CONTRADICTORY BROKER EVIDENCE"
                raise EvidenceError(self.invalid)
            self.positions[key] = int(qty)

    def position_end(self, generation, received):
        with self.lock:
            self._current(generation)
            if not isinstance(received, (int, float)) or not math.isfinite(received):
                raise EvidenceError("Position completion receipt invalid")
            self.positions_done = True
            self.positions_received = received

    def open_order(self, generation, order_id, account, con_id):
        with self.lock:
            self._current(generation)
            if (self.orders_done or not isinstance(order_id, int) or isinstance(order_id, bool)
                    or not self.accounts or account not in self.accounts or not _positive_int(con_id)):
                self.invalid = "CONTRADICTORY OPEN ORDER EVIDENCE"
                raise EvidenceError(self.invalid)
            value = (account, con_id)
            if order_id in self.orders and self.orders[order_id] != value:
                self.invalid = "CONTRADICTORY OPEN ORDER EVIDENCE"
                raise EvidenceError(self.invalid)
            self.orders[order_id] = value

    def open_order_end(self, generation, received):
        with self.lock:
            self._current(generation)
            if not isinstance(received, (int, float)) or not math.isfinite(received):
                raise EvidenceError("Order completion receipt invalid")
            self.orders_done = True
            self.orders_received = received

    def spx_details(self, generation, symbol, sec_type, currency, con_id, exchange):
        with self.lock:
            self._current(generation)
            value = (symbol, sec_type, currency, con_id, exchange)
            if (self.underlying_done or symbol != "SPX" or sec_type != "IND"
                    or currency != "USD" or not _positive_int(con_id) or not exchange
                    or self.underlying is not None and self.underlying != value):
                self.invalid = "AMBIGUOUS SPX IDENTITY"
                raise EvidenceError(self.invalid)
            self.underlying = value

    def spx_details_end(self, generation):
        with self.lock:
            self._current(generation)
            self.underlying_done = True

    def chain(self, generation, exchange, underlying_con_id, trading_class,
              multiplier, expirations, strikes):
        with self.lock:
            self._current(generation)
            if self.chain_done or not self.underlying or underlying_con_id != self.underlying[3]:
                self.invalid = "CONTRADICTORY CHAIN EVIDENCE"
                raise EvidenceError(self.invalid)
            if trading_class != "SPXW" or multiplier != "100":
                return
            if (not isinstance(expirations, (set, tuple, list)) or not isinstance(strikes, (set, tuple, list))
                    or not exchange or any(not isinstance(x, str) or len(x) != 8 or not x.isdigit() for x in expirations)
                    or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in strikes)):
                self.invalid = "INVALID SPXW CHAIN"
                raise EvidenceError(self.invalid)
            self.chains.append((exchange, frozenset(expirations), frozenset(strikes)))

    def chain_end(self, generation):
        with self.lock:
            self._current(generation)
            self.chain_done = True

    def exact_option(self, generation, request_key, contract, exchange):
        with self.lock:
            self._current(generation)
            if self.contract_done or not isinstance(contract, VerifiedOptionContract) or not exchange:
                self.invalid = "INVALID EXACT CONTRACT"
                raise EvidenceError(self.invalid)
            if request_key in self.exact and self.exact[request_key] != (contract, exchange):
                self.invalid = "AMBIGUOUS EXACT CONTRACT"
                raise EvidenceError(self.invalid)
            self.exact[request_key] = (contract, exchange)

    def exact_option_end(self, generation):
        with self.lock:
            self._current(generation)
            self.contract_done = True

    def market_type(self, generation, instrument, code):
        with self.lock:
            self._current(generation)
            if instrument not in self.market:
                raise EvidenceError("Unknown market instrument")
            slot = self.market[instrument]
            if slot.get("type") is not None and slot["type"] != code:
                slot.clear()  # A type switch invalidates all old ticks.
            slot["type"] = code

    def market_tick(self, generation, instrument, field, value, source_time, received,
                    con_id=None):
        """Legacy diagnostic capture only; never authoritative for readiness."""
        with self.lock:
            self._current(generation)
            if instrument not in self.market or field not in ({"price"} if instrument == "SPX" else {"bid", "ask"}):
                raise EvidenceError("Unknown market field")
            slot = self.market[instrument]
            if slot.get("type") != 1 or not isinstance(source_time, datetime) or source_time.tzinfo is None:
                slot.pop(field, None)
                raise EvidenceError("Live source timestamp not proven")
            try:
                numeric = _decimal(value)
            except EvidenceError:
                slot.pop(field, None)
                raise
            if numeric <= 0 or not isinstance(received, (int, float)) or not math.isfinite(received):
                slot.pop(field, None)
                raise EvidenceError("Malformed market tick")
            if instrument == "OPTION" and not _positive_int(con_id):
                slot.pop(field, None)
                raise EvidenceError("Exact quote conId missing")
            previous = slot.get(field)
            if previous and (source_time < previous[1] or received < previous[2]
                             or source_time == previous[1] and numeric != previous[0]):
                self.invalid = "BACKWARD OR CONTRADICTORY MARKET DATA"
                raise EvidenceError(self.invalid)
            if instrument == "OPTION" and slot.get("con_id") not in (None, con_id):
                self.invalid = "QUOTE CONTRACT CHANGED"
                raise EvidenceError(self.invalid)
            slot[field] = (numeric, source_time, received)
            if instrument == "OPTION":
                slot["con_id"] = con_id

    def observe(self, now_wall, now_monotonic, direction=None, contract_key=None,
                persisted_position=None):
        """Return redacted readiness. Never authorizes an intent or economic action."""
        with self.lock:
            reasons = []
            account = broker = spx = option = contract = None
            if not self.connected:
                return ObservationResult("DISCONNECTED", ("READ-ONLY DISCONNECTED",), None, None, None, None, None)
            if self.invalid:
                reasons.append(self.invalid)
            if not self.accounts or not self.selected:
                reasons.append("ACCOUNT SELECTION REQUIRED")
            if self.selected:
                values = [self.summary.get((self.selected, tag)) for tag in SIZING_TAGS]
                account = VerifiedAccountSnapshot(
                    self.selected, self.selected, self.summary_done and all(v is not None for v in values),
                    "USD", *(str(v[0]) if v else None for v in values),
                    min(v[1] for v in values) if all(v is not None for v in values) else float("nan"))
                try:
                    conservative_usable_funds(account, self.selected, now_monotonic)
                except EvidenceError:
                    reasons.append("ACCOUNT EVIDENCE INCOMPLETE OR STALE")
                positions = [(cid, qty) for (acct, cid), qty in self.positions.items()
                             if acct == self.selected and qty]
                # Any other-account position is kept visible as a contradiction.
                other = any(acct != self.selected and qty for (acct, _), qty in self.positions.items())
                broker = VerifiedBrokerSnapshot(
                    self.selected, self.selected, self.accounts,
                    self.positions_done and self.orders_done and not other and len(positions) <= 1,
                    positions[0][1] if len(positions) == 1 else 0,
                    positions[0][0] if len(positions) == 1 else None,
                    len(self.orders),
                    min(self.positions_received, self.orders_received))
                try:
                    validate_broker_snapshot(broker, self.selected, now_monotonic)
                except EvidenceError:
                    reasons.append("BROKER EVIDENCE INCOMPLETE OR STALE")
                if self.orders:
                    reasons.append("UNEXPECTED API-VISIBLE OPEN ORDER")
                if persisted_position is None:
                    reasons.append("RECONCILIATION REQUIRED")
                elif broker.position_qty != persisted_position[0] or broker.con_id != persisted_position[1]:
                    reasons.append("BROKER/LOCAL POSITION DISAGREEMENT")
            if not self.underlying_done or not self.underlying or not self.chain_done:
                reasons.append("SECURITY DEFINITION INCOMPLETE")
            if direction is not None:
                item = self.exact.get(contract_key)
                if not self.contract_done or not item:
                    reasons.append("EXACT OPTION CONTRACT UNVERIFIED")
                else:
                    contract = item[0]
                    try:
                        validate_contract(contract, direction, now_wall,
                                          lambda t: any(t.strftime("%Y%m%d") in exps and contract.strike in strikes
                                                        for _, exps, strikes in self.chains))
                    except EvidenceError:
                        reasons.append("EXACT OPTION CONTRACT INVALID")
            for instrument in ("SPX", "OPTION"):
                slot = self.market[instrument]
                if slot.get("type") != 1:
                    reasons.append(instrument + " MARKET DATA " + MARKET_TYPES.get(slot.get("type"), "UNAVAILABLE"))
            # This adapter predates source-proven callback ingestion. Its caller-
            # supplied datetimes cannot authorize readiness. All future market
            # readiness must come from executor_market_ingestion.
            reasons.extend(("MARKET DATA UNAVAILABLE OR STALE",
                            "AUTHORITATIVE MARKET INGESTION REDUCER REQUIRED"))
            return ObservationResult("READ-ONLY CONNECTED", tuple(dict.fromkeys(reasons)),
                                     account, broker, spx, option, contract)
