from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from enum import Enum


MAX_CONTRACTS = 25
MAX_CHUNK = 5
DAILY_LIMIT = 2

INITIAL_STOP = 3.25
PROFIT_PROTECTION_ARM = 4.00
PROFIT_PROTECTION_FLOOR = 1.25
LET_IT_RIDE_ARM = 5.00
LET_IT_RIDE_TRAIL = 3.00


class State(Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    LOCKED = "LOCKED"


class Direction(Enum):
    CALL = 1
    PUT = -1


class Strategy(Enum):
    INITIAL = "INITIAL"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    LET_IT_RIDE = "LET_IT_RIDE"


@dataclass
class Contract:
    symbol: str = "SPX"
    sec_type: str = "OPT"
    trading_class: str = "SPXW"
    expiration: str = "20260921"
    right: str = "C"
    strike: float = 6700.0
    multiplier: str = "100"
    currency: str = "USD"
    con_id: int = 123456789
    bid: float = 9.90
    ask: float = 10.00


class Executor:
    def __init__(self):
        self.state = State.FLAT
        self.direction = None
        self.contract = None

        self.target_qty = 0
        self.owned_qty = 0

        self.entry_spx = None
        self.strategy = Strategy.INITIAL
        self.max_favorable = 0.0

        self.trades_today = 0
        self.exit_reason = None

    def lock(self, reason):
        self.state = State.LOCKED
        self.exit_reason = reason
        return False, reason

    # --------------------------------------------------------
    # PRE-TRADE SAFETY GATES
    # --------------------------------------------------------

    def request_trade(
        self,
        direction,
        contract,
        usable_funds,
        spx_price,
        broker_flat,
        reconciliation_complete,
        spx_live,
        option_live,
        data_fresh,
    ):
        if self.state != State.FLAT:
            return False, "NOT_FLAT"

        if self.trades_today >= DAILY_LIMIT:
            return False, "DAILY_LIMIT"

        if not reconciliation_complete:
            return self.lock("RECONCILIATION_INCOMPLETE")

        if not broker_flat:
            return self.lock("BROKER_NOT_FLAT")

        if not spx_live:
            return False, "SPX_NOT_LIVE"

        if not option_live:
            return False, "OPTION_NOT_LIVE"

        if not data_fresh:
            return False, "STALE_DATA"

        expected_right = (
            "C" if direction == Direction.CALL else "P"
        )

        checks = [
            (contract.symbol == "SPX", "WRONG_SYMBOL"),
            (contract.sec_type == "OPT", "WRONG_SECURITY_TYPE"),
            (
                contract.trading_class == "SPXW",
                "WRONG_TRADING_CLASS",
            ),
            (
                contract.expiration == "20260921",
                "WRONG_EXPIRATION",
            ),
            (
                contract.right == expected_right,
                "WRONG_OPTION_RIGHT",
            ),
            (
                contract.multiplier == "100",
                "WRONG_MULTIPLIER",
            ),
            (
                contract.currency == "USD",
                "WRONG_CURRENCY",
            ),
            (
                isinstance(contract.con_id, int)
                and contract.con_id > 0,
                "INVALID_CONID",
            ),
            (
                contract.bid is not None
                and contract.bid > 0,
                "INVALID_BID",
            ),
            (
                contract.ask is not None
                and contract.ask > 0,
                "INVALID_ASK",
            ),
        ]

        for ok, reason in checks:
            if not ok:
                return False, reason

        if contract.ask < contract.bid:
            return False, "CROSSED_MARKET"

        try:
            funds = Decimal(str(usable_funds))
            ask = Decimal(str(contract.ask))

            if not funds.is_finite() or funds < 0:
                return False, "INVALID_FUNDS"

            if not ask.is_finite() or ask <= 0:
                return False, "INVALID_ASK"

            cost = ask * Decimal("100")

            qty = int(
                (funds / cost).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )

        except Exception:
            return False, "INVALID_SIZING"

        qty = min(qty, MAX_CONTRACTS)

        if qty < 1:
            return False, "CANNOT_AFFORD_ONE"

        # Atomic reservation.
        self.state = State.ENTERING
        self.direction = direction
        self.contract = contract
        self.target_qty = qty
        self.owned_qty = 0
        self.entry_spx = spx_price
        self.strategy = Strategy.INITIAL
        self.max_favorable = 0.0

        return True, "ENTRY_RESERVED"

    # --------------------------------------------------------
    # ENTRY FILL ACCOUNTING
    # --------------------------------------------------------

    def next_entry_chunk(self):
        if self.state != State.ENTERING:
            return 0

        remaining = self.target_qty - self.owned_qty

        if remaining <= 0:
            return 0

        return min(MAX_CHUNK, remaining)

    def entry_fill(self, qty):
        if self.state not in (State.ENTERING, State.OPEN):
            return False, "ENTRY_FILL_WRONG_STATE"

        if qty <= 0:
            return False, "INVALID_FILL"

        if self.owned_qty + qty > self.target_qty:
            return False, "ENTRY_OVERFILL"

        first_fill = self.owned_qty == 0

        self.owned_qty += qty

        if first_fill:
            self.trades_today += 1

        # Once we actually own anything, strategy protection
        # must be active even if more entry chunks remain.
        self.state = State.OPEN

        return True, "ENTRY_FILL_CONFIRMED"

    # --------------------------------------------------------
    # STRATEGY
    # --------------------------------------------------------

    def favorable_move(self, spx):
        return (
            (spx - self.entry_spx)
            * self.direction.value
        )

    def update_spx(self, spx):
        if self.state != State.OPEN:
            return "IGNORED"

        move = self.favorable_move(spx)

        if move > self.max_favorable:
            self.max_favorable = move

        # INITIAL STOP
        if (
            self.strategy == Strategy.INITIAL
            and move <= -INITIAL_STOP
        ):
            self.begin_exit("INITIAL_STOP")
            return "EXIT"

        # +5 has priority.
        if (
            self.strategy != Strategy.LET_IT_RIDE
            and self.max_favorable >= LET_IT_RIDE_ARM
        ):
            self.strategy = Strategy.LET_IT_RIDE

        elif (
            self.strategy == Strategy.INITIAL
            and self.max_favorable >= PROFIT_PROTECTION_ARM
        ):
            self.strategy = Strategy.PROFIT_PROTECTION

        # Fixed profit floor before +5.
        if (
            self.strategy == Strategy.PROFIT_PROTECTION
            and move <= PROFIT_PROTECTION_FLOOR
        ):
            self.begin_exit("PROFIT_PROTECTION_FLOOR")
            return "EXIT"

        # LET IT RIDE
        if self.strategy == Strategy.LET_IT_RIDE:
            giveback = self.max_favorable - move

            if giveback >= LET_IT_RIDE_TRAIL:
                self.begin_exit("LET_IT_RIDE_TRAIL")
                return "EXIT"

        return "HOLD"

    # --------------------------------------------------------
    # EXIT
    # --------------------------------------------------------

    def begin_exit(self, reason):
        if self.state != State.OPEN:
            return False, "EXIT_WRONG_STATE"

        if self.owned_qty <= 0:
            return self.lock("EXIT_WITHOUT_POSITION")

        self.state = State.EXITING
        self.exit_reason = reason

        return True, "EXIT_RESERVED"

    def next_exit_chunk(self):
        if self.state != State.EXITING:
            return 0

        return min(MAX_CHUNK, self.owned_qty)

    def exit_fill(self, qty):
        if self.state != State.EXITING:
            return False, "EXIT_FILL_WRONG_STATE"

        if qty <= 0:
            return False, "INVALID_FILL"

        if qty > self.owned_qty:
            return False, "OVERSELL_BLOCKED"

        self.owned_qty -= qty

        if self.owned_qty == 0:
            self.state = State.FLAT
            self.direction = None
            self.contract = None
            self.target_qty = 0
            self.entry_spx = None
            self.strategy = Strategy.INITIAL
            self.max_favorable = 0.0
            self.exit_reason = None

            return True, "FLAT_CONFIRMED"

        return True, "PARTIAL_EXIT"


def expect(value, message):
    assert value, message


def call_contract():
    return Contract()


def put_contract():
    return Contract(
        right="P",
        con_id=987654321,
    )


print()
print("========================================")
print("EXECUTOR END-TO-END SAFETY TEST")
print("========================================")


# ============================================================
# TRADE 1
#
# CALL -> 25 contracts -> partial fills -> +13.5 peak ->
# exactly 3-point reversal -> EXIT ALL -> partial exit ->
# confirmed flat.
# ============================================================

e = Executor()

ok, reason = e.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700.0,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(ok, reason)
expect(e.state == State.ENTERING, "Must reserve entry")
expect(e.target_qty == 25, "Expected 25 contracts")

print("1. CALL INTENT PASSED ALL ENTRY GATES: PASS")


expect(e.next_entry_chunk() == 5, "Expected chunk 5")

ok, reason = e.entry_fill(3)
expect(ok, reason)
expect(e.owned_qty == 3, "Must own exactly 3")
expect(e.trades_today == 1, "Trade must count once")

print("2. PARTIAL ENTRY FILL -> OWN EXACTLY 3: PASS")


# Fill remaining 22.
for qty in (2, 5, 5, 5, 5):
    ok, reason = e.entry_fill(qty)
    expect(ok, reason)

expect(e.owned_qty == 25, "Must own exactly 25")
expect(e.trades_today == 1, "Partial fills double-counted trade")

print("3. COMPLETE ENTRY -> OWN EXACTLY 25: PASS")


# +4.00 arms the fixed profit floor.
result = e.update_spx(6704.0)

expect(result == "HOLD", result)
expect(
    e.strategy == Strategy.PROFIT_PROTECTION,
    "Profit protection not armed",
)

print("4. +4.00 ARMS FIXED PROFIT PROTECTION: PASS")


# Pull back, but still above +1.25.
result = e.update_spx(6701.26)

expect(result == "HOLD", result)

print("5. FIXED FLOOR +1.26 HOLDS: PASS")


# Recover to +5: Let It Ride.
result = e.update_spx(6705.0)

expect(result == "HOLD", result)
expect(
    e.strategy == Strategy.LET_IT_RIDE,
    "Let It Ride not armed",
)

print("6. +5.0 ACTIVATES LET IT RIDE: PASS")


# Continue to +13.5.
result = e.update_spx(6713.5)

expect(result == "HOLD", result)
expect(
    abs(e.max_favorable - 13.5) < 1e-9,
    "Wrong high-water mark",
)

print("7. +13.5 HIGH-WATER RECORDED: PASS")


# Give back only 2.99.
result = e.update_spx(6710.51)

expect(result == "HOLD", result)
expect(e.state == State.OPEN, "Exited at 2.99")

print("8. 2.99-POINT GIVEBACK HOLDS: PASS")


# Exactly 3.00.
result = e.update_spx(6710.50)

expect(result == "EXIT", result)
expect(e.state == State.EXITING, "Must be EXITING")
expect(
    e.exit_reason == "LET_IT_RIDE_TRAIL",
    "Wrong exit reason",
)

print("9. EXACT 3.00-POINT GIVEBACK TRIGGERS EXIT: PASS")


# Partial exit: asked conceptually for max 5, only 2 fill.
expect(e.next_exit_chunk() == 5, "Exit chunk >5")

ok, reason = e.exit_fill(2)

expect(ok, reason)
expect(e.owned_qty == 23, "Must still own 23")
expect(e.state == State.EXITING, "Must remain EXITING")

print("10. PARTIAL EXIT -> 23 STILL PROTECTED: PASS")


# Exit the remaining 23.
for qty in (3, 5, 5, 5, 5):
    ok, reason = e.exit_fill(qty)
    expect(ok, reason)

expect(e.owned_qty == 0, "Position not zero")
expect(e.state == State.FLAT, "Must be FLAT")
expect(e.trades_today == 1, "Trade count changed")

print("11. BROKER-CONFIRMED ZERO -> FLAT: PASS")


# ============================================================
# FAILURE INJECTION
# ============================================================

bad = Executor()

ok, reason = bad.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=False,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Incomplete reconciliation entered")
expect(bad.state == State.LOCKED, "Must fail LOCKED")

print("12. INCOMPLETE RECONCILIATION -> LOCKED: PASS")


bad = Executor()

ok, reason = bad.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700,
    broker_flat=False,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Broker position ignored")
expect(bad.state == State.LOCKED, "Must lock")

print("13. UNEXPECTED BROKER POSITION -> LOCKED: PASS")


bad = Executor()

ok, reason = bad.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=False,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Non-live SPX entered")
expect(bad.state == State.FLAT, "No entry reservation allowed")

print("14. NON-LIVE SPX -> ENTRY DISABLED: PASS")


bad = Executor()

ok, reason = bad.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=False,
    data_fresh=True,
)

