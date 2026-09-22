"""Boundary and gap checks for the current full-position exit rules."""

from executor_engine import Direction, EngineState
from strategy_test import trade


for direction, prices, reason in (
    (Direction.CALL, [7695.0], "INITIAL_STOP"),
    (Direction.PUT, [7705.0], "INITIAL_STOP"),
    (Direction.CALL, [7704.0, 7701.26, 7701.25], "PROFIT_PROTECTION_FLOOR"),
    (Direction.PUT, [7696.0, 7698.74, 7698.75], "PROFIT_PROTECTION_FLOOR"),
    (Direction.CALL, [7704.8, 7705.0, 7702.01, 7702.0], "LET_IT_RIDE_TRAIL"),
    (Direction.PUT, [7695.2, 7695.0, 7697.99, 7698.0], "LET_IT_RIDE_TRAIL"),
    (Direction.CALL, [7712.4, 7711.4, 7709.4], "LET_IT_RIDE_TRAIL"),
):
    engine, decisions = trade(direction, prices)
    exits = [d for d in decisions if d is not None and d["event"] == "DRY_RUN_EXIT_ALL"]
    assert len(exits) == 1
    assert exits[0]["reason"] == reason
    assert exits[0]["quantity"] == 25
    assert engine.state == EngineState.EXITING

for direction, prices in (
    (Direction.CALL, [7712.4, 7710.41]),
    (Direction.PUT, [7687.6, 7689.59]),
):
    engine, decisions = trade(direction, prices)
    assert decisions == [None, None]
    assert engine.state == EngineState.OPEN
    assert engine.position.quantity == 25

print("CURRENT STRATEGY ATTACK TESTS PASS; ZERO IBKR ORDERS")
