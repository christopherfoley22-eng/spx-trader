from dataclasses import dataclass


FLAT = "FLAT"
OPEN = "OPEN"
ENTERING = "ENTERING"
EXITING = "EXITING"


@dataclass
class LocalState:
    status: str
    quantity: int = 0
    con_id: int = 0


@dataclass
class BrokerState:
    position_quantity: int = 0
    position_con_id: int = 0
    open_orders: int = 0
    snapshot_complete: bool = True


def entry_gate(local, broker):
    """
    FAIL-CLOSED reconciliation gate.

    Entry is permitted only when:
      - reconciliation completed successfully
      - Executor believes it is FLAT
      - IBKR confirms no position
      - IBKR confirms no open orders
    """

    if not broker.snapshot_complete:
        return False, "RECONCILIATION INCOMPLETE"

    if local.status != FLAT:
        return False, f"LOCAL STATE IS {local.status}"

    if local.quantity != 0:
        return False, "LOCAL QUANTITY NOT ZERO"

    if broker.position_quantity != 0:
        return False, "IBKR POSITION EXISTS"

    if broker.open_orders != 0:
        return False, "IBKR OPEN ORDER EXISTS"

    return True, "ENTRY ELIGIBLE"


def position_reconciliation(local, broker):
    """
    Check an already-open position.

    Executor does not blindly trust its own database.
    """

    if not broker.snapshot_complete:
        return False, "RECONCILIATION INCOMPLETE"

    if local.status not in (OPEN, EXITING):
        return False, "LOCAL STATE DOES NOT EXPECT POSITION"

    if broker.position_quantity == 0:
        return False, "LOCAL EXPECTS POSITION BUT IBKR IS FLAT"

    if local.quantity != broker.position_quantity:
        return False, "POSITION QUANTITY MISMATCH"

    if local.con_id != broker.position_con_id:
        return False, "CONTRACT MISMATCH"

    return True, "POSITION MATCHES IBKR"


def test_entry(name, local, broker, expected):
    allowed, reason = entry_gate(local, broker)

    print(f"\n{name}")
    print(f"allowed={allowed} | {reason}")

    assert allowed == expected


def test_position(name, local, broker, expected):
    matched, reason = position_reconciliation(local, broker)

    print(f"\n{name}")
    print(f"matched={matched} | {reason}")

    assert matched == expected


print("\n========================================")
print("ENTRY-GATE ATTACK TESTS")
print("========================================")

# Only safe case.
test_entry(
    "1. BOTH SIDES CONFIRM FLAT",
    LocalState(FLAT, 0),
    BrokerState(0, 0, 0, True),
    True,
)

# Executor thinks flat, IBKR says position exists.
test_entry(
    "2. HIDDEN BROKER POSITION",
    LocalState(FLAT, 0),
    BrokerState(10, 12345, 0, True),
    False,
)

# Executor thinks flat, but IBKR has working order.
test_entry(
    "3. UNKNOWN OPEN ORDER",
    LocalState(FLAT, 0),
    BrokerState(0, 0, 1, True),
    False,
)

# Reconciliation failed/timed out.
test_entry(
    "4. RECONCILIATION TIMEOUT",
    LocalState(FLAT, 0),
    BrokerState(0, 0, 0, False),
    False,
)

# Local database itself is contradictory.
test_entry(
    "5. FLAT BUT LOCAL QUANTITY IS 25",
    LocalState(FLAT, 25),
    BrokerState(0, 0, 0, True),
    False,
)

# Duplicate click while entry already underway.
test_entry(
    "6. ENTRY ALREADY IN PROGRESS",
    LocalState(ENTERING, 25),
    BrokerState(0, 0, 0, True),
    False,
)

# Position already open.
test_entry(
    "7. POSITION ALREADY OPEN",
    LocalState(OPEN, 25, 12345),
    BrokerState(25, 12345, 0, True),
    False,
)

# Exit underway.
test_entry(
    "8. EXIT ALREADY IN PROGRESS",
    LocalState(EXITING, 25, 12345),
    BrokerState(25, 12345, 0, True),
    False,
)


print("\n========================================")
print("OPEN-POSITION RECONCILIATION ATTACKS")
print("========================================")

test_position(
    "9. LOCAL AND IBKR MATCH",
    LocalState(OPEN, 25, 12345),
    BrokerState(25, 12345, 0, True),
    True,
)

test_position(
    "10. QUANTITY MISMATCH",
    LocalState(OPEN, 25, 12345),
    BrokerState(23, 12345, 0, True),
    False,
)

test_position(
    "11. WRONG CONTRACT",
    LocalState(OPEN, 25, 12345),
    BrokerState(25, 99999, 0, True),
    False,
)

test_position(
    "12. LOCAL OPEN BUT IBKR FLAT",
    LocalState(OPEN, 25, 12345),
    BrokerState(0, 0, 0, True),
    False,
)

test_position(
    "13. SNAPSHOT DIES DURING RECOVERY",
    LocalState(OPEN, 25, 12345),
    BrokerState(25, 12345, 0, False),
    False,
)

test_position(
    "14. EXITING POSITION STILL MATCHES",
    LocalState(EXITING, 9, 12345),
    BrokerState(9, 12345, 0, True),
    True,
)


print("\n========================================")
print("ALL RECONCILIATION ATTACK TESTS PASS")
print("UNSAFE STATES BLOCKED")
print("ZERO IBKR ORDERS")
print("========================================")
