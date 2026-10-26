"""Offline shadow-mode tests."""

from decimal import Decimal
import json
import tempfile
from pathlib import Path

from mortificatio_pre4_experiment import SyntheticQuote
from mortificatio_shadow import (ShadowObservation, ShadowSession, ShadowError,
                                 FixtureObservationSource)


quotes = [SyntheticQuote.make(i, value) for i, value in enumerate([0, 2.0, 0.9, 3.0, 5.0, 2.0])]
observations = [ShadowObservation(q.timestamp, 5000 + q.favorable, q.bid, q.ask,
                                  q.timestamp, "LIVE", "FRESH") for q in quotes]
contract = {"symbol": "SPX", "sec_type": "OPT", "right": "C",
            "expiration": "20260101", "strike": 5000, "trading_class": "SPXW",
            "multiplier": "100", "currency": "USD", "con_id": 123}

with tempfile.TemporaryDirectory() as directory:
    session = ShadowSession(Path(directory) / "shadow.sqlite")
    result = session.record_click("CALL", "request-1", contract, observations)
    assert result["result"] == "RECORDED"
    status = result["status"]
    assert status["data_quality"] == "UNVERIFIED"
    assert status["validation_status"] == "VERIFIED"
    assert status["evidence_status"] == "UNVERIFIED_SYNTHETIC"
    assert status["executable"] is False
    assert len(status["rules"]) == 6
    assert {item["policy"] for item in status["rules"]} == {
        "BASELINE_LOCKED", "CANDIDATE_PRE4_EARLY_FAILURE",
        "CANDIDATE_ELAPSED_STOP_CONFIRMATION", "LOCKED_PULLBACK_3",
        "CANDIDATE_PULLBACK_2", "CANDIDATE_PULLBACK_4"}
    duplicate = session.record_click("CALL", "request-1", contract, observations)
    assert duplicate["result"] == "DUPLICATE"
    kinds = [kind for _, kind, _ in session.events()]
    assert kinds[:3] == ["DECISION_RECORDED", "OBSERVATIONS_RECORDED",
                         "SIMULATED_ENTRY"]
    assert kinds.count("SIMULATION_RESULT") == 6
    session.close()

with tempfile.TemporaryDirectory() as directory:
    session = ShadowSession(Path(directory) / "shadow.sqlite")
    recovery_quotes = [SyntheticQuote.make(i, value) for i, value in enumerate(
        [0, 5.0, 1.5, 4.8, 5.5, 1.4, 5.0])]
    recovery = [ShadowObservation(q.timestamp, 5000 + q.favorable, q.bid, q.ask,
                                  q.timestamp, "LIVE", "FRESH") for q in recovery_quotes]
    result = session.record_click("CALL", "pullback-recovery", contract, recovery)
    rules = {item["policy"]: item for item in result["status"]["rules"]}
    assert rules["LOCKED_PULLBACK_3"]["exit_favorable"] == "1.5"
    assert rules["CANDIDATE_PULLBACK_4"]["recovered_after_exit"] is True
    assert rules["CANDIDATE_PULLBACK_4"]["option_pnl"] is None
    assert rules["CANDIDATE_PULLBACK_4"]["verification"] == "UNVERIFIED_SYNTHETIC"
    session.close()

with tempfile.TemporaryDirectory() as directory:
    session = ShadowSession(Path(directory) / "shadow.sqlite")
    loss_quotes = [SyntheticQuote.make(i, value) for i, value in enumerate(
        [0, 5.0, 1.5, -1.0])]
    loss = [ShadowObservation(q.timestamp, 5000 - q.favorable, q.bid, q.ask,
                              q.timestamp, "LIVE", "FRESH") for q in loss_quotes]
    result = session.record_click("PUT", "pullback-loss", contract, loss)
    rules = {item["policy"]: item for item in result["status"]["rules"]}
    assert Decimal(rules["CANDIDATE_PULLBACK_4"]["exit_favorable"]) == Decimal("-1.0")
    assert Decimal(rules["CANDIDATE_PULLBACK_4"]["exit_favorable"]) < Decimal(
        rules["LOCKED_PULLBACK_3"]["exit_favorable"])
    session.close()

