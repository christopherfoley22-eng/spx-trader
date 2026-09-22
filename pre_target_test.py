"""Pre-target protection uses the persisted peak and survives restart."""

import os
import tempfile
from mortificatio_strategy_state import DurableStrategyState


for direction, arm, hold, exit_price in (
    ("CALL", 7704.0, 7701.26, 7701.25),
    ("PUT", 7696.0, 7698.74, 7698.75),
):
    fd, path = tempfile.mkstemp(prefix="pre_target_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        state = DurableStrategyState(path)
        state.activate(direction, 123, 7700.0)
        assert state.process_spx(arm).action == "HOLD"
        assert state.status().profit_protection_armed
        state.close()
        state = DurableStrategyState(path)
        assert state.status().profit_protection_armed
        assert state.process_spx(hold).action == "HOLD"
        decision = state.process_spx(exit_price)
        assert decision.action == "EXIT"
        assert decision.reason == "PROFIT_PROTECTION_FLOOR"
        state.close()
        state = DurableStrategyState(path)
        assert state.status().exit_required
        assert state.process_spx(arm).action == "EXIT"
        state.close()
    finally:
        os.remove(path)

print("DURABLE PRE-TARGET PROTECTION PASS; ZERO IBKR ORDERS")
