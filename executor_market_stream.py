"""Read-only IBKR-shaped streaming callbacks and source-gated ATM/delta ranking.

No EClient, socket, account connection, order API, or execution permit exists.
Subscription manifests are data for a future separately approved transport.
Raw callbacks retain local receipts and NEVER enter the proven-source path.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import math
from threading import RLock
import time
from executor_contract_selection import atm_delta_rank

from executor_ibkr_observation import MARKET_TYPES, ReadOnlyIBKREvidenceAdapter
from executor_market_ingestion import (
    INFO_CODES, IngestionError, OptionGreeks, PURPOSES, ReadOnlyMarketIngestion,
    SPXContract, validate_greeks,
)
from safe_sizing import MAX_CONTRACTS, max_affordable_contracts
from simulation_evidence import (
    EvidenceError, MAX_AGE_SECONDS, MAX_FUTURE_SECONDS, MAX_SYNC_SECONDS,
    conservative_usable_funds,
)


class ReadOnlyMarketStream:
    """Generation/request-bound streaming state; all results are non-executable."""

    def __init__(self, adapter: ReadOnlyIBKREvidenceAdapter):
        if not isinstance(adapter, ReadOnlyIBKREvidenceAdapter):
            raise EvidenceError("Existing IBKR evidence adapter required")
        self.adapter = adapter
        self.lock = RLock()
        self.generation = 0
        self.used_ids = set()
        self.connected = False
        self.requests = {}
        self.pairs = {}
        self.failure = None
        self.last_receipt = None

    def begin_session(self, direction, now_wall):
        """Begin an offline callback lifecycle, not a broker connection."""
        with self.lock:
            if self.connected:
                raise EvidenceError("End the previous streaming session first")
            if direction not in {"CALL", "PUT"}:
                raise EvidenceError("Direction required")
            contracts = self.adapter.current_spxw_contracts(self.adapter.generation, direction, now_wall)
            if not contracts:
                raise EvidenceError("No exact current-day SPXW contracts")
            self.generation += 1
            self.adapter_generation = self.adapter.generation
            self.direction = direction
            self.contracts = contracts
            self.connected = True
            self.failure = None
            self.last_receipt = None
            self.requests, self.pairs = {}, {}
            return self.generation

    def _check(self, generation):
        if (not self.connected or generation != self.generation
                or self.adapter.generation != self.adapter_generation
                or not self.adapter.connected or self.adapter.invalid):
            self.failure = "DISCONNECTED_OR_OLD_CALLBACK_GENERATION"
            raise EvidenceError(self.failure)
        if self.failure:
            raise EvidenceError(self.failure)

    def end_session(self, generation):
        with self.lock:
            if generation != self.generation or not self.connected:
                raise EvidenceError("Disconnected or old session close")
            for pair in self.pairs.values():
                if pair.connected:
                    pair.disconnect(pair.generation)
            self.connected = False
            self.requests, self.pairs = {}, {}

    def _register(self, generation, request_id, role, contract):
        self._check(generation)
        if (not isinstance(request_id, int) or isinstance(request_id, bool)
                or request_id < 0 or request_id in self.used_ids):
            raise EvidenceError("Market request ID reused or invalid")
        self.used_ids.add(request_id)
        self.requests[request_id] = {"role": role, "contract": contract,
                                     "classification": "UNAVAILABLE", "values": {},
                                     "greeks": None, "greeks_error": None, "error": None}

    def register_spx(self, generation, request_id):
        with self.lock:
            self._check(generation)
            if self.requests:
                raise EvidenceError("Register exactly one SPX stream first")
            symbol, sec_type, currency, con_id, exchange = self.adapter.underlying
            self._register(generation, request_id, "SPX_UNDERLYING",
                           SPXContract(symbol, sec_type, currency, con_id, exchange))
            self.spx_id = request_id

    def register_option(self, generation, request_id, contract):
        with self.lock:
            self._check(generation)
            if not self.requests or contract not in self.contracts:
                raise EvidenceError("Exact option not qualified for this session")
            if any(item["contract"] == contract for item in self.requests.values()):
                raise EvidenceError("Duplicate exact option stream")
            self._register(generation, request_id, "EXACT_SPXW_OPTION", contract)
            pair = ReadOnlyMarketIngestion()
            pair_generation = pair.connect()  # Pure reducer state; owns no broker client.
            for req_id in (self.spx_id, request_id):
                item = self.requests[req_id]
                pair.register(pair_generation, req_id, PURPOSES[item["role"]], item["role"], item["contract"])
            self.pairs[request_id] = pair

    def subscription_manifest(self, generation):
        with self.lock:
            self._check(generation)
            return tuple({"api": "reqMktData", "request_id": request_id,
                          "contract": asdict(item["contract"]), "generic_ticks": "",
                          "snapshot": False, "regulatory_snapshot": False,
                          "requested_market_data_type": 1}
                         for request_id, item in self.requests.items())

    def _callback(self, generation, request_id, wall, mono):
        self._check(generation)
        if (not isinstance(request_id, int) or isinstance(request_id, bool)
                or request_id not in self.requests):
            self.failure = "UNKNOWN_MARKET_REQUEST"
            raise EvidenceError(self.failure)
        if (not isinstance(wall, datetime) or wall.tzinfo is None
                or not isinstance(mono, (int, float)) or isinstance(mono, bool)
                or not math.isfinite(mono) or mono < 0
                or self.last_receipt is not None and mono < self.last_receipt):
            self.failure = "INVALID_OR_BACKWARD_RECEIPT_CLOCK"
            raise EvidenceError(self.failure)
        self.last_receipt = mono
        return self.requests[request_id]

    def _reducers(self, request_id):
        return tuple(self.pairs.values()) if request_id == self.spx_id else (self.pairs[request_id],)

    def market_data_type(self, generation, request_id, code, wall, mono):
        with self.lock:
            item = self._callback(generation, request_id, wall, mono)
            classification = MARKET_TYPES.get(code) if isinstance(code, int) and not isinstance(code, bool) else None
            if classification != item["classification"] or classification != "LIVE":
                item["values"], item["greeks"] = {}, None
                item["greeks_error"] = None
            item["classification"] = classification or "UNAVAILABLE"
            for pair in self._reducers(request_id):
                pair.market_data_type(pair.generation, request_id, code, wall, mono,
                                      binding_proven=False)

    def tick_price(self, generation, request_id, tick_type, price, wall, mono):
        with self.lock:
            item = self._callback(generation, request_id, wall, mono)
            fields = {4: "price"} if item["role"] == "SPX_UNDERLYING" else {1: "bid", 2: "ask"}
            field = fields.get(tick_type) if not isinstance(tick_type, bool) else None
            if tick_type in (66, 67, 68):
                item["values"], item["greeks"], item["error"] = {}, None, "DELAYED_PRICE_TICK"
                raise EvidenceError(item["error"])
            if field is None:
                return  # Last/mark prices are never substituted for option bid/ask.
            if (item["classification"] != "LIVE" or not isinstance(price, (int, float))
                    or isinstance(price, bool) or not math.isfinite(price) or price <= 0):
                item["values"].pop(field, None)
                item["error"] = "NONLIVE_OR_INVALID_PRICE"
                raise EvidenceError(item["error"])
            item["values"][field] = {"value": price, "source_time": None,
                                      "exchange_time": None, "wall_receipt_time_utc": wall.astimezone(timezone.utc),
                                      "monotonic_receipt": mono}
            for pair in self._reducers(request_id):
                pair.ordinary_tick(pair.generation, request_id, item["contract"],
                                    "TICK_PRICE", tick_type, wall, mono)

    def option_computation(self, generation, request_id, tick_type, greeks, wall, mono):
        with self.lock:
            item = self._callback(generation, request_id, wall, mono)
            if item["role"] != "EXACT_SPXW_OPTION":
                item["error"] = "GREEKS_WRONG_INSTRUMENT"
                raise EvidenceError(item["error"])
            # TWS streams bid/ask/last computations alongside MODEL. Ignore
            # those known channels rather than replacing or poisoning MODEL.
            if tick_type in (10, 11, 12) and isinstance(tick_type, int) and item["classification"] == "LIVE":
                return
            item["greeks"] = None
            self.pairs[request_id].ordinary_option_computation(
                self.pairs[request_id].generation, request_id, item["contract"], tick_type, wall, mono)
            # Consistently use MODEL computations (#13), never mix bid/ask/last Greeks.
            if tick_type != 13 or isinstance(tick_type, bool) or item["classification"] != "LIVE":
                item["greeks_error"] = "MODEL_LIVE_GREEKS_REQUIRED"
                raise EvidenceError(item["greeks_error"])
            try:
                validate_greeks(item["contract"], greeks)
            except IngestionError as exc:
                item["greeks_error"] = str(exc)
                raise EvidenceError(item["greeks_error"]) from exc
            item["greeks_error"] = None
            item["greeks"] = {"values": asdict(greeks), "source_time": None, "exchange_time": None,
                               "wall_receipt_time_utc": wall.astimezone(timezone.utc), "monotonic_receipt": mono}

    def error(self, generation, request_id, code, message, wall, mono):
        with self.lock:
            self._check(generation)
            if code in INFO_CODES and not isinstance(code, bool):
                return
            if request_id not in self.requests:
                self.failure = "GLOBAL_OR_UNKNOWN_FEED_ERROR"
                raise EvidenceError(self.failure)
            item = self._callback(generation, request_id, wall, mono)
            item["values"], item["greeks"], item["error"] = {}, None, "MARKET_REQUEST_ERROR"
            for pair in self._reducers(request_id):
                pair.error(pair.generation, request_id, code, message,
                           receipt_utc=wall, receipt_monotonic=mono)

    def rank(self, generation, now_wall, now_monotonic, account_snapshot, atm_strikes=3):
        """Rank the completed nearby-strike universe by |abs(delta)-0.48|.

        No fallback to raw quotes, missing Greeks, or a farther incomplete chain.
        Even a valid ranked result is a read-only observation, never permission.
        """
        with self.lock:
            result = {"mode": "READ-ONLY SPXW MARKET STREAM / NO ORDERS", "executable": False,
                      "ranked_candidates": [], "selected": None, "block_reasons": []}
            try:
                self._check(generation)
                if not self.pairs:
                    raise EvidenceError("OPTION_STREAMS_MISSING")
                funds = conservative_usable_funds(account_snapshot, self.adapter.selected, now_monotonic)
                candidates, underlying = [], None
                for request_id, pair in self.pairs.items():
                    item = self.requests[request_id]
                    if item["error"] or item["greeks_error"] or self.requests[self.spx_id]["error"]:
                        raise EvidenceError(item["error"] or item["greeks_error"] or self.requests[self.spx_id]["error"])
                    contract = item["contract"]
                    status = pair.status(now_wall, now_monotonic, self.direction,
                                         lambda t: any(t.strftime("%Y%m%d") in expirations
                                                       and contract.strike in strikes
                                                       for _, expirations, strikes in self.adapter.chains),
                                         require_greeks=True)
                    if not status["source_pair_valid"]:
                        raise EvidenceError("; ".join(status["block_reasons"]))
                    spx = pair.requests[self.spx_id].spx
                    if underlying is not None and spx != underlying:
                        raise EvidenceError("SPX_EVIDENCE_DISAGREES_BETWEEN_CANDIDATES")
                    underlying = spx
                    option_request = pair.requests[request_id]
                    quote, greeks = option_request.option, option_request.greeks
                    qty = max_affordable_contracts(funds, quote.ask)
                    if not 0 <= qty <= MAX_CONTRACTS:
                        raise EvidenceError("INVALID_SIZED_QUANTITY")
                    candidates.append({"con_id": contract.con_id, "strike": contract.strike,
                                       "expiration": contract.expiration, "right": contract.right,
                                       "delta": greeks.delta, "bid": quote.bid, "ask": quote.ask,
                                       "quantity": qty})
                nearby = self.adapter.listed_atm_strikes(self.adapter_generation, self.direction,
                                                         now_wall, underlying.price, atm_strikes)
                if set(nearby) - {candidate["strike"] for candidate in candidates}:
                    raise EvidenceError("ATM_CANDIDATE_UNIVERSE_INCOMPLETE")
                ranked = [candidate for candidate in candidates if candidate["strike"] in nearby]
                ranked.sort(key=lambda candidate: atm_delta_rank(candidate['delta'],candidate['strike'],underlying.price,candidate['con_id']))
                result["ranked_candidates"] = ranked
                result["selected"] = next((candidate for candidate in ranked if candidate["quantity"]), None)
                if result["selected"] is None:
                    raise EvidenceError("NO_AFFORDABLE_ATM_CONTRACT")
            except (EvidenceError, IngestionError) as exc:
                result["block_reasons"].append(str(exc))
            return result

    def diagnostics(self, generation, now_wall, now_monotonic):
        """Local callback freshness is diagnostic, not source synchronization."""
        with self.lock:
            self._check(generation)
            rows = []
            for request_id, item in self.requests.items():
                fields = ("price",) if item["role"] == "SPX_UNDERLYING" else ("bid", "ask")
                reasons = [value for value in (item["error"], item["greeks_error"]) if value]
                if item["classification"] != "LIVE":
                    reasons.append("NONLIVE_MARKET_DATA")
                receipts = []
                for field in fields:
                    value = item["values"].get(field)
                    if value is None:
                        reasons.append("INCOMPLETE_QUOTE")
                    else:
                        receipts.append(value)
                if item["role"] == "EXACT_SPXW_OPTION":
                    if item["greeks"] is None:
                        reasons.append("MISSING_GREEKS")
                    else:
                        receipts.append(item["greeks"])
                    if all(field in item["values"] for field in fields):
                        if item["values"]["bid"]["value"] > item["values"]["ask"]["value"]:
                            reasons.append("CROSSED_QUOTE")
                try:
                    for receipt in receipts:
                        age = now_monotonic - receipt["monotonic_receipt"]
                        wall_age = (now_wall - receipt["wall_receipt_time_utc"]).total_seconds()
                        if (not math.isfinite(age) or not 0 <= age <= MAX_AGE_SECONDS
                                or not -MAX_FUTURE_SECONDS <= wall_age <= MAX_AGE_SECONDS):
                            reasons.append("STALE_OR_FUTURE_RECEIPT")
                    if receipts and max(r["monotonic_receipt"] for r in receipts) - min(
                            r["monotonic_receipt"] for r in receipts) > MAX_SYNC_SECONDS:
                        reasons.append("LOCAL_RECEIPTS_NOT_SYNCHRONIZED")
                except (TypeError, ValueError):
                    reasons.append("INVALID_RECEIPT_CLOCK")
                def public_receipt(value):
                    copied = dict(value)
                    copied["wall_receipt_time_utc"] = value["wall_receipt_time_utc"].isoformat()
                    if "values" in copied:
                        copied["values"] = dict(copied["values"])
                    return copied
                rows.append({"request_id": request_id, "classification": item["classification"],
                             "values": {key: public_receipt(value) for key, value in item["values"].items()},
                             "greeks": public_receipt(item["greeks"]) if item["greeks"] else None,
                             "quote_locally_usable": not reasons,
                             "block_reasons": tuple(dict.fromkeys(reasons)),
                             "source_proven": False, "executable": False})
            return tuple(rows)


class IBKRMarketCallbacks:
    """SDK-shaped inbound bridge bound to one generation; owns no broker client."""

    def __init__(self, stream, generation, wall_clock=None, monotonic_clock=None):
        self.stream, self.generation = stream, generation
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic

    def marketDataType(self, reqId, marketDataType):
        self.stream.market_data_type(self.generation, reqId, marketDataType,
                                     self.wall_clock(), self.monotonic_clock())

    def tickPrice(self, reqId, tickType, price, attrib):
        self.stream.tick_price(self.generation, reqId, tickType, price,
                                self.wall_clock(), self.monotonic_clock())

    def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta,
                              optPrice, pvDividend, gamma, vega, theta, undPrice):
        values = OptionGreeks(delta, gamma, vega, theta, impliedVol, optPrice, undPrice, pvDividend)
        self.stream.option_computation(self.generation, reqId, tickType, values,
                                        self.wall_clock(), self.monotonic_clock())

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.stream.error(self.generation, reqId, errorCode, errorString,
                           self.wall_clock(), self.monotonic_clock())

    def connectionClosed(self):
        self.stream.end_session(self.generation)
