"""Synthetic evidence attacks; never imports an IBKR client."""

from dataclasses import replace
from datetime import datetime, timedelta
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from mortificatio_v01 import MortificatioV01, MortificatioV01Error, OptionQuote, SimulatedBroker
from mortificatio_state import MortificatioStateError
from simulation_evidence import (
    EvidenceClock, EvidenceError, FeedStatus, OptionObservation,
    SPXObservation, VerifiedBrokerSnapshot, VerifiedOptionContract,
)
from verified_simulation import VerifiedDryRun

NY = ZoneInfo("America/New_York")
ACCOUNT = "SYNTHETIC_ACCOUNT"
now = datetime.now(NY)
mono = time.monotonic()
expiration = now.strftime("%Y%m%d")
contract = VerifiedOptionContract("SPX", "OPT", "C", expiration,
                                  "SPXW", "100", "USD", 7700.0, 123)
spx = SPXObservation(7700.0, FeedStatus.LIVE, now, mono)
option = OptionObservation(contract, 123, 9.90, 10.0, FeedStatus.LIVE, now, mono)


def reject(action):
    try:
        action()
    except (EvidenceError, MortificatioStateError):
        return
    raise AssertionError("Unsafe evidence accepted")


def flat_snapshot(**changes):
    base = VerifiedBrokerSnapshot(ACCOUNT, ACCOUNT, ("OTHER_SYNTHETIC", ACCOUNT),
                                  True, 0, None, 0, mono)
    return replace(base, **changes)


clock = EvidenceClock()
for changed in (
    replace(contract, symbol="SPY"), replace(contract, sec_type="IND"),
    replace(contract, right="P"), replace(contract, expiration="20991231"),
    replace(contract, trading_class="SPX"), replace(contract, multiplier="50"),
    replace(contract, currency="EUR"), replace(contract, strike=float("nan")),
    replace(contract, con_id=0),
):
    reject(lambda changed=changed: clock.validate_pair(
        "CALL", spx, replace(option, contract=changed), now, mono, lambda _: True))
reject(lambda: clock.validate_pair("CALL", spx, option, now, mono, lambda _: False))
reject(lambda: clock.validate_pair("CALL", spx, replace(option, quote_con_id=999),
                                    now, mono, lambda _: True))
for bad in (float("nan"), float("inf"), 0, -1, None):
    reject(lambda bad=bad: clock.validate_pair(
        "CALL", replace(spx, price=bad), option, now, mono, lambda _: True))
    reject(lambda bad=bad: clock.validate_pair(
        "CALL", spx, replace(option, ask=bad), now, mono, lambda _: True))
for changed_spx, changed_option in (
    (replace(spx, status=FeedStatus.DELAYED), option),
    (spx, replace(option, status=FeedStatus.FROZEN)),
    (replace(spx, received_monotonic=mono - 2), option),
    (spx, replace(option, source_time=now + timedelta(seconds=1))),
    (spx, replace(option, source_time=now - timedelta(seconds=0.5))),
    (spx, replace(option, received_monotonic=mono - 0.5)),
):
    reject(lambda a=changed_spx, b=changed_option: clock.validate_pair(
        "CALL", a, b, now, mono, lambda _: True))
clock.validate_pair("CALL", spx, option, now, mono, lambda _: True)
put_contract = replace(contract, right="P", con_id=124)
put_option = replace(option, contract=put_contract, quote_con_id=124)
clock.validate_pair("PUT", spx, put_option, now, mono, lambda _: True)
reject(lambda: clock.validate_pair("PUT", spx, option, now, mono, lambda _: True))
reject(lambda: clock.validate_pair(
    "CALL", replace(spx, source_time=now.replace(tzinfo=None)), option,
    now, mono, lambda _: True))
reject(lambda: clock.validate_pair(
    "CALL", replace(spx, source_time=now - timedelta(seconds=0.1)), option,
    now, mono, lambda _: True))

for changed in (
    {"account_id": "WRONG"}, {"selected_account": "WRONG"},
    {"managed_accounts": ("OTHER_SYNTHETIC",)},
    {"managed_accounts": "SYNTHETIC_ACCOUNT"}, {"complete": False},
    {"received_monotonic": mono - 2}, {"position_qty": -1},
    {"position_qty": 1, "con_id": None}, {"open_order_count": -1},
):
    with tempfile.TemporaryDirectory() as directory:
        broker = SimulatedBroker()
        gateway = VerifiedDryRun(Path(directory) / "l.db", Path(directory) / "s.db",
                                 broker, ACCOUNT, lambda _: True)
        reject(lambda changed=changed: gateway.reconcile(flat_snapshot(**changed), mono))
        gateway.close()

