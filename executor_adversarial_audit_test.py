"""Offline regressions for date rollback, broker disagreement and split commits."""

import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from executor_state import BrokerSnapshot as ExecutorBrokerSnapshot
from executor_state import ExecutorState, SafetyError
from mortificatio_state import BrokerSnapshot, MortificatioState, MortificatioStateError
from mortificatio_v01 import MortificatioV01 as BaseMortificatioV01, MortificatioV01Error, OptionQuote, SimulatedBroker


class MortificatioV01(BaseMortificatioV01):
    """Legacy lifecycle attack harness; verified entry is tested separately."""
    authorize_entry = BaseMortificatioV01._authorize_entry_unverified
from safe_sizing import max_affordable_contracts


def blocked(action, error):
    try:
        action()
    except error:
        return
    raise AssertionError("Unsafe action was accepted")


assert max_affordable_contracts("999.99", "10.00") == 0
assert max_affordable_contracts("1000.00", "10.00") == 1
assert max_affordable_contracts("1000.00", "10.000000000000001") == 0
assert max_affordable_contracts("1000000", "0.01") == 25
for invalid in (None, True, float("nan"), float("inf"), -1, 0):
    assert max_affordable_contracts(invalid, 10) == 0
    assert max_affordable_contracts(1000, invalid) == 0


with tempfile.TemporaryDirectory(prefix="executor_audit_") as directory:
    root = Path(directory)
    future = (datetime.now(ZoneInfo("America/New_York")).date()
              + timedelta(days=1)).isoformat()
    for cls, snapshot, error, name in (
        (ExecutorState, ExecutorBrokerSnapshot(True, 0, None, 0), SafetyError, "old"),
        (MortificatioState, BrokerSnapshot(True, 0, None, 0), MortificatioStateError, "new"),
    ):
        state = cls(root / (name + ".db"))
        connection = state.db if name == "old" else state.conn
        table = "executor_state" if name == "old" else "mortificatio_state"
        connection.execute("UPDATE {} SET trading_day = ?, trades_today = 2".format(table), (future,))
        blocked(lambda: state.request_entry("CALL", 1, 123, snapshot), error)
        assert state.status().trading_day == future
        assert state.status().trades_today == 2
        state.close()

    for cls, error, name in (
        (ExecutorState, SafetyError, "old_corrupt"),
        (MortificatioState, MortificatioStateError, "new_corrupt"),
    ):
        state = cls(root / (name + ".db"))
        connection = state.db if name == "old_corrupt" else state.conn
        table = "executor_state" if name == "old_corrupt" else "mortificatio_state"
        connection.execute("UPDATE {} SET trading_day = 'garbage'".format(table))
        blocked(state.status, error)
        state.close()

    blocked(lambda: ExecutorState._validate_snapshot(
        ExecutorBrokerSnapshot(True, True, 123, 0)), SafetyError)
    validator = MortificatioState(root / "snapshot.db")
    blocked(lambda: validator._validate_snapshot(
        BrokerSnapshot(True, True, 123, 0)), MortificatioStateError)
    validator.close()

    race_path = root / "race.db"
    initial = MortificatioState(race_path)
    initial.close()
    gate = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def reserve(direction):
        state = MortificatioState(race_path)
        gate.wait()
        try:
            state.request_entry(direction, 1, 123, BrokerSnapshot(True, 0, None, 0))
            outcome = "RESERVED"
        except MortificatioStateError:
            outcome = "BLOCKED"
        finally:
            state.close()
        with outcomes_lock:
            outcomes.append(outcome)

    threads = (
        threading.Thread(target=reserve, args=("CALL",)),
        threading.Thread(target=reserve, args=("PUT",)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["BLOCKED", "RESERVED"]
    final = MortificatioState(race_path)
    assert final.status().state == "ENTERING"
    final.close()

    for cls, flat, position, error, name in (
        (ExecutorState, ExecutorBrokerSnapshot(True, 0, None, 0),
         ExecutorBrokerSnapshot(True, 1, 123, 0), SafetyError, "old_fill"),
        (MortificatioState, BrokerSnapshot(True, 0, None, 0),
         BrokerSnapshot(True, 1, 123, 0), MortificatioStateError, "new_fill"),
    ):
        state = cls(root / (name + ".db"))
        state.request_entry("CALL", 1, 123, flat)
        state._today = lambda: future
        if name == "old_fill":
            blocked(lambda: state.confirm_entry_fill(1, 123, position), error)
        else:
            blocked(lambda: state.recover_entry(position), error)
        assert state.status().state == "ENTERING"
        assert state.status().trades_today == 0
        state.close()

    broker = SimulatedBroker()
    engine = MortificatioV01(root / "lifecycle.db", root / "strategy.db", broker)
    plan = engine.authorize_entry("CALL", 7700.0, 1000.0,
                                  [OptionQuote(123, 7700.0, 10.0)])
    changed_plan = replace(plan, spx_price=7600.0)
    blocked(lambda: engine.simulate_entry(changed_plan), MortificatioV01Error)
    assert broker.position_qty == 0
    engine.simulate_entry(plan)
    assert engine.lifecycle.status().quantity == 1
    broker.complete = False
    blocked(lambda: engine.process_spx(7704.0), MortificatioStateError)
    broker.complete = True
    broker.position_qty = 0
    broker.con_id = None
    blocked(lambda: engine.process_spx(7704.0), MortificatioV01Error)
    assert engine.strategy.status().best_spx == 7700.0
    broker.position_qty = 1
    broker.con_id = 123
    broker.open_order_count = 1
    blocked(lambda: engine.process_spx(7704.0), MortificatioV01Error)
    broker.open_order_count = 0
    assert engine.process_spx(7704.0)["action"] == "HOLD"
    assert engine.process_spx(7701.25)["action"] == "EXITING"
    broker.simulate_exit_fill(123, 1)
    engine.lifecycle.confirm_flat(broker.snapshot())
    engine.close()

    engine = MortificatioV01(root / "lifecycle.db", root / "strategy.db", broker)
    assert engine.lifecycle.status().state == "FLAT"
    assert engine.strategy.status().active
    blocked(lambda: engine.authorize_entry("CALL", 7700.0, 1000.0,
                                           [OptionQuote(123, 7700.0, 10.0)]), MortificatioV01Error)
    engine.reconcile_post_flat_strategy()
    assert not engine.strategy.status().active
    broker.complete = False
    blocked(engine.reconcile_post_flat_strategy, MortificatioStateError)
    engine.close()

print("ADVERSARIAL STATE AND CRASH RECOVERY TESTS PASS")
