from dataclasses import dataclass
from enum import Enum


class State(Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    RECOVERY = "RECOVERY"
    LOCKED = "LOCKED"


class RecoveryAction(Enum):
    READY = "READY"
    RESUME_OPEN = "RESUME_OPEN"
    RESUME_EXITING = "RESUME_EXITING"
    RECOVER_ENTRY_FILL = "RECOVER_ENTRY_FILL"
    CONFIRM_FLAT = "CONFIRM_FLAT"
    LOCKED = "LOCKED"


@dataclass
class LocalPosition:
    con_id: int
    target_qty: int
    known_qty: int


@dataclass
class BrokerSnapshot:
    complete: bool
    con_id: object
    qty: int
    working_exit_qty: int = 0
    working_entry_qty: int = 0


class RecoveryEngine:
    def __init__(self):
        self.state = State.FLAT
        self.position = None
        self.pre_failure_state = None
        self.failure_reason = None
        self.exit_intent = False

    def set_entering(self, con_id, target_qty, known_qty=0):
        self.state = State.ENTERING
        self.position = LocalPosition(
            con_id=con_id,
            target_qty=target_qty,
            known_qty=known_qty,
        )

    def set_open(self, con_id, qty):
        self.state = State.OPEN
        self.position = LocalPosition(
            con_id=con_id,
            target_qty=qty,
            known_qty=qty,
        )

    def set_exiting(self, con_id, qty):
        self.state = State.EXITING
        self.position = LocalPosition(
            con_id=con_id,
            target_qty=qty,
            known_qty=qty,
        )
        self.exit_intent = True

    def connectivity_failure(self, reason):
        if self.state == State.LOCKED:
            return False, "ALREADY_LOCKED"

        if self.state == State.RECOVERY:
            return False, "RECOVERY_ALREADY_ACTIVE"

        self.pre_failure_state = self.state
        self.failure_reason = reason

        # Never erase ownership merely because connectivity
        # or trustworthy data disappeared.
        self.state = State.RECOVERY

        return True, "RECOVERY_REQUIRED"

    def can_accept_new_entry(self):
        return self.state == State.FLAT

    def reconcile(self, broker):
        if self.state != State.RECOVERY:
            return RecoveryAction.LOCKED, "NOT_IN_RECOVERY"

        if not broker.complete:
            self.state = State.LOCKED
            return (
                RecoveryAction.LOCKED,
                "INCOMPLETE_BROKER_SNAPSHOT",
            )

        if broker.qty < 0:
            self.state = State.LOCKED
            return (
                RecoveryAction.LOCKED,
                "UNEXPECTED_SHORT_POSITION",
            )

        if broker.working_exit_qty < 0:
            self.state = State.LOCKED
            return (
                RecoveryAction.LOCKED,
                "INVALID_WORKING_EXIT_QTY",
            )

        if broker.working_entry_qty < 0:
            self.state = State.LOCKED
            return (
                RecoveryAction.LOCKED,
                "INVALID_WORKING_ENTRY_QTY",
            )

        # ----------------------------------------------------
        # FAILURE OCCURRED WHILE LOCALLY FLAT
        # ----------------------------------------------------

        if self.pre_failure_state == State.FLAT:
            if (
                broker.qty == 0
                and broker.working_exit_qty == 0
                and broker.working_entry_qty == 0
            ):
                self.state = State.FLAT
                self.position = None
                return RecoveryAction.READY, "BROKER_CONFIRMS_FLAT"

            self.state = State.LOCKED
            return (
                RecoveryAction.LOCKED,
                "BROKER_NOT_FLAT_AFTER_FLAT_FAILURE",
            )

        if self.position is None:
            self.state = State.LOCKED
            return RecoveryAction.LOCKED, "MISSING_LOCAL_POSITION"

        # If broker reports a position, it must be the exact
        # contract Executor believes it was handling.
        if broker.qty > 0 and broker.con_id != self.position.con_id:
            self.state = State.LOCKED
            return RecoveryAction.LOCKED, "CONTRACT_MISMATCH"

        # ----------------------------------------------------
        # FAILURE DURING ENTRY
        # ----------------------------------------------------

        if self.pre_failure_state == State.ENTERING:
            if broker.working_exit_qty > 0:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "UNEXPECTED_EXIT_ORDER_DURING_ENTRY",
                )

            if broker.qty > self.position.target_qty:
                self.state = State.LOCKED
                return RecoveryAction.LOCKED, "ENTRY_OVERFILL"

            if (
                broker.qty == 0
                and broker.working_entry_qty == 0
            ):
                # No position and no working entry remain.
                # Do NOT silently retry the old entry.
                self.state = State.FLAT
                self.position = None
                return (
                    RecoveryAction.CONFIRM_FLAT,
                    "NO_FILL_CONFIRMED_NO_AUTORETRY",
                )

            if broker.qty > 0:
                self.position.known_qty = broker.qty
                self.state = State.OPEN

                return (
                    RecoveryAction.RECOVER_ENTRY_FILL,
                    "BROKER_POSITION_RECOVERED",
                )

            # No fill yet, but broker still has a working entry.
            self.state = State.RECOVERY
            return (
                RecoveryAction.LOCKED,
                "WORKING_ENTRY_REQUIRES_RESOLUTION",
            )

        # ----------------------------------------------------
        # FAILURE WHILE OPEN
        # ----------------------------------------------------

        if self.pre_failure_state == State.OPEN:
            if broker.working_entry_qty > 0:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "UNEXPECTED_ENTRY_ORDER_WHILE_OPEN",
                )

            if broker.qty == 0:
                # Local system believed it owned a position.
                # Broker now says flat. We do not invent why.
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "LOCAL_OPEN_BUT_BROKER_FLAT",
                )

            if broker.qty != self.position.known_qty:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "OPEN_QUANTITY_MISMATCH",
                )

            if broker.working_exit_qty > broker.qty:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "EXIT_ORDER_EXCEEDS_POSITION",
                )

            self.state = State.OPEN
            self.position.known_qty = broker.qty

            return (
                RecoveryAction.RESUME_OPEN,
                "POSITION_RECONCILED",
            )

        # ----------------------------------------------------
        # FAILURE DURING EXIT
        # ----------------------------------------------------

        if self.pre_failure_state == State.EXITING:
            if broker.working_entry_qty > 0:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "ENTRY_ORDER_DURING_EXIT",
                )

            if broker.qty == 0:
                if broker.working_exit_qty != 0:
                    self.state = State.LOCKED
                    return (
                        RecoveryAction.LOCKED,
                        "EXIT_ORDER_EXISTS_WHILE_FLAT",
                    )

                self.state = State.FLAT
                self.position = None
                self.exit_intent = False

                return (
                    RecoveryAction.CONFIRM_FLAT,
                    "EXIT_COMPLETED_DURING_FAILURE",
                )

            if broker.qty > self.position.known_qty:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "POSITION_GREW_DURING_EXIT",
                )

            if broker.working_exit_qty > broker.qty:
                self.state = State.LOCKED
                return (
                    RecoveryAction.LOCKED,
                    "WORKING_EXIT_EXCEEDS_POSITION",
                )

            # Position still exists. Preserve EXIT intent.
            self.position.known_qty = broker.qty
            self.state = State.EXITING
            self.exit_intent = True

            return (
                RecoveryAction.RESUME_EXITING,
                "EXIT_STILL_REQUIRED",
            )

        self.state = State.LOCKED
        return RecoveryAction.LOCKED, "UNKNOWN_PRE_FAILURE_STATE"


