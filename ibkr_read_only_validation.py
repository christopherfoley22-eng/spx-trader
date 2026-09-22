"""Read-only IBKR evidence probe. Prints no account IDs, balances, prices or conIds."""

import math
import os
import json
import re
import secrets
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from simulation_evidence import (
    EvidenceError, VerifiedAccountSnapshot, VerifiedBrokerSnapshot,
    conservative_usable_funds, validate_broker_snapshot,
)

HOST = "127.0.0.1"
PORT = 7496
NY = ZoneInfo("America/New_York")
TIMEOUT = 8.0
TAGS = (
    "AccountType", "AvailableFunds", "BuyingPower", "ExcessLiquidity",
    "TotalCashValue", "SettledCash", "NetLiquidation",
    "FullAvailableFunds", "FullInitMarginReq", "FullMaintMarginReq",
    "LookAheadAvailableFunds",
)
INFO_CODES = {2104, 2106, 2107, 2108, 2158}
CONNECTION_ERROR_CODES = {326, 502, 504, 1100, 1300}
REQUEST_LABELS = {
    8101: "ACCOUNT_SUMMARY",
    8102: "SPX_UNDERLYING_CONTRACT_DETAILS",
    8103: "CURRENT_DAY_SPXW_SECURITY_DEFINITION",
    8104: "SPX_UNDERLYING_MARKET_DATA",
    8105: "EXACT_SPXW_OPTION_CONTRACT_DETAILS",
    8106: "EXACT_SPXW_OPTION_MARKET_DATA",
    8204: "DELAYED_SPX_DIAGNOSTIC_MARKET_DATA",
    8206: "EXACT_SPXW_OPTION_LIVE_DIAGNOSTIC_MARKET_DATA",
}


def _safe_error_text(value):
    """Keep diagnostic wording while removing likely runtime identities/values."""
    if not isinstance(value, str):
        return "[NON_TEXT_ERROR]"
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            def scrub(item):
                if isinstance(item, dict):
                    return {key: "[REDACTED]" if re.search(
                        r"(?i)account|user|token|password|credential|balance|cash|fund|price|amount|margin|equity|conid",
                        str(key)) else scrub(entry)
                        for key, entry in item.items()}
                if isinstance(item, list):
                    return [scrub(entry) for entry in item]
                return item
            value = json.dumps(scrub(parsed), ensure_ascii=True, separators=(",", ":"))
    text = value.replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    text = re.sub(r"(?i)\b(?:DU|U|D|F)\d{5,}\b", "[ACCOUNT_ID]", text)
    text = re.sub(r"\b\d{6,}\b", "[IDENTIFIER]", text)
    text = re.sub(r"(?i)\b(account|username|user|token|password|credential)\s*[:=]\s*[^\s,;{}]+",
                  lambda match: match.group(1) + "=[REDACTED]", text)
    text = re.sub(r"(?i)\b(balance|cash|funds|buying power|price|amount|margin|equity)\s*[:=]\s*[$€£]?[+-]?\d[\d,.]*",
                  lambda match: match.group(1) + "=[REDACTED]", text)
    text = re.sub(r"[$€£]\s*[+-]?\d[\d,.]*", "[FINANCIAL_VALUE]", text)
    return text


