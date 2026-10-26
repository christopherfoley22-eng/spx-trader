"""Offline research comparison for MORTIFICATIO's pre-+4 wick question.

The production strategy is not changed here. This is a non-executable replay
experiment. Synthetic fixtures are unit-test examples only; empirical
reporting requires an explicitly supplied local JSON dataset with timestamped
SPX and matching option observations.

Dataset format (version 1):
{"dataset_version": 1, "scenarios": [{"name":"case", "direction":"CALL",
"entry_spx":5000, "contract":{"symbol":"SPX", "expiration":"YYYYMMDD",
"right":"C", "strike":5000, "trading_class":"SPXW", "multiplier":"100",
"currency":"USD", "con_id":123}, "observations":[{"timestamp":"...",
"spx_timestamp":"...", "option_timestamp":"...", "spx":5000,
"option_bid":2.0, "option_ask":2.1}]}]}

Both source timestamps are required and must be within the existing 0.25-second
synchronization policy. Local receipt time is never accepted or synthesized.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import argparse
import csv
import json
from pathlib import Path

from mortificatio_strategy_state import (
    INITIAL_STOP_POINTS,
    LET_IT_RIDE_ARM_POINTS,
    TRAIL_REVERSAL_POINTS,
    PROFIT_PROTECTION_ARM_POINTS,
    PROFIT_PROTECTION_FLOOR_POINTS,
)

OPTION_MULTIPLIER = Decimal("100")
MAX_CONTRACTS = 25
MAX_SYNC_SECONDS = Decimal("0.25")
EARLY_FAILURE_PEAK = Decimal("2.00")
EARLY_FAILURE_REVERSAL = Decimal("1.00")
# Candidate parameter only; it is not tuned or part of production behavior.
CONFIRM_STOP_SECONDS = Decimal("1.00")
MODELED_SLIPPAGE_PER_CONTRACT = Decimal("0.05")


@dataclass(frozen=True)
class SyntheticQuote:
    timestamp: str
    favorable: Decimal
    bid: Decimal
    ask: Decimal

    @classmethod
    def make(cls, second, favorable):
        value = Decimal(str(favorable))
        bid = max(Decimal("0.05"), Decimal("2.00") + value * Decimal("0.40"))
        ask = bid + Decimal("0.10")
        return cls("2026-01-01T14:{:02d}:00+00:00".format(second), value, bid, ask)


@dataclass(frozen=True)
class Policy:
    name: str
    early_failure: bool = False
    confirm_stop_seconds: Decimal = Decimal("0")


@dataclass(frozen=True)
class Result:
    policy: str
    scenario: str
    exit_reason: str
    exit_favorable: Decimal
    exit_timestamp: str
    entry_ask: Decimal
    exit_bid: Decimal
    option_pnl: Decimal
    contracts: int
    modeled_slippage: Decimal
    maximum_favorable: Decimal
    maximum_adverse: Decimal


BASELINE = Policy("BASELINE_LOCKED")
EARLY_FAILURE = Policy("CANDIDATE_PRE4_EARLY_FAILURE", early_failure=True)
CONFIRMED_STOP = Policy("CANDIDATE_ELAPSED_STOP_CONFIRMATION",
                        confirm_stop_seconds=CONFIRM_STOP_SECONDS)


def _decimal(value, field):
    if isinstance(value, bool):
        raise ValueError("{} must not be Boolean".format(field))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("{} is malformed".format(field))
    if not result.is_finite():
        raise ValueError("{} must be finite".format(field))
    return result


def _timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError("{} must be an ISO timestamp".format(field))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("{} is malformed".format(field))
    if parsed.tzinfo is None:
        raise ValueError("{} must include timezone".format(field))
    return parsed.astimezone(timezone.utc)


def _validate_quote(quote):
    if not isinstance(quote, SyntheticQuote):
        raise ValueError("quote must be SyntheticQuote")
    _timestamp(quote.timestamp, "timestamp")
    for value in (quote.favorable, quote.bid, quote.ask):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("quote values must be finite Decimal values")
    if quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask:
        raise ValueError("quote must have positive, non-crossed bid/ask")


def _pnl(entry_ask, exit_bid, contracts, slippage):
    return ((exit_bid - slippage) - (entry_ask + slippage)) * OPTION_MULTIPLIER * contracts


def run_policy(policy, quotes, scenario="synthetic", contracts=1,
               slippage=Decimal("0")):
    """Run one policy against ordered timestamped observations."""
    if not quotes:
        raise ValueError("at least one quote is required")
    if not isinstance(contracts, int) or not 1 <= contracts <= MAX_CONTRACTS:
        raise ValueError("contracts must be between 1 and 25")
    slippage = _decimal(slippage, "slippage")
    if slippage < 0:
        raise ValueError("slippage cannot be negative")
    previous = None
    for quote in quotes:
        _validate_quote(quote)
        current = _timestamp(quote.timestamp, "timestamp")
        if previous is not None and current <= previous:
            raise ValueError("timestamps must be strictly increasing")
        previous = current

    entry_ask = quotes[0].ask
    peak = Decimal("0")
    breach_started = None
    exit_quote = None
    exit_reason = None
    for quote in quotes:
        now = _timestamp(quote.timestamp, "timestamp")
        favorable = quote.favorable
        peak = max(peak, favorable)
        if policy.early_failure and peak < PROFIT_PROTECTION_ARM_POINTS:
            if peak >= EARLY_FAILURE_PEAK and favorable <= peak - EARLY_FAILURE_REVERSAL:
                exit_quote, exit_reason = quote, "CANDIDATE_PRE4_EARLY_FAILURE"
                break

        # Locked production behavior, reproduced locally for comparison only.
        if peak >= LET_IT_RIDE_ARM_POINTS:
            if peak - favorable >= TRAIL_REVERSAL_POINTS:
                exit_quote, exit_reason = quote, "LET_IT_RIDE_REVERSAL"
                break
            continue
        if peak >= PROFIT_PROTECTION_ARM_POINTS:
            if favorable <= PROFIT_PROTECTION_FLOOR_POINTS:
                exit_quote, exit_reason = quote, "PROFIT_PROTECTION_FLOOR"
                break
            continue

        if favorable <= -INITIAL_STOP_POINTS:
            if policy.confirm_stop_seconds <= 0:
                exit_quote, exit_reason = quote, "INITIAL_STOP"
                break
            if breach_started is None:
                breach_started = now
            elif (now - breach_started).total_seconds() >= float(policy.confirm_stop_seconds):
                exit_quote, exit_reason = quote, "CANDIDATE_ELAPSED_STOP_CONFIRMATION"
                break
        else:
            breach_started = None

    if exit_quote is None:
        raise ValueError("scenario did not reach an exit")
    adverse = min(Decimal("0"), min(q.favorable for q in quotes))
    return Result(policy.name, scenario, exit_reason, exit_quote.favorable,
                  exit_quote.timestamp, entry_ask, exit_quote.bid,
                  _pnl(entry_ask, exit_quote.bid, contracts, slippage),
                  contracts, slippage, peak, adverse)


def compare(policy, scenarios, contracts=1, slippage=Decimal("0")):
    results = [run_policy(policy, quotes, name, contracts, slippage)
               for name, quotes in scenarios]
    return {
        "policy": policy.name, "contracts": contracts,
        "modeled_slippage": slippage,
        "net_option_pnl": sum((item.option_pnl for item in results), Decimal("0")),
        "losses_avoided": 0, "winners_missed": 0,
        "worst_loss": min(item.option_pnl for item in results),
        "results": results,
    }


def compare_against_baseline(candidate, scenarios, contracts=1,
                              slippage=Decimal("0")):
    baseline = compare(BASELINE, scenarios, contracts, slippage)
    candidate_report = compare(candidate, scenarios, contracts, slippage)
    candidate_report["losses_avoided"] = sum(
        1 for base, item in zip(baseline["results"], candidate_report["results"])
        if base.option_pnl < 0 and item.option_pnl > base.option_pnl)
    candidate_report["winners_missed"] = sum(
        1 for base, item in zip(baseline["results"], candidate_report["results"])
        if base.option_pnl > 0 and item.option_pnl < base.option_pnl)
    candidate_report["losses_worsened_by_waiting"] = sum(
        1 for base, item in zip(baseline["results"], candidate_report["results"])
        if item.option_pnl < base.option_pnl)
    return baseline, candidate_report


def synthetic_scenarios():
    return [
        ("pre4_wick_recovers", [SyntheticQuote.make(i, value) for i, value in enumerate(
            [0, 2.00, 0.90, 3.00, 5.00, 2.00])]),
        ("pre4_wick_becomes_loss", [SyntheticQuote.make(i, value) for i, value in enumerate(
            [0, 2.00, 0.90, -3.25, -4.50])]),
        ("stop_wick_recovers", [SyntheticQuote.make(i, value) for i, value in enumerate(
            [0, -3.25, -2.50, 0, 4.00, 5.00, 2.00])]),
        ("stop_waiting_larger_loss", [SyntheticQuote.make(i, value) for i, value in enumerate(
            [0, -3.25, -4.50])]),
    ]


def _dataset_contract(contract):
    required = ("symbol", "expiration", "right", "strike", "trading_class",
                "multiplier", "currency", "con_id")
    if not isinstance(contract, dict) or any(key not in contract for key in required):
        raise ValueError("complete option contract identity is required")
    if contract["symbol"] != "SPX" or contract["trading_class"] != "SPXW":
        raise ValueError("dataset contract is not SPX/SPXW")
    if contract["right"] not in {"C", "P"} or contract["currency"] != "USD":
        raise ValueError("dataset option identity is invalid")
    if (_decimal(contract["strike"], "strike") <= 0
            or _decimal(contract["multiplier"], "multiplier") != OPTION_MULTIPLIER):
        raise ValueError("dataset strike or multiplier is invalid")
    if not isinstance(contract["con_id"], int) or contract["con_id"] <= 0:
        raise ValueError("dataset con_id is invalid")
    return tuple((key, str(contract[key])) for key in required)


def load_local_dataset(path):
    """Load and validate real local observations; reject incomplete data."""
    raw = json.loads(Path(path).read_text())
    if raw.get("dataset_version") != 1 or not isinstance(raw.get("scenarios"), list):
        raise ValueError("unsupported or malformed dataset version")
    scenarios = []
    for case in raw["scenarios"]:
        if not isinstance(case, dict) or case.get("direction") not in {"CALL", "PUT"}:
            raise ValueError("scenario direction is invalid")
        entry = _decimal(case.get("entry_spx"), "entry_spx")
        if entry <= 0:
            raise ValueError("entry_spx must be positive")
        identity = _dataset_contract(case.get("contract"))
        quotes = []
        previous = None
        for item in case.get("observations", []):
            source_spx = _timestamp(item.get("spx_timestamp"), "spx_timestamp")
            source_option = _timestamp(item.get("option_timestamp"), "option_timestamp")
            observation = _timestamp(item.get("timestamp"), "timestamp")
            if abs((source_spx - source_option).total_seconds()) > float(MAX_SYNC_SECONDS):
                raise ValueError("SPX/option source timestamps exceed 0.25 seconds")
            if previous is not None and observation <= previous:
                raise ValueError("observation timestamps must be strictly increasing")
            previous = observation
            spx = _decimal(item.get("spx"), "spx")
            bid = _decimal(item.get("option_bid"), "option_bid")
            ask = _decimal(item.get("option_ask"), "option_ask")
            if spx <= 0 or bid <= 0 or ask <= 0 or bid > ask:
                raise ValueError("invalid SPX or option quote")
            favorable = spx - entry if case["direction"] == "CALL" else entry - spx
            quotes.append(SyntheticQuote(item["timestamp"], favorable, bid, ask))
        if not quotes:
            raise ValueError("scenario has no observations")
        scenarios.append((case.get("name", "unnamed"), quotes))
    return scenarios


def empirical_report(path):
    scenarios = load_local_dataset(path)
    baseline = compare(BASELINE, scenarios, MAX_CONTRACTS, MODELED_SLIPPAGE_PER_CONTRACT)
    reports = [baseline]
    for candidate in (EARLY_FAILURE, CONFIRMED_STOP):
        _, item = compare_against_baseline(candidate, scenarios, MAX_CONTRACTS,
                                           MODELED_SLIPPAGE_PER_CONTRACT)
        reports.append(item)
    return {"data_kind": "LOCAL_TIMESTAMPED_DATASET", "historical_data_used": True,
            "empirical_conclusion": "QUALIFYING_DATA_COMPARED", "executable": False,
            "reports": reports}


def report():
    return {"data_kind": "NO_QUALIFYING_LOCAL_DATASET", "historical_data_used": False,
            "empirical_conclusion": "NO_EMPIRICAL_CONCLUSION",
            "reason": "No local historical timestamped SPX/option dataset was supplied",
            "production_strategy_changed": False, "executable": False, "reports": []}


@dataclass(frozen=True)
class MinuteBar:
    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class HistoricalEntry:
    entry_id: str
    entry_timestamp: str
    direction: str
    entry_price: Decimal


@dataclass(frozen=True)
class OHLCResult:
    entry_id: str
    policy: str
    outcome: str
    bar_timestamp: str
    confirmed_stop_price: Decimal
    worse_by_points: Decimal


def _bar(item):
    if not isinstance(item, dict):
        raise ValueError("OHLC bar must be an object")
    values = [_decimal(item.get(name), name) for name in ("open", "high", "low", "close")]
    timestamp = _timestamp(item.get("timestamp"), "bar timestamp")
    opening, high, low, closing = values
    if min(values) <= 0 or high < max(opening, closing, low) or low > min(opening, closing, high):
        raise ValueError("OHLC bar violates high/low bounds")
    return MinuteBar(item["timestamp"], opening, high, low, closing)


def load_ohlc_dataset(path):
    """Load explicit historical entry times and one-minute SPX OHLC bars."""
    raw = json.loads(Path(path).read_text())
    if raw.get("dataset_version") != 1 or not isinstance(raw.get("entries"), list):
        raise ValueError("unsupported or malformed OHLC dataset version")
    loaded = []
    for item in raw["entries"]:
        if not isinstance(item, dict) or not isinstance(item.get("entry_id"), str):
            raise ValueError("entry_id is required")
        if item.get("direction") not in {"CALL", "PUT"}:
            raise ValueError("entry direction is invalid")
        entry_time = _timestamp(item.get("entry_timestamp"), "entry_timestamp")
        entry_price = _decimal(item.get("entry_price"), "entry_price")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        bars = [_bar(bar) for bar in item.get("bars", [])]
        if not bars:
            raise ValueError("entry has no OHLC bars")
        previous = None
        for bar in bars:
            current = _timestamp(bar.timestamp, "bar timestamp")
            if previous is not None and current <= previous:
                raise ValueError("OHLC timestamps must be strictly increasing")
            previous = current
        loaded.append((HistoricalEntry(item["entry_id"], item["entry_timestamp"],
                                       item["direction"], entry_price), bars))
    return loaded


def _csv_timestamp(value, unit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("CSV time is missing")
    value = value.strip()
    if unit == "iso":
        return _timestamp(value, "CSV time")
    number = _decimal(value, "CSV epoch time")
    if unit == "unix_seconds":
        seconds = number
    elif unit == "unix_milliseconds":
        seconds = number / Decimal("1000")
    else:
        raise ValueError("timestamp unit must be iso, unix_seconds, or unix_milliseconds")
    if seconds < 0:
        raise ValueError("CSV epoch time cannot be negative")
    return datetime.fromtimestamp(float(seconds), timezone.utc)


def tradingview_csv_to_dataset(path, entry_timestamp, direction, entry_price,
                               timestamp_unit="unix_seconds", entry_id="csv_entry"):
    """Convert a TradingView OHLC export into load_ohlc_dataset JSON data.

    The timestamp unit is explicit; seconds and milliseconds are never guessed.
    TradingView's optional volume and other columns are ignored, while the
    required time/open/high/low/close columns are validated.
    """
    if direction not in {"CALL", "PUT"}:
        raise ValueError("entry direction must be CALL or PUT")
    entry_time = _timestamp(entry_timestamp, "entry_timestamp")
    entry_value = _decimal(entry_price, "entry_price")
    if entry_value <= 0:
        raise ValueError("entry_price must be positive")
    bars = []
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("CSV header is missing")
        fields = {name.strip().lower(): name for name in reader.fieldnames if name}
        required = ("time", "open", "high", "low", "close")
        if any(name not in fields for name in required):
            raise ValueError("CSV requires time, open, high, low, and close columns")
        previous = None
        for row in reader:
            stamp = _csv_timestamp(row[fields["time"]], timestamp_unit)
            values = {name: _decimal(row[fields[name]], name) for name in required[1:]}
            opening, high, low, closing = (values[name] for name in required[1:])
            if min(values.values()) <= 0 or high < max(opening, closing, low):
                raise ValueError("CSV row has invalid OHLC values")
            if previous is not None:
                spacing = (stamp - previous).total_seconds()
                if spacing != 60:
                    raise ValueError("CSV bars must be chronological one-minute intervals")
            previous = stamp
            bars.append({"timestamp": stamp.isoformat(),
                         "open": str(opening), "high": str(high),
                         "low": str(low), "close": str(closing)})
    if not bars:
        raise ValueError("CSV contains no rows")
    if entry_time > _timestamp(bars[-1]["timestamp"], "bar timestamp") + timedelta(minutes=1):
        raise ValueError("entry_timestamp is after the supplied CSV range")
    return {"dataset_version": 1, "entries": [{
        "entry_id": entry_id, "entry_timestamp": entry_time.isoformat(),
        "direction": direction, "entry_price": str(entry_value), "bars": bars
    }]}


def replay_ohlc_entry(entry, bars):
    """Compare range-touch stop with one-minute-close stop confirmation.

    A bar cannot establish intrabar order. If both levels are present in one
    bar, or the entry occurs inside that bar and either level is present, the
    result is UNKNOWN.
    """
    entry_time = _timestamp(entry.entry_timestamp, "entry_timestamp")
    stop = entry.entry_price - Decimal(str(INITIAL_STOP_POINTS)) if entry.direction == "CALL" else entry.entry_price + Decimal(str(INITIAL_STOP_POINTS))
    target = entry.entry_price + Decimal(str(PROFIT_PROTECTION_ARM_POINTS)) if entry.direction == "CALL" else entry.entry_price - Decimal(str(PROFIT_PROTECTION_ARM_POINTS))
    baseline = None
    confirmed = None
    confirmed_price = None
    for bar in bars:
        start = _timestamp(bar.timestamp, "bar timestamp")
        end = start + timedelta(minutes=1)
        if end <= entry_time:
            continue
        partial_entry_bar = start < entry_time < end
        stop_possible = (bar.low <= stop) if entry.direction == "CALL" else (bar.high >= stop)
        target_possible = (bar.high >= target) if entry.direction == "CALL" else (bar.low <= target)
        if not (stop_possible or target_possible):
            continue
        if partial_entry_bar or (stop_possible and target_possible):
            baseline = baseline or OHLCResult(entry.entry_id, "BASELINE_LOCKED", "UNKNOWN",
                                              bar.timestamp, Decimal("0"), Decimal("0"))
            confirmed = confirmed or OHLCResult(entry.entry_id, "CANDIDATE_ONE_MINUTE_CLOSE",
                                                "UNKNOWN", bar.timestamp, Decimal("0"), Decimal("0"))
            break
        if baseline is None:
            baseline = OHLCResult(entry.entry_id, "BASELINE_LOCKED",
                                  "STOPPED_OUT" if stop_possible else "REACHED_PLUS4",
                                  bar.timestamp, Decimal("0"), Decimal("0"))
        if confirmed is None:
            favorable_close = (bar.close - entry.entry_price if entry.direction == "CALL"
                               else entry.entry_price - bar.close)
            if target_possible:
                confirmed = OHLCResult(entry.entry_id, "CANDIDATE_ONE_MINUTE_CLOSE",
                                       "REACHED_PLUS4", bar.timestamp, Decimal("0"), Decimal("0"))
            elif favorable_close <= -Decimal(str(INITIAL_STOP_POINTS)):
                confirmed_price = favorable_close
                worse = max(Decimal("0"), -Decimal(str(INITIAL_STOP_POINTS)) - favorable_close)
                confirmed = OHLCResult(entry.entry_id, "CANDIDATE_ONE_MINUTE_CLOSE",
                                       "STOPPED_OUT", bar.timestamp, favorable_close, worse)
    if baseline is None:
        baseline = OHLCResult(entry.entry_id, "BASELINE_LOCKED", "NO_DECISION", "", Decimal("0"), Decimal("0"))
    if confirmed is None:
        confirmed = OHLCResult(entry.entry_id, "CANDIDATE_ONE_MINUTE_CLOSE", "NO_DECISION", "", Decimal("0"), Decimal("0"))
    return baseline, confirmed


def compare_ohlc_dataset(entries):
    rows = []
    for entry, bars in entries:
        rows.extend(replay_ohlc_entry(entry, bars))
    return {"data_kind": "LOCAL_SPX_ONE_MINUTE_OHLC", "executable": False,
            "empirical_conclusion": "QUALIFYING_DATA_COMPARED" if rows else "NO_EMPIRICAL_CONCLUSION",
            "results": rows,
            "worse_confirmed_stop_points": sum((row.worse_by_points for row in rows
                                                 if row.policy == "CANDIDATE_ONE_MINUTE_CLOSE"), Decimal("0"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline SPX pre-+4 OHLC research")
    parser.add_argument("--csv", help="TradingView 1-minute CSV")
    parser.add_argument("--entry-timestamp")
    parser.add_argument("--direction", choices=("CALL", "PUT"))
    parser.add_argument("--entry-price")
    parser.add_argument("--timestamp-unit", choices=("iso", "unix_seconds", "unix_milliseconds"),
                        default="unix_seconds")
    parser.add_argument("--entry-id", default="csv_entry")
    parser.add_argument("--output", default="/tmp/mortificatio_pre4_replay.json")
    args = parser.parse_args()
    if not args.csv or not args.entry_timestamp or not args.direction or not args.entry_price:
        parser.error("--csv, --entry-timestamp, --direction, and --entry-price are required")
    converted = tradingview_csv_to_dataset(
        args.csv, args.entry_timestamp, args.direction, args.entry_price,
        timestamp_unit=args.timestamp_unit, entry_id=args.entry_id)
    Path(args.output).write_text(json.dumps(converted, indent=2) + "\n")
    results = compare_ohlc_dataset(load_ohlc_dataset(args.output))
    print(json.dumps({"output": args.output, "executable": False,
                      "empirical_conclusion": results["empirical_conclusion"],
                      "worse_confirmed_stop_points": str(results["worse_confirmed_stop_points"]),
                      "results": [result.__dict__ for result in results["results"]]},
                     default=str, indent=2))
