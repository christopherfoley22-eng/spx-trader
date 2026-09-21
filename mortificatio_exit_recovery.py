"""
MORTIFICATIO partial-exit reconciliation.

Once EXITING begins, the decision is irreversible.

Broker truth determines how many contracts remain.
This module plans what remains to be exited but submits ZERO orders.
"""

from dataclasses import dataclass
from typing import List

from executor_state import (
    ExecutorState,
    BrokerSnapshot,
    SafetyError,
    EXITING,
    FLAT,
    MAX_CONTRACTS,
)


class ExitRecoveryError(SafetyError):
    pass


@dataclass(frozen=True)
class ExitRecoveryPlan:
    con_id: int
    remaining_qty: int
    chunks: List[int]


def exit_chunks(quantity: int, max_chunk: int = 10) -> List[int]:
    if not isinstance(quantity, int):
        raise ExitRecoveryError("Exit quantity must be whole")

    if quantity < 0 or quantity > MAX_CONTRACTS:
        raise ExitRecoveryError("Exit quantity outside safety bounds")

    if not isinstance(max_chunk, int) or max_chunk < 1:
        raise ExitRecoveryError("Invalid exit chunk size")

    chunks = []
    remaining = quantity

    while remaining:
        chunk = min(remaining, max_chunk)
        chunks.append(chunk)
        remaining -= chunk

    return chunks


def reconcile_exit(
    state: ExecutorState,
    broker: BrokerSnapshot,
    max_chunk: int = 10,
):
    """
    Reconcile an existing EXITING lifecycle.

    Possible outcomes:

    1. Broker still owns contracts and has NO unresolved orders:
       return a plan for the remaining quantity.

    2. Broker still owns contracts but has working orders:
       fail closed. Do not create another exit plan yet.

    3. Broker owns zero contracts and has zero working orders:
       confirm FLAT persistently.

    This function NEVER submits an order.
    """

    state._validate_snapshot(broker)

    current = state.status()

    if current.state != EXITING:
        raise ExitRecoveryError(
            "Exit reconciliation requires EXITING state"
        )

    if current.con_id is None:
        raise ExitRecoveryError(
            "EXITING state missing contract identity"
        )

    # --------------------------------------------------------
    # BROKER FLAT
    # --------------------------------------------------------

    if broker.position_qty == 0:
        if broker.con_id is not None:
            raise ExitRecoveryError(
                "Zero broker quantity contains unexpected conId"
            )

        if broker.open_order_count != 0:
            raise ExitRecoveryError(
                "Broker position is zero but orders remain unresolved"
            )

        state.confirm_flat(broker)

        final = state.status()

        if final.state != FLAT:
            raise ExitRecoveryError(
                "Persistent state failed to become FLAT"
            )

        return None

    # --------------------------------------------------------
    # BROKER STILL OWNS CONTRACTS
    # --------------------------------------------------------

    if broker.con_id != current.con_id:
        raise ExitRecoveryError(
            "Broker remaining position is wrong contract"
        )

    if broker.position_qty > current.quantity:
        raise ExitRecoveryError(
            "Broker quantity exceeds original EXITING quantity"
        )

    if broker.open_order_count != 0:
        raise ExitRecoveryError(
            "Existing broker order must resolve before another exit"
        )

    return ExitRecoveryPlan(
        con_id=current.con_id,
        remaining_qty=broker.position_qty,
        chunks=exit_chunks(
            broker.position_qty,
            max_chunk=max_chunk,
        ),
    )
