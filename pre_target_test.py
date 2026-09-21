"""Pre-target protection uses the persisted peak and survives restart."""

import os
import tempfile
from mortificatio_strategy_state import DurableStrategyState


for direction, arm, hold, exit_price in (
    ("CALL", 7704.8, 7703.81, 7703.8),
    ("PUT", 7695.2, 7696.19, 7696.2),
):
    fd, path = tempfile.mkstemp(prefix="pre_target_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        state = DurableStrategyState(path)
        state.activate(direction, 123, 7700.0)
        assert state.process_spx(arm).action == "HOLD"
        state.close()
        state = DurableStrategyState(path)
        assert state.process_spx(hold).action == "HOLD"
        decision = state.process_spx(exit_price)
        assert decision.action == "EXIT"
        assert decision.reason == "NEAR_WINNER_REVERSAL"
        state.close()
        state = DurableStrategyState(path)
        assert state.status().exit_required
        assert state.process_spx(arm).action == "EXIT"
        state.close()
    finally:
        os.remove(path)

print("DURABLE PRE-TARGET PROTECTION PASS; ZERO IBKR ORDERS")
