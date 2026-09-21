from dataclasses import dataclass
from enum import Enum

INITIAL_STOP = 3.25
HARVEST_START = 5.00
HARVEST_STEP = 1.00
HARVEST_QTY = 2
POST_TARGET_TRAIL = 1.00


class Direction(Enum):
    CALL = 1
    PUT = -1


@dataclass
class Trade:
    direction: Direction
    entry_spx: float
    contracts: int
    remaining: int = 0
    max_favorable: float = 0.0
    harvest_active: bool = False
    next_harvest_level: float = HARVEST_START
    closed: bool = False

    def __post_init__(self):
        self.remaining = self.contracts

    def favorable_move(self, spx):
        return (spx - self.entry_spx) * self.direction.value

    def sell(self, qty, reason, move):
        if self.closed:
            return

        qty = min(qty, self.remaining)
        if qty <= 0:
            return

        self.remaining -= qty

        print(
            f"SELL {qty} | move={move:+.2f} | "
            f"remaining={self.remaining} | {reason}"
        )

        if self.remaining == 0:
            self.closed = True
            print("POSITION FLAT")

    def exit_all(self, reason, move):
        if self.closed:
            return

        print(
            f"EXIT ALL {self.remaining} | "
            f"move={move:+.2f} | {reason}"
        )

        self.remaining = 0
        self.closed = True
        print("POSITION FLAT")

    def update(self, spx):
        if self.closed:
            return

        move = self.favorable_move(spx)

        if move > self.max_favorable:
            self.max_favorable = move

        # Initial losing-trade protection
        if not self.harvest_active and move <= -INITIAL_STOP:
            self.exit_all("INITIAL -3.25 STOP", move)
            return

        # Once +5 is reached, harvesting mode stays active
        if not self.harvest_active and move >= HARVEST_START:
            self.harvest_active = True

        if self.harvest_active:

            # Sell 2 at +5, +6, +7, +8...
            while (
                not self.closed
                and self.next_harvest_level <= self.max_favorable
            ):
                self.sell(
                    HARVEST_QTY,
                    f"HARVEST +{self.next_harvest_level:.0f}",
                    move,
                )
                self.next_harvest_level += HARVEST_STEP

            if self.closed:
                return

            # After +5, exit everything remaining after
            # a 1.00 SPX-point reversal from the best level reached
            trailing_floor = self.max_favorable - POST_TARGET_TRAIL

            if move <= trailing_floor:
                self.exit_all(
                    f"1.00-POINT TRAIL FROM PEAK "
                    f"+{self.max_favorable:.2f}",
                    move,
                )


def simulate(name, direction, entry, contracts, prices):
    print("\n" + "=" * 60)
    print(name)
    print(
        f"{direction.name} | entry={entry:.2f} | "
        f"contracts={contracts}"
    )
    print("=" * 60)

    trade = Trade(direction, entry, contracts)

    for price in prices:
        if trade.closed:
            break

        print(
            f"SPX {price:.2f} | "
            f"move {trade.favorable_move(price):+.2f}"
        )

        trade.update(price)

    print(
        f"FINAL: remaining={trade.remaining}, "
        f"closed={trade.closed}, "
        f"peak=+{trade.max_favorable:.2f}"
    )

    return trade


if __name__ == "__main__":

    # CALL loses 3.25 SPX points
    t = simulate(
        "TEST 1 - CALL INITIAL STOP",
        Direction.CALL,
        7700.00,
        25,
        [7699.00, 7698.00, 7696.75],
    )
    assert t.closed and t.remaining == 0

    # PUT loses 3.25 SPX points
    t = simulate(
        "TEST 2 - PUT INITIAL STOP",
        Direction.PUT,
        7700.00,
        25,
        [7701.00, 7702.00, 7703.25],
    )
    assert t.closed and t.remaining == 0

    # CALL reaches +5, +6, +7 and +7.50.
    # A reversal to +6.50 exits everything remaining.
    t = simulate(
        "TEST 3 - CALL HARVEST + TRAIL",
        Direction.CALL,
        7700.00,
        25,
        [
            7702.00,
            7704.00,
            7705.00,
            7706.00,
            7707.00,
            7707.50,
            7707.10,
            7706.50,
        ],
    )
    assert t.closed and t.remaining == 0

    # Same behavior mirrored for PUTS
    t = simulate(
        "TEST 4 - PUT HARVEST + TRAIL",
        Direction.PUT,
        7700.00,
        25,
        [
            7698.00,
            7696.00,
            7695.00,
            7694.00,
            7693.00,
            7692.50,
            7693.50,
        ],
    )
    assert t.closed and t.remaining == 0

    # Small position cannot sell more contracts than it owns
    t = simulate(
        "TEST 5 - SMALL POSITION",
        Direction.CALL,
        7700.00,
        3,
        [7705.00, 7706.00],
    )
    assert t.closed and t.remaining == 0

    print("\n================================")
    print("ALL STRATEGY TESTS PASS")
    print("SIMULATION ONLY - ZERO IBKR ORDERS")
    print("================================")
