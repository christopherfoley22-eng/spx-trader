from dataclasses import dataclass, field
from enum import Enum


MAX_CONTRACTS = 25
MAX_CHUNK = 5


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class FillLedger:
    target_qty: int
    owned_qty: int = 0
    seen_exec_ids: set = field(default_factory=set)

    def __post_init__(self):
        if (
            not isinstance(self.target_qty, int)
            or isinstance(self.target_qty, bool)
            or self.target_qty < 1
            or self.target_qty > MAX_CONTRACTS
        ):
            raise ValueError("INVALID_TARGET_QTY")

        if (
            not isinstance(self.owned_qty, int)
            or isinstance(self.owned_qty, bool)
            or self.owned_qty < 0
            or self.owned_qty > self.target_qty
        ):
            raise ValueError("INVALID_OWNED_QTY")

    def apply_fill(self, exec_id, side, qty):
        # Every execution must have a unique broker execution ID.
        if not isinstance(exec_id, str) or not exec_id.strip():
            return False, "INVALID_EXEC_ID"

        # Duplicate callback: ignore it completely.
        if exec_id in self.seen_exec_ids:
            return False, "DUPLICATE_EXECUTION_IGNORED"

        if (
            not isinstance(qty, int)
            or isinstance(qty, bool)
            or qty <= 0
        ):
            return False, "INVALID_FILL_QTY"

        if side == Side.BUY:
            # Never let fills push us beyond authorized target.
            if self.owned_qty + qty > self.target_qty:
                return False, "BUY_FILL_EXCEEDS_TARGET"

            self.owned_qty += qty

        elif side == Side.SELL:
            # Never sell more than broker-confirmed ownership.
            if qty > self.owned_qty:
                return False, "SELL_FILL_EXCEEDS_OWNED"

            self.owned_qty -= qty

        else:
            return False, "INVALID_SIDE"

        # Only record the execution after validation succeeds.
        self.seen_exec_ids.add(exec_id)

        return True, "FILL_APPLIED"

    def remaining_entry_qty(self):
        return self.target_qty - self.owned_qty

    def next_entry_chunk(self):
        remaining = self.remaining_entry_qty()

        if remaining <= 0:
            return 0

        return min(MAX_CHUNK, remaining)

    def next_exit_chunk(self):
        if self.owned_qty <= 0:
            return 0

        return min(MAX_CHUNK, self.owned_qty)


def expect(condition, message):
    assert condition, message


print()
print("========================================")
print("PARTIAL-FILL / CHUNK SAFETY TESTS")
print("========================================")


# ------------------------------------------------------------
# 1. Initial 25-contract request -> first chunk is only 5.
# ------------------------------------------------------------

ledger = FillLedger(target_qty=25)

expect(ledger.next_entry_chunk() == 5, "Expected chunk 5")
print("1. 25-CONTRACT ENTRY CHUNK LIMITED TO 5: PASS")


# ------------------------------------------------------------
# 2. Only 3 of first 5 fill.
# Executor must own exactly 3, NOT assume 5.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXEC-001",
    Side.BUY,
    3,
)

expect(ok, reason)
expect(ledger.owned_qty == 3, "Owned qty must be 3")
expect(
    ledger.remaining_entry_qty() == 22,
    "Remaining target must be 22",
)

print("2. PARTIAL ENTRY FILL TRACKED EXACTLY: PASS")


# ------------------------------------------------------------
# 3. Same execution callback arrives again.
# Must NOT become 6 contracts.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXEC-001",
    Side.BUY,
    3,
)

expect(not ok, "Duplicate execution must be ignored")
expect(
    reason == "DUPLICATE_EXECUTION_IGNORED",
    reason,
)
expect(ledger.owned_qty == 3, "Duplicate changed ownership")

print("3. DUPLICATE ENTRY FILL IGNORED: PASS")


