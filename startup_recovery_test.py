from dataclasses import dataclass
from enum import Enum, auto


class LocalState(Enum):
    FLAT = auto()
    ENTERING = auto()
    OPEN = auto()
    EXITING = auto()


class RecoveryAction(Enum):
    READY = auto()
    RESUME_OPEN = auto()
    RESUME_EXITING = auto()
    ENTERING_NO_FILL = auto()
    RECOVER_FILLED_ENTRY = auto()
    LOCKED = auto()


@dataclass(frozen=True)
class LocalSnapshot:
    state: LocalState
    con_id: int = None
    quantity: int = 0


@dataclass(frozen=True)
class BrokerSnapshot:
    complete: bool
    con_id: int = None
    quantity: int = 0
    open_orders: int = 0


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str


def recover(local: LocalSnapshot,
            broker: BrokerSnapshot) -> RecoveryDecision:

    # --------------------------------------------------------
    # RULE 1:
    # If broker truth is incomplete, NOTHING is trusted.
    # --------------------------------------------------------

    if not broker.complete:
        return RecoveryDecision(
            RecoveryAction.LOCKED,
            "BROKER_SNAPSHOT_INCOMPLETE",
        )

    # --------------------------------------------------------
    # RULE 2:
    # Unknown working orders mean we cannot safely infer state.
    # --------------------------------------------------------

    if broker.open_orders != 0:
        return RecoveryDecision(
            RecoveryAction.LOCKED,
            "BROKER_OPEN_ORDER_EXISTS",
        )

    # --------------------------------------------------------
    # LOCAL FLAT
    # --------------------------------------------------------

    if local.state == LocalState.FLAT:

        if local.quantity != 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "FLAT_LOCAL_QUANTITY_NOT_ZERO",
            )

        if broker.quantity != 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "UNEXPECTED_BROKER_POSITION",
            )

        return RecoveryDecision(
            RecoveryAction.READY,
            "BOTH_SIDES_CONFIRM_FLAT",
        )

    # --------------------------------------------------------
    # LOCAL ENTERING
    #
    # This is the dangerous crash window:
    # Executor intended to enter, but may have died before
    # recording whether IBKR actually filled it.
    # --------------------------------------------------------

    if local.state == LocalState.ENTERING:

        if broker.quantity == 0:
            return RecoveryDecision(
                RecoveryAction.ENTERING_NO_FILL,
                "BROKER_CONFIRMS_NO_POSITION",
            )

        if local.con_id is None:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "ENTERING_MISSING_EXPECTED_CONTRACT",
            )

        if broker.con_id != local.con_id:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "ENTERING_CONTRACT_MISMATCH",
            )

        if broker.quantity < 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "UNEXPECTED_SHORT_POSITION",
            )

        return RecoveryDecision(
            RecoveryAction.RECOVER_FILLED_ENTRY,
            "ENTRY_FILLED_BEFORE_CRASH",
        )

    # --------------------------------------------------------
    # LOCAL OPEN
    # --------------------------------------------------------

    if local.state == LocalState.OPEN:

        if local.con_id is None or local.quantity <= 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "INVALID_LOCAL_OPEN_STATE",
            )

        if broker.quantity == 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "LOCAL_OPEN_BUT_BROKER_FLAT",
            )

        if broker.quantity < 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "UNEXPECTED_SHORT_POSITION",
            )

        if broker.con_id != local.con_id:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "OPEN_CONTRACT_MISMATCH",
            )

        if broker.quantity != local.quantity:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "OPEN_QUANTITY_MISMATCH",
            )

        return RecoveryDecision(
            RecoveryAction.RESUME_OPEN,
            "OPEN_POSITION_CONFIRMED",
        )

    # --------------------------------------------------------
    # LOCAL EXITING
    # --------------------------------------------------------

    if local.state == LocalState.EXITING:

        if broker.quantity == 0:
            return RecoveryDecision(
                RecoveryAction.READY,
                "EXIT_COMPLETED_BEFORE_CRASH",
            )

        if local.con_id is None:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "EXITING_MISSING_CONTRACT",
            )

        if broker.quantity < 0:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "UNEXPECTED_SHORT_POSITION",
            )

        if broker.con_id != local.con_id:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "EXITING_CONTRACT_MISMATCH",
            )

        # Quantity may legitimately be smaller than local qty
        # because part of the exit could have filled before crash.
        if broker.quantity > local.quantity:
            return RecoveryDecision(
                RecoveryAction.LOCKED,
                "BROKER_QUANTITY_EXCEEDS_LOCAL_POSITION",
            )

        return RecoveryDecision(
            RecoveryAction.RESUME_EXITING,
            "EXIT_PARTIALLY_OR_NOT_FILLED",
        )

    return RecoveryDecision(
        RecoveryAction.LOCKED,
        "UNKNOWN_STATE",
    )


def expect(local, broker, action, reason=None):
    result = recover(local, broker)

    assert result.action == action, (
        f"Expected {action.name}, "
        f"got {result.action.name}: {result.reason}"
    )

    if reason is not None:
        assert result.reason == reason

    return result


