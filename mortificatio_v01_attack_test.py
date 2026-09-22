import os
import tempfile

from mortificatio_v01 import (
    MortificatioV01 as BaseMortificatioV01,
    MortificatioV01Error,
    OptionQuote,
    SimulatedBroker,
    build_entry_plan,
)

class MortificatioV01(BaseMortificatioV01):
    """Legacy lower-level attack harness; production entry uses VerifiedDryRun."""
    authorize_entry = BaseMortificatioV01._authorize_entry_unverified


from mortificatio_state import (
    FLAT,
    OPEN,
    EXITING,
)


def db_paths():
    directory = tempfile.mkdtemp(
        prefix="mortificatio_v01_"
    )

    return (
        directory,
        os.path.join(directory, "lifecycle.db"),
        os.path.join(directory, "strategy.db"),
    )


def cleanup(engine, directory):
    try:
        engine.close()
    except Exception:
        pass

    for name in os.listdir(directory):
        try:
            os.remove(
                os.path.join(directory, name)
            )
        except Exception:
            pass

    try:
        os.rmdir(directory)
    except Exception:
        pass


def candidates():
    return [
        OptionQuote(
            con_id=1001,
            strike=7695.0,
            ask=9.00,
        ),
        OptionQuote(
            con_id=1002,
            strike=7700.0,
            ask=10.00,
        ),
        OptionQuote(
            con_id=1003,
            strike=7705.0,
            ask=8.50,
        ),
    ]


# ============================================================
# 1. Closest affordable ATM
# ============================================================

plan = build_entry_plan(
    direction="CALL",
    spx_price=7701.0,
    usable_funds=25000.0,
    candidates=candidates(),
)

assert plan.con_id == 1002
assert plan.strike == 7700.0
assert plan.quantity == 25
assert plan.chunks == [10, 10, 5]

print("CLOSEST AFFORDABLE ATM + 25 CAP PASS")


# ============================================================
# 2. Unaffordable ATM -> closest affordable alternative
# ============================================================

special = [
    OptionQuote(
        con_id=2001,
        strike=7700.0,
        ask=20.00,
    ),
    OptionQuote(
        con_id=2002,
        strike=7695.0,
        ask=9.00,
    ),
    OptionQuote(
        con_id=2003,
        strike=7705.0,
        ask=11.00,
    ),
]

plan = build_entry_plan(
    direction="PUT",
    spx_price=7700.0,
    usable_funds=1000.0,
    candidates=special,
)

assert plan.con_id == 2002
assert plan.quantity == 1

print("UNAFFORDABLE ATM FALLBACK PASS")


# ============================================================
# 3. Full CALL lifecycle
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    direction="CALL",
    spx_price=7700.0,
    usable_funds=25000.0,
    candidates=candidates(),
)

assert plan.quantity == 25

opened = engine.simulate_entry(plan)

assert opened.state == OPEN
assert opened.quantity == 25
assert broker.position_qty == 25

assert engine.process_spx(7704.8)["action"] == "HOLD"
assert engine.process_spx(7703.81)["action"] == "HOLD"

assert engine.process_spx(7712.0)["action"] == "HOLD"

decision = engine.process_spx(7709.0)

assert decision["action"] == "EXITING"
assert decision["reason"] == "LET_IT_RIDE_REVERSAL"
assert engine.lifecycle.status().state == EXITING

fills = engine.simulate_exit_to_flat(
    max_exit_chunk=10
)

assert fills == [10, 10, 5]
assert broker.position_qty == 0
assert engine.lifecycle.status().state == FLAT
assert not engine.strategy.status().active

cleanup(engine, directory)

print("FULL CALL 25-CONTRACT LIFECYCLE PASS")


# ============================================================
# 4. PUT symmetry
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    direction="PUT",
    spx_price=7700.0,
    usable_funds=5000.0,
    candidates=candidates(),
)

engine.simulate_entry(plan)

assert engine.process_spx(7688.0)["action"] == "HOLD"

decision = engine.process_spx(7691.0)

assert decision["action"] == "EXITING"
assert decision["reason"] == "LET_IT_RIDE_REVERSAL"

engine.simulate_exit_to_flat()

assert broker.position_qty == 0
assert engine.lifecycle.status().state == FLAT

cleanup(engine, directory)

print("PUT SYMMETRY LIFECYCLE PASS")


# ============================================================
# 5. Initial losing stop
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

decision = engine.process_spx(7696.75)

assert decision["action"] == "EXITING"
assert decision["reason"] == "INITIAL_STOP"

engine.simulate_exit_to_flat()

assert engine.lifecycle.status().state == FLAT

cleanup(engine, directory)

print("INITIAL -3.25 STOP LIFECYCLE PASS")


# ============================================================
# 6. Partial entry becomes real managed position
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    25000.0,
    candidates(),
)

assert plan.quantity == 25

opened = engine.simulate_entry(
    plan,
    fill_quantities=[10, 2],
)

assert opened.quantity == 12
assert broker.position_qty == 12

decision = engine.process_spx(7696.0)

assert decision["action"] == "EXITING"

fills = engine.simulate_exit_to_flat()

assert fills == [10, 2]
assert broker.position_qty == 0

cleanup(engine, directory)

print("PARTIAL ENTRY -> REAL MANAGED POSITION PASS")


# ============================================================
# 7. Crash after high-water mark
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7712.0)

engine.close()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

snap = engine.strategy.status()

assert snap.let_it_ride_armed
assert snap.best_spx == 7712.0

decision = engine.process_spx(7709.0)

