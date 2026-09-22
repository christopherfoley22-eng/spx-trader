"""Deterministic offline tests of read-only IBKR error provenance."""

from datetime import datetime, timezone
import inspect
import json

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter
from ibkr_adapter_read_only_validation import AdapterProbe
from ibkr_read_only_validation import EWrapper, ReadOnlyProbe, REQUEST_LABELS

FIXED = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)

# Installed ibapi 9.81.1-1 decodes three callback fields. Our override also
# accepts a future/direct fourth advanced-text field without losing it.
installed = inspect.signature(EWrapper.error)
assert tuple(installed.parameters) == ("self", "reqId", "errorCode", "errorString")
override = inspect.signature(ReadOnlyProbe.error)
assert tuple(override.parameters) == (
    "self", "reqId", "errorCode", "errorString", "advancedOrderRejectJson")
assert override.parameters["advancedOrderRejectJson"].default == ""

probe = ReadOnlyProbe(wall_clock=lambda: FIXED, monotonic_clock=lambda: 123.5)
probe.error(8104, 10168, "SPX underlying market-data diagnostic")
probe.error(8106, 10168, "Exact option market-data diagnostic",
            '{"reason":"subscription unavailable","account":"U12345678","price":12.34,"token":"demo"}')
probe.error(9999, 777, "Unknown request diagnostic")
probe.error(-1, 2104, "Information farm connected")
probe.error(-1, 1100, "Connection lost")
assert len(probe.error_events) == 5
assert [item["request_id"] for item in probe.error_events] == [8104, 8106, 9999, -1, -1]
assert probe.error_events[0]["message"] == "SPX underlying market-data diagnostic"
assert probe.error_events[1]["advanced_text"].startswith('{"reason"')
assert all(item["receipt_utc"] == FIXED.isoformat() and
           item["receipt_monotonic"] == 123.5 for item in probe.error_events)
safe = probe.safe_error_events()
assert safe[0]["request_label"] == "SPX_UNDERLYING_MARKET_DATA"
assert safe[1]["request_label"] == "EXACT_SPXW_OPTION_MARKET_DATA"
assert safe[0]["category"] == safe[1]["category"] == "REQUEST_SPECIFIC"
assert safe[0]["advanced_text"] == ""
assert json.loads(safe[1]["advanced_text"]) == {
    "reason": "subscription unavailable", "account": "[REDACTED]",
    "price": "[REDACTED]", "token": "[REDACTED]"}
assert safe[2]["request_id"] == 9999 and safe[2]["request_label"] == "UNKNOWN_REQUEST_ID"
assert safe[2]["category"] == "UNKNOWN_REQUEST"
assert safe[3]["category"] == "INFORMATIONAL"
assert safe[4]["category"] == "CONNECTION_OR_SYSTEM"
assert [item["code"] for item in safe] == [10168, 10168, 777, 2104, 1100]
assert REQUEST_LABELS[8104] != REQUEST_LABELS[8106]
assert probe.error_codes_by_request[8104] == {10168}
assert probe.error_codes_by_request[8106] == {10168}

# A malformed/unknown ID remains visible by category without printing raw text.
other = ReadOnlyProbe(wall_clock=lambda: FIXED, monotonic_clock=lambda: 123.5)
other.error("unsafe account=U12345678", 777, "Account U12345678 balance $123.45 user@example.com")
reported = other.safe_error_events()[0]
assert reported["request_id"] is None
assert reported["request_label"] == "GLOBAL_OR_SYSTEM"
assert "U12345678" not in reported["message"] and "$123.45" not in reported["message"]
assert "user@example.com" not in reported["message"]

# Error receipt clocks never become market source timestamps or open entry.
adapter = ReadOnlyIBKREvidenceAdapter()
bridge = AdapterProbe(adapter)
bridge.error(8104, 10168, "market data not available")
assert bridge.adapter.invalid == "TWS CALLBACK ERROR"
assert bridge.safe_error_events()[0]["request_label"] == "SPX_UNDERLYING_MARKET_DATA"
assert not adapter.observe(FIXED, 123.5).executable
assert not hasattr(bridge, "submit")
receipt_only = ReadOnlyIBKREvidenceAdapter()
receipt_bridge = AdapterProbe(receipt_only)
receipt_bridge.nextValidId(1)
receipt_bridge.marketDataType(8104, 1)
receipt_bridge.tickPrice(8104, 4, 5000.0, None)
assert receipt_bridge.spx_last == 5000.0  # Received callback, without source time.
assert "price" not in receipt_only.market["SPX"]
assert not receipt_only.observe(FIXED, 123.5).executable

print("READ-ONLY IBKR ERROR PROVENANCE PASS")
