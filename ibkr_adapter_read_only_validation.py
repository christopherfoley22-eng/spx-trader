"""Controlled, redacted TWS observation validation through the evidence adapter.

Run only when explicitly authorized. Requests are read-only. This script never
manufactures exchange/source timestamps from local callback receipt times.
"""

import math
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from ibapi.contract import Contract

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter, SIZING_TAGS
from ibkr_read_only_validation import HOST, PORT, NY, TIMEOUT, ReadOnlyProbe
from simulation_evidence import EvidenceError, VerifiedOptionContract


class AdapterProbe(ReadOnlyProbe):
    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.generation = adapter.generation
        self.adapter_failure = False
        self.source_timestamp_tick_seen = False
        self.pending_accounts = None

    def _feed(self, method, *args):
        try:
            getattr(self.adapter, method)(self.generation, *args)
        except EvidenceError:
            self.adapter_failure = True
            self.adapter.invalid = self.adapter.invalid or "UNVERIFIABLE TWS CALLBACK"

    def nextValidId(self, orderId):
        self._feed("handshake")
        if self.pending_accounts is not None:
            self._feed("managed_accounts", self.pending_accounts)
            self.pending_accounts = None
        super().nextValidId(orderId)

    def managedAccounts(self, accountsList):
        accounts = tuple(x.strip() for x in accountsList.split(",") if x.strip())
        if self.adapter.connected:
            self._feed("managed_accounts", accounts)
        elif self.pending_accounts is not None and self.pending_accounts != accounts:
            self.adapter_failure = True
            self.adapter.invalid = "CONTRADICTORY ACCOUNT EVIDENCE"
        else:
            self.pending_accounts = accounts
        super().managedAccounts(accountsList)

    def accountSummary(self, reqId, account, tag, value, currency):
        if reqId == 8101 and tag in SIZING_TAGS:
            self._feed("account_value", account, tag, currency or "", value, time.monotonic())
        super().accountSummary(reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId):
        if reqId == 8101:
            self._feed("account_end")
        super().accountSummaryEnd(reqId)

    def position(self, account, contract, position, avgCost):
        self._feed("position", account, getattr(contract, "conId", None), position)
        super().position(account, contract, position, avgCost)

    def positionEnd(self):
        self._feed("position_end", time.monotonic())
        super().positionEnd()

    def openOrder(self, orderId, contract, order, orderState):
        self._feed("open_order", orderId, getattr(order, "account", None),
                   getattr(contract, "conId", None))
        super().openOrder(orderId, contract, order, orderState)

    def openOrderEnd(self):
        self._feed("open_order_end", time.monotonic())
        super().openOrderEnd()

    def contractDetails(self, reqId, details):
        c = details.contract
        if reqId == 8102:
            self._feed("spx_details", c.symbol, c.secType, c.currency,
                       c.conId, c.exchange)
        elif reqId == 8105:
            option = VerifiedOptionContract(
                c.symbol, c.secType, c.right, c.lastTradeDateOrContractMonth,
                c.tradingClass, c.multiplier, c.currency, c.strike, c.conId)
            self._feed("exact_option", "candidate", option, c.exchange)
        super().contractDetails(reqId, details)

    def contractDetailsEnd(self, reqId):
        if reqId == 8102:
            self._feed("spx_details_end")
        elif reqId == 8105:
            self._feed("exact_option_end")
        super().contractDetailsEnd(reqId)

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        if reqId == 8103:
            self._feed("chain", exchange, underlyingConId, tradingClass,
                       multiplier, expirations, strikes)
        super().securityDefinitionOptionParameter(
            reqId, exchange, underlyingConId, tradingClass, multiplier,
            expirations, strikes)

    def securityDefinitionOptionParameterEnd(self, reqId):
        if reqId == 8103:
            self._feed("chain_end")
        super().securityDefinitionOptionParameterEnd(reqId)

    def marketDataType(self, reqId, marketDataType):
        if reqId in (8104, 8106):
            self._feed("market_type", "SPX" if reqId == 8104 else "OPTION",
                       marketDataType)
        super().marketDataType(reqId, marketDataType)

    def tickString(self, reqId, tickType, value):
        if reqId in (8104, 8106) and tickType in (45, 48):
            self.source_timestamp_tick_seen = True
        # Neither last-trade timestamp tick proves bid/ask source time.

    def connectionClosed(self):
        self.adapter.reset()
        super().connectionClosed()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
        if errorCode not in {2104, 2106, 2107, 2108, 2158}:
            self.adapter.invalid = "TWS CALLBACK ERROR"


def _print(name, value):
    print("{}={}".format(name, value))


