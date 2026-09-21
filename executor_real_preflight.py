from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import time
import math


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 98

ACCOUNT_REQ_ID = 9801
SPX_REQ_ID = 9802

TIMEOUT = 10.0

ACCOUNT_TAGS = ",".join([
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
])


class Preflight(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.positions_end = threading.Event()
        self.orders_end = threading.Event()
        self.account_end = threading.Event()

        self.managed_accounts = []

        self.nonzero_positions = []
        self.open_orders = []

        self.account_values = {}

        self.spx_market_data_type = None
        self.spx_last = None
        self.spx_last_time = None

        self.errors = []

    # --------------------------------------------------------
    # CONNECTION
    # --------------------------------------------------------

    def nextValidId(self, orderId):
        self.connected_event.set()

    def managedAccounts(self, accountsList):
        self.managed_accounts = [
            x.strip()
            for x in accountsList.split(",")
            if x.strip()
        ]

    # --------------------------------------------------------
    # POSITIONS
    # --------------------------------------------------------

    def position(
        self,
        account,
        contract,
        position,
        avgCost,
    ):
        try:
            qty = float(position)
        except Exception:
            return

        if qty != 0:
            self.nonzero_positions.append({
                "symbol": contract.symbol,
                "secType": contract.secType,
                "conId": contract.conId,
                "qty": qty,
            })

    def positionEnd(self):
        self.positions_end.set()

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
            "orderId": orderId,
            "symbol": contract.symbol,
            "secType": contract.secType,
            "conId": contract.conId,
            "action": order.action,
            "qty": str(order.totalQuantity),
            "status": orderState.status,
        })

    def openOrderEnd(self):
        self.orders_end.set()

    # --------------------------------------------------------
    # ACCOUNT SUMMARY
    # --------------------------------------------------------

    def accountSummary(
        self,
        reqId,
        account,
        tag,
        value,
        currency,
    ):
        if reqId != ACCOUNT_REQ_ID:
            return

        self.account_values[(tag, currency)] = value

    def accountSummaryEnd(self, reqId):
        if reqId == ACCOUNT_REQ_ID:
            self.account_end.set()

    # --------------------------------------------------------
    # MARKET DATA
    # --------------------------------------------------------

    def marketDataType(self, reqId, marketDataType):
        if reqId == SPX_REQ_ID:
            self.spx_market_data_type = marketDataType

    def tickPrice(
        self,
        reqId,
        tickType,
        price,
        attrib,
    ):
        if reqId != SPX_REQ_ID:
            return

        # IBKR tick type 4 = LAST.
        if tickType == 4 and price is not None and price > 0:
            self.spx_last = float(price)
            self.spx_last_time = time.monotonic()

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


def number(value):
    try:
        x = float(value)

        if not math.isfinite(x):
            return None

        return x

    except Exception:
        return None


def account_value(app, tag):
    # Prefer USD.
    value = app.account_values.get((tag, "USD"))

    if value is not None:
        return number(value)

    # Some fields can arrive with blank/base currency.
    matches = [
        value
        for (stored_tag, currency), value
        in app.account_values.items()
        if stored_tag == tag
    ]

    if len(matches) == 1:
        return number(matches[0])

    return None


def yes_no(value):
    return "YES" if value else "NO"


app = Preflight()

print()
print("========================================")
print("EXECUTOR REAL-WORLD READ-ONLY PREFLIGHT")
print("========================================")
print()

# ============================================================
# 1. CONNECT
# ============================================================

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

if not app.connected_event.wait(TIMEOUT):
    print("TWS CONNECTION: FAILED")
    print("EXECUTOR READINESS: LOCKED")
    print("REASON: TWS_CONNECTION_FAILED")
    print("ZERO ORDERS SUBMITTED")
    app.disconnect()
    raise SystemExit(1)

print("TWS CONNECTION: CONFIRMED")


# ============================================================
# 2. ACCOUNT VISIBILITY
# ============================================================

deadline = time.time() + 3.0

