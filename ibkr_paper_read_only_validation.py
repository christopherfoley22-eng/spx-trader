"""Opt-in Stage 5A transport. Importing/preflight never opens a socket.

Reuse the existing hard-disabled ReadOnlyProbe. All requests are observations.
No execution session is admitted to the offline paper ledger.
"""
import argparse
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import stat
import threading
import time

from ibapi import __version__ as SDK_VERSION
from ibapi.client import MAX_CLIENT_VER
from ibapi.contract import Contract

from executor_ibkr_read_only import (ReadOnlyPaperValidation, SettingsVerification,
    validate_configuration, SUMMARY, SPX_DETAILS, CHAIN, SPX_QUOTE)
from executor_ibkr_observation import SIZING_TAGS
from executor_paper_broker import integer
from executor_paper_types import ApprovedPaperAccount
from ibkr_read_only_validation import ReadOnlyProbe
from simulation_evidence import EvidenceError


class PaperReadOnlyProbe(ReadOnlyProbe):
    def __init__(self, validation):
        super().__init__(validation.wall, validation.mono)
        self.validation = validation
        validation.transport_info = {'ibapi_version': SDK_VERSION,
            'sdk_max_client_protocol_version': MAX_CLIENT_VER, 'server_version': None}
        self.reader_failed = False

    def connect(self, host, port, clientId, *, authorized=False):
        validate_configuration(self.validation.approval, self.validation.settings, self.validation.wall())
        if (authorized is not True or self.validation.started or host != self.validation.approval.host
                or port != self.validation.approval.port or not integer(clientId, 1, 2**31-1)):
            raise EvidenceError('EXPLICIT_ONE_SHOT_PAPER_READ_ONLY_AUTHORIZATION_REQUIRED')
        self.validation.started = True
        result = super().connect(host, port, clientId)
        self.validation.transport_info['server_version'] = self.serverVersion()
        return result

    def _feed(self, method, *args):
        with self.validation.lock:
            try:
                return getattr(self.validation, method)(*args)
            except Exception as exc:
                # Retain our machine reason codes, never arbitrary broker/account text.
                reason = str(exc)
                reason = reason if re.fullmatch(r'[A-Z_]{1,96}', reason) else 'INVALID_OR_UNEXPECTED_IBKR_CALLBACK'
                self.validation.fail(reason)
                self.reader_failed = True
                self.disconnect()
                return None

    def nextValidId(self, orderId):
        self._feed('handshake', orderId)

    def managedAccounts(self, accountsList):
        self._feed('managed_accounts', accountsList)

    def accountSummary(self, reqId, account, tag, value, currency):
        self._feed('summary', reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId):
        if reqId != SUMMARY:
            self.validation.fail('UNKNOWN_SUMMARY_COMPLETION')
            self.disconnect()
        else:
            self._feed('complete', 'SUMMARY')

    def position(self, account, contract, position, avgCost):
        self._feed('position', account, contract, position)

    def positionEnd(self):
        self._feed('complete', 'POSITIONS')

    def openOrder(self, orderId, contract, order, orderState):
        self._feed('open_order', orderId, contract, order)

    def openOrderEnd(self):
        self._feed('complete', 'ORDERS')

    def contractDetails(self, reqId, details):
        self._feed('contract_details', reqId, details.contract)

    def contractDetailsEnd(self, reqId):
        self._feed('contract_end', reqId)

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId, tradingClass,
                                          multiplier, expirations, strikes):
        self._feed('chain', reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes)

    def securityDefinitionOptionParameterEnd(self, reqId):
        self._feed('chain_end', reqId)

    def marketDataType(self, reqId, marketDataType):
        self._feed('market', 'marketDataType', reqId, marketDataType)

    def tickPrice(self, reqId, tickType, price, attrib):
        self._feed('market', 'tickPrice', reqId, tickType, price, attrib)

    def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta,
                              optPrice, pvDividend, gamma, vega, theta, undPrice):
        self._feed('market', 'tickOptionComputation', reqId, tickType, tickAttrib, impliedVol,
                   delta, optPrice, pvDividend, gamma, vega, theta, undPrice)

    def tickString(self, reqId, tickType, value):
        self._feed('timestamp', reqId, tickType, value)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=''):
        # Error codes suffice for entitlement/session diagnostics; no raw text retained.
        self._feed('error', reqId, errorCode)
        if self.validation.failure:
            self.disconnect()

    def connectionClosed(self):
        self.validation.fail('CONNECTION_CLOSED_NEW_APPROVAL_REQUIRED')
        self.validation.closed = True

    def disconnect(self):
        super().disconnect()
        self.validation.closed = True
        self.validation.fail('SESSION_CLOSED_NEW_VALIDATION_REQUIRED')


def contract(**fields):
    value = Contract()
    for key, item in fields.items():
        setattr(value, key, item)
    return value