expect(not ok, "Non-live option entered")

print("15. NON-LIVE OPTION -> ENTRY DISABLED: PASS")


bad = Executor()

wrong = call_contract()
wrong.right = "P"

ok, reason = bad.request_trade(
    Direction.CALL,
    wrong,
    usable_funds=25000,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Wrong option right entered")
expect(reason == "WRONG_OPTION_RIGHT", reason)

print("16. WRONG CONTRACT -> ENTRY DISABLED: PASS")


bad = Executor()

ok, reason = bad.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=999,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Unaffordable contract entered")
expect(reason == "CANNOT_AFFORD_ONE", reason)

print("17. UNAFFORDABLE CONTRACT -> ENTRY DISABLED: PASS")


# ============================================================
# TRADE 2 — PUT
# Verify mirrored strategy and daily count.
# ============================================================

ok, reason = e.request_trade(
    Direction.PUT,
    put_contract(),
    usable_funds=7000,
    spx_price=6700.0,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(ok, reason)
expect(e.target_qty == 7, "Expected 7 contracts")

ok, reason = e.entry_fill(7)

expect(ok, reason)
expect(e.trades_today == 2, "Expected trade count 2")

print("18. SECOND TRADE / PUT ENTRY: PASS")


# PUT favorable move = SPX falling.
result = e.update_spx(6695.0)

expect(result == "HOLD", result)
expect(
    e.strategy == Strategy.LET_IT_RIDE,
    "PUT Let It Ride not armed",
)

print("19. PUT +5 MIRRORS CALL LOGIC: PASS")


# Peak favorable +10.
result = e.update_spx(6690.0)

expect(result == "HOLD", result)
expect(
    abs(e.max_favorable - 10.0) < 1e-9,
    "PUT high-water wrong",
)

# Reverse exactly 3 points: 6693 = still +7 favorable.
result = e.update_spx(6693.0)

expect(result == "EXIT", result)
expect(e.state == State.EXITING, "PUT must exit")

print("20. PUT 3-POINT TRAIL TRIGGERS: PASS")


ok, reason = e.exit_fill(7)

expect(ok, reason)
expect(e.state == State.FLAT, "Must be flat")

print("21. SECOND TRADE CONFIRMED FLAT: PASS")


# Third trade must be impossible.
ok, reason = e.request_trade(
    Direction.CALL,
    call_contract(),
    usable_funds=25000,
    spx_price=6700,
    broker_flat=True,
    reconciliation_complete=True,
    spx_live=True,
    option_live=True,
    data_fresh=True,
)

expect(not ok, "Third trade accepted")
expect(reason == "DAILY_LIMIT", reason)

print("22. THIRD DAILY TRADE BLOCKED: PASS")


print()
print("========================================")
print("ALL END-TO-END SAFETY TESTS PASS")
print("INTENT -> GATES -> SIZE -> FILLS -> OPEN")
print("-> LET IT RIDE -> EXIT -> CONFIRMED FLAT")
print("2-TRADE DAILY CAP ENFORCED")
print("FAILURE STATES FAIL CLOSED")
print("SIMULATION ONLY")
print("ZERO IBKR ORDERS")
print("========================================")
print()
