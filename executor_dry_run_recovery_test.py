"""Adversarial offline replay, reconciliation, and crash-boundary checks."""

from dataclasses import replace
from datetime import timedelta
import tempfile
import threading

from executor_dry_run import DryRunError, DryRunExecutor
from executor_dry_run_lifecycle_test import Scenario, ACCOUNT
from simulation_evidence import EvidenceError, FeedStatus


def blocked(action):
    try:
        action()
    except (DryRunError, EvidenceError, ValueError):
        return
    raise AssertionError("Unsafe transition accepted")


def fill(s, event, chunk, qty, rejected=False, **changes):
    args = dict(fill_con_id=101, account=s.account(26000), spx=s.spx(), option=s.options()[0],
                snapshot=s.broker(), now_wall=s.wall, now_monotonic=s.mono)
    args.update(changes)
    return s.engine.entry_event(event, chunk, qty, rejected, **args)


with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    assert s.engine.press("first", "CALL", s.broker(), s.wall, s.mono) == "ACCEPTED"
    assert s.engine.press("first", "CALL", s.broker(), s.wall, s.mono) == "DUPLICATE"
    assert s.engine.press("put", "PUT", s.broker(), s.wall, s.mono) == "REJECTED"
    assert s.engine.press("other", "CALL", s.broker(), s.wall, s.mono) == "REJECTED"
    s.restart()  # after intent accepted
    plan = s.engine.authorize("first", s.account(21000), s.spx(), s.options(),
                              s.broker(), s.wall, s.mono)
    assert plan["quantity"] == 21 and plan["chunks"] == [10, 10, 1]
    s.restart()  # after plan authorization, before first fill
    blocked(lambda: s.engine.authorize("put", s.account(21000), s.spx(),
                                       s.options(), s.broker(), s.wall, s.mono))
    first, second, third = s.engine.entry_chunks()
    blocked(lambda: fill(s, "out-of-order", second[0], second[1]))
    blocked(lambda: fill(s, "wrong-ask", first[0], 1,
                         option=replace(s.options()[0], ask=10.01)))
    blocked(lambda: fill(s, "wrong-account", first[0], 1,
                         account=replace(s.account(21000), account_id="WRONG")))
    blocked(lambda: fill(s, "stale", first[0], 1,
                         spx=replace(s.spx(), status=FeedStatus.DELAYED)))
    blocked(lambda: fill(s, "wrong-broker", first[0], 1,
                         snapshot=s.broker(position_qty=1, con_id=101)))
    blocked(lambda: fill(s, "wrong-fill-contract", first[0], 1,
                         fill_con_id=999))
    assert s.engine.status().owned == 0
    assert fill(s, "partial", first[0], 4) == "FILLED"
    assert fill(s, "partial", first[0], 4) == "DUPLICATE"
    blocked(lambda: fill(s, "partial", first[0], 5))
    blocked(lambda: s.engine.finish_entry(s.broker(), s.mono))
    s.restart()  # after partial entry fill
    assert s.engine.status().owned == 4 and s.engine.status().trades_today == 1
    blocked(lambda: fill(s, "funds-drop", first[0], 6,
                         account=s.account(9000)))
    assert fill(s, "first-rest", first[0], 6) == "FILLED"
    assert fill(s, "second-reject", second[0], 2, rejected=True) == "FILLED"
    assert s.engine.entry_chunks() == [(third[0], 1)]
    assert fill(s, "third-fill", third[0], 1) == "FILLED"
    s.restart()  # after final entry fill, before OPEN persistence
    assert s.engine.status().phase == "ENTERING"
    assert s.engine.status().owned == 13
    assert s.engine.finish_entry(s.broker(), s.mono) == "OPEN"
    assert s.engine.status().trades_today == 1
    s.restart()  # while OPEN
    assert s.tick(4.80) == "OPEN"
    blocked(lambda: s.engine.observe("tick-1", replace(s.spx(5004.80),
                    status=FeedStatus.FROZEN), s.broker(), s.wall, s.mono))
    s.restart()  # immediately before exit trigger
    assert s.tick(3.80) == "OPEN"
    assert s.tick(1.25) == "EXITING"
    assert s.engine.status().exit_reason == "PROFIT_PROTECTION_FLOOR"
    assert s.engine.status().owned == 13
    s.restart()  # immediately after EXITING begins
    assert s.engine.press("blocked-during-exit", "PUT", s.broker(),
                          s.wall, s.mono) == "REJECTED"
    blocked(lambda: s.engine.confirm_flat(s.broker(), s.mono))
    chunk, size = s.engine.next_exit_chunk(s.broker(), s.mono)
    assert size == 10
    blocked(lambda: s.engine.exit_event("oversell", chunk, 11,
                                        fill_con_id=101, snapshot=s.broker(), now_monotonic=s.mono))
    blocked(lambda: s.engine.exit_event("wrong-exit-contract", chunk, 1,
                                        fill_con_id=999, snapshot=s.broker(), now_monotonic=s.mono))
    assert s.engine.exit_event("exit-partial", chunk, 3,
                               fill_con_id=101, snapshot=s.broker(), now_monotonic=s.mono) == "FILLED"
    assert s.engine.exit_event("exit-partial", chunk, 3, fill_con_id=101) == "DUPLICATE"
    blocked(lambda: s.engine.exit_event("exit-partial", chunk, 4))
    s.restart()  # after partial exit fill
    assert s.engine.status().phase == "EXITING" and s.engine.status().owned == 10
    assert s.engine.next_exit_chunk(s.broker(), s.mono) == (chunk, 7)
    assert s.engine.exit_event("exit-reject", chunk, 2, rejected=True,
                               fill_con_id=101, snapshot=s.broker(), now_monotonic=s.mono) == "FILLED"
    assert s.engine.status().owned == 8
    assert s.engine.next_exit_chunk(s.broker(), s.mono)[1] == 8
    chunk2, size2 = s.engine.next_exit_chunk(s.broker(), s.mono)
    assert s.engine.exit_event("exit-last", chunk2, size2,
                               fill_con_id=101, snapshot=s.broker(), now_monotonic=s.mono) == "FILLED"
    s.restart()  # final exit fill occurred, local state remains EXITING
    assert s.engine.status().phase == "EXITING" and s.engine.status().owned == 0
    blocked(lambda: s.engine.confirm_flat(s.broker(complete=False), s.mono))
    blocked(lambda: s.engine.confirm_flat(s.broker(open_order_count=1), s.mono))
    assert s.engine.confirm_flat(s.broker(), s.mono) == "CONFIRMED_FLAT"
    s.restart()  # flat/strategy completion is one transaction
    assert s.engine.status().phase == "FLAT" and s.engine.status().trades_today == 1
    blocked(lambda: s.engine.press("first", "CALL", s.broker(), s.wall, s.mono))
    assert all(ACCOUNT not in detail for _, _, detail in s.engine.journal())
    assert len({event_id for event_id, _, _ in s.engine.journal()}) == len(s.engine.journal())
    s.engine.close()