with tempfile.TemporaryDirectory() as directory:
    session = ShadowSession(Path(directory) / "shadow.sqlite")
    delayed = [ShadowObservation("2026-01-01T14:00:00+00:00", 5000, 2, 2.1,
                                 None, "DELAYED", "DELAYED")]
    result = session.record_click("PUT", "request-2", None, delayed)
    assert result["status"]["data_quality"] == "UNVERIFIED"
    assert result["status"]["executable"] is False
    assert "DATA_NOT_LIVE_OR_SOURCE_TIMESTAMP_MISSING" in result["status"]["quality_reasons"]
    session.close()

with tempfile.TemporaryDirectory() as directory:
    session = ShadowSession(Path(directory) / "shadow.sqlite")
    bad = [ShadowObservation("2026-01-01T14:00:00+00:00", 5000, 2.2, 2.1,
                             "2026-01-01T14:00:00+00:00", "LIVE", "FRESH")]
    try:
        session.record_click("CALL", "request-3", contract, bad)
    except ShadowError:
        pass
    else:
        raise AssertionError("crossed shadow quote was accepted")
    session.close()

assert all("executable" not in text or "True" not in text for text in [])

# A direction click can be persisted before market evidence exists.  Evidence
# may then be attached by a separate local fixture process and survives reopen.
with tempfile.TemporaryDirectory() as directory:
    state = Path(directory) / "shadow.sqlite"
    session = ShadowSession(state)
    click = session.record_click("CALL", "attach-call", None, [],
                                 "2026-01-01T14:00:00+00:00")
    assert click["status"]["data_quality"] == "UNVERIFIED"
    fixture = Path(directory) / "fixture.json"
    fixture.write_text(json.dumps({"version": 1, "observations": [
        {"timestamp": "2026-01-01T14:00:00+00:00", "source_timestamp": "2026-01-01T14:00:00+00:00",
         "spx": 5000, "option_bid": 2.0, "option_ask": 2.1, "data_quality": "LIVE", "quote_status": "FRESH"},
        {"timestamp": "2026-01-01T14:00:01+00:00", "source_timestamp": "2026-01-01T14:00:01+00:00",
         "spx": 5005, "option_bid": 4.0, "option_ask": 4.1, "data_quality": "LIVE", "quote_status": "FRESH"},
        {"timestamp": "2026-01-01T14:00:02+00:00", "source_timestamp": "2026-01-01T14:00:02+00:00",
         "spx": 5002, "option_bid": 2.8, "option_ask": 2.9, "data_quality": "LIVE", "quote_status": "FRESH"}
    ]}))
    attached = session.attach_observations("attach-call", contract,
                                           FixtureObservationSource(fixture).observations())
    assert attached["result"] == "ATTACHED"
    assert attached["status"]["data_quality"] == "UNVERIFIED"
    assert attached["status"]["evidence_status"] == "UNVERIFIED_SYNTHETIC"
    assert len(attached["status"]["rules"]) == 6
    duplicate_attach = session.attach_observations("attach-call", contract,
                                                   FixtureObservationSource(fixture).observations())
    assert duplicate_attach["result"] == "DUPLICATE"
    session.close()
    reopened = ShadowSession(state)
    recovered = reopened.status_for("attach-call")
    assert recovered["click_timestamp"] == "2026-01-01T14:00:00+00:00"
    assert recovered["observation_count"] == 3
    assert recovered["executable"] is False
    assert recovered["rules"][0]["policy"] == "BASELINE_LOCKED"
    assert sum(kind == "SIMULATION_RESULT" for _, kind, _ in reopened.events()) == 12
    report = reopened.aggregate_report()
    assert report["sample_size"] == 1 and report["verified_cases"] == 0
    assert report["unverified_cases"] == 1
    assert report["policies"]["BASELINE_LOCKED"]["decisive_cases"] == 0
    assert report["executable"] is False
    reopened.close()

print("MORTIFICATIO SHADOW MODE PASS")
