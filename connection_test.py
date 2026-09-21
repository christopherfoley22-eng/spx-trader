from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time


class IBConnectionTest(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected_event = threading.Event()

    def nextValidId(self, orderId):
        print(f"\n✅ IBKR API CONNECTED")
        print(f"Next valid order ID: {orderId}")
        print("No orders will be submitted.")
        self.connected_event.set()

    def managedAccounts(self, accountsList):
        # Don't print the actual account number.
        accounts = [a for a in accountsList.split(",") if a]
        print(f"Accessible IBKR accounts: {len(accounts)}")

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        print(f"IBKR message {errorCode}: {errorString}")


app = IBConnectionTest()

print("Connecting to TWS at 127.0.0.1:7496...")
app.connect("127.0.0.1", 7496, clientId=91)

thread = threading.Thread(target=app.run, daemon=True)
thread.start()

if app.connected_event.wait(timeout=10):
    time.sleep(2)
    app.disconnect()
    print("✅ Test complete — disconnected safely.")
else:
    app.disconnect()
    print("❌ Connection was not confirmed within 10 seconds.")

