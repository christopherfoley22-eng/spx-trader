import threading
import time

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 94


class ReconcileTest(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.positions_done = threading.Event()
        self.orders_done = threading.Event()

        self.positions = []
        self.open_orders = []

    def nextValidId(self, orderId):
        print("✅ Connected to IBKR")
        self.connected_event.set()

    def position(self, account, contract, position, avgCost):
        # We intentionally do NOT print the account number.
        if position != 0:
            self.positions.append({
                "symbol": contract.symbol,
                "secType": contract.secType,
                "expiry": getattr(contract, "lastTradeDateOrContractMonth", ""),
                "strike": getattr(contract, "strike", 0),
                "right": getattr(contract, "right", ""),
                "position": position,
                "avgCost": avgCost,
                "conId": contract.conId,
            })

    def positionEnd(self):
        self.positions_done.set()

    def openOrder(self, orderId, contract, order, orderState):
        self.open_orders.append({
            "orderId": orderId,
            "symbol": contract.symbol,
            "secType": contract.secType,
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", ""),
            "strike": getattr(contract, "strike", 0),
            "right": getattr(contract, "right", ""),
            "action": order.action,
            "quantity": order.totalQuantity,
            "type": order.orderType,
            "status": orderState.status,
        })

    def openOrderEnd(self):
        self.orders_done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # IBKR informational farm messages are normal.
        informational = {2104, 2106, 2107, 2108, 2158}

        if errorCode in informational:
            print(f"IBKR INFO {errorCode}: {errorString}")
        else:
            print(f"IBKR ERROR {errorCode}: {errorString}")


app = ReconcileTest()

print("Connecting to IBKR in READ-ONLY development mode...")
app.connect(HOST, PORT, clientId=CLIENT_ID)

thread = threading.Thread(target=app.run, daemon=True)
thread.start()

if not app.connected_event.wait(10):
    print("❌ Could not establish IBKR API connection.")
    app.disconnect()
    raise SystemExit(1)


# ---------------------------------------------------------
# Ask IBKR what positions ACTUALLY exist.
# ---------------------------------------------------------
print("\nRequesting actual IBKR positions...")
app.reqPositions()

if not app.positions_done.wait(10):
    print("❌ Position request timed out.")
    app.cancelPositions()
    app.disconnect()
    raise SystemExit(1)

app.cancelPositions()


# ---------------------------------------------------------
# Ask IBKR what API-visible open orders ACTUALLY exist.
# ---------------------------------------------------------
print("Requesting API-visible open orders...")
app.reqAllOpenOrders()

if not app.orders_done.wait(10):
    print("❌ Open-order request timed out.")
    app.disconnect()
    raise SystemExit(1)


print("\n========================================")
print("IBKR RECONCILIATION SNAPSHOT")
print("========================================")

print(f"\nNon-zero positions found: {len(app.positions)}")

for p in app.positions:
    print(
        f"POSITION | {p['symbol']} {p['secType']} | "
        f"expiry={p['expiry']} | "
        f"strike={p['strike']} | "
        f"right={p['right']} | "
        f"qty={p['position']} | "
        f"conId={p['conId']}"
    )

print(f"\nAPI-visible open orders found: {len(app.open_orders)}")

for o in app.open_orders:
    print(
        f"ORDER | id={o['orderId']} | "
        f"{o['action']} {o['quantity']} | "
        f"{o['symbol']} {o['secType']} | "
        f"expiry={o['expiry']} | "
        f"strike={o['strike']} | "
        f"right={o['right']} | "
        f"type={o['type']} | "
        f"status={o['status']}"
    )


print("\n========================================")
print("RECONCILIATION REQUEST COMPLETE")
print("NO ORDERS WERE SUBMITTED")
print("========================================")

app.disconnect()
thread.join(timeout=2)