while (
    not app.managed_accounts
    and time.time() < deadline
):
    time.sleep(0.05)

account_count = len(app.managed_accounts)

print(
    "MANAGED ACCOUNT COUNT:",
    account_count,
)

account_ok = account_count == 1

if account_count == 1:
    print("ACCOUNT SELECTION: UNAMBIGUOUS")
elif account_count == 0:
    print("ACCOUNT SELECTION: LOCKED - NO ACCOUNT")
else:
    print(
        "ACCOUNT SELECTION: LOCKED - "
        "MULTIPLE ACCOUNTS REQUIRE EXPLICIT SELECTION"
    )


# ============================================================
# 3. POSITIONS
# ============================================================

app.reqPositions()

positions_complete = app.positions_end.wait(TIMEOUT)

try:
    app.cancelPositions()
except Exception:
    pass

print()
print("----------------------------------------")
print("BROKER POSITION SNAPSHOT")
print("----------------------------------------")

print(
    "POSITION SNAPSHOT COMPLETE:",
    yes_no(positions_complete),
)

print(
    "NONZERO POSITIONS:",
    len(app.nonzero_positions),
)

broker_flat = (
    positions_complete
    and len(app.nonzero_positions) == 0
)

print(
    "BROKER FLAT:",
    yes_no(broker_flat),
)


# ============================================================
# 4. API-VISIBLE OPEN ORDERS
# ============================================================

app.reqAllOpenOrders()

orders_complete = app.orders_end.wait(TIMEOUT)

print()
print("----------------------------------------")
print("BROKER ORDER SNAPSHOT")
print("----------------------------------------")

print(
    "ORDER SNAPSHOT COMPLETE:",
    yes_no(orders_complete),
)

print(
    "API-VISIBLE OPEN ORDERS:",
    len(app.open_orders),
)

orders_clear = (
    orders_complete
    and len(app.open_orders) == 0
)

print(
    "OPEN-ORDER GATE CLEAR:",
    yes_no(orders_clear),
)


# ============================================================
# 5. ACCOUNT FUNDS
# ============================================================

app.reqAccountSummary(
    ACCOUNT_REQ_ID,
    "All",
    ACCOUNT_TAGS,
)

account_complete = app.account_end.wait(TIMEOUT)

try:
    app.cancelAccountSummary(ACCOUNT_REQ_ID)
except Exception:
    pass

available = account_value(
    app,
    "AvailableFunds",
)

buying_power = account_value(
    app,
    "BuyingPower",
)

excess = account_value(
    app,
    "ExcessLiquidity",
)

cash = account_value(
    app,
    "TotalCashValue",
)

settled = account_value(
    app,
    "SettledCash",
)

print()
print("----------------------------------------")
print("ACCOUNT FUNDS SNAPSHOT")
print("----------------------------------------")

print(
    "ACCOUNT SUMMARY COMPLETE:",
    yes_no(account_complete),
)


def show_money(name, value):
    if value is None:
        print(
            name + ":",
            "NOT RECEIVED / INVALID",
        )
    else:
        print(
            name + ":",
            "${:,.2f}".format(value),
        )


show_money("AvailableFunds", available)
show_money("BuyingPower", buying_power)
show_money("ExcessLiquidity", excess)
show_money("TotalCashValue", cash)
show_money("SettledCash", settled)

fund_values = [
    available,
    buying_power,
    excess,
    cash,
    settled,
]

funds_valid = (
    account_complete
    and all(
        x is not None and x >= 0
        for x in fund_values
    )
)

if funds_valid:
    conservative_funds = min(
        available,
        excess,
        cash,
        settled,
    )

    print(
        "PROVISIONAL CONSERVATIVE FUNDS:",
        "${:,.2f}".format(conservative_funds),
    )
else:
    conservative_funds = None

print(
    "ACCOUNT-FUNDS DATA VALID:",
    yes_no(funds_valid),
)

print(
    "FINAL PRODUCTION SIZING RULE:",
    "NOT YET LOCKED",
)