class ReadOnlyProbe(EWrapper, EClient):
    def __init__(self, wall_clock=None, monotonic_clock=None):
        EClient.__init__(self, self)
        self._wall_clock = wall_clock or (lambda: datetime.now(ZoneInfo("UTC")))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self.ready = threading.Event()
        self.accounts_done = threading.Event()
        self.summary_done = threading.Event()
        self.positions_done = threading.Event()
        self.orders_done = threading.Event()
        self.contract_done = threading.Event()
        self.chain_done = threading.Event()
        self.option_details_done = threading.Event()
        self.price_seen = threading.Event()
        self.option_quote_seen = threading.Event()
        self.managed_accounts = ()
        self.account_values = {}
        self.positions = []
        self.orders = []
        self.underlyings = []
        self.chains = []
        self.option_details = []
        self.market_type = None
        self.option_market_type = None
        self.spx_last = None
        self.spx_receipt = None
        self.option_bid = None
        self.option_ask = None
        self.error_codes = set()
        self.error_codes_by_request = {}
        self.error_events = []  # In-memory only; never persist raw broker text.
        self.disconnected = False
        self.duplicate_client = False

    # Inherited EClient mutation methods are disabled on this probe.
    def placeOrder(self, *args, **kwargs):
        raise RuntimeError("Order submission disabled")

    def cancelOrder(self, *args, **kwargs):
        raise RuntimeError("Order mutation disabled")

    def reqGlobalCancel(self, *args, **kwargs):
        raise RuntimeError("Order mutation disabled")

    def exerciseOptions(self, *args, **kwargs):
        raise RuntimeError("Order mutation disabled")

    def replaceFA(self, *args, **kwargs):
        raise RuntimeError("Account mutation disabled")

    def reqAutoOpenOrders(self, *args, **kwargs):
        raise RuntimeError("Order binding disabled")

    def setServerLogLevel(self, *args, **kwargs):
        raise RuntimeError("TWS settings mutation disabled")

    def nextValidId(self, orderId):
        self.ready.set()

    def managedAccounts(self, accountsList):
        self.managed_accounts = tuple(x.strip() for x in accountsList.split(",") if x.strip())
        self.accounts_done.set()

    def accountSummary(self, reqId, account, tag, value, currency):
        if reqId == 8101:
            self.account_values[(account, tag, currency or "")] = (value, time.monotonic())

    def accountSummaryEnd(self, reqId):
        if reqId == 8101:
            self.summary_done.set()

    def position(self, account, contract, position, avgCost):
        self.positions.append((account, getattr(contract, "conId", None), position))

    def positionEnd(self):
        self.positions_done.set()

    def openOrder(self, orderId, contract, order, orderState):
        self.orders.append((getattr(order, "account", None), getattr(contract, "conId", None)))

    def openOrderEnd(self):
        self.orders_done.set()

    def contractDetails(self, reqId, details):
        if reqId == 8102:
            self.underlyings.append(details.contract)
        elif reqId == 8105:
            self.option_details.append(details.contract)

    def contractDetailsEnd(self, reqId):
        if reqId == 8102:
            self.contract_done.set()
        elif reqId == 8105:
            self.option_details_done.set()

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        if reqId == 8103:
            self.chains.append((exchange, underlyingConId, tradingClass,
                                multiplier, set(expirations), set(strikes)))

    def securityDefinitionOptionParameterEnd(self, reqId):
        if reqId == 8103:
            self.chain_done.set()

    def marketDataType(self, reqId, marketDataType):
        if reqId == 8104:
            self.market_type = marketDataType
        elif reqId == 8106:
            self.option_market_type = marketDataType

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId == 8104 and tickType == 4 and isinstance(price, (int, float)):
            if math.isfinite(price) and price > 0:
                self.spx_last = price
                self.spx_receipt = time.monotonic()
                self.price_seen.set()
        if reqId == 8106 and tickType in {1, 2} and isinstance(price, (int, float)):
            if math.isfinite(price) and price > 0:
                if tickType == 1:
                    self.option_bid = price
                else:
                    self.option_ask = price
                if self.option_bid is not None and self.option_ask is not None:
                    self.option_quote_seen.set()

    def connectionClosed(self):
        self.disconnected = True
        for event in (self.ready, self.accounts_done, self.summary_done,
                      self.positions_done, self.orders_done,
                      self.contract_done, self.chain_done, self.price_seen,
                      self.option_details_done, self.option_quote_seen):
            event.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        receipt_utc = self._wall_clock().isoformat()
        receipt_monotonic = self._monotonic_clock()
        request_id = reqId if isinstance(reqId, int) and not isinstance(reqId, bool) else None
        if errorCode in INFO_CODES:
            category = "INFORMATIONAL"
        elif errorCode in CONNECTION_ERROR_CODES or request_id is None or request_id < 0:
            category = "CONNECTION_OR_SYSTEM"
        elif request_id in REQUEST_LABELS:
            category = "REQUEST_SPECIFIC"
        else:
            category = "UNKNOWN_REQUEST"
        self.error_events.append({
            "request_id": request_id,
            "request_label": REQUEST_LABELS.get(request_id, "GLOBAL_OR_SYSTEM" if request_id is None or request_id < 0 else "UNKNOWN_REQUEST_ID"),
            "category": category,
            "code": errorCode,
            "message": errorString,  # Complete callback text retained in memory.
            "advanced_text": advancedOrderRejectJson,
            "receipt_utc": receipt_utc,
            "receipt_monotonic": receipt_monotonic,
        })
        if errorCode not in INFO_CODES:
            self.error_codes.add(errorCode)
            self.error_codes_by_request.setdefault(reqId, set()).add(errorCode)
        if errorCode == 326:
            self.duplicate_client = True
        if errorCode in {326, 502, 504, 1100, 1300}:
            self.connectionClosed()

    def safe_error_events(self):
        """Sanitized report copies; receipt clocks are not market source clocks."""
        return [{**event,
                 "message": _safe_error_text(event["message"]),
                 "advanced_text": _safe_error_text(event["advanced_text"])
                 if event["advanced_text"] else ""}
                for event in self.error_events]


