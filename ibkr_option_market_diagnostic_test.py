"""Offline synthetic SPXW entitlement diagnostic safety cases."""

from datetime import datetime, timezone
import inspect
import math

from ibkr_option_market_diagnostic import (
    Contract, DELAYED_SPX_ID, LIVE_OPTION_ID, DiagnosticMarketEvidence,
    OptionDiagnosticProbe, choose_diagnostic_strike, request_delayed_spx,
    request_live_option, request_exact_option_details, resolve_diagnostic_option,
    choose_chain_median_strike, select_diagnostic_strike,
    MAX_DIAGNOSTIC_OPTION_DETAIL_REQUESTS, TickTypeEnum,
)
from ibkr_read_only_validation import EClient
from simulation_evidence import EvidenceError

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
EXPIRATION = "20260922"
UNDERLYING_ID = 999
CHAINS = [("CBOE", UNDERLYING_ID, "SPXW", "100", {EXPIRATION}, {4990.0, 5000.0, 5010.0}),
          ("OTHER", 998, "SPXW", "100", {EXPIRATION}, {5001.0})]


def fails(fn):
    try:
        fn()
    except EvidenceError:
        return
    raise AssertionError("Unsafe diagnostic evidence accepted")


# Installed reqMarketDataType has one connection-wide argument, no request ID.
assert tuple(inspect.signature(EClient.reqMarketDataType).parameters) == ("self", "marketDataType")
assert [(name, getattr(TickTypeEnum, name)) for name in
        ("DELAYED_BID", "DELAYED_ASK", "DELAYED_LAST", "DELAYED_CLOSE")] == [
            ("DELAYED_BID", 66), ("DELAYED_ASK", 67),
            ("DELAYED_LAST", 68), ("DELAYED_CLOSE", 75)]

e = DiagnosticMarketEvidence()
e.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
e.price(DELAYED_SPX_ID, 4, 5001.0, 100.0)  # LIVE tick cannot bootstrap delayed.
assert not e.delayed_bootstrap_fresh(100.1)
e.price(DELAYED_SPX_ID, 68, 5001.0, 100.0)
assert e.delayed_bootstrap_fresh(100.1)
assert e.spx_type == 3 and e.option_status(100.1)["executable"] is False
assert e.delayed_spx_tick_type == "DELAYED_LAST"
assert choose_diagnostic_strike(CHAINS, UNDERLYING_ID, EXPIRATION, e.delayed_spx) == 5000.0
assert choose_diagnostic_strike(CHAINS, UNDERLYING_ID, EXPIRATION, 5008.0) == 5010.0
fails(lambda: choose_diagnostic_strike(CHAINS, UNDERLYING_ID, "20260923", 5001.0))
fails(lambda: choose_diagnostic_strike(CHAINS, UNDERLYING_ID, EXPIRATION, float("nan")))
assert not e.delayed_bootstrap_fresh(102.0)
e.market_type(DELAYED_SPX_ID, 1, NOW.isoformat(), 102.0)
assert e.delayed_spx is None and not e.delayed_bootstrap_fresh(102.0)
early = DiagnosticMarketEvidence()
early.price(DELAYED_SPX_ID, 68, 5002.0, 100.0)
assert early.delayed_spx is None
early.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.1)
assert early.delayed_bootstrap_fresh(100.2) and early.spx_type == 3
assert not early.option_status(100.2)["executable"]

# Each installed delayed price field is diagnostic-only; last wins over fallback.
for tick_type, label in ((66, "DELAYED_BID"), (67, "DELAYED_ASK"),
                         (68, "DELAYED_LAST"), (75, "DELAYED_CLOSE")):
    sample = DiagnosticMarketEvidence()
    sample.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
    sample.price(DELAYED_SPX_ID, tick_type, 5000.0, 100.1, NOW.isoformat())
    assert sample.delayed_bootstrap_fresh(100.2)
    assert sample.delayed_spx_tick_type == label
    assert sample.spx_price_callbacks[0]["tick_label"] == label
    assert sample.spx_price_callbacks[0]["receipt_utc"] == NOW.isoformat()
    assert sample.spx_price_callbacks[0]["source_timestamp_available"] is False
    assert not sample.option_status(100.2)["executable"]
    assert not sample.delayed_bootstrap_fresh(102.0)

