import math
import threading
import time

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 96

REQ_ID = 9601
WAIT_SECONDS = 8.0

# IBKR marketDataType values:
# 1 = Live
# 2 = Frozen
# 3 = Delayed
# 4 = Delayed-Frozen

MARKET_DATA_TYPES = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}


def valid_price(value):
    return (
        isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


class MarketDataProbe(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.data_event = threading.Event()

        self.market_data_type = None

        self.last = None
        self.last_timestamp = None

        self.close = None
        self.close_timestamp = None

        self.errors = []

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(self, orderId):
        self.connected_event.set()

    # --------------------------------------------------------
    # MARKET DATA TYPE
    # --------------------------------------------------------

    def marketDataType(self, reqId, marketDataType):
        if reqId != REQ_ID:
            return

        self.market_data_type = marketDataType

        print(
            "IBKR MARKET DATA TYPE:",
            MARKET_DATA_TYPES.get(
                marketDataType,
                f"UNKNOWN({marketDataType})",
            ),
        )

    # --------------------------------------------------------
    # PRICE CALLBACKS
    # --------------------------------------------------------

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):
        if reqId != REQ_ID:
            return

        now = time.monotonic()

        # IBKR tick types:
        # 4  = LAST
        # 9  = CLOSE
        # 68 = DELAYED LAST
        # 75 = DELAYED CLOSE

        if tickType in (4, 68):
            if valid_price(price):
                self.last = float(price)
                self.last_timestamp = now
                self.data_event.set()

        elif tickType in (9, 75):
            if valid_price(price):
                self.close = float(price)
                self.close_timestamp = now

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
        # Connection/farm status messages are informational.
        informational = {
            2104,
            2106,
            2107,
            2108,
            2158,
        }

        if errorCode not in informational:
            self.errors.append(
                (reqId, errorCode, errorString)
            )


def spx_contract():
    contract = Contract()

    contract.symbol = "SPX"
    contract.secType = "IND"
    contract.exchange = "CBOE"
    contract.currency = "USD"

    return contract


def main():
    print()
    print("========================================")
    print("EXECUTOR REAL MARKET-DATA GATE")
    print("========================================")
    print()
    print("ORDER CAPABILITY: DISABLED")
    print("ZERO ORDER METHODS USED")
    print()

    app = MarketDataProbe()

    try:
        app.connect(
            HOST,
            PORT,
            clientId=CLIENT_ID,
        )

        api_thread = threading.Thread(
            target=app.run,
            daemon=True,
        )
        api_thread.start()

        if not app.connected_event.wait(5.0):
            print("TWS CONNECTION: FAILED")
            print()
            print("MARKET DATA GATE: LOCKED")
            print("CALLS: DISABLED")
            print("PUTS: DISABLED")
            print("REASON: API CONNECTION NOT CONFIRMED")
            return

        print("TWS CONNECTION: CONFIRMED")

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # We explicitly request LIVE data.
        #
        # If IBKR cannot provide live SPX data because the
        # account lacks the subscription/permissions, that
        # must NOT silently become permission to trade.
        # ----------------------------------------------------

        app.reqMarketDataType(1)

        request_started = time.monotonic()

        app.reqMktData(
            REQ_ID,
            spx_contract(),
            "",
            False,
            False,
            [],
        )

        app.data_event.wait(WAIT_SECONDS)

        # Small grace period for marketDataType/error callbacks.
        time.sleep(0.5)

        now = time.monotonic()

        if app.last_timestamp is not None:
            age = now - app.last_timestamp
        else:
            age = None

        print()

        md_name = MARKET_DATA_TYPES.get(
            app.market_data_type,
            (
                "UNKNOWN"
                if app.market_data_type is None
                else f"UNKNOWN({app.market_data_type})"
            ),
        )

        print("SPX FEED CLASSIFICATION:", md_name)

        if app.last is None:
            print("SPX LAST PRICE: NOT RECEIVED")
        else:
            print(
                "SPX LAST PRICE RECEIVED:",
                f"{app.last:.2f}",
            )

        if age is None:
            print("SPX CALLBACK AGE: UNKNOWN")
        else:
            print(
                "SPX CALLBACK AGE:",
                f"{age:.3f} seconds",
            )

        if app.errors:
            print()
            print("IBKR NON-INFORMATIONAL MESSAGES:")

            for _, code, message in app.errors:
                print(
                    f"  {code}: {message}"
                )

        print()

        # ----------------------------------------------------
        # FAIL-CLOSED ENTRY GATE
        # ----------------------------------------------------

        allowed = True
        reason = "LIVE_FRESH_SPX_CONFIRMED"

        if app.market_data_type != 1:
            allowed = False
            reason = "SPX_FEED_NOT_CONFIRMED_LIVE"

        elif app.last is None:
            allowed = False
            reason = "SPX_LIVE_PRICE_NOT_RECEIVED"

        elif age is None:
            allowed = False
            reason = "SPX_TIMESTAMP_UNKNOWN"

        elif age > 1.0:
            allowed = False
            reason = "SPX_DATA_STALE"

        # Subscription/permission/API errors also fail closed.
        if app.errors:
            allowed = False
            reason = "IBKR_MARKET_DATA_ERROR"

        if allowed:
            print("SPX MARKET DATA GATE: PASS")
            print()
            print(
                "NOTE: SPX ONLY PASSED. "
                "OPTION DATA HAS NOT BEEN VERIFIED."
            )
            print("CALLS: DISABLED")
            print("PUTS: DISABLED")
            print(
                "REASON: EXACT OPTION LIVE-DATA "
                "GATE STILL REQUIRED"
            )

        else:
            print("SPX MARKET DATA GATE: LOCKED")
            print()
            print("CALLS: DISABLED")
            print("PUTS: DISABLED")
            print("REASON:", reason)

    finally:
        try:
            app.cancelMktData(REQ_ID)
        except Exception:
            pass

        try:
            app.disconnect()
        except Exception:
            pass

        print()
        print("ZERO ORDERS SUBMITTED")
        print("========================================")
        print()


if __name__ == "__main__":
    main()