# A second filled trade is permitted; the third is blocked across restart.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    for number in (1, 2):
        intent = "trade-" + str(number)
        s.start(1, intent)
        chunk, size = s.engine.entry_chunks()[0]
        fill(s, "fill-" + intent, chunk, size)
        assert s.engine.finish_entry(s.broker(), s.mono) == "OPEN"
        assert s.tick(-3.25) == "EXITING"
        s.exit_all()
        s.restart()
    assert s.engine.status().trades_today == 2
    assert s.engine.press("third", "CALL", s.broker(), s.wall, s.mono) == "REJECTED"
    s.restart()
    assert s.engine.press("third-again", "CALL", s.broker(), s.wall, s.mono) == "REJECTED"
    assert s.engine.status().trades_today == 2
    s.engine.close()

# Two processes pressing opposite directions serialize on SQLite's write lock.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    results = []
    errors = []
    snapshot = s.broker()
    def press_in_thread(intent, direction):
        try:
            engine = DryRunExecutor(s.path, ACCOUNT, lambda _: True)
            try:
                results.append(engine.press(intent, direction, snapshot, s.wall, s.mono))
            finally:
                engine.close()
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=press_in_thread, args=("call", "CALL")),
               threading.Thread(target=press_in_thread, args=("put", "PUT"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert sorted(results) == ["ACCEPTED", "REJECTED"]
    assert s.engine.status().phase == "INTENT"
    s.engine.close()

# No filled contracts means no daily trade; new intent may then proceed.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    s.start(1)
    chunk, _ = s.engine.entry_chunks()[0]
    assert fill(s, "reject-all", chunk, 0, rejected=True) == "REJECTED"
    s.restart()
    assert s.engine.finish_entry(s.broker(), s.mono) == "FLAT"
    assert s.engine.status().trades_today == 0
    assert s.engine.press("second", "CALL", s.broker(), s.wall, s.mono) == "ACCEPTED"
    s.engine.close()

# The nearest valid affordable quote is selected using the current ask.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    assert s.engine.press("one", "CALL", s.broker(), s.wall, s.mono) == "ACCEPTED"
    options = s.options(ask=11.0)
    plan = s.engine.authorize("one", s.account(1000), s.spx(), options,
                              s.broker(), s.wall, s.mono)
    assert plan["con_id"] == 102 and plan["quantity"] == 1
    s.engine.close()

# Exact affordability, stale/changed account evidence, and LIVE gate.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    assert s.engine.press("one", "CALL", s.broker(), s.wall, s.mono) == "ACCEPTED"
    blocked(lambda: s.engine.authorize("one", replace(s.account(1000),
                    oldest_required_receipt_monotonic=s.mono-2), s.spx(),
                    s.options(), s.broker(), s.wall, s.mono))
    blocked(lambda: s.engine.authorize("one", s.account(1000),
                    replace(s.spx(), status=FeedStatus.FROZEN),
                    s.options(), s.broker(), s.wall, s.mono))
    assert s.engine.status().phase == "INTENT"
    assert [kind for _, kind, _ in s.engine.journal()].count("EVIDENCE_REJECTED") == 2
    plan = s.engine.authorize("one", s.account(1000), s.spx(),
                              s.options(), s.broker(), s.wall, s.mono)
    assert plan["quantity"] == 1 and plan["con_id"] == 101
    s.engine.close()

# Simulated process death between state mutation and its audit write rolls back
# the entire transition. This closes the historical lifecycle/strategy split.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    s.start(1)
    chunk, size = s.engine.entry_chunks()[0]
    fill(s, "atomic-entry-fill", chunk, size)
    original = s.engine._event
    def fail_open(event_id, kind, detail):
        if kind == "OPEN_CONFIRMED":
            raise RuntimeError("simulated process death")
        return original(event_id, kind, detail)
    s.engine._event = fail_open
    try:
        s.engine.finish_entry(s.broker(), s.mono)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Open crash injection did not fire")
    s.restart()
    assert s.engine.status().phase == "ENTERING" and s.engine.status().trades_today == 1
    s.engine.finish_entry(s.broker(), s.mono)
    original = s.engine._event
    def fail_exit(event_id, kind, detail):
        if kind == "EXIT_TRIGGERED":
            raise RuntimeError("simulated process death")
        return original(event_id, kind, detail)
    s.engine._event = fail_exit
    observation = s.step(4996.75)
    try:
        s.engine.observe("atomic-exit", observation, s.broker(), s.wall, s.mono)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Exit crash injection did not fire")
    s.restart()
    assert s.engine.status().phase == "OPEN" and s.engine.status().exit_reason is None
    assert s.engine.observe("atomic-exit", observation, s.broker(), s.wall, s.mono) == "EXITING"
    chunk, size = s.engine.next_exit_chunk(s.broker(), s.mono)
    s.engine.exit_event("atomic-exit-fill", chunk, size,
                        fill_con_id=101, snapshot=s.broker(), now_monotonic=s.mono)
    original = s.engine._event
    def fail_flat(event_id, kind, detail):
        if kind == "CONFIRMED_FLAT":
            raise RuntimeError("simulated process death")
        return original(event_id, kind, detail)
    s.engine._event = fail_flat
    try:
        s.engine.confirm_flat(s.broker(), s.mono)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Flat crash injection did not fire")
    s.restart()
    assert s.engine.status().phase == "EXITING" and s.engine.status().owned == 0
    assert s.engine.confirm_flat(s.broker(), s.mono) == "CONFIRMED_FLAT"
    s.engine.close()

# Persisted trade count is checked against the journal, including after restart.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    s.start(1)
    chunk, size = s.engine.entry_chunks()[0]
    fill(s, "count-fill", chunk, size)
    s.engine.finish_entry(s.broker(), s.mono)
    s.engine.db.execute("UPDATE controller SET trades_today=0 WHERE singleton=1")
    blocked(lambda: s.engine.status())
    s.engine.close()
    blocked(lambda: DryRunExecutor(s.path, ACCOUNT, lambda _: True))

# New York date rollover is permitted only after confirmed flat; rollback fails.
with tempfile.TemporaryDirectory() as directory:
    s = Scenario(directory)
    next_day = s.wall + timedelta(days=1)
    assert s.engine.press("tomorrow", "CALL", s.broker(), next_day, s.mono) == "ACCEPTED"
    s.restart()
    blocked(lambda: s.engine.press("old-clock", "PUT", s.broker(), s.wall, s.mono))
    s.engine.close()

print("DRY-RUN RESTART, REPLAY, RECONCILIATION AND DAILY CAP PASS")