def finite_decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def print_status(label, value):
    print("{}={}".format(label, value))


def main():
    app = ReadOnlyProbe()
    client_id = secrets.randbelow(50000) + 10000
    thread = None
    try:
        app.connect(HOST, PORT, clientId=client_id)
        thread = threading.Thread(target=app.run, daemon=True)
        thread.start()
        if not app.ready.wait(TIMEOUT) or app.disconnected or app.duplicate_client:
            print_status("connection", "FAILED")
            print_status("duplicate_client_id", app.duplicate_client)
            return 1
        print_status("connection", "API_HANDSHAKE_CONFIRMED")
        print_status("endpoint", "LOCALHOST_PORT_7496")
        print_status("server_version_available", app.serverVersion() is not None)
        print_status("read_only_mode_verified_by_api", False)

        if not app.accounts_done.wait(TIMEOUT) or app.disconnected:
            print_status("managed_accounts", "INCOMPLETE")
            return 1
        accounts = app.managed_accounts
        print_status("managed_account_count", len(accounts))
        selected = os.environ.get("EXECUTOR_IBKR_ACCOUNT")
        if selected is None and len(accounts) == 1:
            selected = accounts[0]
        if selected not in accounts or not selected:
            print_status("account_selection", "BLOCKED")
            return 1
        print_status("account_selection", "EXPLICIT_RUNTIME" if os.environ.get("EXECUTOR_IBKR_ACCOUNT") else "UNIQUE_RUNTIME")

        app.reqAccountSummary(8101, "All", ",".join(TAGS))
        summary_complete = app.summary_done.wait(TIMEOUT) and not app.disconnected
        print_status("account_summary_complete", summary_complete)
        for tag in TAGS:
            matches = [(currency, value, receipt) for (account, observed_tag, currency),
                       (value, receipt) in app.account_values.items()
                       if account == selected and observed_tag == tag]
            valid_usd = any(currency == "USD" and finite_decimal(value) is not None
                            and 0 <= time.monotonic() - receipt <= 1.0
                            for currency, value, receipt in matches)
            if tag == "AccountType":
                print_status("field_" + tag, "OBSERVED" if matches else "MISSING")
            else:
                print_status("field_" + tag, "FRESH_NUMERIC_USD" if valid_usd else
                             ("OBSERVED_NOT_FRESH_OR_USD" if matches else "MISSING"))
        sizing_tags = ("AvailableFunds", "BuyingPower", "ExcessLiquidity",
                       "TotalCashValue", "SettledCash")
        sizing_values = [app.account_values.get((selected, tag, "USD"))
                         for tag in sizing_tags]
        summary_snapshot = VerifiedAccountSnapshot(
            account_id=selected, selected_account=selected,
            complete=summary_complete and all(value is not None for value in sizing_values),
            currency="USD",
            available_funds=sizing_values[0][0] if sizing_values[0] else None,
            buying_power=sizing_values[1][0] if sizing_values[1] else None,
            excess_liquidity=sizing_values[2][0] if sizing_values[2] else None,
            total_cash_value=sizing_values[3][0] if sizing_values[3] else None,
            settled_cash=sizing_values[4][0] if sizing_values[4] else None,
            oldest_required_receipt_monotonic=min(value[1] for value in sizing_values)
            if all(sizing_values) else float("nan"),
        )
        try:
            conservative_usable_funds(summary_snapshot, selected, time.monotonic())
            print_status("conservative_sizing_field_set", "VALID_AT_SNAPSHOT_TIME")
        except EvidenceError:
            print_status("conservative_sizing_field_set", "BLOCKED")
        print_status("future_sizing_candidates", "AvailableFunds,ExcessLiquidity,TotalCashValue,SettledCash")
        print_status("buying_power_role", "VALIDATE_ONLY")

        app.reqPositions()
        app.reqAllOpenOrders()
        positions_complete = app.positions_done.wait(TIMEOUT) and not app.disconnected
        orders_complete = app.orders_done.wait(TIMEOUT) and not app.disconnected
        print_status("positions_complete", positions_complete)
        print_status("open_orders_complete", orders_complete)
        nonzero = [(account, con_id, qty) for account, con_id, qty in app.positions
                   if finite_decimal(qty) is not None and finite_decimal(qty) != 0]
        print_status("nonzero_position_count", len(nonzero))
        print_status("api_visible_open_order_count", len(app.orders))
        account_positions = [item for item in nonzero if item[0] == selected]
        other_account_evidence = any(item[0] != selected for item in nonzero) or any(
            order_account not in {selected, None} for order_account, _ in app.orders)
        representation_complete = (positions_complete and orders_complete and
                                   summary_complete and not other_account_evidence and
                                   len(account_positions) <= 1 and not app.error_codes)
        qty = 0
        con_id = None
        if len(account_positions) == 1:
            con_id = account_positions[0][1]
            observed_qty = finite_decimal(account_positions[0][2])
            if observed_qty is not None and observed_qty == int(observed_qty):
                qty = int(observed_qty)
            else:
                representation_complete = False
        snapshot = VerifiedBrokerSnapshot(
            account_id=selected, selected_account=selected,
            managed_accounts=accounts, complete=representation_complete,
            position_qty=qty, con_id=con_id,
            open_order_count=len(app.orders), received_monotonic=time.monotonic(),
        )
        try:
            validate_broker_snapshot(snapshot, selected, time.monotonic())
            print_status("verified_snapshot_mapping", "VALID_SHAPE")
        except Exception:
            print_status("verified_snapshot_mapping", "BLOCKED")
        print_status("new_entry_reconciliation", "BLOCKED_UNTIL_PERSISTED_STATE_CHECK")

        underlying = Contract()
        underlying.symbol = "SPX"
        underlying.secType = "IND"
        underlying.exchange = "CBOE"
        underlying.currency = "USD"
        app.reqContractDetails(8102, underlying)
        details_complete = app.contract_done.wait(TIMEOUT) and not app.disconnected
        matches = [c for c in app.underlyings if c.symbol == "SPX" and c.secType == "IND"
                   and c.currency == "USD" and isinstance(c.conId, int) and c.conId > 0]
        print_status("spx_contract_details_complete", details_complete)
        print_status("spx_underlying_unique", details_complete and len(matches) == 1)
        exact_option = None
        if details_complete and len(matches) == 1:
            app.reqSecDefOptParams(8103, "SPX", "", "IND", matches[0].conId)
            chain_complete = app.chain_done.wait(TIMEOUT) and not app.disconnected
            today = datetime.now(NY).strftime("%Y%m%d")
            chains = [item for item in app.chains if item[1] == matches[0].conId
                      and item[2] == "SPXW" and item[3] == "100"]
            today_chains = [item for item in chains if today in item[4]]
            print_status("spxw_chain_complete", chain_complete)
            print_status("spxw_100_chain_count", len(chains))
            print_status("current_ny_date_expiration_present", chain_complete and bool(today_chains))
            print_status("current_expiration_strike_count", len(set().union(*(x[5] for x in today_chains))) if today_chains else 0)
            if chain_complete and today_chains:
                strikes = sorted(x for x in set().union(*(chain[5] for chain in today_chains))
                                 if isinstance(x, (int, float)) and math.isfinite(x) and x > 0)
                if strikes:
                    requested_strike = strikes[len(strikes) // 2]
                    option = Contract()
                    option.symbol = "SPX"
                    option.secType = "OPT"
                    option.exchange = "SMART"
                    option.currency = "USD"
                    option.tradingClass = "SPXW"
                    option.multiplier = "100"
                    option.lastTradeDateOrContractMonth = today
                    option.strike = requested_strike
                    option.right = "C"
                    app.reqContractDetails(8105, option)
                    option_details_complete = app.option_details_done.wait(TIMEOUT) and not app.disconnected
                    exact = [c for c in app.option_details if c.symbol == "SPX"
                             and c.secType == "OPT" and c.currency == "USD"
                             and c.tradingClass == "SPXW" and c.multiplier == "100"
                             and c.lastTradeDateOrContractMonth == today
                             and c.strike == requested_strike and c.right == "C"
                             and isinstance(c.conId, int) and c.conId > 0]
                    unique_ids = {c.conId for c in exact}
                    if option_details_complete and len(unique_ids) == 1:
                        exact_option = exact[0]
                    print_status("exact_option_contract_details_complete", option_details_complete)
                    print_status("exact_option_identity_resolved", exact_option is not None)
        else:
            print_status("spxw_chain_complete", False)

        app.reqMarketDataType(1)
        app.reqMktData(8104, underlying, "", False, False, [])
        app.price_seen.wait(TIMEOUT)
        age = None if app.spx_receipt is None else time.monotonic() - app.spx_receipt
        print_status("spx_market_data_type", {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(app.market_type, "UNKNOWN"))
        print_status("spx_last_received", app.spx_last is not None)
        print_status("spx_last_receipt_fresh", age is not None and 0 <= age <= 1.0)
        print_status("spx_source_timestamp_available", False)
        if exact_option is not None:
            app.reqMktData(8106, exact_option, "", False, False, [])
            app.option_quote_seen.wait(TIMEOUT)
            print_status("option_market_data_type", {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(app.option_market_type, "UNKNOWN"))
            print_status("option_bid_ask_received", app.option_bid is not None and app.option_ask is not None)
            print_status("option_source_timestamp_available", False)
        print_status("exact_option_live_quote_validated", False)
        print_status("noninformational_error_codes", ",".join(str(x) for x in sorted(app.error_codes)) or "NONE")
        return 0
    finally:
        try:
            app.cancelPositions()
        except Exception:
            pass
        try:
            app.cancelAccountSummary(8101)
        except Exception:
            pass
        try:
            app.cancelMktData(8104)
        except Exception:
            pass
        try:
            app.cancelMktData(8106)
        except Exception:
            pass
        app.disconnect()
        if thread is not None:
            thread.join(timeout=2)
        print_status("disconnected", not app.isConnected())


if __name__ == "__main__":
    raise SystemExit(main())
