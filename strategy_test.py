"""Simulation checks against the Executor strategy implementation."""

from executor_engine import Direction, EngineState, Executor, OptionCandidate


def trade(direction, prices, quantity=25):
    engine = Executor()
    candidate = OptionCandidate(1001, 7700, 10.0)
    request = engine.request_entry(direction, 7700.0, quantity * 1000.0,
                                   [candidate], True, 0, 0)
    assert request["event"] == "DRY_RUN_ENTRY_REQUEST"
    engine.simulate_entry_fill(direction, 1001, 7700, quantity, 7700.0)
    decisions = [engine.update_spx(price) for price in prices]
    return engine, decisions


if __name__ == "__main__":
    for direction, stop in ((Direction.CALL, 7696.75),
                            (Direction.PUT, 7703.25)):
        engine, decisions = trade(direction, [stop])
        assert decisions[-1]["reason"] == "INITIAL_STOP"
        assert decisions[-1]["quantity"] == 25
    for direction, peak, reversal in (
        (Direction.CALL, 7704.0, 7701.25),
        (Direction.PUT, 7696.0, 7698.75),
    ):
        engine, decisions = trade(direction, [peak, reversal])
        assert decisions[-1]["reason"] == "PROFIT_PROTECTION_FLOOR"
        assert engine.state == EngineState.EXITING
    for direction, peak, hold, exit_price in (
        (Direction.CALL, 7710.0, 7707.01, 7707.0),
        (Direction.PUT, 7690.0, 7692.99, 7693.0),
    ):
        engine, decisions = trade(direction, [peak, hold, exit_price])
        assert decisions[1] is None
        assert decisions[2]["reason"] == "LET_IT_RIDE_TRAIL"
        assert decisions[2]["quantity"] == 25
    print("CURRENT STRATEGY SIMULATION PASS; ZERO IBKR ORDERS")