sample = DiagnosticMarketEvidence()
sample.price(DELAYED_SPX_ID, 66, 4999.0, 100.0)
assert sample.delayed_spx is None  # Price before type callback stays unclassified.
sample.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.1)
assert sample.delayed_spx_tick_type == "DELAYED_BID"
sample.price(DELAYED_SPX_ID, 75, 4998.0, 100.2)
assert sample.delayed_spx_tick_type == "DELAYED_BID"
sample.price(DELAYED_SPX_ID, 68, 5001.0, 100.3)
assert sample.delayed_spx_tick_type == "DELAYED_LAST"
sample.price(DELAYED_SPX_ID, 68, -1, 100.4)
assert sample.delayed_spx_tick_type == "DELAYED_BID"
assert sample.delayed_bootstrap_fresh(100.5)
for bad in (0, -1, float("nan"), float("inf"), "5000", None):
    invalid = DiagnosticMarketEvidence()
    invalid.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
    invalid.price(DELAYED_SPX_ID, 68, bad, 100.1)
    assert not invalid.delayed_bootstrap_fresh(100.2)
sample.market_type(DELAYED_SPX_ID, 1, NOW.isoformat(), 100.6)
assert not sample.delayed_bootstrap_fresh(100.6)
sample.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.7)
assert not sample.delayed_bootstrap_fresh(100.7)  # Old delayed ticks cannot cross type switches.
late = DiagnosticMarketEvidence()
late.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
late.price(DELAYED_SPX_ID, 66, 4999.0, 101.0)
late.price(DELAYED_SPX_ID, 68, 5000.0, 100.0)  # Backward receipt cannot replace bid.
assert late.delayed_bootstrap_fresh(101.1)
assert late.delayed_spx_tick_type == "DELAYED_BID"
rollback = DiagnosticMarketEvidence()
rollback.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
rollback.price(DELAYED_SPX_ID, 66, 4999.0, 101.0)
assert not rollback.delayed_bootstrap_fresh(100.9)  # Clock/sequence rollback fails closed.

# No SPX price still permits one non-ATM entitlement diagnostic contract.
no_spx_price = DiagnosticMarketEvidence()
no_spx_price.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
assert not no_spx_price.delayed_bootstrap_fresh(100.1)
assert select_diagnostic_strike(no_spx_price, CHAINS, UNDERLYING_ID, EXPIRATION, 100.1) == (
    5000.0, "NON_ATM_ENTITLEMENT_DIAGNOSTIC_CHAIN_LOWER_MEDIAN")
fresh_price = DiagnosticMarketEvidence()
fresh_price.market_type(DELAYED_SPX_ID, 3, NOW.isoformat(), 100.0)
fresh_price.price(DELAYED_SPX_ID, 68, 5008.0, 100.1)
assert select_diagnostic_strike(fresh_price, CHAINS, UNDERLYING_ID, EXPIRATION, 100.2) == (
    5010.0, "CLOSEST_TO_DELAYED_SPX_DIAGNOSTIC_ONLY")
assert select_diagnostic_strike(e, CHAINS, UNDERLYING_ID, EXPIRATION, 100.1)[1] == (
    "NON_ATM_ENTITLEMENT_DIAGNOSTIC_CHAIN_LOWER_MEDIAN")  # Earlier delayed evidence was invalidated.
assert choose_chain_median_strike(CHAINS, UNDERLYING_ID, EXPIRATION) == 5000.0
assert choose_chain_median_strike([("CBOE", UNDERLYING_ID, "SPXW", "100",
                                    {EXPIRATION}, {5020.0, 5000.0})],
                                  UNDERLYING_ID, EXPIRATION) == 5000.0
fails(lambda: choose_chain_median_strike(CHAINS, UNDERLYING_ID, "20260923"))
fails(lambda: choose_chain_median_strike([("CBOE", UNDERLYING_ID, "SPXW", "100",
                                           {EXPIRATION}, {float("nan"), 0, -1})],
                                         UNDERLYING_ID, EXPIRATION))
assert MAX_DIAGNOSTIC_OPTION_DETAIL_REQUESTS == 1

