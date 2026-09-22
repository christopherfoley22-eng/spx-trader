"""Current CALL and PUT exit boundaries, exercised through Executor."""

from executor_engine import Direction, EngineState, StrategyState
from strategy_test import trade


for direction, before, arm, floor_hold, floor_exit, ride, ride_hold, ride_exit in (
    (Direction.CALL, 7703.99, 7704.0, 7701.26, 7701.25,
     7705.0, 7702.01, 7702.0),
    (Direction.PUT, 7696.01, 7696.0, 7698.74, 7698.75,
     7695.0, 7697.99, 7698.0),
):
    engine, decisions = trade(direction, [before])
    assert decisions == [None]
    assert engine.position.strategy_state == StrategyState.INITIAL

    engine, decisions = trade(direction, [arm, floor_hold])
    assert decisions == [None, None]
    assert engine.position.strategy_state == StrategyState.PROFIT_PROTECTION

    engine, decisions = trade(direction, [arm, floor_exit])
    assert decisions[-1]["reason"] == "PROFIT_PROTECTION_FLOOR"
    assert decisions[-1]["quantity"] == 25

    engine, decisions = trade(direction, [arm, ride, ride_hold, ride_exit])
    assert decisions[:3] == [None, None, None]
    assert decisions[-1]["reason"] == "LET_IT_RIDE_TRAIL"
    assert decisions[-1]["quantity"] == 25
    assert engine.position.strategy_state == StrategyState.LET_IT_RIDE
    assert engine.state == EngineState.EXITING

print("LET IT RIDE BOUNDARIES PASS; ZERO IBKR ORDERS")
