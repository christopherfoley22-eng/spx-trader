from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time
from datetime import datetime


SPX_CONID = 416904


class ChainTest(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.chains = []

    def nextValidId(self, orderId):
        print("✅ Connected to IBKR")
        self.ready.set()

    def securityDefinitionOptionParameter(
        self,
        reqId,
        exchange,
        underlyingConId,
        tradingClass,
        multiplier,
        expirations,
        strikes
    ):
        if underlyingConId != SPX_CONID:
            return

        self.chains.append({
            "exchange": exchange,
            "tradingClass": tradingClass,
            "multiplier": multiplier,
            "expirations": set(expirations),
            "strikes": set(strikes)
        })

    def securityDefinitionOptionParameterEnd(self, reqId):
        print(f"\nReceived {len(self.chains)} option-chain definitions.")

        today = datetime.now().strftime("%Y%m%d")
        print(f"Looking for expiration: {today}")

        matches = []

        for chain in self.chains:
            if (
                chain["tradingClass"] == "SPXW"
                and today in chain["expirations"]
            ):
                matches.append(chain)

        if not matches:
            print("❌ No SPXW 0DTE chain found for today.")
        else:
            print(f"✅ Found {len(matches)} SPXW 0DTE chain(s).")

            for i, chain in enumerate(matches, 1):
                strikes = sorted(chain["strikes"])

                print(f"\n--- SPXW 0DTE CHAIN {i} ---")
                print(f"Exchange:      {chain['exchange']}")
                print(f"Trading class: {chain['tradingClass']}")
                print(f"Multiplier:    {chain['multiplier']}")
                print(f"Expiration:    {today}")
                print(f"Strike count:  {len(strikes)}")

                if strikes:
                    print(f"Lowest strike: {strikes[0]}")
                    print(f"Highest strike:{strikes[-1]}")

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


app = ChainTest()

app.connect("127.0.0.1", 7496, clientId=94)
threading.Thread(target=app.run, daemon=True).start()

if not app.ready.wait(10):
    print("❌ Could not connect to TWS.")
    app.disconnect()
    raise SystemExit(1)

print("Requesting SPX option-chain definitions...")

app.reqSecDefOptParams(
    3001,
    "SPX",
    "",
    "IND",
    SPX_CONID
)

if not app.finished.wait(30):
    print("❌ Option-chain request timed out.")

time.sleep(1)
app.disconnect()

print("\n✅ Disconnected. No orders submitted.")
