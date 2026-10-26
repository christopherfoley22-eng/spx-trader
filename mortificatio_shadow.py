"""Offline, durable and non-executable MORTIFICATIO shadow mode."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from mortificatio_pre4_experiment import (
    BASELINE, CONFIRMED_STOP, EARLY_FAILURE, SyntheticQuote, run_policy,
    _decimal, _timestamp,
)

MAX_SYNC_SECONDS = 0.25
ALLOWED_DATA_QUALITY = {"LIVE", "DELAYED", "FROZEN", "STALE", "MISSING", "SYNTHETIC"}
REQUIRED_CONTRACT_FIELDS = {"symbol", "sec_type", "right", "expiration", "strike", "trading_class", "multiplier", "currency", "con_id"}
POST5_PULLBACK_RULES = {"LOCKED_PULLBACK_3": 3.0, "CANDIDATE_PULLBACK_2": 2.0, "CANDIDATE_PULLBACK_4": 4.0}
POLICIES = (BASELINE, EARLY_FAILURE, CONFIRMED_STOP)
RULE_VERSION = "MORTIFICATIO_SHADOW_RULES_V1"

@dataclass(frozen=True)
class ShadowObservation:
    timestamp: str
    spx: object
    option_bid: object
    option_ask: object
    source_timestamp: str = None
    data_quality: str = "MISSING"
    quote_status: str = "UNKNOWN"
    evidence_origin: str = "SYNTHETIC_FIXTURE"

class ShadowError(ValueError):
    pass

class ShadowObservationSource:
    """Read-only source interface; implementations return local evidence only."""
    def observations(self):
        raise NotImplementedError

class FixtureObservationSource(ShadowObservationSource):
    """Loads a versioned local JSON fixture, never a broker feed."""
    def __init__(self, path): self.path = Path(path)
    def observations(self):
        try: payload = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc: raise ShadowError("fixture cannot be read") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1: raise ShadowError("unsupported fixture version")
        rows = payload.get("observations")
        if not isinstance(rows, list): raise ShadowError("fixture observations are malformed")
        return [observation_from_dict(row) for row in rows]

def observation_from_dict(value):
    if not isinstance(value, dict) or not {"timestamp", "spx", "option_bid", "option_ask"} <= set(value):
        raise ShadowError("observation fields are incomplete")
    return ShadowObservation(value["timestamp"], value["spx"], value["option_bid"], value["option_ask"], value.get("source_timestamp"), value.get("data_quality", "MISSING"), value.get("quote_status", "UNKNOWN"), value.get("evidence_origin", "SYNTHETIC_FIXTURE"))

def _validate_observation(item):
    if not isinstance(item, ShadowObservation): raise ShadowError("observation is malformed")
    receipt = _timestamp(item.timestamp, "observation timestamp")
    if item.data_quality not in ALLOWED_DATA_QUALITY: raise ShadowError("unknown data quality")
    spx, bid, ask = (_decimal(item.spx, "SPX"), _decimal(item.option_bid, "option bid"), _decimal(item.option_ask, "option ask"))
    if spx <= 0 or bid <= 0 or ask <= 0 or bid > ask: raise ShadowError("invalid SPX or option quote")
    if item.source_timestamp is None: return receipt, None, (spx, bid, ask)
    source = _timestamp(item.source_timestamp, "source timestamp")
    if abs((source - receipt).total_seconds()) > MAX_SYNC_SECONDS: raise ShadowError("source and observation timestamps exceed synchronization limit")
    return receipt, source, (spx, bid, ask)

def _contract_view(contract):
    if contract is None: return None
    if not isinstance(contract, dict): raise ShadowError("selected contract is malformed")
    allowed = ("symbol", "sec_type", "right", "expiration", "strike", "trading_class", "multiplier", "currency", "con_id")
    return {key: contract[key] for key in allowed if key in contract}

def _safe_json(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k: _safe_json(v) for k, v in value.items()}
    if isinstance(value, list): return [_safe_json(v) for v in value]
    return value

def _pullback_result(rule, pullback, quotes, verified):
    peak = None; exit_index = None
    for index, quote in enumerate(quotes):
        peak = quote.favorable if peak is None else max(peak, quote.favorable)
        if peak >= 5 and peak - quote.favorable >= Decimal(str(pullback)):
            exit_index = index; break
    if exit_index is None:
        return {"policy": rule, "outcome": "NO_DECISION", "peak": str(peak or 0), "exit_favorable": None, "later_recovery_favorable": None, "recovered_after_exit": False, "option_pnl": None, "verification": "VERIFIED" if verified else "UNVERIFIED", "executable": False}
    exit_quote = quotes[exit_index]
    later_peak = max((q.favorable for q in quotes[exit_index + 1:]), default=exit_quote.favorable)
    pnl = (exit_quote.bid - quotes[0].ask) * 100 if verified else None
    return {"policy": rule, "outcome": "EXITED", "peak": str(peak), "exit_favorable": str(exit_quote.favorable), "later_recovery_favorable": str(later_peak), "recovered_after_exit": later_peak > exit_quote.favorable, "option_pnl": str(pnl) if pnl is not None else None, "verification": "VERIFIED" if verified else "UNVERIFIED", "executable": False}

class ShadowSession:
    """Durable local shadow journal; attachment replay is idempotent."""
    def __init__(self, state_path):
        self.path = Path(state_path); self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The local web server uses worker threads.  A single connection is
        # protected by this re-entrant lock so status reads and journal writes
        # cannot interleave or race duplicate idempotency checks.
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS shadow_events (event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)")
        self.path.chmod(0o600)
    def close(self):
        with self.lock: self.db.close()
    def _write(self, event_id, request_id, kind, detail): self.db.execute("INSERT OR IGNORE INTO shadow_events VALUES (?,?,?,?)", (event_id, request_id, kind, json.dumps(detail, sort_keys=True)))
    def _decision(self, request_id):
        row = self.db.execute("SELECT detail FROM shadow_events WHERE event_id=?", ("decision:" + request_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def _parse(self, observations):
        if not isinstance(observations, list): raise ShadowError("observations must be a list")
        parsed=[]; raw=[]; reasons=[]; previous=None
        for item in observations:
            receipt, source, quote = _validate_observation(item)
            if previous is not None and receipt <= previous: raise ShadowError("observation timestamps must be strictly increasing")
            previous=receipt; parsed.append(quote)
            raw.append({"timestamp":item.timestamp,"source_timestamp":item.source_timestamp,"spx":str(item.spx),"option_bid":str(item.option_bid),"option_ask":str(item.option_ask),"data_quality":item.data_quality,"quote_status":item.quote_status,"evidence_origin":item.evidence_origin})
            if item.data_quality != "LIVE" or source is None: reasons.append("DATA_NOT_LIVE_OR_SOURCE_TIMESTAMP_MISSING")
        return raw, parsed, sorted(set(reasons))
    def _evaluate(self, request_id, contract, observations, prefix, observation_kind):
        raw, values, reasons = self._parse(observations); contract=_contract_view(contract)
        if contract is None or not REQUIRED_CONTRACT_FIELDS <= set(contract): reasons.append("CONTRACT_IDENTITY_INCOMPLETE")
        reasons=sorted(set(reasons)); direction=self._decision(request_id)["direction"]; parsed=[]
        if values:
            entry_spx=values[0][0]
            parsed=[SyntheticQuote(item.timestamp, value[0]-entry_spx if direction=="CALL" else entry_spx-value[0], value[1], value[2]) for item,value in zip(observations,values)]
        quality="VERIFIED" if parsed and not reasons else "UNVERIFIED"
        empirical = quality == "VERIFIED" and all(item.evidence_origin not in {"SYNTHETIC", "SYNTHETIC_FIXTURE"} for item in observations)
        evidence_status = "VERIFIED" if empirical else ("UNVERIFIED_SYNTHETIC" if parsed else "UNVERIFIED")
        reported_quality = quality if empirical else ("UNVERIFIED" if parsed else quality)
        self._write(prefix+":observations",request_id,observation_kind,{"quotes":raw})
        entry={"ask":str(parsed[0].ask),"spx":str(observations[0].spx),"verification":evidence_status,"executable":False} if parsed else {"status":"UNVERIFIED","executable":False}
        self._write(prefix+":entry",request_id,"SIMULATED_ENTRY",entry); results=[]
        for policy in POLICIES:
            if not parsed: view={"policy":policy.name,"outcome":"NO_DECISION","verification":"UNVERIFIED","reason":"No usable observations","executable":False}
            else:
                try: view=_safe_json(run_policy(policy,parsed,request_id).__dict__.copy()); view.update({"verification":evidence_status,"executable":False})
                except ValueError as exc: view={"policy":policy.name,"outcome":"NO_DECISION","verification":"UNVERIFIED","reason":str(exc),"executable":False}
                if not empirical: view["option_pnl"] = None
            results.append(view); self._write(prefix+":result:"+policy.name,request_id,"SIMULATION_RESULT",view)
        for rule,pullback in POST5_PULLBACK_RULES.items():
            view=_pullback_result(rule,pullback,parsed,empirical) if parsed else {"policy":rule,"outcome":"NO_DECISION","verification":"UNVERIFIED","reason":"No usable observations","executable":False}
            if parsed: view["verification"] = evidence_status
            results.append(view); self._write(prefix+":result:"+rule,request_id,"SIMULATION_RESULT",_safe_json(view))
        self._write(prefix+":status",request_id,"SHADOW_EVALUATED",{"request_id":request_id,"rule_version":RULE_VERSION,"selected_contract":contract,"observation_count":len(observations),"data_quality":reported_quality,"validation_status":quality,"evidence_status":evidence_status,"quality_reasons":reasons,"observed_quotes":raw,"simulated_entry":entry,"rules":results,"executable":False})
    def record_click(self,direction,request_id,selected_contract,observations,click_timestamp=None):
        with self.lock:
            if direction not in {"CALL","PUT"} or not isinstance(request_id,str) or not request_id: raise ShadowError("direction or request ID is malformed")
            click_timestamp=click_timestamp or datetime.now(timezone.utc).isoformat(); _timestamp(click_timestamp,"click timestamp")
            if self._decision(request_id): return {"result":"DUPLICATE","status":self.status_for(request_id)}
            contract=_contract_view(selected_contract)
            self._write("decision:"+request_id,request_id,"DECISION_RECORDED",{"direction":direction,"request_id":request_id,"click_timestamp":click_timestamp,"selected_contract":contract,"rule_version":RULE_VERSION,"executable":False})
            self._evaluate(request_id,contract,observations,"initial:"+request_id,"OBSERVATIONS_RECORDED")
            return {"result":"RECORDED","status":self.status_for(request_id)}
    def attach_observations(self,request_id,selected_contract,observations):
        with self.lock:
            if not isinstance(request_id,str) or not request_id: raise ShadowError("request ID is malformed")
            if not self._decision(request_id): raise ShadowError("unknown shadow decision")
            contract=_contract_view(selected_contract); canonical={"contract":contract,"observations":[_safe_json(item.__dict__) for item in observations]}
            digest=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest(); prefix="attachment:{}:{}".format(request_id,digest)
            if self.db.execute("SELECT 1 FROM shadow_events WHERE event_id=?",(prefix+":status",)).fetchone(): return {"result":"DUPLICATE","status":self.status_for(request_id)}
            self._evaluate(request_id,contract,observations,prefix,"OBSERVATIONS_ATTACHED")
            return {"result":"ATTACHED","status":self.status_for(request_id)}
    def status_for(self,request_id):
        with self.lock:
            decision=self._decision(request_id)
            if not decision:return None
            rows=self.db.execute("SELECT kind,detail FROM shadow_events WHERE request_id=? ORDER BY rowid",(request_id,)).fetchall(); evaluated=[json.loads(detail) for kind,detail in rows if kind=="SHADOW_EVALUATED"]
            latest=evaluated[-1] if evaluated else {"rule_version":decision.get("rule_version",RULE_VERSION),"selected_contract":decision.get("selected_contract"),"observation_count":0,"data_quality":"UNVERIFIED","evidence_status":"UNVERIFIED","quality_reasons":["MARKET_EVIDENCE_REQUIRED"],"observed_quotes":[],"simulated_entry":{"status":"UNVERIFIED","executable":False},"rules":[],"executable":False}
            out=dict(decision); out.update(latest); return out
    def latest_status(self):
        with self.lock:
            row=self.db.execute("SELECT request_id FROM shadow_events WHERE kind='DECISION_RECORDED' ORDER BY rowid DESC LIMIT 1").fetchone(); return self.status_for(row[0]) if row else None
    def all_statuses(self):
        with self.lock:
            rows=self.db.execute("SELECT request_id FROM shadow_events WHERE kind='DECISION_RECORDED' ORDER BY rowid").fetchall()
            return [self.status_for(row[0]) for row in rows]
    def aggregate_report(self):
        """Aggregate only immutable recorded outcomes; synthetic cases never count as decisive."""
        with self.lock:
            decisions = self.all_statuses()
            policies = ["BASELINE_LOCKED", "CANDIDATE_PRE4_EARLY_FAILURE", "CANDIDATE_ELAPSED_STOP_CONFIRMATION", "LOCKED_PULLBACK_3", "CANDIDATE_PULLBACK_2", "CANDIDATE_PULLBACK_4"]
            report = {"rule_version": RULE_VERSION, "sample_size": len(decisions), "verified_cases": 0,
                      "unverified_cases": 0, "policies": {}, "executable": False}
            for policy in policies:
                report["policies"][policy] = {"sample_size": len(decisions), "verified_cases": 0, "unverified_cases": 0,
                    "decisive_cases": 0, "total_modeled_pnl": None, "worst_modeled_pnl": None,
                    "recovered_winners": 0, "premature_exits": 0, "losses_worsened_by_waiting": 0}
            for decision in decisions:
                verified = decision.get("evidence_status") == "VERIFIED"
                report["verified_cases" if verified else "unverified_cases"] += 1
                by_policy = {item.get("policy"): item for item in decision.get("rules", [])}
                baseline = by_policy.get("BASELINE_LOCKED")
                for policy in policies:
                    item = by_policy.get(policy)
                    if not item: continue
                    summary = report["policies"][policy]
                    if verified and item.get("option_pnl") is not None:
                        pnl = Decimal(str(item["option_pnl"])); summary["verified_cases"] += 1; summary["decisive_cases"] += 1
                        summary["total_modeled_pnl"] = str(Decimal(summary["total_modeled_pnl"] or "0") + pnl)
                        summary["worst_modeled_pnl"] = str(min(Decimal(summary["worst_modeled_pnl"]) if summary["worst_modeled_pnl"] is not None else pnl, pnl))
                        if item.get("recovered_after_exit") and pnl > 0: summary["recovered_winners"] += 1
                        if baseline and policy != "BASELINE_LOCKED" and baseline.get("exit_favorable") is not None and item.get("exit_favorable") is not None and Decimal(item["exit_favorable"]) < Decimal(baseline["exit_favorable"]): summary["premature_exits"] += 1
                        if baseline and policy != "BASELINE_LOCKED" and baseline.get("option_pnl") is not None and pnl < Decimal(baseline["option_pnl"]): summary["losses_worsened_by_waiting"] += 1
                    else:
                        summary["unverified_cases"] += 1
            return report
    def events(self):
        with self.lock:
            return [(row[0],row[1],json.loads(row[2])) for row in self.db.execute("SELECT request_id,kind,detail FROM shadow_events ORDER BY rowid")]

class ShadowLocalService:
    """Local button-facing shadow service; it never creates a position."""
    def __init__(self,state_dir):
        self.root=Path(state_dir); self.root.mkdir(mode=0o700,parents=True,exist_ok=True); self.session=ShadowSession(self.root/"shadow.sqlite")
    def close(self): self.session.close()
    def record_click(self,direction,request_id,click_timestamp): return self.session.record_click(direction,request_id,None,[],click_timestamp)
    def attach_observations(self,request_id,selected_contract,observations): return self.session.attach_observations(request_id,selected_contract,observations)
    def status(self):
        with self.session.lock:
            latest=self.session.latest_status(); events=self.session.events()
            return {"mode":"SHADOW / NO ORDERS","state":"SHADOW RECORDED" if latest else "SHADOW READY","lifecycle":"SHADOW","ready":True,"executable":False,"reason":"Market evidence required; results remain UNVERIFIED" if latest else None,"decision":latest,"decisions":self.session.all_statuses(),"aggregate_report":self.session.aggregate_report(),"activity":[{"type":kind,"detail":detail} for _,kind,detail in events[-12:]]}
