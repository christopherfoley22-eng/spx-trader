"""Stage 5A diagnostic reducer. No socket, credentials, or execution capability.

Operator paper/settings verification is explicitly separate from API evidence.
Raw quotes/Greeks NEVER enter the source-proven ingestion path.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
import math
import re
from threading import RLock
import time
from zoneinfo import ZoneInfo

from executor_ibkr_observation import ReadOnlyIBKREvidenceAdapter, SIZING_TAGS
from executor_market_stream import ReadOnlyMarketStream, IBKRMarketCallbacks
from executor_paper_broker import integer, text_id, utc
from executor_paper_types import ApprovedPaperAccount, PaperBrokerError
from simulation_evidence import EvidenceError, MAX_AGE_SECONDS, MAX_FUTURE_SECONDS, MAX_SYNC_SECONDS, VerifiedOptionContract

NY = ZoneInfo('America/New_York')
SUMMARY, SPX_DETAILS, CHAIN, SPX_QUOTE = 1, 2, 3, 1000
INFO = {2104, 2106, 2107, 2108, 2158}
ERROR_NAMES = {354: 'MARKET_DATA_NOT_SUBSCRIBED', 10167: 'LIVE_DATA_NOT_SUBSCRIBED_DELAYED_AVAILABLE',
    10090: 'PARTIAL_MARKET_DATA_SUBSCRIPTION', 10197: 'COMPETING_MARKET_DATA_SESSION',
    200: 'SECURITY_DEFINITION_MISSING_OR_AMBIGUOUS', 326: 'CLIENT_ID_IN_USE',
    502: 'API_CONNECTION_FAILED', 504: 'API_NOT_CONNECTED', 1100: 'CONNECTIVITY_LOST',
    1101: 'CONNECTIVITY_RESTORED_DATA_LOST_NEW_VALIDATION_REQUIRED',
    1102: 'CONNECTIVITY_RESTORED_NEW_VALIDATION_REQUIRED', 101: 'MARKET_DATA_LINE_LIMIT'}


@dataclass(frozen=True)
class SettingsVerification:
    reference: str
    verified_at: datetime
    expires_at: datetime
    paper_login_verified: bool = False
    read_only_api_checked: bool = False


def validate_configuration(approval, settings, now):
    """Same paper identity/endpoint restrictions as the durable paper ledger."""
    if (not isinstance(approval, ApprovedPaperAccount)
            or not isinstance(approval.account, str) or not re.fullmatch(r'DU[0-9]{5,}', approval.account)
            or not text_id(approval.verification_ref) or approval.host != '127.0.0.1'
            or type(approval.port) is not int or approval.port not in (7497, 4002)
            or utc(approval.approved_until) <= utc(now)):
        raise PaperBrokerError('EXPLICIT_CURRENT_PAPER_APPROVAL_REQUIRED')
    if (not isinstance(settings, SettingsVerification) or not text_id(settings.reference)
            or settings.paper_login_verified is not True or settings.read_only_api_checked is not True
            or not utc(settings.verified_at) <= utc(now) < utc(settings.expires_at)
            or (utc(settings.expires_at)-utc(settings.verified_at)).total_seconds() > 600):
        raise PaperBrokerError('FRESH_PAPER_AND_READ_ONLY_SETTINGS_VERIFICATION_REQUIRED')


class ReadOnlyPaperValidation:
    """One-shot session: reconnect requires a new object and new verification.

    Optional existing snapshot machine receives actual callbacks, never an
    invented FLAT baseline. The Stage 3A paper ledger remains OFFLINE_FIXTURE
    only; these callbacks cannot verify or enable a paper execution session.
    """
    def __init__(self, approval, settings, wall_clock=None, monotonic_clock=None, snapshot_machine=None):
        self.wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self.mono = monotonic_clock or time.monotonic
        validate_configuration(approval, settings, self.wall())
        self.approval, self.settings = approval, settings
        self.adapter = ReadOnlyIBKREvidenceAdapter(approval.account)
        self.generation = self.adapter.generation
        self.lock = RLock()
        self.snapshot = snapshot_machine
        self.snapshot_generation = self.cycle = None
        self.handshake_seen = False
        self.accounts = None
        self.verified = False
        self.failure = None
        self.closed = False
        self.started = False
        self.transport_info = None
        self.last_monotonic = None
        self.errors = []
        self.underlying_rows = []
        self.pending = {}
        self.done = set()
        self.streams, self.bridges = {}, {}
        self.bootstrap = None
        self.spx_type = None
        self.trade_timestamps = []
        self.summary_requested = False
        self.funds_fresh_at_completion = False

    def fail(self, reason):
        self.failure = self.failure or reason
        self.verified = False
        if self.adapter.connected:
            self.adapter.reset()
        if self.snapshot is not None and self.snapshot.connected:
            self.snapshot.disconnect(self.snapshot_generation)

    def gate(self):
        if self.closed or self.failure:
            raise EvidenceError(self.failure or 'SESSION_CLOSED')
        validate_configuration(self.approval, self.settings, self.wall())
        receipt = self.mono()
        if (not isinstance(receipt, (int, float)) or isinstance(receipt, bool)
                or not math.isfinite(receipt) or receipt < 0
                or self.last_monotonic is not None and receipt < self.last_monotonic):
            raise EvidenceError('INVALID_OR_BACKWARD_RECEIPT_CLOCK')
        self.last_monotonic = receipt
        if not self.verified:
            raise EvidenceError('PAPER_ACCOUNT_NOT_VERIFIED')

    def handshake(self, order_id):
        with self.lock:
            if self.closed or self.failure or self.handshake_seen or not integer(order_id):
                raise EvidenceError('INVALID_OR_REPEATED_HANDSHAKE')
            self.handshake_seen = True
            self.adapter.handshake(self.generation)
            self._identity()

    def managed_accounts(self, accounts_list):
        with self.lock:
            parts = accounts_list.split(',') if isinstance(accounts_list, str) else []
            if parts and parts[-1] == '':
                parts.pop()
            accounts = tuple(parts)
            if accounts != (self.approval.account,) or self.accounts not in (None, accounts):
                self.fail('LIVE_UNKNOWN_OR_AMBIGUOUS_ACCOUNT')
                raise EvidenceError(self.failure)
            self.accounts = accounts
            self._identity()

    def _identity(self):
        if self.handshake_seen and self.accounts and not self.failure and not self.closed:
            validate_configuration(self.approval, self.settings, self.wall())
            self.adapter.managed_accounts(self.generation, self.accounts)
            self.verified = True

    def begin_summary(self):
        self.gate()
        if self.summary_requested:
            raise EvidenceError('ONE_SHOT_SUMMARY_ALREADY_REQUESTED')
        self.summary_requested = True
        if self.snapshot is not None:
            self.snapshot_generation = self.snapshot.connect()
            self.snapshot.managed_accounts(self.snapshot_generation, self.accounts)
            self.cycle = self.snapshot.begin_cycle(self.snapshot_generation, self.mono())
            for name in ('SUMMARY', 'POSITIONS', 'ORDERS'):
                self.snapshot.begin_component(self.snapshot_generation, self.cycle, name, self.mono())

    def _snapshot_value(self, name, account, key, value):
        if self.snapshot is not None:
            self.snapshot.observation(self.snapshot_generation, self.cycle, name, account, key, value, self.mono())

    def summary(self, req_id, account, tag, value, currency):
        self.gate()
        if req_id != SUMMARY or not self.summary_requested:
            raise EvidenceError('UNKNOWN_SUMMARY_REQUEST')
        self.adapter.account_value(self.generation, account, tag, currency, value, self.mono())
        if tag in SIZING_TAGS:
            self._snapshot_value('SUMMARY', account, tag, (value, currency))

    def complete(self, component):
        self.gate()
        {'SUMMARY': self.adapter.account_end,
         'POSITIONS': lambda g: self.adapter.position_end(g, self.mono()),
         'ORDERS': lambda g: self.adapter.open_order_end(g, self.mono())}[component](self.generation)
        if component == 'SUMMARY':
            from simulation_evidence import conservative_usable_funds
            account = self.adapter.observe(self.wall(), self.mono()).account
            conservative_usable_funds(account, self.approval.account, self.mono())
            self.funds_fresh_at_completion = True
        if self.snapshot is not None:
            self.snapshot.complete_component(self.snapshot_generation, self.cycle, component, self.mono())

    def position(self, account, contract, quantity):
        self.gate()
        self.adapter.position(self.generation, account, contract.conId, quantity)
        self._snapshot_value('POSITIONS', account, contract.conId, int(quantity))

    def open_order(self, order_id, contract, order):
        self.gate()
        self.adapter.open_order(self.generation, order_id, order.account, contract.conId)
        self._snapshot_value('ORDERS', order.account, order_id, contract.conId)

    def contract_details(self, request_id, contract):
        self.gate()
        if request_id == SPX_DETAILS:
            if (contract.symbol, contract.secType, contract.currency, contract.exchange) != ('SPX', 'IND', 'USD', 'CBOE'):
                raise EvidenceError('WRONG_SPX_CONTRACT')
            self.underlying_rows.append(contract)
            if len(self.underlying_rows) != 1:
                raise EvidenceError('AMBIGUOUS_SPX_CONTRACT')
            self.adapter.spx_details(self.generation, contract.symbol, contract.secType, contract.currency, contract.conId, contract.exchange)
        else:
            spec = self.pending.get(request_id)
            if spec is None or request_id in self.done or spec['seen']:
                raise EvidenceError('UNKNOWN_OR_AMBIGUOUS_EXACT_CONTRACT')
            c = VerifiedOptionContract(contract.symbol, contract.secType, contract.right,
                contract.lastTradeDateOrContractMonth, contract.tradingClass, contract.multiplier,
                contract.currency, contract.strike, contract.conId)
            if (c.right != spec['right'] or c.strike != spec['strike'] or c.expiration != spec['expiration']
                    or contract.exchange not in ('SMART', 'CBOE')):
                raise EvidenceError('EXACT_CONTRACT_REQUEST_MISMATCH')
            from simulation_evidence import validate_contract
            validate_contract(c, 'CALL' if c.right == 'C' else 'PUT', self.wall(),
                              lambda t: any(t.strftime('%Y%m%d') in exps and c.strike in strikes
                                            for _, exps, strikes in self.adapter.chains))
            self.adapter.exact_option(self.generation, str(request_id), c, contract.exchange)
            spec['seen'] = True

    def contract_end(self, request_id):
        self.gate()
        if request_id == SPX_DETAILS:
            if len(self.underlying_rows) != 1:
                raise EvidenceError('SPX_CONTRACT_MISSING')
            self.adapter.spx_details_end(self.generation)
        else:
            if request_id not in self.pending or not self.pending[request_id]['seen'] or request_id in self.done:
                raise EvidenceError('EXACT_CONTRACT_MISSING_OR_REPEATED_END')
            self.done.add(request_id)
            if len(self.done) == len(self.pending):
                self.adapter.exact_option_end(self.generation)
                for direction in ('CALL', 'PUT'):
                    stream = ReadOnlyMarketStream(self.adapter)
                    generation = stream.begin_session(direction, self.wall())
                    stream.register_spx(generation, SPX_QUOTE)
                    for key, (contract, _) in self.adapter.exact.items():
                        if contract in stream.contracts:
                            stream.register_option(generation, int(key)+1000, contract)
                    self.streams[direction] = stream
                    self.bridges[direction] = IBKRMarketCallbacks(stream, generation, self.wall, self.mono)
                    if self.spx_type is not None:
                        self.bridges[direction].marketDataType(SPX_QUOTE, self.spx_type)

    def chain(self, req_id, exchange, underlying, trading_class, multiplier, expirations, strikes):
        self.gate()
        if req_id != CHAIN:
            raise EvidenceError('UNKNOWN_CHAIN_REQUEST')
        self.adapter.chain(self.generation, exchange, underlying, trading_class, multiplier, expirations, strikes)

    def chain_end(self, req_id):
        self.gate()
        if req_id != CHAIN:
            raise EvidenceError('UNKNOWN_CHAIN_REQUEST')
        self.adapter.chain_end(self.generation)

    def plan_options(self):
        self.gate()
        if (self.pending or not self.bootstrap or self.spx_type != 1 or not 0 <= self.mono()-self.bootstrap['receipt'] <= MAX_AGE_SECONDS
                or self.bootstrap and not 0 <= (self.wall()-self.bootstrap['wall_receipt']).total_seconds() <= MAX_AGE_SECONDS):
            raise EvidenceError('FRESH_LIVE_SPX_LOOKUP_BOOTSTRAP_REQUIRED')
        strikes = self.adapter.listed_atm_strikes(self.generation, 'CALL', self.wall(), self.bootstrap['price'])
        today = self.wall().astimezone(NY).strftime('%Y%m%d')
        for right in ('C', 'P'):
            for strike in strikes:
                self.pending[100+len(self.pending)] = dict(right=right, strike=strike, expiration=today, seen=False)
        return dict(self.pending)

    def market(self, method, request_id, *args):
        self.gate()
        if request_id == SPX_QUOTE:
            if method == 'marketDataType':
                self.spx_type = args[0]
                if type(self.spx_type) is not int or self.spx_type not in (1, 2, 3, 4):
                    self.bootstrap = None
                    raise EvidenceError('INVALID_MARKET_DATA_CLASSIFICATION')
                if self.spx_type != 1:
                    self.bootstrap = None
            elif method == 'tickPrice' and args[0] in (66, 67, 68):
                self.bootstrap = None
                raise EvidenceError('DELAYED_SPX_BOOTSTRAP_REJECTED')
            elif method == 'tickPrice' and args[0] == 4:
                price = args[1]
                if self.spx_type == 1 and isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and price > 0:
                    self.bootstrap = {'price': price, 'receipt': self.mono(), 'wall_receipt': self.wall(), 'source_time': None}
                else:
                    self.bootstrap = None
        recipients = [bridge for name, bridge in self.bridges.items() if request_id in self.streams[name].requests]
        if not recipients and request_id != SPX_QUOTE:
            raise EvidenceError('UNKNOWN_MARKET_REQUEST')
        for bridge in recipients:
            getattr(bridge, method)(request_id, *args)

    def timestamp(self, request_id, tick_type, value):
        self.gate()
        known = request_id == SPX_QUOTE or any(request_id in s.requests for s in self.streams.values())
        if not known:
            raise EvidenceError('UNKNOWN_TIMESTAMP_REQUEST')
        if tick_type not in (45, 48, 77, 85, 88):
            return
        # These timestamps describe trades, never associate them with bid/ask or Greeks.
        try:
            parts = value.split(';')
            raw = parts[2] if tick_type in (48, 77) else value
            number = float(raw)
            timestamp = datetime.fromtimestamp(number / (1000 if tick_type in (48, 77, 85) else 1), timezone.utc)
        except (ValueError, TypeError, IndexError, OverflowError, OSError):
            raise EvidenceError('INVALID_TRADE_TIMESTAMP')
        self.trade_timestamps.append({'request_id': request_id, 'tick_type': tick_type,
            'trade_source_time': timestamp.isoformat(), 'local_receipt_utc': self.wall().isoformat(),
            'local_receipt_monotonic': self.mono(), 'proves_quote_source_time': False,
            'trade_source_age_seconds': (self.wall()-timestamp).total_seconds(),
            'trade_timestamp_recent': -MAX_FUTURE_SECONDS <= (self.wall()-timestamp).total_seconds() <= MAX_AGE_SECONDS})
        self.trade_timestamps = self.trade_timestamps[-256:]

    def error(self, req_id, code):
        self.errors.append({'request_id': req_id, 'code': code,
                            'meaning': ERROR_NAMES.get(code, 'INFORMATION' if code in INFO else 'UNEXPECTED_IBKR_ERROR')})
        self.errors = self.errors[-256:]
        if code not in INFO:
            self.fail('IBKR_ERROR_REQUIRES_NEW_VALIDATION')

    def report(self):
        with self.lock:
            reasons = ['ORDER_EXECUTION_DISABLED', 'TWS_READ_ONLY_SETTING_NOT_API_ATTESTABLE',
                       'QUOTE_AND_GREEKS_SOURCE_TIMESTAMPS_UNPROVEN', 'FULL_PAPER_LEDGER_RECONCILIATION_NOT_VALIDATED']
            valid_session = True
            try:
                self.gate()
            except (EvidenceError, PaperBrokerError):
                valid_session = False
                reasons.append(self.failure or 'SESSION_UNVERIFIED_EXPIRED_OR_CLOSED')
            account = self.adapter.observe(self.wall(), self.mono()).account
            account_fresh = account is not None and account.complete
            if account_fresh:
                from simulation_evidence import conservative_usable_funds
                try:
                    conservative_usable_funds(account, self.approval.account, self.mono())
                except EvidenceError:
                    account_fresh = False
            if not account_fresh:
                reasons.append('ACCOUNT_FUNDS_INCOMPLETE_OR_STALE')
            evidence = self.adapter.observe(self.wall(), self.mono())
            reasons.extend(evidence.reasons)
            rows, rankings = {}, {}
            for name, stream in self.streams.items():
                try:
                    rows[name] = stream.diagnostics(stream.generation, self.wall(), self.mono())
                    rankings[name] = stream.rank(stream.generation, self.wall(), self.mono(), account)
                except EvidenceError:
                    reasons.append('STREAM_DISCONNECTED_OR_INVALID')
            if any(not event['trade_timestamp_recent'] for event in self.trade_timestamps):
                reasons.append('TRADE_SOURCE_TIMESTAMP_STALE_OR_FUTURE')
            if not rows:
                reasons.append('EXACT_CALL_PUT_STREAMS_NOT_ESTABLISHED')
            # A diagnostic local-receipt spread is never evidence for the source gate.
            receipts = [v['monotonic_receipt'] for group in rows.values() for row in group
                        for v in list(row['values'].values()) + ([row['greeks']] if row['greeks'] else [])]
            for group in rows.values():
                for row in group:
                    reasons.extend(row['block_reasons'])
            return {'mode': 'PAPER READ-ONLY VALIDATION / NO ORDERS', 'executable': False,
                'connection_attempted': self.started, 'transport': dict(self.transport_info) if self.transport_info else None,
                'submission_enabled': False, 'paper_account_matched': valid_session, 'session_failure': self.failure,
                'operator_settings_verification_required': True, 'read_only_api_independently_verified': False,
                'account_funds_fresh': account_fresh, 'funds_fresh_at_summary_completion': self.funds_fresh_at_completion,
                'exact_contracts': [asdict(c) for c, _ in self.adapter.exact.values()], 'fund_tags_received': [tag for tag in SIZING_TAGS if (self.approval.account, tag) in self.adapter.summary],
                'streams': rows, 'rankings': rankings, 'trade_timestamps': list(self.trade_timestamps),
                'source_synchronization': 'NOT_PROVABLE_FROM_RECEIPT_OR_TRADE_TIMESTAMPS',
                'required_source_skew_seconds': MAX_SYNC_SECONDS, 'measured_source_skew_seconds': None,
                'diagnostic_local_receipt_spread_seconds': max(receipts)-min(receipts) if receipts else None,
                'snapshot': self.snapshot.status(self.mono()) if self.snapshot else None,
                'broker_errors': list(self.errors), 'block_reasons': list(dict.fromkeys(reasons))}
