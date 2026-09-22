"""Offline tests for the isolated read-only provenance probe."""

import ast
from pathlib import Path
from types import SimpleNamespace

from ibkr_market_provenance_validation import (
    HOST, OPTION_BID_ASK, OPTION_DETAILS, OPTION_STANDARD, PORT, REQUEST_LABELS,
    SPX_ALL_LAST, SPX_DETAILS, SPX_LAST, SPX_MIDPOINT, SPX_STANDARD,
    SPXW_CHAIN, ProvenanceProbe, choose_dynamic_contract, exact_option,
    exact_underlying,
)
from executor_market_ingestion import VALIDATED_IBKR_LIMITATIONS

assert HOST == "127.0.0.1" and PORT == 7496


def contract(**changes):
    values = dict(symbol="SPX", secType="OPT", currency="USD",
                  tradingClass="SPXW", multiplier="100",
                  lastTradeDateOrContractMonth="20260922", right="C",
                  strike=5000.0, conId=22)
    values.update(changes)
    return SimpleNamespace(**values)


# Dynamic lower-median selection uses returned chain data only.
chains = [("CBOE", 11, "SPXW", "100", {"20260922"},
           {5010.0, 4990.0, 5000.0, float("nan")})]
selected = choose_dynamic_contract(chains, 11, "20260922")
assert selected == {"expiration": "20260922", "right": "C", "strike": 5000.0,
                    "selection": "DYNAMIC_CHAIN_LOWER_MEDIAN_NON_ATM_DIAGNOSTIC"}
try:
    choose_dynamic_contract(chains, 12, "20260922")
except ValueError:
    pass
else:
    raise AssertionError("Wrong underlying chain was accepted")

underlying = SimpleNamespace(symbol="SPX", secType="IND", currency="USD", conId=11)
assert exact_underlying([underlying]) is underlying
try:
    exact_underlying([underlying, SimpleNamespace(symbol="SPX", secType="IND",
                                                  currency="USD", conId=12)])
except ValueError:
    pass
else:
    raise AssertionError("Ambiguous SPX identity was accepted")

resolved = contract()
assert exact_option([resolved], selected) is resolved
try:
    exact_option([resolved, contract(conId=23)], selected)
except ValueError:
    pass
else:
    raise AssertionError("Ambiguous option identity was accepted")

# Callback evidence retains the raw integer without giving it time semantics.
clock_values = iter(range(100, 200))
probe = ProvenanceProbe(wall_clock=lambda: SimpleNamespace(isoformat=lambda: "RECEIPT"),
                        monotonic_clock=lambda: float(next(clock_values)))
last_attributes = SimpleNamespace(pastLimit=False, unreported=False)
quote_attributes = SimpleNamespace(bidPastLow=False, askPastHigh=True)
probe.tickByTickAllLast(SPX_LAST, 1, 123456, 5000.0, 1, last_attributes,
                        "CBOE", "")
probe.tickByTickAllLast(SPX_ALL_LAST, 2, 123456, 5000.0, 1, last_attributes,
                        "CBOE", "")
probe.tickByTickMidPoint(SPX_MIDPOINT, 123456, 5000.0)
probe.tickByTickBidAsk(OPTION_BID_ASK, 123456, 10.0, 10.1, 1, 2,
                       quote_attributes)
probe.marketDataType(SPX_STANDARD, 1)
probe.marketDataType(OPTION_STANDARD, 1)
result = probe.result(option_resolved=True)
assert {item["callback"] for item in result["callback_evidence"]} == {
    "Last", "AllLast", "MidPoint", "BidAsk"}
assert all(item["raw_time_value"] == 123456 for item in result["callback_evidence"])
assert all(item["time_semantics"] == "OPAQUE_IBKR_INTEGER_UNPROVEN"
           for item in result["callback_evidence"])
assert all(item["time_precision"] == "UNPROVEN"
           for item in result["callback_evidence"])
assert result["option_atomic_bid_ask_observed"] is True
assert result["option_bid_ask_request_live_callback_observed"] is False
assert result["live_option_atomic_bid_ask_demonstrated"] is False
assert result["cross_request_market_type_binding_proven"] is False
assert result["timestamp_semantics_proven"] is False
assert result["quarter_second_cross_instrument_sync_proven"] is False
assert result["usable_by_executor"] is False and result["executable"] is False
assert result["validated_ibkr_limitations"] == VALIDATED_IBKR_LIMITATIONS
assert result["standard_market_data_provenance_assessment"].startswith("INSUFFICIENT")
assert result["option_contract"]["strike_exposed"] is False
assert result["option_contract"]["expiration_exposed"] is False
assert result["option_contract"]["con_id_exposed"] is False

