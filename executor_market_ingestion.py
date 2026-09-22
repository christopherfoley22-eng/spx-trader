"""Pure read-only market reducer with deliberately proven provenance only.

Installed ibapi exposes opaque integer ``time`` fields but its bundled source
does not prove their epoch, timezone, origin, precision, SPX availability, or
binding to marketDataType. Raw callbacks are diagnostic only. A future
transport must supply normalized proof explicitly. This module owns no client,
socket, account, intent, or order capability.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from threading import RLock

from executor_ibkr_observation import MARKET_TYPES
from executor_redaction import safe_error_text
from simulation_evidence import (
    EvidenceClock, EvidenceError, FeedStatus, MAX_AGE_SECONDS,
    MAX_FUTURE_SECONDS, MAX_SYNC_SECONDS, OptionObservation, SPXObservation,
    VerifiedOptionContract, positive_number, validate_contract,
    validate_observation_time,
)

ROLES = ("SPX_UNDERLYING", "EXACT_SPXW_OPTION")
PURPOSES = {"SPX_UNDERLYING": "SPX_UNDERLYING_MARKET_DATA",
            "EXACT_SPXW_OPTION": "EXACT_SPXW_OPTION_MARKET_DATA"}
INFO_CODES = frozenset((2104, 2106, 2107, 2108, 2158))
PROVEN_SEMANTICS = "UTC_EVENT_TIME_SEMANTICS_PROVEN"
SPX_NORMALIZED_CALLBACK = "NORMALIZED_SPX_VALUE"
OPTION_NORMALIZED_CALLBACK = "NORMALIZED_ATOMIC_BID_ASK"
VALIDATED_IBKR_LIMITATIONS = (
    "SPX_LAST_ALLLAST_MIDPOINT_TICK_BY_TICK_UNSUPPORTED",
    "SPXW_BID_ASK_TICK_BY_TICK_UNSUPPORTED_FOR_VALIDATED_CONTRACT",
    "REQUEST_SPECIFIC_LIVE_CLASSIFICATION_NOT_OBSERVED",
    "MARKET_SOURCE_TIMESTAMP_NOT_OBSERVED",
    "VALIDATED_IBKR_PATH_CANNOT_PROVE_0_25_SECOND_SYNCHRONIZATION",
)


class IngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SPXContract:
    symbol: str
    sec_type: str
    currency: str
    con_id: int
    exchange: str


@dataclass(frozen=True)
class Request:
    request_id: int
    generation: int
    purpose: str
    role: str
    contract: object
    requested_mode: str
    classification: str = "UNAVAILABLE"
    classification_bound: bool = False
    cancelled: bool = False
    invalid: str = None
    spx: object = None
    option: object = None
    last_source: object = None
    events: tuple = ()
    diagnostics: tuple = ()


@dataclass(frozen=True)
class Provenance:
    source_utc: datetime
    receipt_utc: datetime
    receipt_monotonic: float
    source_semantics: str
    source_type: str
    callback_type: str
    source_uncertainty_seconds: float


@dataclass(frozen=True)
class PriceEvidence:
    price: float
    provenance: Provenance


@dataclass(frozen=True)
class QuoteEvidence:
    bid: float
    ask: float
    provenance: Provenance
    bid_size: object = None
    ask_size: object = None
    bid_past_low: object = None
    ask_past_high: object = None


def _mono(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IngestionError("WALL_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _valid_contract(role, contract):
    if role == "SPX_UNDERLYING":
        return (isinstance(contract, SPXContract) and contract.symbol == "SPX"
                and contract.sec_type == "IND" and contract.currency == "USD"
                and isinstance(contract.con_id, int) and not isinstance(contract.con_id, bool)
                and contract.con_id > 0 and isinstance(contract.exchange, str)
                and bool(contract.exchange.strip()))
    return (isinstance(contract, VerifiedOptionContract)
            and contract.symbol == "SPX" and contract.sec_type == "OPT"
            and contract.right in {"C", "P"} and contract.trading_class == "SPXW"
            and contract.multiplier == "100" and contract.currency == "USD"
            and positive_number(contract.strike) and isinstance(contract.con_id, int)
            and not isinstance(contract.con_id, bool) and contract.con_id > 0
            and isinstance(contract.expiration, str) and len(contract.expiration) == 8
            and contract.expiration.isdigit())


class ReadOnlyMarketIngestion:
    """Generation-bound immutable transitions; status is never executable."""

    def __init__(self):
        self.lock = RLock()
        self.generation = 0
        self.connected = False
        self.requests = {}
        self.used_ids = set()
        self.last_receipt = None
        self.global_invalid = None
        self.errors = []

    def connect(self):
        with self.lock:
            if self.connected or self.generation >= 2**63 - 1:
                self.global_invalid = "GENERATION_INVALID"
                raise IngestionError(self.global_invalid)
            self.generation += 1
            self.connected = True
            self.requests = {}
            self.last_receipt = None
            self.global_invalid = None
            return self.generation

    def disconnect(self, generation):
        with self.lock:
            if generation != self.generation or not self.connected:
                self._invalidate_all("OLD_GENERATION_OR_DISCONNECTED")
                raise IngestionError(self.global_invalid)
            self.connected = False
            self.requests = {}
            self.global_invalid = "DISCONNECTED"

    def _invalidate_all(self, reason):
        self.global_invalid = reason
        self.requests = {key: replace(value, invalid=reason, spx=None, option=None)
                         for key, value in self.requests.items()}

    def _request(self, generation, request_id):
        if generation != self.generation or not self.connected:
            self._invalidate_all("OLD_GENERATION_OR_DISCONNECTED")
            raise IngestionError(self.global_invalid)
        if (not isinstance(request_id, int) or isinstance(request_id, bool)
                or request_id not in self.requests):
            self._invalidate_all("UNKNOWN_REQUEST")
            raise IngestionError(self.global_invalid)
        request = self.requests[request_id]
        if request.cancelled:
            self._invalidate_all("POST_CANCELLATION_CALLBACK")
            raise IngestionError(self.global_invalid)
        if request.invalid or self.global_invalid:
            raise IngestionError(request.invalid or self.global_invalid)
        return request

    def _receipt(self, receipt_utc, receipt_monotonic):
        try:
            wall = _utc(receipt_utc)
        except IngestionError:
            self._invalidate_all("WALL_RECEIPT_INVALID")
            raise
        if not _mono(receipt_monotonic):
            self._invalidate_all("MONOTONIC_RECEIPT_INVALID")
            raise IngestionError(self.global_invalid)
        if self.last_receipt is not None and receipt_monotonic < self.last_receipt:
            self._invalidate_all("MONOTONIC_RECEIPT_BACKWARD")
            raise IngestionError(self.global_invalid)
        self.last_receipt = receipt_monotonic
        return wall

    def register(self, generation, request_id, purpose, role, contract,
                 requested_mode="LIVE"):
        with self.lock:
            if generation != self.generation or not self.connected or self.global_invalid:
                self._invalidate_all("OLD_GENERATION_OR_DISCONNECTED")
                raise IngestionError(self.global_invalid)
            if (not isinstance(request_id, int) or isinstance(request_id, bool)
                    or request_id < 0 or request_id in self.used_ids):
                self._invalidate_all("REQUEST_ID_REUSED_OR_INVALID")
                raise IngestionError(self.global_invalid)
            if (role not in ROLES or purpose != PURPOSES.get(role)
                    or requested_mode not in MARKET_TYPES.values()
                    or not _valid_contract(role, contract)
                    or any(item.role == role for item in self.requests.values())):
                self._invalidate_all("REQUEST_IDENTITY_INVALID")
                raise IngestionError(self.global_invalid)
            self.used_ids.add(request_id)
            self.requests[request_id] = Request(request_id, generation, purpose, role,
                                                contract, requested_mode)

    def cancel(self, generation, request_id):
        with self.lock:
            request = self._request(generation, request_id)
            self.requests[request_id] = replace(request, cancelled=True, spx=None, option=None)

    def market_data_type(self, generation, request_id, code, receipt_utc,
                         receipt_monotonic, binding_proven=False):
        """Accept classification only with independently proven request binding."""
        with self.lock:
            request = self._request(generation, request_id)
            self._receipt(receipt_utc, receipt_monotonic)
            classification = MARKET_TYPES.get(code)
            if classification is None or isinstance(code, bool):
                self.requests[request_id] = replace(request, invalid="MARKET_TYPE_UNKNOWN",
                                                    spx=None, option=None)
                return
            if binding_proven is not True:
                diagnostic = ("MARKET_DATA_TYPE", classification,
                              "REQUEST_BINDING_UNPROVEN")
                self.requests[request_id] = replace(
                    request, classification="UNAVAILABLE", classification_bound=False,
                    spx=None, option=None, diagnostics=request.diagnostics + (diagnostic,))
                return
            if request.classification != classification or not request.classification_bound:
                request = replace(request, classification=classification,
                                  classification_bound=True, spx=None, option=None,
                                  last_source=None, events=())
            self.requests[request_id] = request

    def _opaque_callback(self, generation, request_id, role, contract, raw_time,
                         callback_type, receipt_utc, receipt_monotonic, detail):
        request = self._request(generation, request_id)
        if request.role != role or contract != request.contract:
            self._invalidate_all("CALLBACK_CONTRACT_OR_ROLE_MISMATCH")
            raise IngestionError(self.global_invalid)
        self._receipt(receipt_utc, receipt_monotonic)
        if (not isinstance(raw_time, int) or isinstance(raw_time, bool) or raw_time < 0):
            self.requests[request_id] = replace(request, invalid="OPAQUE_TIME_INVALID")
            raise IngestionError("OPAQUE_TIME_INVALID")
        diagnostic = (callback_type, detail,
                      "OPAQUE_IBKR_INTEGER_AND_EVENT_IDENTITY_AMBIGUOUS_DIAGNOSTIC_ONLY")
        self.requests[request_id] = replace(
            request, diagnostics=request.diagnostics + (diagnostic,), spx=None, option=None)

    def tick_by_tick_last(self, generation, request_id, contract, tick_type,
                          raw_time, price, receipt_utc, receipt_monotonic):
        with self.lock:
            if tick_type not in (1, 2) or isinstance(tick_type, bool) or not positive_number(price):
                request = self._request(generation, request_id)
                self.requests[request_id] = replace(request, invalid="SPX_PRICE_INVALID")
                raise IngestionError("SPX_PRICE_INVALID")
            callback = "TICK_BY_TICK_LAST" if tick_type == 1 else "TICK_BY_TICK_ALL_LAST"
            self._opaque_callback(generation, request_id, "SPX_UNDERLYING", contract,
                                  raw_time, callback, receipt_utc, receipt_monotonic,
                                  "SPX_STREAM_AND_TIME_SEMANTICS_UNPROVEN")

    def tick_by_tick_bid_ask(self, generation, request_id, contract, raw_time,
                             bid, ask, receipt_utc, receipt_monotonic, bid_size=None,
                             ask_size=None, bid_past_low=None, ask_past_high=None):
        with self.lock:
            if not positive_number(bid) or not positive_number(ask) or ask < bid:
                request = self._request(generation, request_id)
                self.requests[request_id] = replace(request, invalid="OPTION_QUOTE_INVALID")
                raise IngestionError("OPTION_QUOTE_INVALID")
            self._validate_metadata(request_id, generation, bid_size, ask_size,
                                    bid_past_low, ask_past_high)
            self._opaque_callback(generation, request_id, "EXACT_SPXW_OPTION", contract,
                                  raw_time, "TICK_BY_TICK_BID_ASK", receipt_utc,
                                  receipt_monotonic, "ATOMIC_SIDES_BUT_TIME_SEMANTICS_UNPROVEN")

    def tick_by_tick_midpoint(self, generation, request_id, contract, raw_time,
                              midpoint, receipt_utc, receipt_monotonic):
        with self.lock:
            if not positive_number(midpoint):
                request = self._request(generation, request_id)
                self.requests[request_id] = replace(request, invalid="MIDPOINT_INVALID")
                raise IngestionError("MIDPOINT_INVALID")
            self._opaque_callback(generation, request_id, "SPX_UNDERLYING", contract,
                                  raw_time, "TICK_BY_TICK_MIDPOINT", receipt_utc,
                                  receipt_monotonic,
                                  "MIDPOINT_SPX_SUPPORT_AND_TIME_SEMANTICS_UNPROVEN")

    def ordinary_tick(self, generation, request_id, contract, callback_type,
                      tick_type, receipt_utc, receipt_monotonic):
        with self.lock:
            request = self._request(generation, request_id)
            if contract != request.contract or callback_type not in {"TICK_PRICE", "TICK_STRING"}:
                self._invalidate_all("CALLBACK_CONTRACT_OR_ROLE_MISMATCH")
                raise IngestionError(self.global_invalid)
            self._receipt(receipt_utc, receipt_monotonic)
            label = ("LAST_TRADE_TIME_NOT_BID_ASK" if callback_type == "TICK_STRING"
                     and tick_type in (45, 88) else "RECEIPT_ONLY")
            self.requests[request_id] = replace(
                request, diagnostics=request.diagnostics + ((callback_type, tick_type, label),))

    def _validate_metadata(self, request_id, generation, bid_size, ask_size,
                           bid_past_low, ask_past_high):
        request = self._request(generation, request_id)
        if ((bid_size is not None and (not isinstance(bid_size, int)
                                       or isinstance(bid_size, bool) or bid_size < 0))
                or (ask_size is not None and (not isinstance(ask_size, int)
                                              or isinstance(ask_size, bool) or ask_size < 0))
                or (bid_past_low is not None and not isinstance(bid_past_low, bool))
                or (ask_past_high is not None and not isinstance(ask_past_high, bool))):
            self.requests[request_id] = replace(request, invalid="OPTION_METADATA_INVALID")
            raise IngestionError("OPTION_METADATA_INVALID")

    def _normalized(self, generation, request_id, role, contract, event_id, source_utc,
                    uncertainty, semantics, callback_type, receipt_utc,
                    receipt_monotonic, fingerprint):
        request = self._request(generation, request_id)
        if request.role != role or contract != request.contract:
            self._invalidate_all("CALLBACK_CONTRACT_OR_ROLE_MISMATCH")
            raise IngestionError(self.global_invalid)
        wall = self._receipt(receipt_utc, receipt_monotonic)
        valid_uncertainty = _mono(uncertainty) and uncertainty <= MAX_SYNC_SECONDS
        if (not isinstance(event_id, str) or not event_id or len(event_id) > 128
                or semantics != PROVEN_SEMANTICS or not valid_uncertainty
                or callback_type != ({"SPX_UNDERLYING": SPX_NORMALIZED_CALLBACK,
                                      "EXACT_SPXW_OPTION": OPTION_NORMALIZED_CALLBACK}[role])):
            self.requests[request_id] = replace(request, invalid="NORMALIZED_PROVENANCE_INVALID")
            raise IngestionError("NORMALIZED_PROVENANCE_INVALID")
        try:
            source = _utc(source_utc)
            validate_observation_time(source, receipt_monotonic, wall, receipt_monotonic)
        except (EvidenceError, IngestionError) as exc:
            self.requests[request_id] = replace(request, invalid="SOURCE_TIME_INVALID")
            raise IngestionError("SOURCE_TIME_INVALID") from exc
        prior_events = dict(request.events)
        if event_id in prior_events:
            if prior_events[event_id] != fingerprint:
                self.requests[request_id] = replace(
                    request, invalid="CONTRADICTORY_EVENT_ID", spx=None, option=None)
                raise IngestionError("CONTRADICTORY_EVENT_ID")
            return request, None, None, True
        if request.last_source is not None and source < request.last_source:
            self.requests[request_id] = replace(
                request, invalid="SOURCE_TIME_BACKWARD", spx=None, option=None)
            raise IngestionError("SOURCE_TIME_BACKWARD")
        return (replace(request, events=request.events + ((event_id, fingerprint),)),
                source, wall, False)

    def proven_spx_event(self, generation, request_id, contract, event_id, source_utc,
                         source_uncertainty_seconds, price, receipt_utc,
                         receipt_monotonic, source_semantics=PROVEN_SEMANTICS,
                         callback_type=SPX_NORMALIZED_CALLBACK):
        with self.lock:
            if not positive_number(price):
                request = self._request(generation, request_id)
                self.requests[request_id] = replace(request, invalid="SPX_PRICE_INVALID",
                                                    spx=None)
                raise IngestionError("SPX_PRICE_INVALID")
            fingerprint = (source_utc, source_uncertainty_seconds, price,
                           source_semantics, callback_type)
            request, source, wall, duplicate = self._normalized(
                generation, request_id, "SPX_UNDERLYING", contract, event_id,
                source_utc, source_uncertainty_seconds, source_semantics,
                callback_type, receipt_utc, receipt_monotonic, fingerprint)
            if duplicate:
                return
            if request.classification != "LIVE" or not request.classification_bound:
                self.requests[request_id] = replace(request, last_source=source)
                return
            provenance = Provenance(source, wall, receipt_monotonic, source_semantics,
                                    "PROVEN_NORMALIZED_SOURCE", callback_type,
                                    float(source_uncertainty_seconds))
            self.requests[request_id] = replace(
                request, spx=PriceEvidence(float(price), provenance), last_source=source)

    def proven_option_quote(self, generation, request_id, contract, event_id, source_utc,
                            source_uncertainty_seconds, bid, ask, receipt_utc,
                            receipt_monotonic, source_semantics=PROVEN_SEMANTICS,
                            callback_type=OPTION_NORMALIZED_CALLBACK, bid_size=None,
                            ask_size=None, bid_past_low=None, ask_past_high=None):
        with self.lock:
            if not positive_number(bid) or not positive_number(ask) or ask < bid:
                request = self._request(generation, request_id)
                self.requests[request_id] = replace(request, invalid="OPTION_QUOTE_INVALID",
                                                    option=None)
                raise IngestionError("OPTION_QUOTE_INVALID")
            self._validate_metadata(request_id, generation, bid_size, ask_size,
                                    bid_past_low, ask_past_high)
            fingerprint = (source_utc, source_uncertainty_seconds, bid, ask,
                           source_semantics, callback_type, bid_size, ask_size,
                           bid_past_low, ask_past_high)
            request, source, wall, duplicate = self._normalized(
                generation, request_id, "EXACT_SPXW_OPTION", contract, event_id,
                source_utc, source_uncertainty_seconds, source_semantics,
                callback_type, receipt_utc, receipt_monotonic, fingerprint)
            if duplicate:
                return
            if request.classification != "LIVE" or not request.classification_bound:
                self.requests[request_id] = replace(request, last_source=source)
                return
            provenance = Provenance(source, wall, receipt_monotonic, source_semantics,
                                    "PROVEN_NORMALIZED_ATOMIC_BID_ASK", callback_type,
                                    float(source_uncertainty_seconds))
            quote = QuoteEvidence(float(bid), float(ask), provenance, bid_size, ask_size,
                                  bid_past_low, ask_past_high)
            self.requests[request_id] = replace(request, option=quote, last_source=source)

    def error(self, generation, request_id, code, message, advanced="",
              receipt_utc=None, receipt_monotonic=None):
        with self.lock:
            if generation != self.generation or not self.connected:
                self._invalidate_all("OLD_GENERATION_OR_DISCONNECTED")
                raise IngestionError(self.global_invalid)
            wall = self._receipt(receipt_utc, receipt_monotonic)
            request = self.requests.get(request_id)
            informational = (isinstance(code, int) and not isinstance(code, bool)
                             and code in INFO_CODES)
            self.errors.append({
                "request_id": request_id if isinstance(request_id, int)
                and not isinstance(request_id, bool) else None,
                "purpose": request.purpose if request else "UNKNOWN_OR_GLOBAL",
                "scope": "REQUEST" if request else "GLOBAL_OR_UNKNOWN",
                "code": code if isinstance(code, int) and not isinstance(code, bool) else None,
                "informational": informational,
                "message": safe_error_text(message),
                "advanced": safe_error_text(advanced),
                "wall_receipt_time_utc": wall.isoformat(),
                "monotonic_receipt": receipt_monotonic,
                "source_time": None,
            })
            if informational:
                return
            if request is not None:
                self.requests[request_id] = replace(
                    request, invalid="REQUEST_ERROR", spx=None, option=None)
            else:
                self._invalidate_all("GLOBAL_OR_UNKNOWN_ERROR")

    @staticmethod
    def _provenance_status(evidence):
        if evidence is None:
            return {"source_semantics": None, "source_type": None,
                    "callback_type": None, "normalized_source_time_utc": None,
                    "wall_receipt_time_utc": None, "monotonic_receipt": None,
                    "source_uncertainty_seconds": None}
        item = evidence.provenance
        return {"source_semantics": item.source_semantics,
                "source_type": item.source_type,
                "callback_type": item.callback_type,
                "normalized_source_time_utc": item.source_utc.isoformat(),
                "wall_receipt_time_utc": item.receipt_utc.isoformat(),
                "monotonic_receipt": item.receipt_monotonic,
                "source_uncertainty_seconds": item.source_uncertainty_seconds}

    def status(self, now_wall, now_monotonic, direction=None,
               session_has_expiration=None):
        with self.lock:
            reasons = []
            by_role = {request.role: request for request in self.requests.values()}
            spx_req, opt_req = (by_role.get(role) for role in ROLES)
            if not self.connected or self.global_invalid:
                reasons.append(self.global_invalid or "DISCONNECTED")
            if not _mono(now_monotonic):
                reasons.append("MONOTONIC_NOW_INVALID")
            try:
                now_utc = _utc(now_wall)
            except IngestionError:
                now_utc = None
                reasons.append("WALL_NOW_INVALID")
            for label, request in (("SPX", spx_req), ("OPTION", opt_req)):
                if request is None:
                    reasons.append(label + "_REQUEST_MISSING")
                elif request.invalid or request.cancelled:
                    reasons.append(label + "_REQUEST_INVALID")
                elif request.classification != "LIVE" or not request.classification_bound:
                    reasons.extend((label + "_" + request.classification,
                                    label + "_LIVE_REQUEST_BINDING_UNPROVEN"))
                elif request.requested_mode != "LIVE":
                    reasons.append(label + "_LIVE_NOT_REQUESTED")
            spx = spx_req.spx if spx_req else None
            option = opt_req.option if opt_req else None
            if spx is None:
                reasons.append("SPX_SOURCE_UNAVAILABLE")
            if option is None:
                reasons.append("OPTION_BID_ASK_SOURCE_UNAVAILABLE")
            if not reasons:
                try:
                    if direction not in {"CALL", "PUT"} or session_has_expiration is None:
                        raise EvidenceError("SESSION_CONTRACT_VALIDATION_UNAVAILABLE")
                    try:
                        validate_contract(opt_req.contract, direction, now_utc,
                                          session_has_expiration)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        if isinstance(exc, EvidenceError):
                            raise
                        raise EvidenceError("SESSION_VALIDATOR_FAILURE") from exc
                    for evidence in (spx, option):
                        validate_observation_time(evidence.provenance.source_utc,
                                                  evidence.provenance.receipt_monotonic,
                                                  now_utc, now_monotonic)
                        wall_age = (now_utc - evidence.provenance.receipt_utc).total_seconds()
                        wall_future = (evidence.provenance.receipt_utc - now_utc).total_seconds()
                        if (not math.isfinite(wall_age) or wall_age > MAX_AGE_SECONDS
                                or wall_future > MAX_FUTURE_SECONDS):
                            raise EvidenceError("WALL_RECEIPT_STALE_OR_FUTURE")
                    source_delta = abs((spx.provenance.source_utc
                                        - option.provenance.source_utc).total_seconds())
                    worst = (source_delta + spx.provenance.source_uncertainty_seconds
                             + option.provenance.source_uncertainty_seconds)
                    if worst > MAX_SYNC_SECONDS:
                        raise EvidenceError("SOURCE_PRECISION_CANNOT_PROVE_SYNCHRONIZATION")
                    EvidenceClock().validate_pair(
                        direction,
                        SPXObservation(spx.price, FeedStatus.LIVE,
                                       spx.provenance.source_utc,
                                       spx.provenance.receipt_monotonic),
                        OptionObservation(opt_req.contract, opt_req.contract.con_id,
                                          option.bid, option.ask, FeedStatus.LIVE,
                                          option.provenance.source_utc,
                                          option.provenance.receipt_monotonic),
                        now_utc, now_monotonic, session_has_expiration)
                    if self.last_receipt is not None and now_monotonic < self.last_receipt:
                        raise EvidenceError("MONOTONIC_CLOCK_ROLLBACK")
                except (EvidenceError, TypeError, ValueError) as exc:
                    reasons.append("PAIR_PROVENANCE_INVALID: " + safe_error_text(str(exc)))
            requests = {}
            for role, request in (("SPX_UNDERLYING", spx_req),
                                  ("EXACT_SPXW_OPTION", opt_req)):
                evidence = (request.spx or request.option) if request else None
                requests[role] = {
                    "request_id": request.request_id if request else None,
                    "purpose": request.purpose if request else None,
                    "classification": request.classification if request else "UNAVAILABLE",
                    "classification_binding_proven": request.classification_bound
                    if request else False,
                    "requested_mode": request.requested_mode if request else None,
                    "callback_diagnostics": request.diagnostics if request else (),
                    "provenance": self._provenance_status(evidence),
                }
            return {
                "mode": "READ-ONLY MARKET INGESTION / NO ORDERS",
                "generation": self.generation,
                "connected": self.connected,
                "authoritative_provenance_path": "executor_market_ingestion",
                "runtime_dependencies": (
                    "SPX_INDEX_CALLBACK_STREAM_UNPROVEN",
                    "TICK_BY_TICK_MIDPOINT_RELEVANCE_UNPROVEN",
                    "IBKR_INTEGER_TIME_SEMANTICS_UNPROVEN",
                    "TICK_BY_TICK_TIME_PRECISION_INSUFFICIENT_FOR_0_25_SECOND_GATE",
                    "TICK_BY_TICK_MARKET_DATA_TYPE_BINDING_UNPROVEN",
                    "ENTITLEMENT_BACKED_CALLBACK_AVAILABILITY_UNPROVEN",
                ) + VALIDATED_IBKR_LIMITATIONS,
                "requests": requests,
                "source_pair_valid": not reasons,
                "block_reasons": tuple(dict.fromkeys(reasons)),
                "errors": tuple(self.errors),
                "executable": False,
            }