with tempfile.TemporaryDirectory() as directory:
    broker = SimulatedBroker()
    lifecycle = Path(directory) / "l.db"
    strategy = Path(directory) / "s.db"
    bare = MortificatioV01(lifecycle, strategy, broker)
    try:
        bare.authorize_entry("CALL", 7700.0, 1000.0,
                             [OptionQuote(123, 7700.0, 10.0)])
    except MortificatioV01Error:
        pass
    else:
        raise AssertionError("Unverified public entry path accepted")
    bare.close()
    gateway = VerifiedDryRun(lifecycle, strategy, broker, ACCOUNT, lambda _: True)
    assert gateway.reconcile(flat_snapshot(), mono) == "ENTRY_ELIGIBLE"
    assert gateway.reconcile(flat_snapshot(), mono) == "ENTRY_ELIGIBLE"
    broker.position_qty = 1
    broker.con_id = 123
    assert gateway.reconcile(flat_snapshot(position_qty=1, con_id=123), mono) == "BLOCK_ENTRY"
    broker.position_qty = 0
    broker.con_id = None
    broker.open_order_count = 1
    assert gateway.reconcile(flat_snapshot(open_order_count=1), mono) == "BLOCK_ENTRY"
    broker.open_order_count = 0
    plan = gateway.authorize_entry("CALL", spx, [option], 1000.0,
                                   flat_snapshot(), now, mono)
    assert plan.con_id == 123 and plan.quantity == 1
    reject(lambda: gateway.simulate_entry(plan, spx, replace(option, contract=replace(
        contract, currency="EUR")), flat_snapshot(), now, mono))
    reject(lambda: gateway.simulate_entry(plan, spx, replace(option, ask=10.01),
                                          flat_snapshot(), now, mono))
    gateway.simulate_entry(plan, spx, option, flat_snapshot(), now, mono)
    opened = flat_snapshot(position_qty=1, con_id=123)
    assert gateway.reconcile(opened, mono) == "MANAGE_EXISTING"
    for wrong in (
        flat_snapshot(), flat_snapshot(position_qty=1, con_id=999),
        flat_snapshot(position_qty=2, con_id=123),
        flat_snapshot(position_qty=1, con_id=123, open_order_count=1),
    ):
        broker.position_qty = wrong.position_qty
        broker.con_id = wrong.con_id
        broker.open_order_count = wrong.open_order_count
        assert gateway.reconcile(wrong, mono) == "BLOCK_ENTRY"
    broker.position_qty = 1
    broker.con_id = 123
    broker.open_order_count = 0
    gateway.close()

    restarted = VerifiedDryRun(lifecycle, strategy, broker, ACCOUNT, lambda _: True)
    assert restarted.reconcile(opened, mono) == "MANAGE_EXISTING"
    assert restarted.reconcile(opened, mono) == "MANAGE_EXISTING"
    reject(lambda: restarted.reconcile(flat_snapshot(account_id="WRONG"), mono))
    reject(lambda: VerifiedDryRun(lifecycle, strategy, broker, "OTHER_SYNTHETIC",
                                  lambda _: True))
    next_time = now + timedelta(milliseconds=100)
    next_mono = mono + 0.1
    next_spx = replace(spx, price=7704.0, source_time=next_time,
                       received_monotonic=next_mono)
    next_option = replace(option, source_time=next_time,
                          received_monotonic=next_mono)
    assert restarted.process_market(next_spx, next_option,
                                    replace(opened, received_monotonic=next_mono),
                                    next_time, next_mono)["action"] == "HOLD"
    restarted.close()
    restarted_again = VerifiedDryRun(lifecycle, strategy, broker, ACCOUNT,
                                     lambda _: True)
    reject(lambda: restarted_again.process_market(
        spx, option, opened, now, mono))
    risk_time = now + timedelta(milliseconds=200)
    risk_mono = mono + 0.2
    risk_spx = replace(spx, price=7701.25, source_time=risk_time,
                       received_monotonic=risk_mono)
    decision = restarted_again.process_spx_risk(
        risk_spx, replace(opened, received_monotonic=risk_mono),
        risk_time, risk_mono)
    assert decision["action"] == "EXITING"
    assert decision["reason"] == "PROFIT_PROTECTION_FLOOR"
    restarted_again.close()

print("VERIFIED SYNTHETIC CONTRACT, MARKET AND ACCOUNT TESTS PASS")
