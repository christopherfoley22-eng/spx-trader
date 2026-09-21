from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum
import threading
import time


class SPXLiveTest(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.received_price = threading.Event()

    def nextValidId(self, orderId):
        print("✅ Connected to IBKR")
        self.ready.set()

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId != 1001 or price <= 0:
            return

        tick_name = TickTypeEnum.toStr(tickType)

        if tick_name in ("LAST", "DELAYED_LAST"):
            print(f"SPX {tick_name}: {price}")
            self.received_price.set()

        elif tick_name in ("BID", "ASK", "CLOSE"):
            print(f"SPX {tick_name}: {price}")

    def marketDataType(self, reqId, marketDataType):
        names = {
            1: "LIVE",
            2: "FROZEN",
            3: "DELAYED",
            4: "DELAYED-FROZEN"
        }
        print(
            f"Market data type: "
            f"{names.get(marketDataType, marketDataType)}"
        )

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        # Suppress normal farm-status messages so output stays readable.
        if errorCode not in (2104, 2106, 2158):
            print(f"IBKR {errorCode}: {errorString}")


app = SPXLiveTest()

print("Connecting to live TWS...")
app.connect("127.0.0.1", 7496, clientId=92)

threading.Thread(target=app.run, daemon=True).start()

if not app.ready.wait(10):
    print("❌ Could not establish API connection.")
    app.disconnect()
    raise SystemExit(1)

spx = Contract()
spx.symbol = "SPX"
spx.secType = "IND"
spx.exchange = "CBOE"
spx.currency = "USD"

# Explicitly request LIVE market data.
app.reqMarketDataType(3)

print("Requesting live SPX...")
app.reqMktData(
    1001,
    spx,
    "",
    False,
    False,
    []
)

if app.received_price.wait(15):
    print("✅ Live SPX price received.")
else:
    print("❌ No SPX last price received within 15 seconds.")
    print("Check the IBKR messages above for market-data permissions.")

app.cancelMktData(1001)
time.sleep(1)
app.disconnect()

print("✅ Disconnected. No orders submitted.")