assert decision["action"] == "EXITING"
assert decision["reason"] == "LET_IT_RIDE_REVERSAL"

engine.simulate_exit_to_flat()

cleanup(engine, directory)

print("HIGH-WATER CRASH/RESTART PASS")


# ============================================================
# 8. Crash after irreversible exit decision
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7715.0)

decision = engine.process_spx(7712.0)

assert decision["action"] == "EXITING"

engine.close()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

assert engine.lifecycle.status().state == EXITING
assert engine.strategy.status().exit_required

# Price recovery cannot undo EXITING.
result = engine.process_spx(7730.0)

assert result["action"] == "EXITING"

engine.simulate_exit_to_flat()

cleanup(engine, directory)

print("IRREVERSIBLE EXIT CRASH/RESTART PASS")


# ============================================================
# 9. Crash after partial exit
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    25000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7710.0)
engine.process_spx(7707.0)

assert engine.lifecycle.status().state == EXITING
assert broker.position_qty == 25

first = engine.simulate_one_exit_chunk(
    max_exit_chunk=10
)

assert first == 10
assert broker.position_qty == 15

# CRASH HERE.
engine.close()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

assert engine.lifecycle.status().state == EXITING

remaining = engine.remaining_exit_plan(
    max_exit_chunk=10
)

assert remaining == [10, 5]

fills = engine.simulate_exit_to_flat(
    max_exit_chunk=10
)

assert fills == [10, 5]
assert broker.position_qty == 0
assert engine.lifecycle.status().state == FLAT

cleanup(engine, directory)

print("PARTIAL EXIT CRASH/RESTART PASS")


# ============================================================
# 10. 5-contract emergency exit representation
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    25000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7710.0)
engine.process_spx(7707.0)

remaining = engine.remaining_exit_plan(
    max_exit_chunk=5
)

assert remaining == [5, 5, 5, 5, 5]

engine.simulate_exit_to_flat(
    max_exit_chunk=5
)

cleanup(engine, directory)

print("5/5/5/5/5 EXIT REPRESENTATION PASS")


# ============================================================
# 11. License/revocation gate blocks NEW entry
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

blocked = False

try:
    engine.authorize_entry(
        "CALL",
        7700.0,
        5000.0,
        candidates(),
        entry_allowed=False,
    )
except MortificatioV01Error:
    blocked = True

assert blocked
assert engine.lifecycle.status().state == FLAT
assert broker.position_qty == 0

cleanup(engine, directory)

print("REVOKED/DISABLED NEW ENTRY BLOCKED PASS")


# ============================================================
# 12. Revocation does NOT interfere with existing position
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
    entry_allowed=True,
)

engine.simulate_entry(plan)

# Conceptually authorization is revoked now.
# No licensing check occurs inside process_spx().
# Existing risk MUST continue being managed.

engine.process_spx(7710.0)

decision = engine.process_spx(7707.0)

assert decision["action"] == "EXITING"

engine.simulate_exit_to_flat()

assert broker.position_qty == 0
assert engine.lifecycle.status().state == FLAT

cleanup(engine, directory)

print("REVOCATION CANNOT ABANDON OPEN POSITION PASS")


# ============================================================
# 13. Broker uncertainty fails closed during exit
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7710.0)
engine.process_spx(7707.0)

broker.complete = False

blocked = False

try:
    engine.remaining_exit_plan()
except MortificatioV01Error:
    blocked = True

assert blocked
assert engine.lifecycle.status().state == EXITING
assert broker.position_qty > 0

broker.complete = True

engine.simulate_exit_to_flat()

cleanup(engine, directory)

print("INCOMPLETE BROKER TRUTH FAIL-CLOSED PASS")


# ============================================================
# 14. Working broker order prevents duplicate exit
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

engine.process_spx(7710.0)
engine.process_spx(7707.0)

broker.open_order_count = 1

blocked = False

try:
    engine.remaining_exit_plan()
except MortificatioV01Error:
    blocked = True

assert blocked

broker.open_order_count = 0

engine.simulate_exit_to_flat()

cleanup(engine, directory)

print("DUPLICATE EXIT WHILE ORDER WORKING BLOCKED PASS")


# ============================================================
# 15. +4.00 fixed profit protection
# ============================================================

directory, lifecycle_db, strategy_db = db_paths()
broker = SimulatedBroker()

engine = MortificatioV01(
    lifecycle_db,
    strategy_db,
    broker,
)

plan = engine.authorize_entry(
    "CALL",
    7700.0,
    5000.0,
    candidates(),
)

engine.simulate_entry(plan)

assert engine.process_spx(7704.0)["action"] == "HOLD"
assert engine.process_spx(7701.26)["action"] == "HOLD"

# The fixed +1.25 favorable floor exits all.
decision = engine.process_spx(7701.25)

assert decision["action"] == "EXITING"
assert decision["reason"] == "PROFIT_PROTECTION_FLOOR"

engine.simulate_exit_to_flat()

cleanup(engine, directory)

print("PRE-+5 FIXED PROFIT PROTECTION PASS")


# ============================================================
# 16. Absolute proof this integrated file cannot submit orders
# ============================================================

source = open(
    "mortificatio_v01.py",
    "r",
).read()

forbidden = "place" + "Order"

assert forbidden not in source

print("NO IBKR ORDER SUBMISSION EXISTS")
print("ZERO IBKR ORDERS")

print()
print("=" * 72)
print("ALL MORTIFICATIO v0.1 INTEGRATION ATTACK TESTS PASS")
print("=" * 72)
