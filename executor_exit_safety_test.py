"""Safety regressions for simulated fills and uncertain SPX data."""

import math
import os
import tempfile

from executor_engine import Direction, EngineState, Executor, OptionCandidate
from mortificatio_strategy_state import DurableStrategyState, StrategyStateError


candidate = OptionCandidate(123, 7700.0, 10.0)
engine = Executor()
request = engine.request_entry(Direction.CALL, 7700.0, 25000.0,
                               [candidate], True, 0, 0)
assert request["event"] == "DRY_RUN_ENTRY_REQUEST"
for args in (
    (Direction.PUT, 123, 7700.0, 25, 7700.0),
    (Direction.CALL, 124, 7700.0, 25, 7700.0),
    (Direction.CALL, 123, 7700.0, 26, 7700.0),
):
    try:
        engine.simulate_entry_fill(*args)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Mismatched simulated fill accepted")
assert engine.state == EngineState.ENTERING
engine.simulate_entry_fill(Direction.CALL, 123, 7700.0, 25, 7700.0)
for price in (math.nan, math.inf, -math.inf):
    try:
        engine.update_spx(price)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Uncertain SPX price accepted")
assert engine.state == EngineState.OPEN
assert engine.position.max_favorable == 0.0

fd, path = tempfile.mkstemp(prefix="durable_exit_safety_", suffix=".db")
os.close(fd)
os.unlink(path)
try:
    state = DurableStrategyState(path)
    state.activate("CALL", 123, 7700.0)
    for price in (math.nan, math.inf, -math.inf):
        try:
            state.process_spx(price)
        except StrategyStateError:
            pass
        else:
            raise AssertionError("Uncertain durable SPX price accepted")
    assert state.status().best_spx == 7700.0
    state.close()
finally:
    os.remove(path)

print("SIMULATED FILL AND UNCERTAIN DATA SAFETY PASS")
