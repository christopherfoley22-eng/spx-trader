"""Gap-through and sharp reversal simulation against the durable strategy."""

import os
import tempfile
from mortificatio_strategy_state import DurableStrategyState


cases = (
    ("CALL", [7695.0], "INITIAL_STOP"),
    ("PUT", [7705.0], "INITIAL_STOP"),
    ("CALL", [7704.8, 7702.0], "NEAR_WINNER_REVERSAL"),
    ("PUT", [7695.2, 7698.0], "NEAR_WINNER_REVERSAL"),
    ("CALL", [7712.0, 7708.0], "LET_IT_RIDE_REVERSAL"),
    ("PUT", [7688.0, 7692.0], "LET_IT_RIDE_REVERSAL"),
)

for direction, prices, reason in cases:
    fd, path = tempfile.mkstemp(prefix="violent_move_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        state = DurableStrategyState(path)
        state.activate(direction, 123, 7700.0)
        decisions = [state.process_spx(price) for price in prices]
        assert decisions[-1].action == "EXIT"
        assert decisions[-1].reason == reason
        assert state.status().exit_required
        state.close()
    finally:
        os.remove(path)

print("VIOLENT MOVE SIMULATION PASS; ZERO IBKR ORDERS")