contract = Contract()
contract.symbol, contract.secType = "SPX", "OPT"
contract.exchange = "SMART"
contract.currency, contract.tradingClass, contract.multiplier = "USD", "SPXW", "100"
contract.lastTradeDateOrContractMonth = EXPIRATION
contract.strike, contract.right, contract.conId = 5000.0, "C", 12345
resolved, verified = resolve_diagnostic_option([contract], CHAINS, UNDERLYING_ID, 5000.0, NOW)
assert resolved is contract and verified.con_id == contract.conId
fails(lambda: resolve_diagnostic_option([contract], CHAINS, UNDERLYING_ID, 5010.0, NOW))
fails(lambda: resolve_diagnostic_option([contract, contract], CHAINS, UNDERLYING_ID, 5000.0, NOW))
fails(lambda: resolve_diagnostic_option([contract], CHAINS, 998, 5000.0, NOW))
fails(lambda: resolve_diagnostic_option([], CHAINS, UNDERLYING_ID,
                                        choose_chain_median_strike(CHAINS, UNDERLYING_ID, EXPIRATION), NOW))


class FakeClient:
    def __init__(self):
        self.calls = []
        self.diagnostic = DiagnosticMarketEvidence()

    def reqMarketDataType(self, code):
        self.calls.append(("type", code))

    def reqMktData(self, req_id, contract, ticks, snapshot, regulatory, options):
        self.calls.append(("data", req_id, contract))

    def cancelMktData(self, req_id):
        self.calls.append(("end_data", req_id))

    def reqContractDetails(self, req_id, contract):
        self.calls.append(("details", req_id, contract))


fake = FakeClient()
request_exact_option_details(fake, contract)
fails(lambda: request_exact_option_details(fake, contract))
assert fake.calls == [("details", 8105, contract)]
fake.calls.clear()
request_delayed_spx(fake, "resolved SPX")
request_live_option(fake, contract)
assert fake.calls == [("type", 3), ("data", DELAYED_SPX_ID, "resolved SPX"),
                      ("end_data", DELAYED_SPX_ID), ("type", 1),
                      ("data", LIVE_OPTION_ID, contract)]
assert fake.diagnostic.option_live_requested

silent = DiagnosticMarketEvidence()
silent.option_live_requested = True
silent.price(LIVE_OPTION_ID, 1, 10.0, 100.0)
silent.price(LIVE_OPTION_ID, 2, 10.2, 100.1)
assert silent.option_status(100.2)["reason"] == "OPTION_NO_MARKET_TYPE_CALLBACK"
assert not silent.option_status(100.2)["live_bid_ask_seen"]
assert not silent.option_status(100.2)["usable_for_executor"]
assert silent.entitlement_conclusion(100.2) == "INCONCLUSIVE"
far_option = DiagnosticMarketEvidence()
far_option.option_live_requested = True
far_option.market_type(LIVE_OPTION_ID, 1, NOW.isoformat(), 100.0)
assert far_option.entitlement_conclusion(100.1) == "INCONCLUSIVE"


def live_evidence():
    result = DiagnosticMarketEvidence()
    result.option_live_requested = True
    result.market_type(LIVE_OPTION_ID, 1, NOW.isoformat(), 100.0)
    return result


e = DiagnosticMarketEvidence()
e.option_live_requested = True
e.price(LIVE_OPTION_ID, 1, 10.0, 100.0)  # Tick before per-request type callback.
e.price(LIVE_OPTION_ID, 2, 10.2, 100.1)
assert e.option_status(100.2)["reason"] == "OPTION_NO_MARKET_TYPE_CALLBACK"
e.market_type(LIVE_OPTION_ID, 1, NOW.isoformat(), 100.2)
assert e.option_status(100.2)["reason"] == "BID_OR_ASK_MISSING_OR_INVALID"
e.price(LIVE_OPTION_ID, 1, 10.0, 100.2)
e.price(LIVE_OPTION_ID, 2, 10.2, 100.3)
status = e.option_status(100.4)
assert status["market_type"] == "LIVE" and status["live_bid_ask_seen"]
assert status["shape_and_receipt_fresh"]
assert status["reason"] == "BID_ASK_SOURCE_TIMESTAMPS_UNAVAILABLE"
assert e.entitlement_conclusion(100.4) == "LIVE_QUOTE_OBSERVED_DIAGNOSTIC_ONLY"
assert [(item["tick_label"], item["request_id"]) for item in e.option_price_callbacks]
assert all(item["source_timestamp_available"] is False for item in e.option_price_callbacks)
assert not status["bid_ask_source_timestamp_available"]
assert not status["usable_for_executor"] and not status["executable"]
e.last_timestamp(LIVE_OPTION_ID, 45, "1600000000", NOW.isoformat(), 100.3)
assert e.source_timestamps[0]["source_kind"] == "LAST_TRADE_NOT_BID_ASK"
assert e.source_timestamps[0]["source_utc"] != e.source_timestamps[0]["receipt_utc"]
assert not e.option_status(100.4)["bid_ask_source_timestamp_available"]
assert e.option_status(102.0)["reason"] == "OPTION_QUOTE_RECEIPT_STALE_OR_SKEWED"