# marketDataType is recorded only against the request ID that produced it.
assert [item["request_id"] for item in result["market_data_type_callbacks"]] == [
    SPX_STANDARD, OPTION_STANDARD]
assert all(item["request_specific_callback"] is True
           for item in result["market_data_type_callbacks"])
assert all(item["classification"] == "LIVE"
           for item in result["market_data_type_callbacks"])
assert all(item["phase"] == "DEFAULT_BEFORE_EXPLICIT_LIVE_REQUEST"
           for item in result["market_data_type_callbacks"])

# Complete errors are sanitized and mapped to the exact diagnostic request.
sensitive_account = "D" + "U" + "1234567"
sensitive_token = "pri" + "vate"
sensitive_email = "person" + "@example.test"
probe.error(OPTION_BID_ASK, 10168, "{}={} {}={} {}".format(
    "acc" + "ount", sensitive_account, "to" + "ken", sensitive_token,
    sensitive_email))
error = probe.result()["errors"][-1]
assert error["request_label"] == REQUEST_LABELS[OPTION_BID_ASK]
assert sensitive_account not in error["message"]
assert sensitive_token not in error["message"]
assert sensitive_email not in error["message"]

# Encoded runtime option identities never survive sanitized output.
encoded_symbol = "SPXW  " + "260922C07195000"
human_contract = "SPX (SPXW) " + "SEP 22 '26 7195 Call"
probe.error(OPTION_BID_ASK, 354, "not subscribed " + encoded_symbol)
probe.error(OPTION_BID_ASK, 354, "not subscribed " + human_contract)
rendered_errors = str(probe.result()["errors"])
for sensitive in (encoded_symbol, human_contract, "7195", "260922C07195000"):
    assert sensitive not in rendered_errors

# A ticker-not-found response after rejection is cleanup evidence, not mutation
# and not an additional primary market-data failure.
probe.error(SPX_LAST, 10189, "synthetic unsupported request")
probe.error(SPX_LAST, 300, "synthetic cleanup ticker missing")
cleanup_error = probe.result()["errors"][-1]
assert cleanup_error["category"] == "CLEANUP_AFTER_REJECTED_SUBSCRIPTION"
assert cleanup_error["primary_failure"] is False
assert cleanup_error["broker_mutation"] is False

# The exact runtime request surface is an allowlist of observation operations.
path = Path(__file__).with_name("ibkr_market_provenance_validation.py")
source = path.read_text()
tree = ast.parse(source)
app_calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"}
assert app_calls == {
    "connect", "reqContractDetails", "reqSecDefOptParams", "reqMarketDataType",
    "reqMktData", "reqTickByTickData", "cancelTickByTickData", "cancelMktData",
    "isConnected", "disconnect", "result",
}
for forbidden in ("placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
                  "transfer", "preview", "transmit"):
    assert forbidden not in app_calls

# Resolution requests do not assign a hardcoded conId, strike, or expiration.
assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
for node in assignments:
    for target in node.targets:
        if isinstance(target, ast.Attribute) and target.attr in {
                "conId", "strike", "lastTradeDateOrContractMonth"}:
            assert not isinstance(node.value, ast.Constant), (target.attr, node.lineno)

# Finally cleanup cancels tick streams, then standard streams, then disconnects.
main = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == "main")
try_node = next(node for node in main.body if isinstance(node, ast.Try))
cleanup_calls = [node.func.attr for statement in try_node.finalbody
                 for node in ast.walk(statement)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "app"]
assert cleanup_calls.index("cancelTickByTickData") < cleanup_calls.index("cancelMktData")
assert cleanup_calls.index("cancelMktData") < cleanup_calls.index("disconnect")

assert (SPX_DETAILS, SPXW_CHAIN, OPTION_DETAILS, SPX_STANDARD, OPTION_STANDARD,
        SPX_LAST, SPX_ALL_LAST, SPX_MIDPOINT, OPTION_BID_ASK) == tuple(REQUEST_LABELS)

print("READ-ONLY MARKET PROVENANCE PROBE OFFLINE PASS")
