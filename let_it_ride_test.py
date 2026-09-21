"""Current CALL and PUT exit boundaries, exercised through Executor."""

from executor_engine import Direction, EngineState, StrategyState
from strategy_test import trade


for direction, before, arm, near_hold, near_exit, ride, ride_hold, ride_exit in (
    (Direction.CALL, 7704.79, 7704.8, 7703.81, 7703.8,
     7705.0, 7702.01, 7702.0),
    (Direction.PUT, 7695.21, 7695.2, 7696.19, 7696.2,
     7695.0, 7697.99, 7698.0),
):
    engine, decisions = trade(direction, [before])
    assert decisions == [None]
    assert engine.position.strategy_state == StrategyState.INITIAL

    engine, decisions = trade(direction, [arm, near_hold])
    assert decisions == [None, None]
    assert engine.position.strategy_state == StrategyState.NEAR_WINNER

    engine, decisions = trade(direction, [arm, near_exit])
    assert decisions[-1]["reason"] == "NEAR_WINNER_REVERSAL"
    assert decisions[-1]["quantity"] == 25

    engine, decisions = trade(direction, [arm, ride, ride_hold, ride_exit])
    assert decisions[:3] == [None, None, None]
    assert decisions[-1]["reason"] == "LET_IT_RIDE_TRAIL"
    assert decisions[-1]["quantity"] == 25
    assert engine.position.strategy_state == StrategyState.LET_IT_RIDE
    assert engine.state == EngineState.EXITING

print("LET IT RIDE BOUNDARIES PASS; ZERO IBKR ORDERS")
