from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import math
import threading
import time


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 97
REQ_ID = 9701

TAGS = ",".join([
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
    "FullAvailableFunds",
    "FullExcessLiquidity",
    "EquityWithLoanValue",
    "InitMarginReq",
    "MaintMarginReq",
    "LookAheadAvailableFunds",
    "LookAheadExcessLiquidity",
])


class AccountProbe(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.finished_event = threading.Event()

        self.values = {}
        self.errors = []
        self.managed_accounts = []

    def nextValidId(self, orderId):
        self.connected_event.set()

    def managedAccounts(self, accountsList):
        accounts = [
            x.strip()
            for x in accountsList.split(",")
            if x.strip()
        ]
        self.managed_accounts = accounts

    def accountSummary(
        self,
        reqId,
        account,
        tag,
        value,
        currency,
    ):
        if reqId != REQ_ID:
            return

        # Do NOT print/store the account number in output.
        self.values[(tag, currency)] = value

    def accountSummaryEnd(self, reqId):
        if reqId == REQ_ID:
            self.finished_event.set()

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        # Common farm/connectivity informational messages
        # should not be treated as fatal probe failures.
        informational = {
            2104, 2106, 2107, 2108, 2158
        }

        if errorCode not in informational:
            self.errors.append(
                (reqId, errorCode, errorString)
            )


def safe_number(value):
    try:
        number = float(value)

        if not math.isfinite(number):
            return None

        return number

    except (TypeError, ValueError):
        return None


def masked_money(value):
    number = safe_number(value)

    if number is None:
        return "INVALID / NOT NUMERIC"

    # We intentionally print actual balances because we need
    # to inspect which fields IBKR supplies. No account number
    # is printed.
    return f"${number:,.2f}"


app = AccountProbe()

print()
print("========================================")
print("IBKR READ-ONLY ACCOUNT SUMMARY PROBE")
print("========================================")
print()

app.connect(HOST, PORT, clientId=CLIENT_ID)

thread = threading.Thread(
    target=app.run,
    daemon=True,
)
thread.start()

if not app.connected_event.wait(10):
    print("TWS CONNECTION: FAILED")
    print("ACCOUNT DATA GATE: LOCKED")
    print("ZERO ORDERS SUBMITTED")
    app.disconnect()
    raise SystemExit(1)

print("TWS CONNECTION: CONFIRMED")

# Give managedAccounts callback a brief chance to arrive.
deadline = time.time() + 3

while (
    not app.managed_accounts
    and time.time() < deadline
):
    time.sleep(0.05)

if len(app.managed_accounts) == 0:
    print("MANAGED ACCOUNT COUNT: 0")
    print("ACCOUNT SELECTION: LOCKED")
    print("REASON: NO API-VISIBLE ACCOUNT")
    print("ZERO ORDERS SUBMITTED")
    app.disconnect()
    raise SystemExit(1)

if len(app.managed_accounts) > 1:
    print(
        f"MANAGED ACCOUNT COUNT: "
        f"{len(app.managed_accounts)}"
    )
    print("ACCOUNT SELECTION: LOCKED")
    print("REASON: MULTIPLE ACCOUNTS REQUIRE EXPLICIT SELECTION")
    print("ZERO ORDERS SUBMITTED")
    app.disconnect()
    raise SystemExit(1)

print("MANAGED ACCOUNT COUNT: 1")
print("ACCOUNT SELECTION: UNAMBIGUOUS")

# "All" is valid here because we have already proven that only
# one managed account is visible. We still suppress account ID.
app.reqAccountSummary(
    REQ_ID,
    "All",
    TAGS,
)

if not app.finished_event.wait(10):
    print()
    print("ACCOUNT SUMMARY: INCOMPLETE / TIMEOUT")
    print("ACCOUNT DATA GATE: LOCKED")
    print("ZERO ORDERS SUBMITTED")

    try:
        app.cancelAccountSummary(REQ_ID)
    except Exception:
        pass

    app.disconnect()
    raise SystemExit(1)

try:
    app.cancelAccountSummary(REQ_ID)
except Exception:
    pass


print()
print("----------------------------------------")
print("VALUES REPORTED BY IBKR")
print("----------------------------------------")


display_order = [
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
    "FullAvailableFunds",
    "FullExcessLiquidity",
    "EquityWithLoanValue",
    "InitMarginReq",
    "MaintMarginReq",
    "LookAheadAvailableFunds",
    "LookAheadExcessLiquidity",
]


found_tags = set()

for tag in display_order:
    matches = [
        (currency, value)
        for (stored_tag, currency), value
        in app.values.items()
        if stored_tag == tag
    ]

    if not matches:
        print(f"{tag}: NOT RECEIVED")
        continue

    found_tags.add(tag)

    for currency, value in matches:
        currency_text = currency or "N/A"

        if tag == "AccountType":
            print(
                f"{tag}: {value} "
                f"(currency={currency_text})"
            )
        else:
            print(
                f"{tag}: {masked_money(value)} "
                f"(currency={currency_text})"
            )


print()
print("----------------------------------------")
print("SAFETY CHECK")
print("----------------------------------------")


critical_candidates = [
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
    "TotalCashValue",
    "SettledCash",
]


received_critical = [
    tag
    for tag in critical_candidates
    if tag in found_tags
]


if not received_critical:
    print("ACCOUNT DATA GATE: LOCKED")
    print("REASON: NO USABLE FUNDING FIELDS RECEIVED")

elif app.errors:
    print("ACCOUNT DATA GATE: LOCKED")
    print("REASON: NON-INFORMATIONAL IBKR ERROR")

else:
    print("ACCOUNT SUMMARY SNAPSHOT: COMPLETE")
    print(
        "FUNDING FIELDS RECEIVED: "
        + ", ".join(received_critical)
    )
    print("SIZING FIELD: NOT YET SELECTED")
    print("ORDER CAPABILITY: DISABLED")


if app.errors:
    print()
    print("----------------------------------------")
    print("NON-INFORMATIONAL IBKR ERRORS")
    print("----------------------------------------")

    for req_id, code, message in app.errors:
        print(
            f"reqId={req_id} "
            f"code={code} "
            f"message={message}"
        )


print()
print("========================================")
print("READ-ONLY PROBE COMPLETE")
print("NO ACCOUNT NUMBER PRINTED")
print("ZERO ORDERS SUBMITTED")
print("========================================")
print()

app.disconnect()
