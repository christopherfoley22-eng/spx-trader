"""Separate read-only SPXW entitlement diagnostic; never feeds Executor entry.

Run only after explicit approval. The delayed-SPX bootstrap and option quote
stay in this process and are never converted to production observations.
"""

import json
import math
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum

from ibkr_read_only_validation import HOST, PORT, NY, TIMEOUT, INFO_CODES, ReadOnlyProbe
from simulation_evidence import EvidenceError, VerifiedOptionContract, validate_contract

DELAYED_SPX_ID = 8204
LIVE_OPTION_ID = 8206
MARKET_TYPES = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}
MAX_RECEIPT_AGE = 1.0
MAX_SIDE_SKEW = 0.25
DELAYED_SPX_FIELDS = ((TickTypeEnum.DELAYED_LAST, "DELAYED_LAST"),
                      (TickTypeEnum.DELAYED_BID, "DELAYED_BID"),
                      (TickTypeEnum.DELAYED_ASK, "DELAYED_ASK"),
                      (TickTypeEnum.DELAYED_CLOSE, "DELAYED_CLOSE"))
DELAYED_SPX_NAMES = dict(DELAYED_SPX_FIELDS)
MAX_DIAGNOSTIC_OPTION_DETAIL_REQUESTS = 1


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def choose_diagnostic_strike(chains, underlying_con_id, expiration, delayed_spx):
    """Closest listed strike to delayed SPX; never claims true live ATM."""
    if not _positive(delayed_spx):
        raise EvidenceError("Delayed SPX bootstrap unavailable")
    strikes = {strike for _, chain_underlying_id, trading_class, multiplier, expirations, values in chains
               if chain_underlying_id == underlying_con_id and trading_class == "SPXW"
               and multiplier == "100" and expiration in expirations
               for strike in values if _positive(strike)}
    if not strikes:
        raise EvidenceError("No proven current-day SPXW strikes")
    return min(strikes, key=lambda strike: (abs(strike - delayed_spx), strike))