for bid, ask, expected in ((None, 10.2, "BID_OR_ASK_MISSING_OR_INVALID"),
                           (10.0, None, "BID_OR_ASK_MISSING_OR_INVALID"),
                           (10.3, 10.2, "CROSSED_OPTION_QUOTE"),
                           (float("nan"), 10.2, "BID_OR_ASK_MISSING_OR_INVALID"),
                           (10.0, 0, "BID_OR_ASK_MISSING_OR_INVALID"),
                           (math.inf, 10.2, "BID_OR_ASK_MISSING_OR_INVALID")):
    sample = live_evidence()
    if bid is not None:
        sample.price(LIVE_OPTION_ID, 1, bid, 100.0)
    if ask is not None:
        sample.price(LIVE_OPTION_ID, 2, ask, 100.1)
    assert sample.option_status(100.2)["reason"] == expected

e = live_evidence()
e.price(LIVE_OPTION_ID, 1, 10.0, 100.0)
e.price(LIVE_OPTION_ID, 2, 10.2, 100.4)
assert e.option_status(100.5)["reason"] == "OPTION_QUOTE_RECEIPT_STALE_OR_SKEWED"
e = live_evidence()
e.market_type(LIVE_OPTION_ID, 3, NOW.isoformat(), 100.1)
e.price(LIVE_OPTION_ID, 66, 10.0, 100.1)
e.price(LIVE_OPTION_ID, 67, 10.2, 100.2)
assert e.option_status(100.3)["market_type"] == "DELAYED"
assert not e.option_status(100.3)["live_bid_ask_seen"]
e.market_type(LIVE_OPTION_ID, 1, NOW.isoformat(), 100.3)
e.price(LIVE_OPTION_ID, 1, 10.0, 100.3)
e.price(LIVE_OPTION_ID, 2, 10.2, 100.4)
assert e.option_status(100.5)["reason"] == "DELAYED_OPTION_TICKS"
assert e.entitlement_conclusion(100.5) == "INCONCLUSIVE"
for code, label in ((2, "FROZEN"), (4, "DELAYED_FROZEN"), (0, "UNAVAILABLE")):
    sample = DiagnosticMarketEvidence()
    sample.option_live_requested = True
    sample.market_type(LIVE_OPTION_ID, code, NOW.isoformat(), 100.0)
    sample.price(LIVE_OPTION_ID, 1, 10.0, 100.0)
    sample.price(LIVE_OPTION_ID, 2, 10.2, 100.1)
    assert sample.option_status(100.2)["market_type"] == label
    assert not sample.option_status(100.2)["live_bid_ask_seen"]

probe = OptionDiagnosticProbe()
probe.error(LIVE_OPTION_ID, 10168, "synthetic option entitlement error")
assert probe.safe_error_events()[0]["request_label"] == "EXACT_SPXW_OPTION_LIVE_DIAGNOSTIC_MARKET_DATA"
assert probe.safe_error_events()[0]["message"] == "synthetic option entitlement error"
assert probe.diagnostic.option_status(100.0)["reason"] == "OPTION_REQUEST_DENIED"
assert probe.diagnostic.entitlement_conclusion(100.0) == "EXPLICIT_REQUEST_DENIAL"
assert probe.option_seen.is_set()
assert not hasattr(probe, "submit")

print("READ-ONLY OPTION ENTITLEMENT DIAGNOSTIC PASS")
