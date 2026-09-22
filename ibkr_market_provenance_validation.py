"""Controlled read-only callback provenance probe; never Executor evidence.

Run only with explicit approval. It connects only through HOST/PORT imported
from the locked localhost read-only probe. Prices, conIds, strikes, expiration
values, and account identifiers are never printed. Raw tick-by-tick ``time``
integers are retained as opaque values and are never converted to UTC.
"""

import json
import math
import secrets
import threading
import time
from datetime import datetime

from ibapi.contract import Contract

from ibkr_read_only_validation import HOST, INFO_CODES, NY, PORT, ReadOnlyProbe
from executor_market_ingestion import VALIDATED_IBKR_LIMITATIONS

SPX_DETAILS = 8301
SPXW_CHAIN = 8302
OPTION_DETAILS = 8303
SPX_STANDARD = 8310
OPTION_STANDARD = 8311
SPX_LAST = 8320
SPX_ALL_LAST = 8321
SPX_MIDPOINT = 8322
OPTION_BID_ASK = 8323

REQUEST_LABELS = {
    SPX_DETAILS: "SPX_UNDERLYING_CONTRACT_DETAILS",
    SPXW_CHAIN: "CURRENT_NY_DATE_SPXW_SECURITY_DEFINITION",
    OPTION_DETAILS: "DYNAMIC_SPXW_OPTION_CONTRACT_DETAILS",
    SPX_STANDARD: "SPX_STANDARD_MARKET_DATA_CLASSIFICATION",
    OPTION_STANDARD: "EXACT_SPXW_STANDARD_MARKET_DATA_CLASSIFICATION",
    SPX_LAST: "SPX_TICK_BY_TICK_LAST",
    SPX_ALL_LAST: "SPX_TICK_BY_TICK_ALL_LAST",
    SPX_MIDPOINT: "SPX_TICK_BY_TICK_MIDPOINT",
    OPTION_BID_ASK: "EXACT_SPXW_TICK_BY_TICK_BID_ASK",
}
MARKET_TYPES = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}
SPX_TICK_REQUESTS = ((SPX_LAST, "Last"), (SPX_ALL_LAST, "AllLast"),
                     (SPX_MIDPOINT, "MidPoint"))
TICK_OBSERVE_SECONDS = 8.0
SAME_INSTRUMENT_PACING_SECONDS = 15.1
DETAIL_TIMEOUT_SECONDS = 8.0
DEFAULT_CLASSIFICATION_OBSERVE_SECONDS = 2.0


