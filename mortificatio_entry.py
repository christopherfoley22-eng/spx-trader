from dataclasses import dataclass
from typing import List


MAX_CONTRACTS = 25
DEFAULT_ENTRY_CHUNK = 10


class EntryLifecycleError(RuntimeError):
    pass


@dataclass
class EntryLifecycle:
    planned_qty: int
    con_id: int
    filled_qty: int = 0
    finished: bool = False

    def __post_init__(self):
        if not isinstance(self.planned_qty, int):
            raise EntryLifecycleError("Planned quantity must be whole")

        if self.planned_qty < 1 or self.planned_qty > MAX_CONTRACTS:
            raise EntryLifecycleError("Invalid planned quantity")

        if not isinstance(self.con_id, int) or self.con_id <= 0:
            raise EntryLifecycleError("Invalid conId")

    @property
    def remaining_qty(self) -> int:
        return self.planned_qty - self.filled_qty

    def next_chunk(self, max_chunk: int = DEFAULT_ENTRY_CHUNK) -> int:
        if self.finished:
            return 0

        if max_chunk < 1:
            raise EntryLifecycleError("Invalid max chunk")

        return min(self.remaining_qty, max_chunk)

    def record_fill(self, con_id: int, quantity: int):
        if self.finished:
            raise EntryLifecycleError(
                "Cannot record fill after entry lifecycle finished"
            )

        if con_id != self.con_id:
            raise EntryLifecycleError("Fill belongs to wrong contract")

        if not isinstance(quantity, int) or quantity < 1:
            raise EntryLifecycleError("Invalid fill quantity")

        if self.filled_qty + quantity > self.planned_qty:
            raise EntryLifecycleError(
                "Fill would exceed planned position"
            )

        self.filled_qty += quantity

        if self.filled_qty == self.planned_qty:
            self.finished = True

    def stop_entry(self):
        """
        Stop trying to acquire additional contracts.

        This does NOT mean the trade disappeared.
        Any already-filled contracts remain a real position that
        MORTIFICATIO must manage.
        """
        self.finished = True

    @property
    def has_position(self) -> bool:
        return self.filled_qty > 0


def chunk_plan(quantity: int, max_chunk: int = DEFAULT_ENTRY_CHUNK) -> List[int]:
    if not isinstance(quantity, int):
        raise EntryLifecycleError("Quantity must be whole")

    if quantity < 1 or quantity > MAX_CONTRACTS:
        raise EntryLifecycleError("Invalid quantity")

    if max_chunk < 1:
        raise EntryLifecycleError("Invalid max chunk")

    chunks = []

    remaining = quantity

    while remaining:
        chunk = min(remaining, max_chunk)
        chunks.append(chunk)
        remaining -= chunk

    return chunks