# ------------------------------------------------------------
# 4. Remaining 2 from original 5 arrive separately.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXEC-002",
    Side.BUY,
    2,
)

expect(ok, reason)
expect(ledger.owned_qty == 5, "Owned qty must be 5")
expect(ledger.remaining_entry_qty() == 20, "Expected 20 remaining")

print("4. SECOND PARTIAL FILL ACCOUNTED: PASS")


# ------------------------------------------------------------
# 5. Next chunk remains 5.
# ------------------------------------------------------------

expect(ledger.next_entry_chunk() == 5, "Expected next chunk 5")
print("5. NEXT ENTRY CHUNK CALCULATED FROM FILLS: PASS")


# ------------------------------------------------------------
# 6. Fill remaining entry in valid pieces.
# ------------------------------------------------------------

for i in range(3, 7):
    ok, reason = ledger.apply_fill(
        f"EXEC-00{i}",
        Side.BUY,
        5,
    )
    expect(ok, reason)

expect(ledger.owned_qty == 25, "Must own exactly 25")
expect(ledger.remaining_entry_qty() == 0, "Entry must be complete")
expect(ledger.next_entry_chunk() == 0, "No more entry allowed")

print("6. FULL 25-CONTRACT ENTRY ACCOUNTED: PASS")


# ------------------------------------------------------------
# 7. Any extra BUY fill beyond target is rejected.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXEC-007",
    Side.BUY,
    1,
)

expect(not ok, "Overfill must be rejected")
expect(reason == "BUY_FILL_EXCEEDS_TARGET", reason)
expect(ledger.owned_qty == 25, "Ownership changed on rejected fill")

print("7. ENTRY OVERFILL BLOCKED: PASS")


# ------------------------------------------------------------
# 8. Exit chunk is capped at 5.
# ------------------------------------------------------------

expect(ledger.next_exit_chunk() == 5, "Exit chunk must be 5")
print("8. EXIT CHUNK LIMITED TO 5: PASS")


# ------------------------------------------------------------
# 9. Attempt to sell 5, only 2 actually fill.
# Ownership must become 23.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXIT-001",
    Side.SELL,
    2,
)

expect(ok, reason)
expect(ledger.owned_qty == 23, "Must still own 23")

print("9. PARTIAL EXIT FILL TRACKED EXACTLY: PASS")


# ------------------------------------------------------------
# 10. Duplicate SELL execution callback.
# Must NOT subtract another 2.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXIT-001",
    Side.SELL,
    2,
)

expect(not ok, "Duplicate exit must be ignored")
expect(
    reason == "DUPLICATE_EXECUTION_IGNORED",
    reason,
)
expect(ledger.owned_qty == 23, "Duplicate changed ownership")

print("10. DUPLICATE EXIT FILL IGNORED: PASS")


# ------------------------------------------------------------
# 11. Remaining 3 of that attempted 5 fill.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXIT-002",
    Side.SELL,
    3,
)

expect(ok, reason)
expect(ledger.owned_qty == 20, "Must own 20")

print("11. REMAINING PARTIAL EXIT ACCOUNTED: PASS")


# ------------------------------------------------------------
# 12. Never sell more than confirmed ownership.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXIT-BAD",
    Side.SELL,
    21,
)

expect(not ok, "Oversell must be rejected")
expect(reason == "SELL_FILL_EXCEEDS_OWNED", reason)
expect(ledger.owned_qty == 20, "Rejected oversell changed ownership")

print("12. OVERSELL BLOCKED: PASS")


# ------------------------------------------------------------
# 13. Exit remaining position in chunks.
# ------------------------------------------------------------

for i in range(3, 7):
    expect(ledger.next_exit_chunk() == 5, "Expected 5 exit chunk")

    ok, reason = ledger.apply_fill(
        f"EXIT-00{i}",
        Side.SELL,
        5,
    )

    expect(ok, reason)