def main():
    # Account identity is runtime-only and never printed.
    adapter = ReadOnlyIBKREvidenceAdapter(os.environ.get("EXECUTOR_IBKR_ACCOUNT"))
    app = AdapterProbe(adapter)
    thread = None
    try:
        app.connect(HOST, PORT, clientId=secrets.randbelow(50000) + 10000)
        thread = threading.Thread(target=app.run, daemon=True)
        thread.start()
        if not app.ready.wait(TIMEOUT) or app.disconnected or app.duplicate_client:
            _print("connection", "FAILED")
            return 1
        _print("connection", "LOCAL_API_HANDSHAKE_CONFIRMED")
        _print("endpoint", "127.0.0.1:7496")
        _print("read_only_setting_api_verifiable", False)
        if not app.accounts_done.wait(TIMEOUT) or app.disconnected:
            _print("managed_accounts", "INCOMPLETE")
            return 1
        _print("managed_account_count", len(app.managed_accounts))
        _print("account_selected_unambiguously", adapter.selected is not None)
        if adapter.selected is None:
            return 1

        app.reqAccountSummary(8101, "All", ",".join(SIZING_TAGS))
        _print("account_summary_end", app.summary_done.wait(TIMEOUT) and not app.disconnected)
        account_at_completion = adapter.observe(datetime.now(timezone.utc), time.monotonic())
        _print("account_summary_fresh_at_completion", account_at_completion.account is not None and
               "ACCOUNT EVIDENCE INCOMPLETE OR STALE" not in account_at_completion.reasons)
        _print("sizing_fields_observed", ",".join(
            tag for tag in SIZING_TAGS if (adapter.selected, tag) in adapter.summary) or "NONE")
        app.reqPositions()
        app.reqAllOpenOrders()
        _print("positions_end", app.positions_done.wait(TIMEOUT) and not app.disconnected)
        _print("api_visible_open_orders_end", app.orders_done.wait(TIMEOUT) and not app.disconnected)
        broker_at_completion = adapter.observe(datetime.now(timezone.utc), time.monotonic())
        _print("broker_snapshot_fresh_at_completion", broker_at_completion.broker is not None and
               "BROKER EVIDENCE INCOMPLETE OR STALE" not in broker_at_completion.reasons)

        underlying = Contract()
        underlying.symbol = "SPX"
        underlying.secType = "IND"
        underlying.exchange = "CBOE"
        underlying.currency = "USD"
        app.reqContractDetails(8102, underlying)
        _print("spx_contract_details_end", app.contract_done.wait(TIMEOUT) and not app.disconnected)
        resolved = adapter.underlying is not None and adapter.underlying_done and not adapter.invalid
        _print("spx_identity_resolved", resolved)
        exact = None
        if resolved:
            app.reqSecDefOptParams(8103, "SPX", "", "IND", adapter.underlying[3])
            _print("spxw_chain_end", app.chain_done.wait(TIMEOUT) and not app.disconnected)
            today = datetime.now(NY).strftime("%Y%m%d")
            strikes = sorted({strike for _, expirations, values in adapter.chains
                              if today in expirations for strike in values
                              if isinstance(strike, (int, float)) and math.isfinite(strike) and strike > 0})
            _print("current_ny_date_spxw_expiration", bool(strikes))
            if strikes:
                option = Contract()
                option.symbol = "SPX"
                option.secType = "OPT"
                option.exchange = "SMART"
                option.currency = "USD"
                option.tradingClass = "SPXW"
                option.multiplier = "100"
                option.lastTradeDateOrContractMonth = today
                option.strike = strikes[len(strikes) // 2]
                option.right = "C"
                app.reqContractDetails(8105, option)
                _print("exact_option_contract_details_end",
                       app.option_details_done.wait(TIMEOUT) and not app.disconnected)
                exact = adapter.exact.get("candidate")
                _print("exact_option_identity_resolved", bool(exact) and not adapter.invalid)

        # Request LIVE only. TWS may report delayed/frozen/unavailable; no fallback.
        app.reqMarketDataType(1)
        resolved_underlying = next((c for c in app.underlyings
                                    if c.conId == adapter.underlying[3]), None) if resolved else None
        app.reqMktData(8104, resolved_underlying or underlying, "", False, False, [])
        app.price_seen.wait(TIMEOUT)
        if exact and not adapter.invalid:
            resolved_option = next((c for c in app.option_details
                                    if c.conId == exact[0].con_id), None)
            if resolved_option is not None:
                app.reqMktData(8106, resolved_option, "", False, False, [])
            app.option_quote_seen.wait(TIMEOUT)
        observed = adapter.observe(datetime.now(timezone.utc), time.monotonic(),
                                   "CALL", "candidate", None)
        _print("spx_market_type", {1: "LIVE", 2: "FROZEN", 3: "DELAYED",
                                   4: "DELAYED_FROZEN"}.get(app.market_type, "UNAVAILABLE"))
        _print("option_market_type", {1: "LIVE", 2: "FROZEN", 3: "DELAYED",
                                      4: "DELAYED_FROZEN"}.get(
                                          app.option_market_type, "UNAVAILABLE"))
        _print("spx_price_callback_seen", app.spx_last is not None)
        _print("option_bid_ask_callbacks_seen", app.option_quote_seen.is_set() and
               app.option_bid is not None and app.option_ask is not None)
        _print("last_trade_timestamp_tick_seen", app.source_timestamp_tick_seen)
        _print("spx_source_timestamp_adequate", observed.spx is not None)
        _print("option_bid_ask_source_timestamp_adequate", observed.option is not None)
        _print("adapter_account_complete_fresh", observed.account is not None and
               "ACCOUNT EVIDENCE INCOMPLETE OR STALE" not in observed.reasons)
        _print("adapter_broker_complete_fresh", observed.broker is not None and
               "BROKER EVIDENCE INCOMPLETE OR STALE" not in observed.reasons)
        _print("adapter_reconciliation_required", "RECONCILIATION REQUIRED" in observed.reasons)
        _print("adapter_market_data_blocked", "MARKET DATA UNAVAILABLE OR STALE" in observed.reasons)
        _print("adapter_executable", observed.executable)
        _print("adapter_callback_failure", app.adapter_failure)
        _print("noninformational_error_codes", ",".join(str(x) for x in sorted(app.error_codes)) or "NONE")
        _print("api_visible_orders_scope", observed.order_scope)
        return 0
    finally:
        # These end only data subscriptions. They do not alter broker orders.
        for stop, args in ((app.cancelPositions, ()),
                           (app.cancelAccountSummary, (8101,)),
                           (app.cancelMktData, (8104,)),
                           (app.cancelMktData, (8106,))):
            try:
                stop(*args)
            except Exception:
                pass
        app.disconnect()
        if thread is not None:
            thread.join(timeout=2)
        _print("disconnected", not app.isConnected())


if __name__ == "__main__":
    raise SystemExit(main())