def expect(condition, message):
    assert condition, message


print()
print("========================================")
print("DISCONNECT / RECOVERY ATTACK TESTS")
print("========================================")


# 1. Flat disconnect, broker confirms flat.
e = RecoveryEngine()

ok, reason = e.connectivity_failure("IBKR_DISCONNECTED")
expect(ok, reason)
expect(not e.can_accept_new_entry(), "Entry allowed in recovery")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=None,
        qty=0,
    )
)

expect(action == RecoveryAction.READY, reason)
expect(e.state == State.FLAT, "Must return FLAT")

print("1. FLAT DISCONNECT + BROKER FLAT: PASS")


# 2. Flat locally, hidden broker position appears.
e = RecoveryEngine()
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=5,
    )
)

expect(action == RecoveryAction.LOCKED, reason)
expect(e.state == State.LOCKED, "Must lock")

print("2. HIDDEN POSITION AFTER FLAT FAILURE -> LOCKED: PASS")


# 3. Incomplete reconciliation snapshot.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=False,
        con_id=111,
        qty=25,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("3. INCOMPLETE BROKER SNAPSHOT -> LOCKED: PASS")


# 4. Entry disconnected before any fill.
e = RecoveryEngine()
e.set_entering(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=None,
        qty=0,
        working_entry_qty=0,
    )
)

expect(action == RecoveryAction.CONFIRM_FLAT, reason)
expect(e.state == State.FLAT, "Must be flat")

print("4. ENTRY FAILURE / NO FILL -> FLAT, NO AUTORETRY: PASS")


# 5. Entry fill occurred while disconnected.
e = RecoveryEngine()
e.set_entering(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=7,
        working_entry_qty=0,
    )
)

expect(action == RecoveryAction.RECOVER_ENTRY_FILL, reason)
expect(e.state == State.OPEN, "Must recover OPEN")
expect(e.position.known_qty == 7, "Must own broker qty 7")

print("5. ENTRY FILLED DURING DISCONNECT RECOVERED: PASS")


