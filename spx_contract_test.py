from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import time


class ContractTest(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.finished = threading.Event()

    def nextValidId(self, orderId):
        print("✅ Connected to IBKR")
        self.ready.set()

    def contractDetails(self, reqId, details):
        c = details.contract

        print("\n--- CONTRACT FOUND ---")
        print(f"Symbol:        {c.symbol}")
        print(f"Security type: {c.secType}")
        print(f"Exchange:      {c.exchange}")
        print(f"Currency:      {c.currency}")
        print(f"ConId:         {c.conId}")
        print(f"Local symbol:  {c.localSymbol}")
        print(f"Trading class: {c.tradingClass}")

    def contractDetailsEnd(self, reqId):
        print("\n✅ Contract search complete.")
        self.finished.set()

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        if errorCode not in (2104, 2106, 2108, 2158):
            print(f"IBKR {errorCode}: {errorString}")


app = ContractTest()

app.connect("127.0.0.1", 7496, clientId=93)
threading.Thread(target=app.run, daemon=True).start()

if not app.ready.wait(10):
    print("❌ Could not connect to TWS.")
    app.disconnect()
    raise SystemExit(1)

spx = Contract()
spx.symbol = "SPX"
spx.secType = "IND"
spx.exchange = "CBOE"
spx.currency = "USD"

print("Searching IBKR's contract database for SPX...")
app.reqContractDetails(2001, spx)

if not app.finished.wait(15):
    print("❌ Contract lookup timed out.")

time.sleep(1)
app.disconnect()

print("✅ Disconnected. No orders submitted.")
