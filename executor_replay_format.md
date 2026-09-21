# Offline Executor replay format (version 1)

Replay is synthetic. It does not connect to IBKR, submit orders, or infer real
execution quality. `ReplaySession.from_dict(...)` validates the input, and
`ReplayRunner(session).run()` returns a JSON-serializable result.

The top-level object requires `version: 1`, a unique `session_id`, `direction`
(`CALL` or `PUT`), `intent_sequence`, a synthetic `account_id`, an explicit list
of `YYYYMMDD` SPXW `expirations`, `fill_mode` (`AUTO` or `SCRIPTED`),
`max_exit_chunk` (`5` or `10`), and chronological `events`.

Each event requires:

| Field | Meaning |
| --- | --- |
| `sequence` | Increasing replay sequence; exact adjacent duplicates are ignored, conflicting duplicates fail. |
| `ny_time` | ISO timestamp with the correct New York UTC offset. |
| `monotonic` | Increasing synthetic monotonic receipt time. |
| `spx` | `price`, `status`, `source_time`, and optional `received_monotonic`; `null` means no feed. |
| `options` | List of exact `contract` metadata, `quote_con_id`, `bid`, `ask`, `status`, `source_time`, and optional `received_monotonic`; empty means no option feed. |
| `account` | Verified account snapshot fields from `VerifiedAccountSnapshot`, or `null` when unavailable. |
| `broker` | `SIMULATOR` for the independent synthetic fill ledger, a complete `VerifiedBrokerSnapshot` shaped object, or `null` for missing evidence. |
| `fills` | Scripted simulated fill/rejection events; empty in `AUTO` mode. |
| `restart` | Optional boolean to reconstruct the controller after this observation. |

A scripted fill contains a stable `event_id`, `side` (`ENTRY` or `EXIT`),
zero-based `chunk_index`, integer `quantity`, and boolean `rejected`. Zero
quantity requires rejection. BUY simulation uses the event's observed ask;
SELL simulation uses its observed bid. A changed authorized entry quote blocks
the fill. A repeated fill ID with a changed simulated price is rejected.
Neither price is an actual broker fill.

`AUTO` mode fills all available chunks at the current usable synthetic quote.
`SCRIPTED` mode processes only listed callbacks, allowing partial, rejected,
duplicate, and delayed fills. Missing or invalid evidence leaves the position
in its durable phase and appears in `safety_blocks`.

`ReplayRunner.run(restart_sequences=..., restart_after_actions=True)` rebuilds
Executor from its persisted database during a session. Passing
`persistent_db_path` permits sequential completed sessions on one simulated
account and date; a new session is blocked if that database still has an
active lifecycle.

The permanent boundary-path manifest is `executor_replay_fixtures.json`.
`executor_replay_sessions.make_session(...)` converts those paths into complete
version-1 observations for both CALL and PUT. The fixed date and contract IDs
in that generator are synthetic fixture data; the replay validator still checks
the explicit expiration calendar and exact contract metadata.

Run all offline tests with `.venv/bin/python offline_test_runner.py`.

## Fresh-process recovery

`executor_replay_process_worker.py` runs one validated session against a durable
SQLite database. The worker can exit without cleanup at a named checkpoint;
the next Python process receives the same session input and database path.
`DurableReplayRunner` reconstructs simulated position and fill prices from the
controller's committed audit, verifies the journal prefix at each completed
input event, then continues from the persisted cursor. The input itself must
be supplied again and must match its persisted digest. No Python object,
pickle, or synthetic broker state is saved outside the controller journal.

The replay cursor and completion audit are committed in one SQLite transaction.
An event interrupted before that commit is redelivered with its stable IDs.
Completed events may be delivered again; a missing unprocessed event cannot be
skipped. Corrupt databases, changed input, contradictory broker evidence, and
controller/journal disagreement stop replay without assuming a flat position.