# 6. Entry overfill impossible state.
e = RecoveryEngine()
e.set_entering(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=26,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("6. ENTRY OVERFILL AFTER RECONNECT -> LOCKED: PASS")


# 7. Wrong contract after reconnect.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=999,
        qty=25,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("7. CONTRACT MISMATCH AFTER RECONNECT -> LOCKED: PASS")


# 8. Open position survives connectivity loss.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

expect(e.position.known_qty == 25, "Local ownership erased")
expect(not e.can_accept_new_entry(), "New entry allowed")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=25,
    )
)

expect(action == RecoveryAction.RESUME_OPEN, reason)
expect(e.state == State.OPEN, "Must resume OPEN")

print("8. OPEN POSITION RECONCILED AFTER DISCONNECT: PASS")


# 9. Market-data death while open uses same recovery principle.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("MARKET_DATA_STALE")

expect(e.state == State.RECOVERY, "Must enter recovery")
expect(e.position.known_qty == 25, "Position erased")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=25,
    )
)

expect(action == RecoveryAction.RESUME_OPEN, reason)

print("9. DATA FAILURE CANNOT ERASE OPEN POSITION: PASS")


# 10. Local OPEN but broker says flat is contradiction.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=None,
        qty=0,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("10. LOCAL OPEN / BROKER FLAT -> LOCKED: PASS")


# 11. Open quantity changed unexpectedly.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=20,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("11. UNEXPLAINED OPEN QUANTITY CHANGE -> LOCKED: PASS")


# 12. Exit was fully completed while connection was lost.
e = RecoveryEngine()
e.set_exiting(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=None,
        qty=0,
        working_exit_qty=0,
    )
)

expect(action == RecoveryAction.CONFIRM_FLAT, reason)
expect(e.state == State.FLAT, "Must confirm flat")

print("12. EXIT COMPLETED DURING DISCONNECT: PASS")


# 13. Exit partially filled while disconnected.
e = RecoveryEngine()
e.set_exiting(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=17,
        working_exit_qty=0,
    )
)

expect(action == RecoveryAction.RESUME_EXITING, reason)
expect(e.state == State.EXITING, "Must resume EXITING")
expect(e.position.known_qty == 17, "Must use broker qty")
expect(e.exit_intent, "Exit intent disappeared")

print("13. PARTIAL EXIT DURING DISCONNECT RECOVERED: PASS")


# 14. Exit still working at broker.
e = RecoveryEngine()
e.set_exiting(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=25,
        working_exit_qty=5,
    )
)

expect(action == RecoveryAction.RESUME_EXITING, reason)
expect(e.exit_intent, "Exit intent lost")

print("14. WORKING EXIT SURVIVES RECONNECT: PASS")


# 15. Working exit larger than actual remaining position.
e = RecoveryEngine()
e.set_exiting(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=3,
        working_exit_qty=5,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("15. EXIT ORDER > POSITION -> LOCKED: PASS")


# 16. Position mysteriously grows during exit.
e = RecoveryEngine()
e.set_exiting(111, 20)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=25,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("16. POSITION GROWTH DURING EXIT -> LOCKED: PASS")


# 17. Entry order appears while EXITING.
e = RecoveryEngine()
e.set_exiting(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=25,
        working_entry_qty=5,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("17. ENTRY ORDER DURING EXIT -> LOCKED: PASS")


# 18. Unexpected short position.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("IBKR_DISCONNECTED")

action, reason = e.reconcile(
    BrokerSnapshot(
        complete=True,
        con_id=111,
        qty=-1,
    )
)

expect(action == RecoveryAction.LOCKED, reason)

print("18. UNEXPECTED SHORT POSITION -> LOCKED: PASS")


# 19. Repeated disconnect callback cannot reset recovery.
e = RecoveryEngine()
e.set_open(111, 25)

ok, reason = e.connectivity_failure("IBKR_DISCONNECTED")
expect(ok, reason)

ok, reason = e.connectivity_failure("IBKR_DISCONNECTED")

expect(not ok, "Second failure reset recovery")
expect(reason == "RECOVERY_ALREADY_ACTIVE", reason)
expect(e.state == State.RECOVERY, "Recovery state changed")

print("19. DUPLICATE DISCONNECT EVENT IS IDEMPOTENT: PASS")


# 20. New entry remains impossible throughout uncertainty.
e = RecoveryEngine()
e.set_open(111, 25)
e.connectivity_failure("MARKET_DATA_LOST")

expect(
    not e.can_accept_new_entry(),
    "Entry enabled while ownership uncertain",
)

print("20. NEW ENTRY BLOCKED THROUGHOUT UNCERTAINTY: PASS")


print()
print("========================================")
print("ALL DISCONNECT / RECOVERY ATTACK TESTS PASS")
print("UNKNOWN BROKER STATE NEVER BECOMES ASSUMED FLAT")
print("POSITIONS SURVIVE CONNECTIVITY FAILURE")
print("EXIT INTENT SURVIVES CONNECTIVITY FAILURE")
print("RECONNECT REQUIRES BROKER RECONCILIATION")
print("CONTRADICTIONS FAIL CLOSED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
