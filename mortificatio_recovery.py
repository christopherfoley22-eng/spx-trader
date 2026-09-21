"""
MORTIFICATIO entry crash-recovery layer.

Purpose:
If MORTIFICATIO reserved an entry and contracts reached IBKR before
local state finalized the entry, broker truth can recover ownership.

NO IBKR order submission exists in this module.
"""

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    ENTERING,
    OPEN,
    MAX_CONTRACTS,
)


class RecoveryError(SafetyError):
    pass


def recover_entering_position(
    state: ExecutorState,
    broker: BrokerSnapshot,
):
    """
    Recover an ENTERING lifecycle after a crash/restart.

    Safe recovery is allowed only when:

    - local state is ENTERING
    - broker snapshot is complete
    - broker owns 1..25 contracts
    - broker contract exactly matches reserved conId
    - no unresolved broker orders remain

    The existing confirm_entry_fill() method remains the authority
    that transitions ENTERING -> OPEN and increments the daily trade
    count exactly once.

    This function submits ZERO orders.
    """

    state._validate_snapshot(broker)

    current = state.status()

    if current.state != ENTERING:
        raise RecoveryError(
            "Recovery requires persistent ENTERING state"
        )

    if current.con_id is None:
        raise RecoveryError(
            "ENTERING state has no reserved conId"
        )

    if broker.open_order_count != 0:
        raise RecoveryError(
            "Cannot finalize recovery while broker orders remain"
        )

    if broker.position_qty == 0:
        raise RecoveryError(
            "Broker has no position to recover"
        )

    if broker.position_qty < 1 or broker.position_qty > MAX_CONTRACTS:
        raise RecoveryError(
            "Recovered broker quantity outside safety bounds"
        )

    if broker.con_id != current.con_id:
        raise RecoveryError(
            "Broker position does not match reserved contract"
        )

    # Reuse the already-tested persistent transition.
    state.confirm_entry_fill(
        filled_qty=broker.position_qty,
        con_id=broker.con_id,
        broker=broker,
    )

    recovered = state.status()

    if recovered.state != OPEN:
        raise RecoveryError(
            "Recovery failed to reach OPEN"
        )

    if recovered.quantity != broker.position_qty:
        raise RecoveryError(
            "Recovered quantity differs from broker truth"
        )

    if recovered.con_id != broker.con_id:
        raise RecoveryError(
            "Recovered contract differs from broker truth"
        )

    return recovered