def run_validation(validation, *, authorized=False, client_id=5091, seconds=10, timeout=8,
                   probe_factory=PaperReadOnlyProbe):
    """Only call after explicit user authorization. Permanently read-only."""
    validate_configuration(validation.approval, validation.settings, validation.wall())
    if authorized is not True or type(seconds) is not int or not 1 <= seconds <= 60 or not 0 < timeout <= 10:
        raise EvidenceError('EXPLICIT_AUTHORIZATION_AND_BOUNDED_DURATION_REQUIRED')
    app = probe_factory(validation)
    thread = connector = None
    connect_done = threading.Event()

    def connect_worker():
        try:
            app.connect(validation.approval.host, validation.approval.port, client_id, authorized=True)
        except Exception:
            validation.fail('SOCKET_HANDSHAKE_FAILED')
        finally:
            if validation.closed:
                app.disconnect()
            connect_done.set()

    def read_worker():
        try:
            app.run()
        except Exception:
            validation.fail('SDK_READER_FAILED')
            app.disconnect()

    def wait(predicate, label):
        deadline = time.monotonic()+timeout
        while not predicate():
            validation.gate() if validation.verified else None
            if validation.failure or validation.closed:
                raise EvidenceError('SESSION_FAILED')
            if time.monotonic() >= deadline:
                validation.fail(label+'_TIMEOUT')
                raise EvidenceError('VALIDATION_TIMEOUT')
            time.sleep(.02)

    def request(method, *args):
        if method not in {'reqAccountSummary', 'reqPositions', 'reqAllOpenOrders', 'reqContractDetails',
                           'reqSecDefOptParams', 'reqMarketDataType', 'reqMktData'}:
            raise EvidenceError('NON_READ_ONLY_REQUEST_REJECTED')
        with validation.lock:
            validation.gate()
            return getattr(app, method)(*args)

    try:
        connector = threading.Thread(target=connect_worker, daemon=True)
        connector.start()
        if not connect_done.wait(timeout):
            validation.fail('SOCKET_HANDSHAKE_TIMEOUT')
            raise EvidenceError('SOCKET_HANDSHAKE_TIMEOUT')
        if validation.failure or validation.closed:
            raise EvidenceError('SOCKET_HANDSHAKE_FAILED')
        thread = threading.Thread(target=read_worker, daemon=True)
        thread.start()
        wait(lambda: validation.verified, 'PAPER_IDENTITY')
        with validation.lock:
            validation.begin_summary()
        request('reqAccountSummary', SUMMARY, 'All', ','.join(SIZING_TAGS))
        request('reqPositions')
        request('reqAllOpenOrders')  # Observation only; no binding/auto-open-order call.
        request('reqContractDetails', SPX_DETAILS, contract(symbol='SPX', secType='IND', exchange='CBOE', currency='USD'))
        wait(lambda: validation.adapter.underlying_done, 'SPX_CONTRACT')
        request('reqSecDefOptParams', CHAIN, 'SPX', '', 'IND', validation.adapter.underlying[3])
        request('reqMarketDataType', 1)
        request('reqMktData', SPX_QUOTE, validation.underlying_rows[0], '', False, False, [])
        wait(lambda: validation.adapter.chain_done and validation.bootstrap is not None, 'LIVE_SPX_AND_CHAIN')
        with validation.lock:
            options = validation.plan_options()
        for req_id, spec in options.items():
            request('reqContractDetails', req_id, contract(symbol='SPX', secType='OPT', exchange='SMART',
                currency='USD', tradingClass='SPXW', multiplier='100', right=spec['right'],
                strike=spec['strike'], lastTradeDateOrContractMonth=spec['expiration']))
        wait(lambda: bool(validation.bridges), 'EXACT_CALL_PUT_CONTRACTS')
        for key, (option, exchange) in validation.adapter.exact.items():
            request('reqMktData', int(key)+1000, contract(symbol=option.symbol, secType=option.sec_type,
                conId=option.con_id, exchange=exchange, currency=option.currency, right=option.right,
                strike=option.strike, tradingClass=option.trading_class, multiplier=option.multiplier,
                lastTradeDateOrContractMonth=option.expiration), '', False, False, [])
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            validation.gate()
            time.sleep(.05)
        return validation.report()
    except Exception:
        validation.fail(validation.failure or 'VALIDATION_ABORTED')
        return validation.report()
    finally:
        app.disconnect()  # Closes subscriptions via socket; never cancels an order.
        if thread is not None:
            thread.join(timeout=2)
        if connector is not None:
            connector.join(timeout=2)


def load_config(path):
    path = Path(path)
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise EvidenceError('PRIVATE_0600_CONFIGURATION_REQUIRED')
    data = json.loads(path.read_text())
    if set(data) != {'account', 'verification_ref', 'approved_until', 'host', 'port', 'settings'}:
        raise EvidenceError('EXACT_CONFIGURATION_FIELDS_REQUIRED_NO_CREDENTIALS')
    settings = data.pop('settings')
    if set(settings) != {'reference', 'verified_at', 'expires_at', 'paper_login_verified', 'read_only_api_checked'}:
        raise EvidenceError('EXACT_SETTINGS_FIELDS_REQUIRED')
    data['approved_until'] = datetime.fromisoformat(data['approved_until'])
    for key in ('verified_at', 'expires_at'):
        settings[key] = datetime.fromisoformat(settings[key])
    return ApprovedPaperAccount(**data), SettingsVerification(**settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--authorize-read-only-connection', action='store_true')
    parser.add_argument('--seconds', type=int, default=10)
    args = parser.parse_args(argv)
    attempted = False
    try:
        approval, settings = load_config(args.config)
        validation = ReadOnlyPaperValidation(approval, settings)
        if not args.authorize_read_only_connection:
            print(json.dumps({'connection_attempted': False, 'configuration_valid': True,
                              'executable': False, 'next_step': 'Explicit user authorization required'}))
            return 0
        attempted = True
        report = run_validation(validation, authorized=True, seconds=args.seconds)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['paper_account_matched'] and not report['session_failure'] else 1
    except Exception:
        print(json.dumps({'connection_attempted': attempted, 'executable': False,
                          'block_reasons': ['CONFIGURATION_OR_VALIDATION_REJECTED']}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
