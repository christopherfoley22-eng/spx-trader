import threading
import time

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 95

SNAPSHOT_TIMEOUT = 10.0


class IBKRSnapshot(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.positions = []
        self.open_orders = []

        self.positions_done = threading.Event()
        self.orders_done = threading.Event()

        self.connected_ok = False
        self.errors = []

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(self, orderId):
        self.connected_ok = True

    # --------------------------------------------------------
    # POSITIONS
    # --------------------------------------------------------

    def position(self, account, contract, position, avgCost):
        qty = float(position)

        if qty == 0:
            return

        self.positions.append({
            "account": account,
            "con_id": contract.conId,
            "symbol": contract.symbol,
            "sec_type": contract.secType,
            "currency": contract.currency,
            "quantity": qty,
        })

    def positionEnd(self):
        self.positions_done.set()

    # --------------------------------------------------------
    # OPEN ORDERS
    # --------------------------------------------------------

    def openOrder(
        self,
        orderId,
        contract,
        order,
        orderState,
    ):
        self.open_orders.append({
            "order_id": orderId,
            "perm_id": order.permId,
            "con_id": contract.conId,
            "symbol": contract.symbol,
            "sec_type": contract.secType,
            "action": order.action,
            "quantity": float(order.totalQuantity),
            "status": orderState.status,
        })

    def openOrderEnd(self):
        self.orders_done.set()

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        # Common IBKR informational connection/farm messages.
        informational_codes = {
            2104, 2106, 2107, 2108, 2158
        }

        if errorCode not in informational_codes:
            self.errors.append(
                (reqId, errorCode, errorString)
            )


def main():
    print()
    print("========================================")
    print("EXECUTOR / IBKR STARTUP RECONCILIATION")
    print("========================================")
    print()
    print("ORDER CAPABILITY: DISABLED")
    print("READ-ONLY SNAPSHOT ONLY")
    print()

    app = IBKRSnapshot()

    try:
        app.connect(
            HOST,
            PORT,
            clientId=CLIENT_ID,
        )

        thread = threading.Thread(
            target=app.run,
            daemon=True,
        )
        thread.start()

        # Give IBKR time to complete API handshake.
        deadline = time.time() + SNAPSHOT_TIMEOUT

        while (
            not app.connected_ok
            and time.time() < deadline
        ):
            time.sleep(0.05)

        if not app.connected_ok:
            print("RECOVERY DECISION: LOCKED")
            print("REASON: IBKR CONNECTION NOT CONFIRMED")
            return

        # ----------------------------------------------------
        # REQUEST BROKER TRUTH
        # ----------------------------------------------------

        app.reqPositions()

        # API-visible open orders.
        app.reqAllOpenOrders()

        positions_ok = app.positions_done.wait(
            SNAPSHOT_TIMEOUT
        )

        orders_ok = app.orders_done.wait(
            SNAPSHOT_TIMEOUT
        )

        try:
            app.cancelPositions()
        except Exception:
            pass

        snapshot_complete = (
            positions_ok
            and orders_ok
        )

        print(
            "IBKR SNAPSHOT COMPLETE:",
            "YES" if snapshot_complete else "NO"
        )

        print(
            "NONZERO POSITIONS:",
            len(app.positions)
        )

        print(
            "API-VISIBLE OPEN ORDERS:",
            len(app.open_orders)
        )

        print()

        # ----------------------------------------------------
        # FAIL CLOSED
        # ----------------------------------------------------

        if not snapshot_complete:
            print("RECOVERY DECISION: LOCKED")
            print(
                "REASON: BROKER SNAPSHOT INCOMPLETE"
            )
            return

        if app.errors:
            print("RECOVERY DECISION: LOCKED")
            print("REASON: IBKR API ERROR")
            print()

            for _, code, message in app.errors:
                print(
                    f"ERROR {code}: {message}"
                )

            return

        # ----------------------------------------------------
        # CURRENT TEST EXPECTATION
        #
        # We are NOT yet automatically resuming a position.
        # Any broker position/order locks Executor.
        # ----------------------------------------------------

        if app.positions:
            print("RECOVERY DECISION: LOCKED")
            print(
                "REASON: BROKER POSITION EXISTS"
            )

            # Deliberately do not print account numbers.
            for p in app.positions:
                print(
                    "POSITION:",
                    p["symbol"],
                    p["sec_type"],
                    "conId=" + str(p["con_id"]),
                    "qty=" + str(p["quantity"]),
                )

            return

        if app.open_orders:
            print("RECOVERY DECISION: LOCKED")
            print(
                "REASON: BROKER OPEN ORDER EXISTS"
            )

            for o in app.open_orders:
                print(
                    "ORDER:",
                    o["symbol"],
                    o["sec_type"],
                    "conId=" + str(o["con_id"]),
                    "qty=" + str(o["quantity"]),
                    "status=" + str(o["status"]),
                )

            return

        # ----------------------------------------------------
        # Broker snapshot alone cannot prove local persisted state.
        # ----------------------------------------------------

        print("BROKER SNAPSHOT: FLAT")
        print()
        print("RECOVERY DECISION: LOCKED")
        print(
            "REASON: PERSISTED EXECUTOR STATE "
            "HAS NOT BEEN RECONCILED"
        )

    finally:
        try:
            app.disconnect()
        except Exception:
            pass

        print()
        print("ORDER CAPABILITY: DISABLED")
        print("ZERO ORDERS SUBMITTED")
        print("========================================")
        print()


if __name__ == "__main__":
    main()