# ============================================================
# 6. REAL SPX MARKET DATA
# ============================================================

spx = Contract()
spx.symbol = "SPX"
spx.secType = "IND"
spx.exchange = "CBOE"
spx.currency = "USD"

# Explicitly request LIVE market data.
app.reqMarketDataType(1)

app.reqMktData(
    SPX_REQ_ID,
    spx,
    "",
    False,
    False,
    [],
)

time.sleep(5.0)

try:
    app.cancelMktData(SPX_REQ_ID)
except Exception:
    pass

print()
print("----------------------------------------")
print("SPX MARKET-DATA SNAPSHOT")
print("----------------------------------------")

market_type_names = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}

market_name = market_type_names.get(
    app.spx_market_data_type,
    "UNKNOWN",
)

print(
    "SPX FEED CLASSIFICATION:",
    market_name,
)

if app.spx_last is None:
    print("SPX LAST PRICE: NOT RECEIVED")
    print("SPX CALLBACK AGE: UNKNOWN")
    spx_fresh = False

else:
    age = (
        time.monotonic()
        - app.spx_last_time
    )

    print(
        "SPX LAST PRICE:",
        "{:.2f}".format(app.spx_last),
    )

    print(
        "SPX CALLBACK AGE:",
        "{:.3f} sec".format(age),
    )

    spx_fresh = age <= 1.0

spx_live_ready = (
    app.spx_market_data_type == 1
    and app.spx_last is not None
    and spx_fresh
)

print(
    "SPX LIVE/FRESH GATE:",
    "PASS" if spx_live_ready else "LOCKED",
)


# ============================================================
# 7. ERROR REVIEW
# ============================================================

fatal_errors = []

for req_id, code, message in app.errors:
    fatal_errors.append(
        (req_id, code, message)
    )

print()
print("----------------------------------------")
print("IBKR ERROR REVIEW")
print("----------------------------------------")

if not fatal_errors:
    print("NON-INFORMATIONAL ERRORS: 0")
else:
    print(
        "NON-INFORMATIONAL ERRORS:",
        len(fatal_errors),
    )

    for req_id, code, message in fatal_errors:
        print(
            "code={} reqId={} message={}".format(
                code,
                req_id,
                message,
            )
        )


# ============================================================
# 8. CONSOLIDATED READINESS
# ============================================================

print()
print("----------------------------------------")
print("CONSOLIDATED EXECUTOR READINESS")
print("----------------------------------------")

checks = [
    (
        "ACCOUNT_SELECTION",
        account_ok,
    ),
    (
        "POSITION_SNAPSHOT",
        positions_complete,
    ),
    (
        "BROKER_FLAT",
        broker_flat,
    ),
    (
        "ORDER_SNAPSHOT",
        orders_complete,
    ),
    (
        "NO_API_VISIBLE_OPEN_ORDERS",
        orders_clear,
    ),
    (
        "ACCOUNT_FUNDS_DATA",
        funds_valid,
    ),
    (
        "SPX_LIVE_FRESH_DATA",
        spx_live_ready,
    ),
]

for name, passed in checks:
    print(
        "{}: {}".format(
            name,
            "PASS" if passed else "LOCKED",
        )
    )

all_ready = all(
    passed
    for _, passed in checks
)

print()

if all_ready:
    print("EXECUTOR PREFLIGHT: BASE GATES PASS")
    print(
        "NOTE: EXACT OPTION DATA + AFFORDABILITY "
        "STILL REQUIRED BEFORE ENTRY"
    )
else:
    print("EXECUTOR PREFLIGHT: LOCKED")

    failed = [
        name
        for name, passed in checks
        if not passed
    ]

    print(
        "FAILED GATES:",
        ", ".join(failed),
    )

print()
print("ORDER CAPABILITY: DISABLED")
print("READ-ONLY DEVELOPMENT MODE")
print("ZERO ORDERS SUBMITTED")

print()
print("========================================")
print("REAL-WORLD PREFLIGHT COMPLETE")
print("========================================")
print()

app.disconnect()
