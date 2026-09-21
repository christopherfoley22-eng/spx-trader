"""Deterministic synthetic market-session builders; no strategy decisions."""

from datetime import datetime, timedelta
import random
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BASE = datetime(2026, 9, 21, 9, 30, tzinfo=NY)
ACCOUNT = "SYNTHETIC_REPLAY_ONLY"


def make_session(name, direction, favorable, *, quantity=11, fill_mode="AUTO",
                 max_exit_chunk=10, start_offset_seconds=0):
    """Build version-1 observations from a favorable SPX path.

    Favorable values describe test inputs, not trade signals. The controller
    alone determines state transitions and exit triggers.
    """
    if direction not in {"CALL", "PUT"}:
        raise ValueError("Direction invalid")
    right, con_id = ("C", 101) if direction == "CALL" else ("P", 102)
    observations = []
    for sequence, excursion in enumerate([0.0] + list(favorable)):
        now = BASE + timedelta(seconds=start_offset_seconds,
                               milliseconds=100 * sequence)
        mono = 100.0 + start_offset_seconds + sequence * 0.1
        price = 5000.0 + excursion if direction == "CALL" else 5000.0 - excursion
        timestamp = now.isoformat()
        option = {
            "contract": {"symbol": "SPX", "sec_type": "OPT", "right": right,
                         "expiration": "20260921", "trading_class": "SPXW",
                         "multiplier": "100", "currency": "USD", "strike": 5000.0,
                         "con_id": con_id},
            "quote_con_id": con_id, "bid": 9.9, "ask": 10.0,
            "status": "LIVE", "source_time": timestamp,
            "received_monotonic": mono,
        }
        funds = str(quantity * 1000)
        observations.append({
            "sequence": sequence, "ny_time": timestamp, "monotonic": mono,
            "spx": {"price": price, "status": "LIVE", "source_time": timestamp,
                    "received_monotonic": mono},
            "options": [option],
            "account": {"account_id": ACCOUNT, "selected_account": ACCOUNT,
                        "complete": True, "currency": "USD", "available_funds": funds,
                        "buying_power": funds, "excess_liquidity": funds,
                        "total_cash_value": funds, "settled_cash": funds,
                        "oldest_required_receipt_monotonic": mono},
            "broker": "SIMULATOR", "fills": [],
        })
    return {"version": 1, "session_id": name, "direction": direction,
            "intent_sequence": 0, "account_id": ACCOUNT,
            "expirations": ["20260921"], "fill_mode": fill_mode,
            "max_exit_chunk": max_exit_chunk, "events": observations}


def seeded_paths(seed, count, steps=24):
    """Fixed-seed path generation for safety invariants, never P&L claims."""
    rng = random.Random(seed)
    for index in range(count):
        position = 0.0
        path = []
        for _ in range(steps):
            position = round(position + rng.choice((-5.0, -3.25, -1.0, -0.1,
                                                     0.0, 0.1, 1.0, 3.0, 4.8, 5.0)), 2)
            path.append(position)
        yield index, path