expect(ledger.owned_qty == 0, "Position must be flat")
expect(ledger.next_exit_chunk() == 0, "No exit after flat")

print("13. COMPLETE EXIT REACHES EXACTLY ZERO: PASS")


# ------------------------------------------------------------
# 14. Selling again after flat must be impossible.
# ------------------------------------------------------------

ok, reason = ledger.apply_fill(
    "EXIT-007",
    Side.SELL,
    1,
)

expect(not ok, "Sell while flat must fail")
expect(reason == "SELL_FILL_EXCEEDS_OWNED", reason)
expect(ledger.owned_qty == 0, "Flat state changed")

print("14. SELL-WHILE-FLAT BLOCKED: PASS")


# ------------------------------------------------------------
# 15. Zero fill quantity rejected.
# ------------------------------------------------------------

fresh = FillLedger(target_qty=10)

ok, reason = fresh.apply_fill(
    "BAD-ZERO",
    Side.BUY,
    0,
)

expect(not ok, "Zero fill must fail")
expect(reason == "INVALID_FILL_QTY", reason)
expect(fresh.owned_qty == 0, "Invalid fill changed ownership")

print("15. ZERO FILL QUANTITY BLOCKED: PASS")


# ------------------------------------------------------------
# 16. Negative fill quantity rejected.
# ------------------------------------------------------------

ok, reason = fresh.apply_fill(
    "BAD-NEG",
    Side.BUY,
    -1,
)

expect(not ok, "Negative fill must fail")
expect(reason == "INVALID_FILL_QTY", reason)

print("16. NEGATIVE FILL QUANTITY BLOCKED: PASS")


# ------------------------------------------------------------
# 17. Missing execution ID rejected.
# ------------------------------------------------------------

ok, reason = fresh.apply_fill(
    "",
    Side.BUY,
    1,
)

expect(not ok, "Missing execution ID must fail")
expect(reason == "INVALID_EXEC_ID", reason)

print("17. MISSING EXECUTION ID BLOCKED: PASS")


# ------------------------------------------------------------
# 18. Failed execution must NOT poison deduplication.
#
# If an execution callback is malformed first, then later arrives
# correctly with the same ID, the valid one must still be usable.
# ------------------------------------------------------------

ok, reason = fresh.apply_fill(
    "EXEC-RECOVER",
    Side.BUY,
    999,
)

expect(not ok, "Invalid overfill should fail")
expect(reason == "BUY_FILL_EXCEEDS_TARGET", reason)

ok, reason = fresh.apply_fill(
    "EXEC-RECOVER",
    Side.BUY,
    2,
)

expect(ok, "Corrected callback should be accepted")
expect(fresh.owned_qty == 2, "Expected ownership 2")

print("18. REJECTED CALLBACK DOES NOT POISON EXEC ID: PASS")


# ------------------------------------------------------------
# 19. Constructor rejects target above 25.
# ------------------------------------------------------------

try:
    FillLedger(target_qty=26)
    raise AssertionError("26-contract target was accepted")
except ValueError as e:
    expect(str(e) == "INVALID_TARGET_QTY", str(e))

print("19. TARGET ABOVE 25 BLOCKED: PASS")


# ------------------------------------------------------------
# 20. Constructor rejects impossible local ownership.
# ------------------------------------------------------------

try:
    FillLedger(
        target_qty=10,
        owned_qty=11,
    )
    raise AssertionError("Impossible ownership was accepted")
except ValueError as e:
    expect(str(e) == "INVALID_OWNED_QTY", str(e))

print("20. IMPOSSIBLE OWNERSHIP BLOCKED: PASS")


print()
print("========================================")
print("ALL PARTIAL-FILL ATTACK TESTS PASS")
print("DUPLICATE EXECUTIONS ARE IDEMPOTENT")
print("OVERFILLS / OVERSELLS BLOCKED")
print("5-CONTRACT CHUNK CEILING ENFORCED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
