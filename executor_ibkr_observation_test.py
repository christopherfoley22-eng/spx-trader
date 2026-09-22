"""Offline synthetic callback tests; no IBKR import, socket, or account data."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import ast

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter
from executor_live_observation_service import LocalReadOnlyObservationService
from simulation_evidence import EvidenceError, VerifiedOptionContract

NOW = datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc)
ACCOUNT = "SYNTHETIC_ONLY"
CONTRACT = VerifiedOptionContract("SPX", "OPT", "C", "20260921", "SPXW",
                                  "100", "USD", 5000.0, 12345)


def fails(fn):
    try:
        fn()
    except EvidenceError:
        return
    raise AssertionError("Unsafe callback was accepted")


def seeded(selected=None, accounts=(ACCOUNT,), quantity=0, order=False):
    adapter = ReadOnlyIBKREvidenceAdapter(selected)
    g = adapter.generation
    adapter.handshake(g)
    adapter.managed_accounts(g, accounts)
    if adapter.selected:
        for tag in ("AvailableFunds", "BuyingPower", "ExcessLiquidity",
                    "TotalCashValue", "SettledCash"):
            adapter.account_value(g, adapter.selected, tag, "USD", "10000", 100.0)
        adapter.account_end(g)
        if quantity:
            adapter.position(g, adapter.selected, CONTRACT.con_id, quantity)
        adapter.position_end(g, 100.0)
        if order:
            adapter.open_order(g, 1, adapter.selected, CONTRACT.con_id)
        adapter.open_order_end(g, 100.0)
    return adapter, g


def identity(adapter, g, contract=CONTRACT):
    adapter.spx_details(g, "SPX", "IND", "USD", 999, "CBOE")
    adapter.spx_details_end(g)
    adapter.chain(g, "CBOE", 999, "SPXW", "100", {"20260921"}, {5000.0})
    adapter.chain_end(g)
    adapter.exact_option(g, "candidate", contract, "SMART")
    adapter.exact_option_end(g)


def market(adapter, g, code=1, offset=0):
    adapter.market_type(g, "SPX", code)
    adapter.market_type(g, "OPTION", code)
    if code == 1:
        stamp = NOW + timedelta(seconds=offset)
        adapter.market_tick(g, "SPX", "price", 5000.0, stamp, 100.0)
        adapter.market_tick(g, "OPTION", "bid", 10.0, stamp, 100.0, CONTRACT.con_id)
        adapter.market_tick(g, "OPTION", "ask", 10.1, stamp, 100.0, CONTRACT.con_id)


def view(adapter, position=(0, None), now=100.1):
    return adapter.observe(NOW, now, "CALL", "candidate", position)


# A fully shaped synthetic response is still non-executable: observation is
# separate from the persisted dry-run controller and API-visible orders are limited.
a, g = seeded()
identity(a, g)
market(a, g)
r = view(a)
assert r.state == "READ-ONLY CONNECTED" and not r.executable
assert "AUTHORITATIVE MARKET INGESTION REDUCER REQUIRED" in r.reasons
assert r.account.complete and r.broker.complete and r.broker.position_qty == 0
assert r.order_scope == "API_VISIBLE_ONLY" and r.option is None and r.spx is None
assert LocalReadOnlyObservationService(a).status()["ready"] is False

assert ReadOnlyIBKREvidenceAdapter().observe(NOW, 100.1).state == "DISCONNECTED"
a, g = seeded(accounts=(ACCOUNT, "SYNTHETIC_OTHER"))
assert "ACCOUNT SELECTION REQUIRED" in a.observe(NOW, 100.1).reasons
a, g = seeded(selected=ACCOUNT, accounts=(ACCOUNT, "SYNTHETIC_OTHER"))
assert a.selected == ACCOUNT
a, g = seeded()
assert "ACCOUNT EVIDENCE INCOMPLETE OR STALE" in view(a, now=102.0).reasons
a, g = seeded()
a.summary.pop((ACCOUNT, "SettledCash"))
assert "ACCOUNT EVIDENCE INCOMPLETE OR STALE" in view(a).reasons
a, g = seeded(quantity=3)
assert "BROKER/LOCAL POSITION DISAGREEMENT" in view(a).reasons
assert "BROKER/LOCAL POSITION DISAGREEMENT" not in view(a, (3, CONTRACT.con_id)).reasons
assert "BROKER/LOCAL POSITION DISAGREEMENT" in view(a, (4, CONTRACT.con_id)).reasons
assert "BROKER/LOCAL POSITION DISAGREEMENT" in view(a, (3, 12346)).reasons
a, g = seeded(order=True)
assert "UNEXPECTED API-VISIBLE OPEN ORDER" in view(a).reasons
a, g = seeded()
assert "RECONCILIATION REQUIRED" in a.observe(NOW, 100.1).reasons

a, g = seeded()
identity(a, g, replace(CONTRACT, right="P"))
assert "EXACT OPTION CONTRACT INVALID" in view(a).reasons
a, g = seeded()
identity(a, g)
fails(lambda: a.exact_option(g, "candidate", replace(CONTRACT, con_id=12346), "SMART"))
assert "INVALID EXACT CONTRACT" in view(a).reasons
a, g = seeded()
fails(lambda: a.spx_details(g, "SPX", "IND", "USD", 0, "CBOE"))
a, g = seeded()
identity(a, g)
for code, label in ((2, "FROZEN"), (3, "DELAYED"), (4, "DELAYED_FROZEN")):
    market(a, g, code)
    assert any(label in reason for reason in view(a).reasons)
a, g = seeded()
identity(a, g)
assert "MARKET DATA UNAVAILABLE OR STALE" in view(a).reasons
for instrument in ("SPX", "OPTION"):
    a.market_type(g, instrument, 1)
fails(lambda: a.market_tick(g, "SPX", "price", 5000.0, None, 100.0))
assert "MARKET DATA UNAVAILABLE OR STALE" in view(a).reasons
assert view(a).spx is None and not view(a).executable
market(a, g)
assert "MARKET DATA UNAVAILABLE OR STALE" in view(a, now=102.0).reasons
fails(lambda: a.market_tick(g, "SPX", "price", 5001.0, NOW-timedelta(seconds=1), 100.2))
assert "BACKWARD OR CONTRADICTORY MARKET DATA" in view(a).reasons

a, g = seeded()
identity(a, g)
market(a, g)
fails(lambda: a.market_tick(g, "OPTION", "bid", 10.2, NOW+timedelta(milliseconds=100), 100.2, 12346))
assert "QUOTE CONTRACT CHANGED" in view(a).reasons
a, g = seeded()
fails(lambda: a.position(g, ACCOUNT, CONTRACT.con_id, -1))
a, g = seeded()
fails(lambda: a.account_value(g, ACCOUNT, "SettledCash", "USD", "NaN", 100.0))
assert "CONTRADICTORY ACCOUNT EVIDENCE" in view(a).reasons
a = ReadOnlyIBKREvidenceAdapter()
g = a.generation
a.handshake(g)
a.managed_accounts(g, (ACCOUNT,))
fails(lambda: a.account_value(g, ACCOUNT, "SettledCash", "USD", "NaN", 100.0))
assert "ACCOUNT EVIDENCE INCOMPLETE" in a.observe(NOW, 100.1).reasons
a, g = seeded()
fails(lambda: a.open_order(g, 1, ACCOUNT, CONTRACT.con_id))  # callback after completion
a, g = seeded()
fails(lambda: a.position(g, ACCOUNT, CONTRACT.con_id, 1))  # callback after completion
a, g = seeded()
fails(lambda: a.managed_accounts(g, ("SYNTHETIC_OTHER",)))
a.reset()
assert a.observe(NOW, 100.1).state == "DISCONNECTED"
fails(lambda: a.handshake(g))
fails(lambda: a.market_type(g, "SPX", 1))

# Neither the reducer nor the status-only service has a broker client or intent API.
for cls in (ReadOnlyIBKREvidenceAdapter, LocalReadOnlyObservationService):
    assert not any(hasattr(cls, name) for name in
                   ("connect", "submit", "placeOrder", "cancelOrder", "reqGlobalCancel"))
for name in ("executor_ibkr_observation.py", "executor_live_observation_service.py"):
    tree = ast.parse(Path(__file__).with_name(name).read_text())
    assert not any(isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ibapi")
                   for node in ast.walk(tree))
print("READ-ONLY OBSERVATION CALLBACKS AND FAIL-CLOSED STATES PASS")
