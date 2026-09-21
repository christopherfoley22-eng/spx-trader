"""Synthetic account-summary adapter checks; no IBKR import or connection."""

from dataclasses import replace
from decimal import Decimal
from simulation_evidence import (
    EvidenceError, VerifiedAccountSnapshot, conservative_usable_funds,
)
from ibkr_read_only_validation import ReadOnlyProbe

ACCOUNT = "SYNTHETIC_ONLY"
base = VerifiedAccountSnapshot(
    account_id=ACCOUNT, selected_account=ACCOUNT, complete=True,
    currency="USD", available_funds="1200.00", buying_power="9000.00",
    excess_liquidity="1100.00", total_cash_value="1000.00",
    settled_cash="950.00", oldest_required_receipt_monotonic=100.0,
)
assert conservative_usable_funds(base, ACCOUNT, 100.1) == Decimal("950.00")
for changed in (
    {"account_id": "OTHER"}, {"selected_account": "OTHER"},
    {"complete": False}, {"currency": "EUR"},
    {"available_funds": None}, {"buying_power": "NaN"},
    {"excess_liquidity": "Infinity"}, {"settled_cash": "-1"},
    {"oldest_required_receipt_monotonic": 98.0},
    {"oldest_required_receipt_monotonic": 101.0},
):
    try:
        conservative_usable_funds(replace(base, **changed), ACCOUNT, 100.1)
    except EvidenceError:
        pass
    else:
        raise AssertionError("Unsafe account evidence accepted")

print("READ-ONLY ACCOUNT SUMMARY ADAPTER PASS")

probe = ReadOnlyProbe()
probe.error(-1, 326, "synthetic duplicate-client message")
assert probe.duplicate_client and probe.disconnected
assert probe.ready.is_set()
assert probe.error_codes == {326}
print("DUPLICATE CLIENT ID FAIL-CLOSED PASS")