def choose_dynamic_contract(chains, underlying_con_id, ny_date):
    """Return a deterministic listed CALL specification without a market price."""
    strikes = sorted({strike for _, chain_id, trading_class, multiplier,
                      expirations, values in chains
                      if chain_id == underlying_con_id and trading_class == "SPXW"
                      and multiplier == "100" and ny_date in expirations
                      for strike in values
                      if isinstance(strike, (int, float)) and not isinstance(strike, bool)
                      and math.isfinite(strike) and strike > 0})
    if not strikes:
        raise ValueError("NO_CURRENT_DATE_SPXW_CONTRACT_CANDIDATE")
    return {"expiration": ny_date, "right": "C",
            "strike": strikes[(len(strikes) - 1) // 2],
            "selection": "DYNAMIC_CHAIN_LOWER_MEDIAN_NON_ATM_DIAGNOSTIC"}


def exact_underlying(contracts):
    matches = [item for item in contracts
               if item.symbol == "SPX" and item.secType == "IND"
               and item.currency == "USD" and isinstance(item.conId, int)
               and not isinstance(item.conId, bool) and item.conId > 0]
    identities = {item.conId for item in matches}
    if len(identities) != 1:
        raise ValueError("SPX_IDENTITY_AMBIGUOUS_OR_MISSING")
    return next(item for item in matches if item.conId in identities)


def exact_option(contracts, specification):
    matches = [item for item in contracts
               if item.symbol == "SPX" and item.secType == "OPT"
               and item.currency == "USD" and item.tradingClass == "SPXW"
               and item.multiplier == "100"
               and item.lastTradeDateOrContractMonth == specification["expiration"]
               and item.right == specification["right"]
               and item.strike == specification["strike"]
               and isinstance(item.conId, int) and not isinstance(item.conId, bool)
               and item.conId > 0]
    identities = {item.conId for item in matches}
    if len(identities) != 1:
        raise ValueError("OPTION_IDENTITY_AMBIGUOUS_OR_MISSING")
    return next(item for item in matches if item.conId in identities)


class ProvenanceProbe(ReadOnlyProbe):
    """Read-only callback recorder with permanently non-executable output."""

    def __init__(self, wall_clock=None, monotonic_clock=None):
        super().__init__(wall_clock, monotonic_clock)
        self.lock = threading.RLock()
        self.details = {SPX_DETAILS: [], OPTION_DETAILS: []}
        self.detail_ends = {SPX_DETAILS: threading.Event(),
                            OPTION_DETAILS: threading.Event()}
        self.chain_rows = []
        self.chain_end = threading.Event()
        self.market_types = []
        self.market_type_phase = "DEFAULT_BEFORE_EXPLICIT_LIVE_REQUEST"
        self.market_type_events = {SPX_STANDARD: threading.Event(),
                                   OPTION_STANDARD: threading.Event()}
        self.raw_ticks = []
        self.tick_events = {request_id: threading.Event()
                            for request_id in (SPX_LAST, SPX_ALL_LAST,
                                               SPX_MIDPOINT, OPTION_BID_ASK)}
        self.request_started = set()
        self.cleanup_history = []

    def contractDetails(self, reqId, details):
        if reqId in self.details:
            with self.lock:
                self.details[reqId].append(details.contract)

    def contractDetailsEnd(self, reqId):
        if reqId in self.detail_ends:
            self.detail_ends[reqId].set()

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        if reqId == SPXW_CHAIN:
            with self.lock:
                self.chain_rows.append((exchange, underlyingConId, tradingClass,
                                        multiplier, set(expirations), set(strikes)))

    def securityDefinitionOptionParameterEnd(self, reqId):
        if reqId == SPXW_CHAIN:
            self.chain_end.set()

    def marketDataType(self, reqId, marketDataType):
        if reqId in REQUEST_LABELS:
            with self.lock:
                self.market_types.append({
                    "request_id": reqId,
                    "request_label": REQUEST_LABELS[reqId],
                    "classification": MARKET_TYPES.get(marketDataType, "UNAVAILABLE"),
                    "receipt_wall_utc": self._wall_clock().isoformat(),
                    "receipt_monotonic": self._monotonic_clock(),
                    "request_specific_callback": True,
                    "phase": self.market_type_phase,
                })
                if reqId in self.market_type_events:
                    self.market_type_events[reqId].set()

    def _record_tick(self, req_id, callback, raw_time, valid_values, metadata):
        if req_id not in self.tick_events:
            return
        with self.lock:
            self.raw_ticks.append({
                "request_id": req_id,
                "request_label": REQUEST_LABELS[req_id],
                "callback": callback,
                "raw_time_type": type(raw_time).__name__,
                "raw_time_value": raw_time if isinstance(raw_time, int)
                and not isinstance(raw_time, bool) else None,
                "time_semantics": "OPAQUE_IBKR_INTEGER_UNPROVEN",
                "time_precision": "UNPROVEN",
                "values_positive_finite": valid_values,
                "metadata": metadata,
                "receipt_wall_utc": self._wall_clock().isoformat(),
                "receipt_monotonic": self._monotonic_clock(),
            })
            self.tick_events[req_id].set()

    def tickByTickAllLast(self, reqId, tickType, raw_time, price, size,
                          tickAttribLast, exchange, specialConditions):
        valid = (isinstance(price, (int, float)) and not isinstance(price, bool)
                 and math.isfinite(price) and price > 0)
        self._record_tick(reqId, "Last" if tickType == 1 else "AllLast", raw_time,
                          valid, {"tick_type": tickType,
                                  "size_nonnegative": isinstance(size, int) and size >= 0,
                                  "exchange_supplied": bool(exchange),
                                  "past_limit": bool(tickAttribLast.pastLimit),
                                  "unreported": bool(tickAttribLast.unreported)})

    def tickByTickMidPoint(self, reqId, raw_time, midPoint):
        valid = (isinstance(midPoint, (int, float)) and not isinstance(midPoint, bool)
                 and math.isfinite(midPoint) and midPoint > 0)
        self._record_tick(reqId, "MidPoint", raw_time, valid, {})

    def tickByTickBidAsk(self, reqId, raw_time, bidPrice, askPrice, bidSize,
                         askSize, tickAttribBidAsk):
        valid_bid = (isinstance(bidPrice, (int, float)) and not isinstance(bidPrice, bool)
                     and math.isfinite(bidPrice) and bidPrice > 0)
        valid_ask = (isinstance(askPrice, (int, float)) and not isinstance(askPrice, bool)
                     and math.isfinite(askPrice) and askPrice > 0)
        self._record_tick(reqId, "BidAsk", raw_time,
                          valid_bid and valid_ask and askPrice >= bidPrice,
                          {"atomic_sides": True,
                           "locked": valid_bid and valid_ask and askPrice == bidPrice,
                           "crossed": valid_bid and valid_ask and bidPrice > askPrice,
                           "bid_size_nonnegative": isinstance(bidSize, int) and bidSize >= 0,
                           "ask_size_nonnegative": isinstance(askSize, int) and askSize >= 0,
                           "bid_past_low": bool(tickAttribBidAsk.bidPastLow),
                           "ask_past_high": bool(tickAttribBidAsk.askPastHigh)})

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId in (SPX_STANDARD, OPTION_STANDARD):
            with self.lock:
                self.raw_ticks.append({
                    "request_id": reqId,
                    "request_label": REQUEST_LABELS[reqId],
                    "callback": "tickPrice",
                    "tick_type": tickType,
                    "value_positive_finite": isinstance(price, (int, float))
                    and not isinstance(price, bool) and math.isfinite(price) and price > 0,
                    "source_time": None,
                    "receipt_wall_utc": self._wall_clock().isoformat(),
                    "receipt_monotonic": self._monotonic_clock(),
                })

    def sanitized_errors(self):
        rejected = {event["request_id"] for event in self.error_events
                    if event["code"] in {10168, 10090, 10189, 354}}
        result = []
        for event in self.safe_error_events():
            item = {**event,
                    "request_label": REQUEST_LABELS.get(
                        event["request_id"], event["request_label"]),
                    "primary_failure": event["code"] not in INFO_CODES,
                    "broker_mutation": False}
            if event["code"] == 300 and event["request_id"] in rejected:
                item["category"] = "CLEANUP_AFTER_REJECTED_SUBSCRIPTION"
                item["primary_failure"] = False
            elif event["request_id"] in REQUEST_LABELS and event["code"] not in INFO_CODES:
                item["category"] = "REQUEST_SPECIFIC"
            result.append(item)
        return result

    def result(self, option_resolved=False):
        with self.lock:
            callbacks = tuple(self.raw_ticks)
            classifications = tuple(self.market_types)
        option_atomic = any(item["request_id"] == OPTION_BID_ASK
                            and item["callback"] == "BidAsk"
                            and item["values_positive_finite"] for item in callbacks)
        option_request_live = any(item["request_id"] == OPTION_BID_ASK
                                  and item["classification"] == "LIVE"
                                  for item in classifications)
        return {
            "mode": "READ-ONLY CALLBACK PROVENANCE DIAGNOSTIC / NO ORDERS",
            "endpoint": "127.0.0.1:7496",
            "read_only_setting_api_verifiable": False,
            "option_contract": {
                "resolved": option_resolved,
                "selection": "DYNAMIC_CHAIN_LOWER_MEDIAN_NON_ATM_DIAGNOSTIC",
                "current_ny_date": option_resolved,
                "right": "CALL" if option_resolved else None,
                "strike_exposed": False,
                "expiration_exposed": False,
                "con_id_exposed": False,
            },
            "market_data_type_callbacks": classifications,
            "callback_evidence": callbacks,
            "option_atomic_bid_ask_observed": option_atomic,
            "option_bid_ask_request_live_callback_observed": option_request_live,
            "live_option_atomic_bid_ask_demonstrated": (option_atomic
                                                         and option_request_live),
            "cross_request_market_type_binding_proven": False,
            "timestamp_semantics_proven": False,
            "quarter_second_cross_instrument_sync_proven": False,
            "validated_ibkr_limitations": VALIDATED_IBKR_LIMITATIONS,
            "standard_market_data_provenance_assessment": (
                "INSUFFICIENT: tickPrice has receipt time only; last-trade time cannot "
                "timestamp option bid/ask or prove cross-instrument 0.25-second synchronization"
            ),
            "usable_by_executor": False,
            "executable": False,
            "errors": tuple(self.sanitized_errors()),
            "cleanup": tuple(self.cleanup_history),
        }


def _contract_for_option(specification):
    contract = Contract()
    contract.symbol = "SPX"
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.tradingClass = "SPXW"
    contract.multiplier = "100"
    contract.lastTradeDateOrContractMonth = specification["expiration"]
    contract.right = specification["right"]
    contract.strike = specification["strike"]
    return contract


def _emit(result):
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def main():
    app = ProvenanceProbe()
    thread = None
    active_market = []
    active_tick = []
    option_resolved = False
    exit_code = 1
    try:
        app.connect(HOST, PORT, clientId=secrets.randbelow(50000) + 10000)
        thread = threading.Thread(target=app.run, daemon=True)
        thread.start()
        if not app.ready.wait(DETAIL_TIMEOUT_SECONDS) or app.disconnected:
            return 1

        spx_query = Contract()
        spx_query.symbol = "SPX"
        spx_query.secType = "IND"
        spx_query.exchange = "CBOE"
        spx_query.currency = "USD"
        app.reqContractDetails(SPX_DETAILS, spx_query)
        if not app.detail_ends[SPX_DETAILS].wait(DETAIL_TIMEOUT_SECONDS):
            return 1
        spx = exact_underlying(app.details[SPX_DETAILS])

        app.reqSecDefOptParams(SPXW_CHAIN, "SPX", "", "IND", spx.conId)
        if not app.chain_end.wait(DETAIL_TIMEOUT_SECONDS):
            return 1
        specification = choose_dynamic_contract(
            app.chain_rows, spx.conId, datetime.now(NY).strftime("%Y%m%d"))
        app.reqContractDetails(OPTION_DETAILS, _contract_for_option(specification))
        if not app.detail_ends[OPTION_DETAILS].wait(DETAIL_TIMEOUT_SECONDS):
            return 1
        option = exact_option(app.details[OPTION_DETAILS], specification)
        option_resolved = True

        app.reqMktData(SPX_STANDARD, spx, "", False, False, [])
        active_market.append(SPX_STANDARD)
        app.reqMktData(OPTION_STANDARD, option, "", False, False, [])
        active_market.append(OPTION_STANDARD)
        deadline = time.monotonic() + DEFAULT_CLASSIFICATION_OBSERVE_SECONDS
        while (not all(event.is_set() for event in app.market_type_events.values())
               and time.monotonic() < deadline):
            time.sleep(0.05)
        app.market_type_phase = "AFTER_EXPLICIT_LIVE_PREFERENCE_REQUEST"
        app.reqMarketDataType(1)

        previous_start = None
        for request_id, tick_name in SPX_TICK_REQUESTS:
            if previous_start is not None:
                remaining = SAME_INSTRUMENT_PACING_SECONDS - (time.monotonic() - previous_start)
                if remaining > 0:
                    time.sleep(remaining)
            previous_start = time.monotonic()
            app.reqTickByTickData(request_id, spx, tick_name, 0, True)
            active_tick.append(request_id)
            app.request_started.add(request_id)
            app.tick_events[request_id].wait(TICK_OBSERVE_SECONDS)
            app.cancelTickByTickData(request_id)
            active_tick.remove(request_id)
            app.cleanup_history.append("cancelTickByTickData:" + REQUEST_LABELS[request_id])

        app.reqTickByTickData(OPTION_BID_ASK, option, "BidAsk", 0, True)
        active_tick.append(OPTION_BID_ASK)
        app.request_started.add(OPTION_BID_ASK)
        app.tick_events[OPTION_BID_ASK].wait(TICK_OBSERVE_SECONDS)
        exit_code = 0
        return exit_code
    except (ValueError, RuntimeError):
        return 1
    finally:
        for request_id in tuple(active_tick):
            if app.isConnected():
                app.cancelTickByTickData(request_id)
                app.cleanup_history.append(
                    "cancelTickByTickData:" + REQUEST_LABELS[request_id])
        for request_id in tuple(active_market):
            if app.isConnected():
                app.cancelMktData(request_id)
                app.cleanup_history.append("cancelMktData:" + REQUEST_LABELS[request_id])
        if app.isConnected():
            app.disconnect()
            app.cleanup_history.append("disconnect")
        if thread is not None:
            thread.join(timeout=2.0)
        _emit(app.result(option_resolved))


if __name__ == "__main__":
    raise SystemExit(main())