print()
print("========================================")
print("STARTUP RECOVERY ATTACK TESTS")
print("========================================")


# 1. Normal clean startup.
expect(
    LocalSnapshot(LocalState.FLAT),
    BrokerSnapshot(True),
    RecoveryAction.READY,
)
print("1. BOTH FLAT: PASS")


# 2. Broker snapshot failed.
expect(
    LocalSnapshot(LocalState.FLAT),
    BrokerSnapshot(False),
    RecoveryAction.LOCKED,
)
print("2. INCOMPLETE BROKER SNAPSHOT: PASS")


# 3. Local flat but broker owns contracts.
expect(
    LocalSnapshot(LocalState.FLAT),
    BrokerSnapshot(True, 1002, 25),
    RecoveryAction.LOCKED,
)
print("3. HIDDEN BROKER POSITION: PASS")


# 4. Working order exists.
expect(
    LocalSnapshot(LocalState.FLAT),
    BrokerSnapshot(True, open_orders=1),
    RecoveryAction.LOCKED,
)
print("4. UNKNOWN WORKING ORDER: PASS")


# 5. Crash during ENTERING; nothing filled.
expect(
    LocalSnapshot(LocalState.ENTERING, 1002, 25),
    BrokerSnapshot(True),
    RecoveryAction.ENTERING_NO_FILL,
)
print("5. ENTERING / NO FILL: PASS")


# 6. THE IMPORTANT ONE:
# Crash during ENTERING, but IBKR actually filled all 25.
expect(
    LocalSnapshot(LocalState.ENTERING, 1002, 25),
    BrokerSnapshot(True, 1002, 25),
    RecoveryAction.RECOVER_FILLED_ENTRY,
)
print("6. ENTERING / FILLED BEFORE CRASH: PASS")


# 7. ENTERING but wrong contract exists.
expect(
    LocalSnapshot(LocalState.ENTERING, 1002, 25),
    BrokerSnapshot(True, 9999, 25),
    RecoveryAction.LOCKED,
)
print("7. ENTERING CONTRACT MISMATCH: PASS")


# 8. Normal OPEN recovery.
expect(
    LocalSnapshot(LocalState.OPEN, 1002, 25),
    BrokerSnapshot(True, 1002, 25),
    RecoveryAction.RESUME_OPEN,
)
print("8. OPEN POSITION RECOVERY: PASS")


# 9. Local says OPEN but IBKR says flat.
expect(
    LocalSnapshot(LocalState.OPEN, 1002, 25),
    BrokerSnapshot(True),
    RecoveryAction.LOCKED,
)
print("9. OPEN / BROKER FLAT CONTRADICTION: PASS")


# 10. OPEN quantity mismatch.
expect(
    LocalSnapshot(LocalState.OPEN, 1002, 25),
    BrokerSnapshot(True, 1002, 17),
    RecoveryAction.LOCKED,
)
print("10. OPEN QUANTITY MISMATCH: PASS")


# 11. EXITING, but exit completed during crash.
expect(
    LocalSnapshot(LocalState.EXITING, 1002, 25),
    BrokerSnapshot(True),
    RecoveryAction.READY,
)
print("11. EXIT COMPLETED DURING CRASH: PASS")


# 12. EXITING, none filled yet.
expect(
    LocalSnapshot(LocalState.EXITING, 1002, 25),
    BrokerSnapshot(True, 1002, 25),
    RecoveryAction.RESUME_EXITING,
)
print("12. EXIT STILL REQUIRED: PASS")


# 13. EXITING, partially filled.
expect(
    LocalSnapshot(LocalState.EXITING, 1002, 25),
    BrokerSnapshot(True, 1002, 11),
    RecoveryAction.RESUME_EXITING,
)
print("13. PARTIAL EXIT RECOVERY: PASS")


# 14. Impossible: broker owns MORE than local position.
expect(
    LocalSnapshot(LocalState.EXITING, 1002, 25),
    BrokerSnapshot(True, 1002, 26),
    RecoveryAction.LOCKED,
)
print("14. IMPOSSIBLE EXIT QUANTITY: PASS")


# 15. Wrong contract during exit.
expect(
    LocalSnapshot(LocalState.EXITING, 1002, 25),
    BrokerSnapshot(True, 2002, 10),
    RecoveryAction.LOCKED,
)
print("15. EXIT CONTRACT MISMATCH: PASS")


# 16. Short position must never be silently accepted.
expect(
    LocalSnapshot(LocalState.OPEN, 1002, 25),
    BrokerSnapshot(True, 1002, -25),
    RecoveryAction.LOCKED,
)
print("16. UNEXPECTED SHORT POSITION: PASS")


print()
print("========================================")
print("ALL STARTUP RECOVERY TESTS PASS")
print("UNSAFE / CONTRADICTORY STATES LOCKED")
print("ZERO IBKR ORDERS")
print("========================================")
print()
