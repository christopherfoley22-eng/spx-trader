"""Offline tests for the separate pre-+4 research comparison."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile

from mortificatio_pre4_experiment import (
    BASELINE, CONFIRMED_STOP, EARLY_FAILURE, MAX_CONTRACTS,
    MODELED_SLIPPAGE_PER_CONTRACT, compare, compare_against_baseline,
    empirical_report, report, run_policy, synthetic_scenarios,
    compare_ohlc_dataset, load_ohlc_dataset, replay_ohlc_entry,
    HistoricalEntry, MinuteBar, tradingview_csv_to_dataset,
)

scenarios = synthetic_scenarios()
baseline, early = compare_against_baseline(EARLY_FAILURE, scenarios)
_, confirmed = compare_against_baseline(CONFIRMED_STOP, scenarios)
assert baseline["net_option_pnl"] == Decimal("-350.0000")
assert baseline["worst_loss"] == Decimal("-140.0000")
assert early["losses_avoided"] == 1 and early["winners_missed"] == 1
assert confirmed["losses_avoided"] == 1
assert confirmed["losses_worsened_by_waiting"] == 2
assert confirmed["worst_loss"] == Decimal("-190.000")

# The wick-recovery example is explicit: the baseline waits while the early
# candidate exits before the later +5 / 3-point reversal path.
base_recovery = run_policy(BASELINE, scenarios[0][1], scenarios[0][0])
early_recovery = run_policy(EARLY_FAILURE, scenarios[0][1], scenarios[0][0])
assert base_recovery.exit_reason == "LET_IT_RIDE_REVERSAL"
assert early_recovery.exit_reason == "CANDIDATE_PRE4_EARLY_FAILURE"
assert base_recovery.option_pnl > early_recovery.option_pnl

# Stop confirmation is elapsed-time based. Exact one-second equality passes;
# a recovery before one second resets the timer.
stop_recovery = run_policy(CONFIRMED_STOP, scenarios[2][1], scenarios[2][0])
assert stop_recovery.exit_reason == "LET_IT_RIDE_REVERSAL"
assert stop_recovery.option_pnl > Decimal("0")
stop_loss = run_policy(CONFIRMED_STOP, scenarios[3][1], scenarios[3][0])
base_loss = run_policy(BASELINE, scenarios[3][1], scenarios[3][0])
assert stop_loss.option_pnl < base_loss.option_pnl

one_second = [scenarios[0][1][0]]
quote_type = type(scenarios[0][1][0])
one_second.append(quote_type(
    "2026-01-01T14:01:00+00:00", Decimal("-3.25"), Decimal("0.70"), Decimal("0.80")))
one_second.append(quote_type(
    "2026-01-01T14:01:01+00:00", Decimal("-3.25"), Decimal("0.70"), Decimal("0.80")))
assert run_policy(CONFIRMED_STOP, one_second).exit_reason == "CANDIDATE_ELAPSED_STOP_CONFIRMATION"

# 25-contract result includes the spread and explicit modeled slippage.
bulk = run_policy(BASELINE, scenarios[0][1], contracts=MAX_CONTRACTS,
                  slippage=MODELED_SLIPPAGE_PER_CONTRACT)
assert bulk.contracts == 25
assert bulk.option_pnl == Decimal("1500.000")
bulk_baseline = compare(BASELINE, scenarios, MAX_CONTRACTS,
                        MODELED_SLIPPAGE_PER_CONTRACT)
_, bulk_early = compare_against_baseline(EARLY_FAILURE, scenarios, MAX_CONTRACTS,
                                         MODELED_SLIPPAGE_PER_CONTRACT)
_, bulk_confirmed = compare_against_baseline(CONFIRMED_STOP, scenarios, MAX_CONTRACTS,
                                             MODELED_SLIPPAGE_PER_CONTRACT)
assert bulk_baseline["net_option_pnl"] == Decimal("-9750.0000")
assert bulk_early["net_option_pnl"] == Decimal("-6700.000")
assert bulk_confirmed["net_option_pnl"] == Decimal("-7000.000")

# No local historical dataset is present, so the default report fails closed
# rather than treating synthetic cases as empirical evidence.
summary = report()
assert summary["empirical_conclusion"] == "NO_EMPIRICAL_CONCLUSION"
assert summary["reports"] == [] and summary["executable"] is False

# A qualifying local dataset requires complete identity, separate source
# timestamps, and matching SPX/option observations.
dataset = {
    "dataset_version": 1,
    "scenarios": [{
        "name": "local_case", "direction": "CALL", "entry_spx": 5000,
        "contract": {"symbol": "SPX", "expiration": "20260101", "right": "C",
                     "strike": 5000, "trading_class": "SPXW", "multiplier": "100",
                     "currency": "USD", "con_id": 123},
        "observations": [{
            "timestamp": "2026-01-01T14:00:00.000000+00:00",
            "spx_timestamp": "2026-01-01T14:00:00.000000+00:00",
            "option_timestamp": "2026-01-01T14:00:00.100000+00:00",
            "spx": 5000, "option_bid": 2.0, "option_ask": 2.1}, {
            "timestamp": "2026-01-01T14:00:01.000000+00:00",
            "spx_timestamp": "2026-01-01T14:00:01.000000+00:00",
            "option_timestamp": "2026-01-01T14:00:01.100000+00:00",
            "spx": 4996.75, "option_bid": 0.7, "option_ask": 0.8}, {
            "timestamp": "2026-01-01T14:00:02.000000+00:00",
            "spx_timestamp": "2026-01-01T14:00:02.000000+00:00",
            "option_timestamp": "2026-01-01T14:00:02.100000+00:00",
            "spx": 4996.75, "option_bid": 0.7, "option_ask": 0.8}]
    }]
}
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "dataset.json"
    path.write_text(json.dumps(dataset))
    empirical = empirical_report(path)
    assert empirical["empirical_conclusion"] == "QUALIFYING_DATA_COMPARED"
    assert empirical["executable"] is False
    dataset["scenarios"][0]["observations"][1]["option_timestamp"] = "2026-01-01T14:00:02+00:00"
    path.write_text(json.dumps(dataset))
    try:
        empirical_report(path)
    except ValueError as exc:
        assert "0.25" in str(exc)
    else:
        raise AssertionError("unsynchronized source timestamps were accepted")

assert report()["historical_data_used"] is False

# TradingView-style CSV import is exercised with a small synthetic export.
# Epoch units are explicit; no seconds/milliseconds guessing is permitted.
with tempfile.TemporaryDirectory() as directory:
    csv_path = Path(directory) / "tv.csv"
    csv_path.write_text(
        "time,open,high,low,close,Volume\n"
        "1798812000,5000,5001,4999,5000,100\n"
        "1798812060,5000,5004,4996,5001,120\n"
    )
    converted = tradingview_csv_to_dataset(
        csv_path, "2027-01-01T13:00:00+00:00", "CALL", "5000",
        timestamp_unit="unix_seconds")
    assert converted["entries"][0]["direction"] == "CALL"
    assert converted["entries"][0]["entry_price"] == "5000"
    converted_path = Path(directory) / "converted.json"
    converted_path.write_text(json.dumps(converted))
    assert compare_ohlc_dataset(load_ohlc_dataset(converted_path))["executable"] is False
    bad_csv = Path(directory) / "bad.csv"
    bad_csv.write_text(
        "time,open,high,low,close\n"
        "1798812000,5000,5001,4999,5000\n"
        "1798812120,5000,5001,4999,5000\n")
    try:
        tradingview_csv_to_dataset(
            bad_csv, "2027-01-01T13:00:00+00:00", "PUT", "5000",
            timestamp_unit="unix_seconds")
    except ValueError as exc:
        assert "one-minute" in str(exc)
    else:
        raise AssertionError("non-one-minute CSV spacing was accepted")

# SPX-only one-minute replay: a same-bar stop and +4 touch is UNKNOWN because
# OHLC cannot establish intraminute order.
entry = HistoricalEntry("ambiguous", "2026-01-01T14:00:00+00:00", "CALL", Decimal("5000"))
ambiguous = [MinuteBar("2026-01-01T14:00:00+00:00", Decimal("5000"),
                       Decimal("5004"), Decimal("4996.75"), Decimal("5001"))]
base_ambiguous, candidate_ambiguous = replay_ohlc_entry(entry, ambiguous)
assert base_ambiguous.outcome == "UNKNOWN"
assert candidate_ambiguous.outcome == "UNKNOWN"

# A confirmed close can become worse than the immediate range-touch stop.
entry = HistoricalEntry("worse_wait", "2026-01-01T14:00:00+00:00", "CALL", Decimal("5000"))
worse_wait = [
    MinuteBar("2026-01-01T14:00:00+00:00", Decimal("5000"), Decimal("5001"), Decimal("4999"), Decimal("5000")),
    MinuteBar("2026-01-01T14:01:00+00:00", Decimal("5000"), Decimal("5000"), Decimal("4996.25"), Decimal("4996.25")),
]
base_worse, candidate_worse = replay_ohlc_entry(entry, worse_wait)
assert base_worse.outcome == "STOPPED_OUT"
assert candidate_worse.outcome == "STOPPED_OUT"
assert candidate_worse.worse_by_points == Decimal("0.50")

# PUT favorable movement is mirrored, and a recovery can reach +4 first.
entry = HistoricalEntry("put_recovery", "2026-01-01T14:00:00+00:00", "PUT", Decimal("5000"))
put_recovery = [
    MinuteBar("2026-01-01T14:00:00+00:00", Decimal("5000"), Decimal("5001"), Decimal("4999"), Decimal("5000")),
    MinuteBar("2026-01-01T14:01:00+00:00", Decimal("5000"), Decimal("4996"), Decimal("4995.9"), Decimal("4996")),
]
base_put, candidate_put = replay_ohlc_entry(entry, put_recovery)
assert base_put.outcome == "REACHED_PLUS4"
assert candidate_put.outcome == "REACHED_PLUS4"

# If the explicit entry occurs inside a candle containing a threshold, order
# relative to entry cannot be reconstructed and both policies remain UNKNOWN.
inside = HistoricalEntry("inside_bar", "2026-01-01T14:00:30+00:00", "CALL", Decimal("5000"))
inside_bar = [MinuteBar("2026-01-01T14:00:00+00:00", Decimal("5000"),
                        Decimal("5004"), Decimal("4999"), Decimal("5001"))]
assert all(result.outcome == "UNKNOWN" for result in replay_ohlc_entry(inside, inside_bar))

# The OHLC loader requires explicit past entry details and rejects malformed
# or missing data before any comparison is attempted.
ohlc_dataset = {"dataset_version": 1, "entries": [{
    "entry_id": "one", "entry_timestamp": "2026-01-01T14:00:00+00:00",
    "direction": "CALL", "entry_price": 5000,
    "bars": [{"timestamp": "2026-01-01T14:00:00+00:00", "open": 5000,
              "high": 5001, "low": 4999, "close": 5000}]
}]}
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "spx_ohlc.json"
    path.write_text(json.dumps(ohlc_dataset))
    loaded = load_ohlc_dataset(path)
    summary = compare_ohlc_dataset(loaded)
    assert summary["executable"] is False
    assert summary["empirical_conclusion"] == "QUALIFYING_DATA_COMPARED"
    del ohlc_dataset["entries"][0]["entry_timestamp"]
    path.write_text(json.dumps(ohlc_dataset))
    try:
        load_ohlc_dataset(path)
    except ValueError:
        pass
    else:
        raise AssertionError("missing explicit entry timestamp was accepted")

print("MORTIFICATIO PRE4 OFFLINE COMPARISON PASS")