def choose_chain_median_strike(chains, underlying_con_id, expiration):
    """One deterministic listed strike without an underlying price or ATM claim."""
    strikes = sorted({strike for _, chain_id, trading_class, multiplier, expirations, values in chains
                      if chain_id == underlying_con_id and trading_class == "SPXW"
                      and multiplier == "100" and expiration in expirations
                      for strike in values if _positive(strike)})
    if not strikes:
        raise EvidenceError("No valid current-day SPXW diagnostic strike")
    return strikes[(len(strikes) - 1) // 2]  # Lower median for even lists.


def select_diagnostic_strike(evidence, chains, underlying_con_id, expiration, now_monotonic):
    if evidence.delayed_bootstrap_fresh(now_monotonic):
        return (choose_diagnostic_strike(chains, underlying_con_id, expiration,
                                         evidence.delayed_spx),
                "CLOSEST_TO_DELAYED_SPX_DIAGNOSTIC_ONLY")
    return (choose_chain_median_strike(chains, underlying_con_id, expiration),
            "NON_ATM_ENTITLEMENT_DIAGNOSTIC_CHAIN_LOWER_MEDIAN")


def resolve_diagnostic_option(details, chains, underlying_con_id, strike, session_time):
    """Require one exact SPXW contract for the selected listed strike."""
    if not isinstance(session_time, datetime) or session_time.tzinfo is None:
        raise EvidenceError("Diagnostic session time missing")
    today = session_time.astimezone(NY).strftime("%Y%m%d")
    exact = [c for c in details if c.symbol == "SPX" and c.secType == "OPT"
             and c.currency == "USD" and c.tradingClass == "SPXW" and c.multiplier == "100"
             and isinstance(c.exchange, str) and bool(c.exchange)
             and c.lastTradeDateOrContractMonth == today and c.strike == strike
             and c.right == "C" and isinstance(c.conId, int) and not isinstance(c.conId, bool)
             and c.conId > 0]
    if len(exact) != 1:
        raise EvidenceError("Exact diagnostic option ambiguous or missing")
    c = exact[0]
    verified = VerifiedOptionContract(c.symbol, c.secType, c.right,
                                      c.lastTradeDateOrContractMonth,
                                      c.tradingClass, c.multiplier, c.currency,
                                      c.strike, c.conId)
    validate_contract(verified, "CALL", session_time,
                      lambda t: any(chain[1] == underlying_con_id
                                    and chain[2] == "SPXW" and chain[3] == "100"
                                    and t.strftime("%Y%m%d") in chain[4]
                                    and verified.strike in chain[5]
                                    for chain in chains))
    return c, verified


class DiagnosticMarketEvidence:
    """Callback reducer whose result is permanently diagnostic-only."""

    def __init__(self):
        self.spx_type = None
        self.option_type = None
        self.type_callbacks = []
        self.delayed_spx = None
        self.delayed_spx_receipt = None
        self.delayed_spx_tick_type = None
        self.delayed_spx_fields = {}
        self.spx_price_callbacks = []
        self.option_live_requested = False
        self.option_bid = None
        self.option_ask = None
        self.option_bid_receipt = None
        self.option_ask_receipt = None
        self.raw_option_bid_seen = False
        self.raw_option_ask_seen = False
        self.option_price_callbacks = []
        self.delayed_option_seen = False
        self.source_timestamps = []  # Last-trade timestamps, not bid/ask timestamps.
        self.errors = []
        self.delayed_spx_error = None
        self.option_error = None

    def market_type(self, req_id, code, receipt_utc, receipt_monotonic):
        if req_id not in {DELAYED_SPX_ID, LIVE_OPTION_ID}:
            return
        self.type_callbacks.append({"request_id": req_id,
                                    "market_type": MARKET_TYPES.get(code, "UNAVAILABLE"),
                                    "receipt_utc": receipt_utc,
                                    "receipt_monotonic": receipt_monotonic})
        if req_id == DELAYED_SPX_ID:
            self.spx_type = code
            if code != 3:
                self.delayed_spx = None
                self.delayed_spx_receipt = None
                self.delayed_spx_tick_type = None
                self.delayed_spx_fields.clear()
            else:
                self.select_delayed_spx(receipt_monotonic)
        else:
            self.option_type = code
            if code != 1:
                self.option_bid = self.option_ask = None
                self.option_bid_receipt = self.option_ask_receipt = None

    def select_delayed_spx(self, now_monotonic):
        self.delayed_spx = self.delayed_spx_receipt = self.delayed_spx_tick_type = None
        if self.spx_type != 3 or self.delayed_spx_error is not None:
            return
        for tick_type, name in DELAYED_SPX_FIELDS:
            item = self.delayed_spx_fields.get(tick_type)
            if item is not None:
                value, received = item
                age = now_monotonic - received
                if math.isfinite(age) and 0 <= age <= MAX_RECEIPT_AGE:
                    self.delayed_spx = value
                    self.delayed_spx_receipt = received
                    self.delayed_spx_tick_type = name
                    return

    def price(self, req_id, tick_type, value, receipt_monotonic, receipt_utc=None):
        if req_id == DELAYED_SPX_ID:
            name = DELAYED_SPX_NAMES.get(tick_type)
            self.spx_price_callbacks.append({"tick_type": tick_type,
                                             "tick_label": name or "OTHER_PRICE_TICK",
                                             "valid_positive_finite": _positive(value),
                                             "receipt_utc": receipt_utc,
                                             "receipt_monotonic": receipt_monotonic,
                                             "source_timestamp_available": False})
            if name is not None:
                if _positive(value) and math.isfinite(receipt_monotonic):
                    self.delayed_spx_fields[tick_type] = (float(value), receipt_monotonic)
                else:
                    self.delayed_spx_fields.pop(tick_type, None)
                self.select_delayed_spx(receipt_monotonic)
            return
        if req_id != LIVE_OPTION_ID:
            return
        if tick_type in {1, 2, 66, 67}:
            self.option_price_callbacks.append({
                "request_id": req_id, "tick_type": tick_type,
                "tick_label": {1: "BID", 2: "ASK", 66: "DELAYED_BID", 67: "DELAYED_ASK"}[tick_type],
                "value": value if _positive(value) else None,
                "valid_positive_finite": _positive(value),
                "receipt_utc": receipt_utc, "receipt_monotonic": receipt_monotonic,
                "source_timestamp_available": False})
        if tick_type in {66, 67}:
            self.delayed_option_seen = True
            self.option_bid = self.option_ask = None
            self.option_bid_receipt = self.option_ask_receipt = None
            return
        if tick_type == 1:
            self.raw_option_bid_seen = True
        elif tick_type == 2:
            self.raw_option_ask_seen = True
        else:
            return
        # A global LIVE preference is insufficient. Require this request's
        # own LIVE callback before accepting subsequent live tick fields.
        if not self.option_live_requested or self.option_type != 1:
            return
        if not _positive(value):
            if tick_type == 1:
                self.option_bid = self.option_bid_receipt = None
            else:
                self.option_ask = self.option_ask_receipt = None
            return
        if tick_type == 1:
            self.option_bid, self.option_bid_receipt = float(value), receipt_monotonic
        else:
            self.option_ask, self.option_ask_receipt = float(value), receipt_monotonic

    def last_timestamp(self, req_id, tick_type, value, receipt_utc, receipt_monotonic):
        if req_id not in {DELAYED_SPX_ID, LIVE_OPTION_ID} or tick_type not in {45, 88}:
            return
        try:
            source = datetime.fromtimestamp(int(value), timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            source = None
        self.source_timestamps.append({
            "request_id": req_id, "tick_type": tick_type,
            "source_utc": source, "source_kind": "LAST_TRADE_NOT_BID_ASK",
            "receipt_utc": receipt_utc, "receipt_monotonic": receipt_monotonic})

    def error(self, req_id, code):
        self.errors.append((req_id, code))
        if code not in INFO_CODES:
            if req_id == DELAYED_SPX_ID:
                self.delayed_spx_error = code
                self.delayed_spx = self.delayed_spx_receipt = None
                self.delayed_spx_tick_type = None
                self.delayed_spx_fields.clear()
            elif req_id == LIVE_OPTION_ID:
                self.option_error = code
                self.option_bid = self.option_ask = None
                self.option_bid_receipt = self.option_ask_receipt = None

    def delayed_bootstrap_fresh(self, now_monotonic):
        self.select_delayed_spx(now_monotonic)
        age = None if self.delayed_spx_receipt is None else now_monotonic - self.delayed_spx_receipt
        return (self.delayed_spx_error is None and self.spx_type == 3 and _positive(self.delayed_spx)
                and age is not None and math.isfinite(age) and 0 <= age <= MAX_RECEIPT_AGE)

    def option_status(self, now_monotonic):
        kind = MARKET_TYPES.get(self.option_type, "UNAVAILABLE")
        if self.option_error is not None:
            return {"market_type": kind, "live_bid_ask_seen": False,
                    "shape_and_receipt_fresh": False,
                    "bid_ask_source_timestamp_available": False,
                    "usable_for_executor": False, "executable": False,
                    "reason": "OPTION_REQUEST_DENIED" if self.option_error == 10168 else "OPTION_REQUEST_ERROR"}
        if self.delayed_option_seen:
            return {"market_type": "DELAYED" if kind != "FROZEN" else kind,
                    "live_bid_ask_seen": False, "shape_and_receipt_fresh": False,
                    "bid_ask_source_timestamp_available": False,
                    "usable_for_executor": False, "executable": False,
                    "reason": "DELAYED_OPTION_TICKS"}
        if kind != "LIVE" or not self.option_live_requested:
            return {"market_type": kind, "live_bid_ask_seen": False,
                    "shape_and_receipt_fresh": False,
                    "bid_ask_source_timestamp_available": False,
                    "usable_for_executor": False, "executable": False,
                    "reason": "OPTION_NO_MARKET_TYPE_CALLBACK" if self.option_type is None and self.option_live_requested
                    else "OPTION_NOT_CONFIRMED_LIVE"}
        if not _positive(self.option_bid) or not _positive(self.option_ask):
            reason = "BID_OR_ASK_MISSING_OR_INVALID"
        elif self.option_ask < self.option_bid:
            reason = "CROSSED_OPTION_QUOTE"
        elif (self.option_bid_receipt is None or self.option_ask_receipt is None
              or not all(math.isfinite(x) for x in
                         (now_monotonic, self.option_bid_receipt, self.option_ask_receipt))
              or not 0 <= now_monotonic - self.option_bid_receipt <= MAX_RECEIPT_AGE
              or not 0 <= now_monotonic - self.option_ask_receipt <= MAX_RECEIPT_AGE
              or abs(self.option_bid_receipt - self.option_ask_receipt) > MAX_SIDE_SKEW):
            reason = "OPTION_QUOTE_RECEIPT_STALE_OR_SKEWED"
        else:
            reason = "BID_ASK_SOURCE_TIMESTAMPS_UNAVAILABLE"
        return {"market_type": kind,
                "live_bid_ask_seen": self.option_bid is not None and self.option_ask is not None,
                "shape_and_receipt_fresh": reason == "BID_ASK_SOURCE_TIMESTAMPS_UNAVAILABLE",
                "bid_ask_source_timestamp_available": False,
                "usable_for_executor": False, "executable": False, "reason": reason}

    def entitlement_conclusion(self, now_monotonic):
        if self.option_error == 10168:
            return "EXPLICIT_REQUEST_DENIAL"
        result = self.option_status(now_monotonic)
        if result["market_type"] == "LIVE" and result["shape_and_receipt_fresh"]:
            return "LIVE_QUOTE_OBSERVED_DIAGNOSTIC_ONLY"
        return "INCONCLUSIVE"


class OptionDiagnosticProbe(ReadOnlyProbe):
    def __init__(self, evidence=None):
        super().__init__()
        self.diagnostic = evidence or DiagnosticMarketEvidence()
        self.delayed_seen = threading.Event()
        self.option_seen = threading.Event()

    def marketDataType(self, reqId, marketDataType):
        self.diagnostic.market_type(reqId, marketDataType,
                                    datetime.now(timezone.utc).isoformat(), time.monotonic())
        if reqId == LIVE_OPTION_ID and marketDataType != 1:
            self.option_seen.set()

    def tickPrice(self, reqId, tickType, price, attrib):
        self.diagnostic.price(reqId, tickType, price, time.monotonic(),
                              datetime.now(timezone.utc).isoformat())
        if (reqId == DELAYED_SPX_ID and tickType == TickTypeEnum.DELAYED_LAST
                and self.diagnostic.delayed_spx is not None):
            self.delayed_seen.set()
        if reqId == LIVE_OPTION_ID and self.diagnostic.option_bid is not None and self.diagnostic.option_ask is not None:
            self.option_seen.set()

    def tickString(self, reqId, tickType, value):
        self.diagnostic.last_timestamp(reqId, tickType, value,
                                       datetime.now(timezone.utc).isoformat(), time.monotonic())

    def tickSize(self, reqId, tickType, size):
        if reqId == DELAYED_SPX_ID:
            self.diagnostic.spx_price_callbacks.append({
                "tick_type": tickType, "tick_label": "SIZE_TICK_NOT_PRICE",
                "receipt_utc": datetime.now(timezone.utc).isoformat(),
                "receipt_monotonic": time.monotonic(),
                "source_timestamp_available": False})

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
        self.diagnostic.error(reqId, errorCode)
        if reqId == DELAYED_SPX_ID:
            self.delayed_seen.set()
        if reqId == LIVE_OPTION_ID:
            self.option_seen.set()


def request_delayed_spx(app, contract):
    app.reqMarketDataType(3)  # Connection-wide preference for subsequent requests.
    app.reqMktData(DELAYED_SPX_ID, contract, "", False, False, [])


def request_exact_option_details(app, contract):
    count = getattr(app, "diagnostic_option_detail_requests", 0)
    if count >= MAX_DIAGNOSTIC_OPTION_DETAIL_REQUESTS:
        raise EvidenceError("Diagnostic option contract request limit reached")
    app.diagnostic_option_detail_requests = count + 1
    app.reqContractDetails(8105, contract)


def request_live_option(app, contract):
    app.cancelMktData(DELAYED_SPX_ID)  # End the diagnostic data stream first.
    app.reqMarketDataType(1)  # Connection-wide preference; not proof of LIVE.
    app.diagnostic.option_live_requested = True
    app.reqMktData(LIVE_OPTION_ID, contract, "", False, False, [])


def _print(key, value):
    print("{}={}".format(key, value))


def main():
    app = OptionDiagnosticProbe()
    thread = None
    try:
        app.connect(HOST, PORT, clientId=secrets.randbelow(50000) + 10000)
        thread = threading.Thread(target=app.run, daemon=True)
        thread.start()
        if not app.ready.wait(TIMEOUT) or app.disconnected or app.duplicate_client:
            _print("connection", "FAILED")
            return 1
        if not app.accounts_done.wait(TIMEOUT) or app.disconnected:
            _print("managed_accounts", "INCOMPLETE")
            return 1
        selected = os.environ.get("EXECUTOR_IBKR_ACCOUNT")
        if selected is None and len(app.managed_accounts) == 1:
            selected = app.managed_accounts[0]
        if selected not in app.managed_accounts or not selected:
            _print("account_selection", "BLOCKED")
            return 1
        _print("connection", "READ_ONLY_LOCALHOST_CONFIRMED")
        _print("account_selection", "UNAMBIGUOUS_RUNTIME")

        underlying = Contract()
        underlying.symbol, underlying.secType = "SPX", "IND"
        underlying.exchange, underlying.currency = "CBOE", "USD"
        app.reqContractDetails(8102, underlying)
        if not app.contract_done.wait(TIMEOUT) or app.disconnected:
            _print("spx_identity", "INCOMPLETE")
            return 1
        matches = [c for c in app.underlyings if c.symbol == "SPX" and c.secType == "IND"
                   and c.currency == "USD" and isinstance(c.conId, int) and c.conId > 0]
        if len(matches) != 1:
            _print("spx_identity", "AMBIGUOUS_OR_MISSING")
            return 1
        _print("spx_identity", "RESOLVED")

        # Resolve today's universe before starting the short-lived delayed
        # price bootstrap, so the price is still fresh at strike selection.
        app.reqSecDefOptParams(8103, "SPX", "", "IND", matches[0].conId)
        if not app.chain_done.wait(TIMEOUT) or app.disconnected:
            _print("spxw_chain", "INCOMPLETE")
            return 1
        today = datetime.now(NY).strftime("%Y%m%d")
        if not any(chain[1] == matches[0].conId and chain[2] == "SPXW"
                   and chain[3] == "100" and today in chain[4] for chain in app.chains):
            _print("spxw_chain", "NO_CURRENT_DAY_EXPIRATION")
            return 0

        request_delayed_spx(app, matches[0])
        app.delayed_seen.wait(TIMEOUT)
        delayed_at_selection = app.diagnostic.delayed_bootstrap_fresh(time.monotonic())
        _print("spx_market_type", MARKET_TYPES.get(app.diagnostic.spx_type, "UNAVAILABLE"))
        _print("delayed_spx_bootstrap_fresh", delayed_at_selection)
        _print("delayed_spx_selected_tick_type", app.diagnostic.delayed_spx_tick_type or "NONE")
        _print("delayed_spx_entry_eligible", False)
        try:
            strike, selection = select_diagnostic_strike(
                app.diagnostic, app.chains, matches[0].conId, today, time.monotonic())
            if selection.startswith("NON_ATM"):
                _print("diagnostic_state", "BLOCKED_NO_FRESH_DELAYED_SPX")
        except EvidenceError:
            _print("diagnostic_state", "NO_VALID_DYNAMIC_DIAGNOSTIC_STRIKE")
            return 0
        _print("diagnostic_strike_selection", selection)
        _print("option_market_request_id", LIVE_OPTION_ID)
        _print("option_market_request_label", "EXACT_SPXW_OPTION_LIVE_DIAGNOSTIC_MARKET_DATA")
        _print("diagnostic_option_detail_request_limit", MAX_DIAGNOSTIC_OPTION_DETAIL_REQUESTS)

        option = Contract()
        option.symbol, option.secType = "SPX", "OPT"
        option.exchange, option.currency = "SMART", "USD"
        option.tradingClass, option.multiplier = "SPXW", "100"
        option.lastTradeDateOrContractMonth = today
        option.strike, option.right = strike, "C"
        request_exact_option_details(app, option)
        if not app.option_details_done.wait(TIMEOUT) or app.disconnected:
            _print("exact_option", "INCOMPLETE")
            return 1
        try:
            exact, _ = resolve_diagnostic_option(app.option_details, app.chains,
                                                 matches[0].conId, strike,
                                                 datetime.now(NY))
        except EvidenceError:
            _print("exact_option", "INVALID")
            return 0
        _print("exact_option", "VERIFIED_DIAGNOSTIC_ONLY")
        _print("exact_option_identity", json.dumps({
            "symbol": exact.symbol, "security_type": exact.secType,
            "trading_class": exact.tradingClass, "right": exact.right,
            "expiration": exact.lastTradeDateOrContractMonth,
            "strike": exact.strike, "multiplier": exact.multiplier,
            "currency": exact.currency, "routing": exact.exchange,
            "con_id_verified": True}, sort_keys=True))

        request_live_option(app, exact)
        app.option_seen.wait(TIMEOUT)
        result = app.diagnostic.option_status(time.monotonic())
        _print("option_market_type", result["market_type"])
        _print("option_live_type_callback_received", app.diagnostic.option_type == 1)
        _print("option_raw_bid_seen", app.diagnostic.raw_option_bid_seen)
        _print("option_raw_ask_seen", app.diagnostic.raw_option_ask_seen)
        _print("option_delayed_ticks_seen", app.diagnostic.delayed_option_seen)
        _print("option_live_bid_ask_seen", result["live_bid_ask_seen"])
        _print("option_quote_receipt_fresh_and_valid", result["shape_and_receipt_fresh"])
        _print("option_bid_ask_source_timestamp_available", result["bid_ask_source_timestamp_available"])
        _print("diagnostic_reason", result["reason"])
        _print("option_entitlement_conclusion", app.diagnostic.entitlement_conclusion(time.monotonic()))
        _print("option_live_availability_demonstrated", result["market_type"] == "LIVE"
               and result["shape_and_receipt_fresh"])
        _print("executor_usable", result["usable_for_executor"])
        _print("executable", result["executable"])
        return 0
    finally:
        for req_id in (DELAYED_SPX_ID, LIVE_OPTION_ID):
            try:
                app.cancelMktData(req_id)
            except Exception:
                pass
        app.disconnect()
        if thread is not None:
            thread.join(timeout=2)
        _print("option_live_request_sent", app.diagnostic.option_live_requested)
        _print("spx_market_type_final", MARKET_TYPES.get(app.diagnostic.spx_type, "UNAVAILABLE"))
        _print("option_market_type_final", app.diagnostic.option_status(time.monotonic())["market_type"])
        _print("market_data_type_callbacks", json.dumps(app.diagnostic.type_callbacks, sort_keys=True))
        _print("spx_price_callbacks", json.dumps(app.diagnostic.spx_price_callbacks, sort_keys=True))
        _print("source_timestamp_callbacks", json.dumps(app.diagnostic.source_timestamps, sort_keys=True))
        _print("option_bid_ask_callbacks", json.dumps(app.diagnostic.option_price_callbacks, sort_keys=True))
        _print("live_preference_callback_guaranteed", False)
        _print("error_timestamp_provenance", "LOCAL_RECEIPT_ONLY_NOT_EXCHANGE_SOURCE")
        _print("error_event_count", len(app.error_events))
        for index, event in enumerate(app.safe_error_events(), 1):
            _print("error_event_{}".format(index), json.dumps(event, sort_keys=True))
        _print("executable", False)
        _print("disconnected", not app.isConnected())


if __name__ == "__main__":
    raise SystemExit(main())
