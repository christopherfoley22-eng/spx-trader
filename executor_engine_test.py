from executor_engine import (
    Executor,
    Direction,
    EngineState,
    StrategyState,
    OptionCandidate,
)

def candidates():
    return [
        OptionCandidate(1001, 7695, 11.00),
        OptionCandidate(1002, 7700, 10.00),
        OptionCandidate(1003, 7705, 9.00),
    ]

# ---- FIRST TRADE: CALL ----

e = Executor()

r = e.request_entry(
    Direction.CALL, 7700.00, 25000, candidates(),
    True, 0, 0
)
assert r["event"] == "DRY_RUN_ENTRY_REQUEST"
assert r["strike"] == 7700
assert r["quantity"] == 25
assert e.state == EngineState.ENTERING

# Duplicate tap must fail.
r = e.request_entry(
    Direction.CALL, 7700, 25000, candidates(),
    True, 0, 0
)
assert r["event"] == "ENTRY_BLOCKED"

e.simulate_entry_fill(
    Direction.CALL,
    1002,
    7700,
    25,
    7700,
)

assert e.state == EngineState.OPEN
assert e.trades_today == 1

# +4.00 arms the fixed profit floor.
e.update_spx(7704.00)
assert e.position.strategy_state == StrategyState.PROFIT_PROTECTION

# The fixed floor remains inactive at +1.26.
assert e.update_spx(7701.26) is None

# +5 switches permanently into Let It Ride.
e.update_spx(7705.00)
assert e.position.strategy_state == StrategyState.LET_IT_RIDE

# Runner reaches +13.50.
e.update_spx(7708.00)
e.update_spx(7710.00)
e.update_spx(7713.50)

assert e.position.max_favorable == 13.50

# 2.99 giveback = HOLD.
assert e.update_spx(7710.51) is None
assert e.state == EngineState.OPEN

# Exactly 3.00 giveback = EXIT ALL.
r = e.update_spx(7710.50)

assert r["event"] == "DRY_RUN_EXIT_ALL"
assert r["reason"] == "LET_IT_RIDE_TRAIL"
assert r["quantity"] == 25
assert e.state == EngineState.EXITING

# Once EXITING, strategy cannot fire another exit.
assert e.update_spx(7700.00) is None

e.simulate_exit_fill()

assert e.state == EngineState.FLAT
assert e.position is None


# ---- SAFETY GATES ----

r = e.request_entry(
    Direction.PUT, 7700, 25000, candidates(),
    True, 7, 0
)
assert r["reason"] == "IBKR_POSITION_EXISTS"

r = e.request_entry(
    Direction.PUT, 7700, 25000, candidates(),
    True, 0, 1
)
assert r["reason"] == "IBKR_OPEN_ORDER_EXISTS"

r = e.request_entry(
    Direction.PUT, 7700, 25000, candidates(),
    False, 0, 0
)
assert r["reason"] == "RECONCILIATION_INCOMPLETE"


# ---- SECOND TRADE: PUT ----

r = e.request_entry(
    Direction.PUT, 7700, 25000, candidates(),
    True, 0, 0
)
assert r["event"] == "DRY_RUN_ENTRY_REQUEST"

e.simulate_entry_fill(
    Direction.PUT,
    1002,
    7700,
    25,
    7700,
)

assert e.trades_today == 2

# PUT moves 3.25 points against us.
r = e.update_spx(7703.25)

assert r["event"] == "DRY_RUN_EXIT_ALL"
assert r["reason"] == "INITIAL_STOP"

e.simulate_exit_fill()

assert e.state == EngineState.FLAT


# ---- THIRD TRADE MUST BE BLOCKED ----

r = e.request_entry(
    Direction.CALL, 7700, 25000, candidates(),
    True, 0, 0
)

assert r["event"] == "ENTRY_BLOCKED"
assert r["reason"] == "DAILY_TRADE_LIMIT"


print()
print("========================================")
print("EXECUTOR INTEGRATION TESTS PASS")
print("========================================")
print("ATM SELECTION:            PASS")
print("25-CONTRACT CAP:          PASS")
print("DUPLICATE ENTRY BLOCK:    PASS")
print("PROFIT PROTECTION STATE:   PASS")
print("LET-IT-RIDE STATE:        PASS")
print("3-POINT TRAILING EXIT:    PASS")
print("EXIT MUTUAL EXCLUSION:    PASS")
print("BROKER POSITION GATE:     PASS")
print("OPEN-ORDER GATE:          PASS")
print("RECONCILIATION GATE:      PASS")
print("2-TRADE DAILY CAP:        PASS")
print()
print("DRY RUN ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
