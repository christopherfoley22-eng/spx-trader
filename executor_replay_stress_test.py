"""Seeded simulation replay safety attacks; no brokerage connection."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile

from executor_replay import ReplayFormatError, ReplayRunner, ReplaySession
from executor_replay_sessions import make_session, seeded_paths


def replay(raw, **kwargs):
    return ReplayRunner(ReplaySession.from_dict(raw)).run(**kwargs)


def malformed(raw):
    try:
        ReplaySession.from_dict(raw)
    except ReplayFormatError:
        return
    raise AssertionError("Malformed replay input accepted")


def fill(event_id, side, chunk_index, quantity, rejected=False):
    return {"event_id": event_id, "side": side, "chunk_index": chunk_index,
            "quantity": quantity, "rejected": rejected}


# A scripted partial-entry/partial-exit session keeps prices separate from
# strategy decisions. The SELL bid changes between exit chunks.
scripted = make_session("scripted_partial", "CALL", [0, 5, 2, 2],
                        fill_mode="SCRIPTED")
events = scripted["events"]
events[0]["fills"] = [fill("buy-four", "ENTRY", 0, 4)]
events[1]["fills"] = [fill("buy-six", "ENTRY", 0, 6),
                       fill("buy-one", "ENTRY", 1, 1)]
events[3]["fills"] = [fill("sell-three", "EXIT", 0, 3)]
events[4]["options"][0]["bid"] = 9.8
events[4]["fills"] = [fill("sell-seven", "EXIT", 0, 7),
                       fill("sell-one", "EXIT", 1, 1)]
result = replay(scripted)
assert result["final_reconciliation"] == "CONFIRMED_FLAT", result
assert [x["quantity"] for x in result["entry_fills"]] == [4, 6, 1]
assert [x["quantity"] for x in result["exit_fills"]] == [3, 7, 1]
assert result["exit_chunks"] == [10, 1]
assert result["hypothetical_option_pnl_usd"] == "-190.0"
assert result["journal_digest"] == replay(scripted, restart_after_actions=True)["journal_digest"]
replayed_exit_callback = deepcopy(scripted)
replayed_exit_callback["events"][4]["fills"].insert(
    0, fill("sell-three", "EXIT", 0, 3))
changed_price_duplicate = replay(replayed_exit_callback)
assert changed_price_duplicate["final_reconciliation"] == "UNRESOLVED_EXITING"
assert [x["quantity"] for x in changed_price_duplicate["exit_fills"]] == [3]
assert any("Conflicting duplicate" in x["reason"]
           for x in changed_price_duplicate["safety_blocks"])
same_price_duplicate = deepcopy(replayed_exit_callback)
same_price_duplicate["events"][4]["options"][0]["bid"] = 9.9
same_price_result = replay(same_price_duplicate)
without_duplicate = deepcopy(same_price_duplicate)
without_duplicate["events"][4]["fills"].pop(0)
assert same_price_result["final_reconciliation"] == "CONFIRMED_FLAT"
assert same_price_result["exit_fills"] == replay(without_duplicate)["exit_fills"]
assert same_price_result["journal_digest"] == replay(without_duplicate)["journal_digest"]

# Delayed fills, rejected chunks, and changing ask are explicit replay events.
delayed = make_session("delayed_entry", "CALL", [0], fill_mode="SCRIPTED")
delayed["events"][1]["fills"] = [fill("late-buy-ten", "ENTRY", 0, 10),
                                  fill("late-buy-one", "ENTRY", 1, 1)]
late = replay(delayed)
assert late["final_reconciliation"] == "UNRESOLVED_OPEN"
assert sum(x["quantity"] for x in late["entry_fills"]) == 11
with tempfile.TemporaryDirectory() as directory:
    active_db = Path(directory) / "active.sqlite"
    ReplayRunner(ReplaySession.from_dict(delayed)).run(persistent_db_path=active_db)
    try:
        ReplayRunner(ReplaySession.from_dict(
            make_session("cannot_reuse_active", "CALL", [5, 2],
                         start_offset_seconds=10))).run(persistent_db_path=active_db)
    except ReplayFormatError:
        pass
    else:
        raise AssertionError("New replay session reused an active lifecycle")

rejected = make_session("rejected_entry_chunk", "CALL", [0], fill_mode="SCRIPTED")
rejected["events"][0]["fills"] = [fill("reject-ten", "ENTRY", 0, 0, True)]
rejected["events"][1]["fills"] = [fill("buy-one", "ENTRY", 1, 1)]
rejection = replay(rejected)
assert rejection["authorized_quantity"] == 11
assert sum(x["quantity"] for x in rejection["entry_fills"]) == 1
assert rejection["trade_count"] == 1

ask_shift = make_session("ask_changes_between_chunks", "CALL", [0],
                         fill_mode="SCRIPTED")
ask_shift["events"][0]["fills"] = [fill("first-ten", "ENTRY", 0, 10)]
ask_shift["events"][1]["options"][0]["ask"] = 10.01
ask_shift["events"][1]["fills"] = [fill("last-one", "ENTRY", 1, 1)]
shifted = replay(ask_shift)
assert shifted["final_reconciliation"] == "UNRESOLVED_ENTERING"
assert sum(x["quantity"] for x in shifted["entry_fills"]) == 10
assert any("changed after authorization" in x["reason"] for x in shifted["safety_blocks"])

affordable_fallback = make_session("closest_affordable", "PUT", [5, 2], quantity=1)
for observation in affordable_fallback["events"]:
    closest = observation["options"][0]
    closest["ask"] = 11.0
    closest["bid"] = 10.9
    farther = deepcopy(closest)
    farther["contract"]["strike"] = 5001.0
    farther["contract"]["con_id"] = 103
    farther["quote_con_id"] = 103
    farther["ask"] = 9.0
    farther["bid"] = 8.9
    observation["options"].append(farther)
fallback_result = replay(affordable_fallback)
assert fallback_result["selected_contract"]["con_id"] == 103
assert fallback_result["authorized_quantity"] == 1
assert fallback_result["final_reconciliation"] == "CONFIRMED_FLAT"

changed_identity = make_session("contract_changes_under_exit", "CALL", [5, 2])
changed_identity["events"][2]["options"][0]["contract"]["strike"] = 5005.0
identity_block = replay(changed_identity)
assert identity_block["final_reconciliation"] == "UNRESOLVED_EXITING"
assert identity_block["exit_fills"] == []
assert any("metadata changed" in x["reason"] for x in identity_block["safety_blocks"])

duplicated_fill = make_session("duplicate_fill", "CALL", [0, 0],
                               fill_mode="SCRIPTED")
duplicated_fill["events"][0]["fills"] = [fill("buy-four", "ENTRY", 0, 4)]
duplicated_fill["events"][1]["fills"] = [fill("buy-four", "ENTRY", 0, 4)]
duplicated_fill["events"][2]["fills"] = [fill("buy-six", "ENTRY", 0, 6),
                                          fill("buy-one", "ENTRY", 1, 1)]
duplicate_result = replay(duplicated_fill)
assert duplicate_result["final_reconciliation"] == "UNRESOLVED_OPEN"
assert sum(x["quantity"] for x in duplicate_result["entry_fills"]) == 11
assert len(duplicate_result["entry_fills"]) == 3

wrong_phase_fill = make_session("wrong_phase_fill", "CALL", [0],
                                fill_mode="SCRIPTED")
wrong_phase_fill["events"][0]["fills"] = [fill("buy-ten", "ENTRY", 0, 10),
                                         fill("buy-one", "ENTRY", 1, 1)]
wrong_phase_fill["events"][1]["fills"] = [fill("late-buy", "ENTRY", 0, 1)]
wrong_phase_result = replay(wrong_phase_fill)
assert wrong_phase_result["final_reconciliation"] == "UNRESOLVED_OPEN"
assert sum(x["quantity"] for x in wrong_phase_result["entry_fills"]) == 11
assert any("incompatible" in x["reason"] for x in wrong_phase_result["safety_blocks"])

exit_rejected = make_session("rejected_exit_chunk", "PUT", [5, 2, 2],
                             fill_mode="SCRIPTED")
exit_rejected["events"][0]["fills"] = [fill("buy-ten", "ENTRY", 0, 10),
                                       fill("buy-one", "ENTRY", 1, 1)]
exit_rejected["events"][2]["fills"] = [fill("reject-exit", "EXIT", 0, 0, True)]
exit_rejected["events"][3]["fills"] = [fill("sell-ten", "EXIT", 1, 10),
                                       fill("sell-one", "EXIT", 2, 1)]
rejected_exit_result = replay(exit_rejected)
assert rejected_exit_result["final_reconciliation"] == "CONFIRMED_FLAT"
assert rejected_exit_result["exit_chunks"] == [10, 10, 1]
assert sum(x["quantity"] for x in rejected_exit_result["exit_fills"]) == 11

# Feed loss and timestamp attacks block evidence-dependent work and preserve
# an owned position. SPX-only risk may trigger EXITING, but no quote means no
# simulated SELL fill or flat confirmation.
stale = make_session("stale_quote_open", "CALL", [5, 2])
old = datetime.fromisoformat(stale["events"][1]["ny_time"]) - timedelta(seconds=2)
stale["events"][1]["options"][0]["source_time"] = old.isoformat()
stale_result = replay(stale)
assert stale_result["final_reconciliation"] == "CONFIRMED_FLAT"
assert any("stale" in x["reason"].lower() for x in stale_result["safety_blocks"])

no_spx = make_session("spx_feed_stops", "CALL", [1])
no_spx["events"][1]["spx"] = None
assert replay(no_spx)["final_reconciliation"] == "UNRESOLVED_OPEN"

no_option = make_session("option_feed_stops", "CALL", [5, 2])
no_option["events"][2]["options"] = []
option_block = replay(no_option)
assert option_block["final_reconciliation"] == "UNRESOLVED_EXITING"
assert option_block["exit_fills"] == []
assert option_block["exit_trigger"]["reason"] == "LET_IT_RIDE_REVERSAL"

skew = make_session("timestamp_skew", "CALL", [1])
skew["events"][1]["options"][0]["source_time"] = (
    datetime.fromisoformat(skew["events"][1]["ny_time"]) - timedelta(seconds=0.5)).isoformat()
assert any("synchronized" in x["reason"] for x in replay(skew)["safety_blocks"])

backward = make_session("backward_quote_timestamp", "CALL", [1])
backward["events"][1]["options"][0]["source_time"] = (
    datetime.fromisoformat(backward["events"][0]["ny_time"]) - timedelta(milliseconds=50)).isoformat()
assert any("backward" in x["reason"] for x in replay(backward)["safety_blocks"])

wrong_broker = make_session("broker_disagrees", "CALL", [1])
wrong_broker["events"][1]["broker"] = {
    "account_id": wrong_broker["account_id"],
    "selected_account": wrong_broker["account_id"],
    "managed_accounts": [wrong_broker["account_id"]], "complete": True,
    "position_qty": 2, "con_id": 101, "open_order_count": 0,
    "received_monotonic": wrong_broker["events"][1]["monotonic"],
}
broker_block = replay(wrong_broker)
assert broker_block["final_reconciliation"] == "UNRESOLVED_OPEN"
assert any("contradicts" in x["reason"] for x in broker_block["safety_blocks"])

for changed in ({"open_order_count": 1}, {"complete": False},
                {"selected_account": "WRONG_SYNTHETIC"}):
    unsafe_broker = deepcopy(wrong_broker)
    unsafe_broker["events"][1]["broker"].update(position_qty=1, **changed)
    outcome = replay(unsafe_broker)
    assert outcome["final_reconciliation"] == "UNRESOLVED_OPEN"
    assert outcome["safety_blocks"]

missing_expiration = make_session("no_expiration", "CALL", [1])
missing_expiration["expirations"] = ["20260922"]
assert replay(missing_expiration)["final_reconciliation"] == "UNRESOLVED_INTENT"

later_intent = make_session("intent_at_sequence_two", "CALL", [0, 0, 5, 2])
later_intent["intent_sequence"] = 2
assert replay(later_intent)["final_reconciliation"] == "CONFIRMED_FLAT"

duplicate_observation = make_session("exact_duplicate", "CALL", [5, 2])
duplicate_observation["events"].insert(2, deepcopy(duplicate_observation["events"][1]))
plain = make_session("exact_duplicate", "CALL", [5, 2])
assert replay(duplicate_observation)["journal_digest"] == replay(plain)["journal_digest"]

for mutation in (
    lambda x: x["events"][1].update(sequence=x["events"][0]["sequence"]),
    lambda x: x["events"][1].update(monotonic=x["events"][0]["monotonic"]),
    lambda x: x["events"][0].update(monotonic=-1),
    lambda x: x["events"][0]["spx"].update(received_monotonic=101),
    lambda x: x["events"][1].update(ny_time=x["events"][0]["ny_time"][:-6] + "+00:00"),
    lambda x: x["events"][0]["spx"].update(price=float("nan")),
    lambda x: x["events"][0]["options"][0].update(ask=float("inf")),
    lambda x: x["events"][0]["options"][0].update(bid=-1),
    lambda x: x["events"][0]["options"][0].update(quote_con_id=0),
    lambda x: x["events"][0].update(fills=[fill("bad", "ENTRY", 0, -1)]),
):
    bad = make_session("malformed", "CALL", [1])
    mutation(bad)
    malformed(bad)

conflict = make_session("conflicting_duplicate", "CALL", [1])
changed = deepcopy(conflict["events"][1])
changed["spx"]["price"] += 1
conflict["events"].append(changed)
malformed(conflict)

# Three replay sessions share durable state and a New York date. Only the
# first two may acquire a simulated contract, including after restarts.
for trial in range(8):
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "shared_replay.sqlite"
        outputs = []
        for number in range(3):
            raw = make_session("cap-{}-{}".format(trial, number),
                               "CALL" if (trial + number) % 2 == 0 else "PUT",
                               [5.0, 2.0], quantity=1 + (trial % 25),
                               start_offset_seconds=number * 10)
            outputs.append(ReplayRunner(ReplaySession.from_dict(raw)).run(
                persistent_db_path=db, restart_after_actions=True))
        assert [x["trade_count"] for x in outputs] == [1, 2, 2]
        assert [x["final_reconciliation"] for x in outputs] == [
            "CONFIRMED_FLAT", "CONFIRMED_FLAT", "BLOCKED_NEW_ENTRY"]
        assert outputs[2]["entry_fills"] == []
        assert any("Intent blocked" in x["reason"] for x in outputs[2]["safety_blocks"])

# Fixed-seed property attacks: 96 generated mirrored paths, plus a restart
# replay for each. These assert safety and determinism, never profitability.
generated = 0
for index, path in seeded_paths(8675309, 48, steps=24):
    call = ReplayRunner(ReplaySession.from_dict(
        make_session("seed-{}-call".format(index), "CALL", path,
                     quantity=26, max_exit_chunk=5)))
    put = ReplayRunner(ReplaySession.from_dict(
        make_session("seed-{}-put".format(index), "PUT", path,
                     quantity=26, max_exit_chunk=5)))
    a, b = call.run(), put.run()
    for runner, output in ((call, a), (put, b)):
        generated += 1
        assert output["authorized_quantity"] == 25
        assert output["trade_count"] <= 2
        assert min(output["simulated_position_path"]) >= 0
        assert max(output["simulated_position_path"]) <= 25
        assert sum(x["quantity"] for x in output["entry_fills"]) <= 25
        assert sum(x["quantity"] for x in output["exit_fills"]) <= sum(
            x["quantity"] for x in output["entry_fills"])
        assert output["journal_digest"] == runner.run()["journal_digest"]
        assert output["journal_digest"] == runner.run(restart_after_actions=True)["journal_digest"]
        peaks = [Decimal(x) for x in output["high_water_points"]]
        assert peaks == sorted(peaks)
        assert output["ride_path"] == sorted(output["ride_path"])
        if Decimal(output["maximum_favorable_spx_points"]) >= 5:
            assert "LET_IT_RIDE" in output["strategy_events"]
        if output["final_reconciliation"] == "CONFIRMED_FLAT":
            assert sum(x["quantity"] for x in output["exit_fills"]) == 25
            assert output["simulated_position_path"][-1] == 0
    assert a["maximum_favorable_spx_points"] == b["maximum_favorable_spx_points"]
    assert a["maximum_adverse_spx_points"] == b["maximum_adverse_spx_points"]
    assert (a["exit_trigger"] or {}).get("reason") == (b["exit_trigger"] or {}).get("reason")

assert generated == 96
print("REPLAY STRESS PASS: 96 seeded mirrored paths plus malformed/feed/fill attacks")
