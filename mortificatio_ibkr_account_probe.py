"""
MORTIFICATIO IBKR read-only account probe.

Purpose:
- Connect to TWS.
- Discover managed-account count without printing account IDs.
- Request selected account-summary fields.
- Print values needed to design the future buying-power safety layer.

SAFETY:
- NO placeOrder capability.
- NO orders submitted.
- Does not print the IBKR account number.
- Python 3.9 compatible.
"""

import threading
import time
from decimal import Decimal, InvalidOperation

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 92
REQ_ID = 9201

TIMEOUT_SECONDS = 15.0

REQUESTED_TAGS = [
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AvailableFunds",
    "BuyingPower",
    "FullAvailableFunds",
    "FullInitMarginReq",
    "FullMaintMarginReq",
    "LookAheadAvailableFunds",
    "Currency",
]


class ProbeError(RuntimeError):
    pass


def safe_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class AccountProbe(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.summary_done_event = threading.Event()

        self.managed_accounts = []
        self.values = {}
        self.errors = []
        self.fatal_error = None

    def nextValidId(self, orderId):
        # Receiving nextValidId proves the API session is established.
        # We intentionally do nothing with the order ID.
        self.connected_event.set()

    def managedAccounts(self, accountsList):
        accounts = [
            item.strip()
            for item in accountsList.split(",")
            if item.strip()
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

        # Never print/store the account identifier in output.
        key = (tag, currency or "")
        self.values[key] = value

    def accountSummaryEnd(self, reqId):
        if reqId == REQ_ID:
            self.summary_done_event.set()

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        # Common IBKR farm/status messages are informational.
        informational = {
            2104,
            2106,
            2107,
            2108,
            2158,
        }

        if errorCode in informational:
            return

        message = "{}: {}".format(
            errorCode,
            errorString,
        )

        self.errors.append(message)

        # Connection/API failures that make the snapshot untrustworthy.
        if errorCode in {
            502,
            504,
            1100,
            1300,
        }:
            self.fatal_error = message
            self.connected_event.set()
            self.summary_done_event.set()


def main():
    app = AccountProbe()

    print("MORTIFICATIO IBKR READ-ONLY ACCOUNT PROBE")
    print("=" * 58)
    print("Connecting to TWS on {}:{} ...".format(HOST, PORT))

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

    if not app.connected_event.wait(TIMEOUT_SECONDS):
        app.disconnect()
        raise ProbeError(
            "Timed out waiting for IBKR API connection"
        )

    if app.fatal_error:
        app.disconnect()
        raise ProbeError(app.fatal_error)

    # managedAccounts may arrive immediately around connection time.
    deadline = time.time() + 3.0

    while (
        not app.managed_accounts
        and time.time() < deadline
    ):
        time.sleep(0.05)

    if not app.managed_accounts:
        app.disconnect()
        raise ProbeError(
            "IBKR returned no managed account"
        )

    if len(app.managed_accounts) != 1:
        app.disconnect()
        raise ProbeError(
            "Multiple managed accounts detected. "
            "MORTIFICATIO refuses to select one automatically."
        )

    print("API session: CONNECTED")
    print(
        "Managed accounts discovered: {}".format(
            len(app.managed_accounts)
        )
    )
    print("Account identifier: REDACTED")

    tags = ",".join(REQUESTED_TAGS)

    app.reqAccountSummary(
        REQ_ID,
        "All",
        tags,
    )

    if not app.summary_done_event.wait(TIMEOUT_SECONDS):
        try:
            app.cancelAccountSummary(REQ_ID)
        except Exception:
            pass

        app.disconnect()

        raise ProbeError(
            "Account summary did not complete before timeout"
        )

    try:
        app.cancelAccountSummary(REQ_ID)
    except Exception:
        pass

    print()
    print("ACCOUNT SUMMARY")
    print("-" * 58)

    for tag in REQUESTED_TAGS:
        matches = [
            (currency, value)
            for (stored_tag, currency), value
            in app.values.items()
            if stored_tag == tag
        ]

        if not matches:
            print("{:<28} MISSING".format(tag))
            continue

        for currency, value in sorted(matches):
            suffix = (
                " {}".format(currency)
                if currency
                else ""
            )

            print(
                "{:<28} {}{}".format(
                    tag,
                    value,
                    suffix,
                )
            )

    # Basic structural checks only.
    # We are intentionally NOT deciding which field is safe
    # for position sizing yet.
    numeric_candidates = {
        "NetLiquidation",
        "TotalCashValue",
        "SettledCash",
        "AvailableFunds",
        "BuyingPower",
        "FullAvailableFunds",
        "FullInitMarginReq",
        "FullMaintMarginReq",
        "LookAheadAvailableFunds",
    }

    numeric_seen = 0

    for (tag, _currency), value in app.values.items():
        if tag in numeric_candidates:
            parsed = safe_decimal(value)
            if parsed is not None:
                numeric_seen += 1

    print()
    print("STRUCTURAL SAFETY CHECK")
    print("-" * 58)
    print("Summary completed: YES")
    print(
        "Numeric account fields received: {}".format(
            numeric_seen
        )
    )

    if numeric_seen == 0:
        app.disconnect()
        raise ProbeError(
            "No usable numeric account fields were returned"
        )

    if app.errors:
        print()
        print("NON-INFORMATIONAL IBKR MESSAGES")
        print("-" * 58)

        for message in app.errors:
            print(message)

    app.disconnect()

    print()
    print("NO ORDER SUBMISSION CAPABILITY")
    print("ZERO IBKR ORDERS")
    print("=" * 58)
    print("ACCOUNT PROBE COMPLETE")


if __name__ == "__main__":
    main()
