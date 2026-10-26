"""Authenticated, durable TradingView RESEARCH inbox. Never broker evidence."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

MAX_BODY = 4096
ID = re.compile(r'[A-Za-z0-9_.:~+\-]{1,160}')


class TradingViewError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise TradingViewError('EXPLICIT_TIMEZONE_TIMESTAMP_REQUIRED')


def price(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 64:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or not 0 < result < Decimal('1e9'):
            raise ValueError
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise TradingViewError('INVALID_SPX_PRICE')


def canonical(value):
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError('UNSUPPORTED_AUDIT_VALUE')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=encode)


def parse_payload(body, symbol, stream_id):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TradingViewError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    try:
        raw = json.loads(body.decode('utf-8'), object_pairs_hook=pairs, parse_float=Decimal,
                         parse_constant=lambda _: (_ for _ in ()).throw(TradingViewError('NONFINITE_JSON')))
    except (UnicodeError, ValueError, RecursionError, InvalidOperation, OverflowError) as exc:
        if isinstance(exc, TradingViewError):
            raise
        raise TradingViewError('INVALID_JSON')
    required = {'version', 'source', 'stream_id', 'event_id', 'symbol', 'event_time', 'event_time_kind', 'price'}
    if not isinstance(raw, dict) or set(raw) not in (required, required | {'bar_time'}):
        raise TradingViewError('EXACT_PAYLOAD_FIELDS_REQUIRED')
    if type(raw['version']) is not int or raw['version'] != 1 or raw['source'] != 'TRADINGVIEW':
        raise TradingViewError('UNSUPPORTED_SOURCE_OR_VERSION')
    if raw['symbol'] != symbol or raw['stream_id'] != stream_id:
        raise TradingViewError('SYMBOL_OR_STREAM_MISMATCH')
    if raw['event_time_kind'] != 'ALERT_FIRE':
        raise TradingViewError('ALERT_FIRE_TIME_REQUIRED_NOT_EXCHANGE_TIME')
    if not isinstance(raw['event_id'], str) or not ID.fullmatch(raw['event_id']):
        raise TradingViewError('UNIQUE_EVENT_ID_REQUIRED')
    stamp = timestamp(raw['event_time'])
    with localcontext() as context:
        context.prec = 64  # Input length is bounded; normalization must not round.
        numeric = price(raw['price']).normalize()
        price_text = format(numeric, 'f') if numeric.as_tuple().exponent >= -12 else str(numeric)
    normalized = dict(raw, event_time=stamp.isoformat(), price=price_text)
    if 'bar_time' in raw:
        bar_time = timestamp(raw['bar_time'])
        if bar_time > stamp:
            raise TradingViewError('BAR_TIME_AFTER_ALERT_TIME')
        normalized['bar_time'] = bar_time.isoformat()
    return raw, normalized


class TradingViewInbox:
    """Append-only hash-chained receipt journal; no secrets stored in the DB.

    Freshness bounds govern research ingestion only, never IBKR/trading gates.
    Out-of-order/equal-time distinct IDs and stale/future events are quarantined.
    A changed duplicate disputes that identity and blocks ordered replay.
    """
    def __init__(self, path, token, symbol='SP:SPX', stream_id='spx-1m-close-v1',
                 wall_clock=None, monotonic_clock=None, max_event_age=60, max_future=1):
        if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}', token)
                or symbol != 'SP:SPX' or not isinstance(stream_id, str) or not ID.fullmatch(stream_id)):
            raise TradingViewError('EXPLICIT_SPX_STREAM_AND_STRONG_AUTH_TOKEN_REQUIRED')
        for bound in (max_event_age, max_future):
            if type(bound) not in (int, float) or not math.isfinite(bound) or bound < 0:
                raise TradingViewError('INVALID_RESEARCH_INGESTION_BOUNDS')
        self.path, self._token = Path(path), token
        self.symbol, self.stream_id = symbol, stream_id
        self.max_event_age, self.max_future = max_event_age, max_future
        self.wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self.mono = monotonic_clock or time.monotonic
        self.process_id = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binding = hashlib.sha256(canonical({'version': 1, 'symbol': symbol, 'stream_id': stream_id,
            'max_event_age': max_event_age, 'max_future': max_future}).encode()).hexdigest()
        with self._db() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {'tv_meta', 'tv_receipts'}:
                raise TradingViewError('REFUSE_UNRELATED_DATABASE')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS tv_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL, sequence INTEGER NOT NULL, head TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS tv_receipts (sequence INTEGER PRIMARY KEY, record TEXT NOT NULL, digest TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO tv_meta VALUES (1,?,0,'')", (self.binding,))
            self._load(db)
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=.25, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
            if db.in_transaction:
                db.execute('COMMIT')
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def authenticate(self, authorization):
        expected = ('Bearer '+self._token).encode('ascii')
        if (not isinstance(authorization, str) or len(authorization) > 160
                or not hmac.compare_digest(authorization.encode('utf-8'), expected)):
            raise TradingViewError('AUTHENTICATION_REQUIRED', 401)

    def _load(self, db):
        meta = db.execute('SELECT binding,sequence,head FROM tv_meta WHERE singleton=1').fetchone()
        if meta is None or meta[0] != self.binding:
            raise TradingViewError('INBOX_BINDING_MISMATCH', 409)
        head, records, events, disputed, watermark = '', [], {}, set(), None
        for number, (sequence, record, digest) in enumerate(db.execute('SELECT sequence,record,digest FROM tv_receipts ORDER BY sequence'), 1):
            expected = hashlib.sha256((head+'|'+str(sequence)+'|'+record).encode()).hexdigest()
            if sequence != number or digest != expected:
                raise TradingViewError('AUDIT_INTEGRITY_FAILURE', 409)
            item = json.loads(record)
            if canonical(item) != record:
                raise TradingViewError('AUDIT_INTEGRITY_FAILURE', 409)
            head = digest
            records.append(item)
            if item['outcome'] in {'ACCEPTED', 'STALE', 'FUTURE', 'LATE', 'UNORDERED'}:
                events[item['event']['event_id']] = item
            if item['outcome'] == 'CONFLICT':
                disputed.add(item['event']['event_id'])
            if item['outcome'] == 'ACCEPTED':
                watermark = item['event']['event_time']
        if (len(records), head) != meta[1:]:
            raise TradingViewError('AUDIT_HEAD_MISMATCH', 409)
        return records, events, disputed, watermark, head

    def _append(self, db, state, record):
        records, _, _, _, head = state
        sequence = len(records)+1
        encoded = canonical(record)
        digest = hashlib.sha256((head+'|'+str(sequence)+'|'+encoded).encode()).hexdigest()
        db.execute('INSERT INTO tv_receipts VALUES (?,?,?)', (sequence, encoded, digest))
        db.execute('UPDATE tv_meta SET sequence=?,head=? WHERE singleton=1', (sequence, digest))
        return sequence

    def ingest(self, body, authorization):
        self.authenticate(authorization)
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY:
            raise TradingViewError('INVALID_BODY_SIZE', 413)
        now = timestamp(self.wall().isoformat())
        mono = self.mono()
        if type(mono) not in (int, float) or not math.isfinite(mono) or mono < 0:
            raise TradingViewError('INVALID_LOCAL_RECEIPT_CLOCK')
        receipt = {'local_receipt_utc': now.isoformat(), 'local_receipt_monotonic': mono,
                   'receipt_process_id': self.process_id, 'body_sha256': hashlib.sha256(body).hexdigest()}
        invalid = None
        try:
            raw, event = parse_payload(body, self.symbol, self.stream_id)
        except TradingViewError as exc:
            invalid = exc
            raw = event = None
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._load(db)
            _, events, _, watermark, _ = state
            if invalid:
                outcome = 'INVALID'
            elif event['event_id'] in events:
                outcome = 'DUPLICATE' if events[event['event_id']]['event'] == event else 'CONFLICT'
            else:
                age = (now-timestamp(event['event_time'])).total_seconds()
                outcome = ('STALE' if age > self.max_event_age else 'FUTURE' if age < -self.max_future else
                           'LATE' if watermark and timestamp(event['event_time']) < timestamp(watermark) else
                           'UNORDERED' if watermark and event['event_time'] == watermark else 'ACCEPTED')
            record = dict(receipt, outcome=outcome, event=event, received_payload=raw,
                          received_json=body.decode('utf-8') if raw is not None else None,
                          reason=invalid.code if invalid else None)
            sequence = self._append(db, state, record)
        if invalid:
            raise invalid
        return {'outcome': outcome, 'receipt_sequence': sequence, 'event_id': event['event_id'],
                'executable': False, 'ready': False, 'exchange_time': None,
                'timestamp_provenance': 'TRADINGVIEW_ALERT_FIRE_NOT_PROVEN_QUOTE_SOURCE'}

    def audit(self):
        with self._db() as db:
            db.execute('BEGIN')
            return self._load(db)[0]

    def replay_events(self):
        with self._db() as db:
            db.execute('BEGIN')
            records, _, disputed, _, _ = self._load(db)
        # Quarantined data may hide an activation or peak. Never silently filter it
        # into a supposedly complete ordered replay; callers must resolve the gap.
        if disputed or any(r['outcome'] in {'STALE', 'FUTURE', 'LATE', 'UNORDERED', 'INVALID'} for r in records):
            raise TradingViewError('QUARANTINED_OR_DISPUTED_STREAM_REQUIRES_REVIEW', 409)
        return [dict(r['event'], local_receipt_utc=r['local_receipt_utc'],
                     local_receipt_monotonic=r['local_receipt_monotonic'], receipt_process_id=r['receipt_process_id'],
                     exchange_time=None, source='TRADINGVIEW_RESEARCH')
                for r in records if r['outcome'] == 'ACCEPTED']

    def status(self):
        records = self.audit()
        outcomes = {}
        for record in records:
            outcomes[record['outcome']] = outcomes.get(record['outcome'], 0)+1
        return {'mode': 'TRADINGVIEW RESEARCH ONLY', 'ready': False, 'executable': False,
                'stream_id': self.stream_id, 'symbol': self.symbol, 'receipt_counts': outcomes,
                'exchange_timestamps_proven': False, 'continuous_tick_coverage_proven': False}
