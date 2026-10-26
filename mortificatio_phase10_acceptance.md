# Mortificatio Phase 10 — prepared entry and execution-quality acceptance

Review date: 2026-10-25. Current code, Phase 8/9 acceptance and raw benchmarks,
runtime notes, allocation, authentication, streaming, protective strategy,
coordinator, order-safety and recovery paths were inspected before modification.
Public official broker/venue documentation was researched; implementation and
testing used offline fixtures only. No IBKR connection, account credentials,
actual orders, execution enablement, deployment, commit or push occurred.
Pre-existing uncommitted work and historical results were preserved.

**Paper execution: NO-GO. Live execution: NO-GO.** The offline safety foundation
improved; enforceable native broker protection, supported runtime, authoritative
data and actual execution quality remain unverified. No Phase 11 work began.

## What improved

### Dual prepared CALL/PUT candidates

`executor_entry_readiness.py` reuses the original allocator, actual listed strike
intervals, delta ranking, Decimal affordability, fees/reserves and risk checks.
CALL and PUT candidates are independently versioned frozen canonical payloads with
exact contracts/expiry, quotes/source evidence, account/exposure evidence, funded
quantities, price caps, maximum reserved cost, reasons and deterministic IDs.
They contain no session token, trading grant, order ID or submission authority.

The click path compares the complete current evidence/policy/reservation fingerprint
and rechecks current source freshness, contracts, funds, risk, expiration and
generation. It binds the actual authenticated command/trade identity and durably
reserves capital through the original application transaction. It does not rerank
or resize unchanged market evidence. Missing/changed/stale hints are rejected,
not silently sent or rebuilt on the click path. Original authentication, grant,
frequency, account, active-trade and duplicate-command controls remain first.

After admission, prepared trades also ingest current provider evidence through
the existing quote validator before further acquisition planning or dispatch.
A price move beyond the funded bound fences acquisition; a still-permitted
price updates dispatch without resizing or increasing the capital reservation.
An admitted plan is not permanent authority to use its original quote.

`StreamingEntryEvidence` retains source and receipt evidence unchanged; snapshot
only supplies the current observation clock. A bounded two-direction Condition
worker reacts to input updates without price polling. Updates invalidate the
affected direction immediately; index/account changes can refresh both. Ranking
may recompute a whole affected direction when ATM selection can change. Unaffected
direction work is independent; unchanged data is not ranked at click time.
Shutdown invalidates candidates. Foundation failure cannot create entry authority
or abandon independent protective handling.

Cross-direction contract-ID collisions are rejected, including the ambiguous
CALL/PUT fixture identity assumption found during implementation. Last valid
source watermarks survive hint invalidation, preventing a newly sequenced but
older quote from becoming eligible. Hints themselves are disposable after restart;
broker generation, original grants and all admission checks still apply.

The feature is privileged, local and offline. `prepare_entry_candidates` and
`start_entry_readiness` are service-capability methods, not browser configuration
or additional trading actions. The existing CALL/PUT action remains one press.

### Unified protection and execution state

`executor_execution_coordinator.py` is a projection/controller over the existing
authoritative portfolio journal, not a second economic ledger. Desired/confirmed
positions, acquisition/reduction reservations, executions, order IDs/permIds/refs,
uncertainty, cancellation state and capital remain in the existing ledger. New
protection evidence/recovery state uses FULL-synchronous hash-chained events.
The existing order-safety boundary still independently authorizes every reduction.

An optional immutable local protection policy is stored in the independent
checkpoint **before entries**. The one-press entry lifecycle then configures the
adaptive coordinator, protection lifecycle and Phase 9 continuous exit readiness
before its first simulated acquisition dispatch. Configuration is not broker
protection or order enablement.

With this policy, every observed partial acquisition fill sets protection recovery
and acquisition fencing **inside the same durable execution event**. Already
accepted orders can still fill during cancellation; ownership is reconciled and
protected-state evidence must be updated. No new acquisition intent is authorized
after the fence. Coverage does not automatically rearm acquisition.

Synthetic stop evidence must match account, current session generation, exact
contract, client/order/permanent identity, quantity, status, original weighted-
entry-minus-3.25 price and fresh evidence. Duplicate quantities are not added.
Native-stop reservations and tactical reductions cannot exceed owned quantity.
Cancellation requests retain coverage reservations until terminal reconciliation.
An unexplained filled-stop report cannot release coverage without execution and
position evidence. Contradictory stop evidence creates sticky durable uncertainty;
there is no invented automatic reconciliation success.

Known stop identities can be reverified after a normal restart. A stale or mixed-
generation stop scope does not receive a fabricated fresh timestamp. Unexpected
stop identity changes remain unresolved pending a future complete broker-history
adapter. No unsafe retry or automatic clearing of that uncertainty was implemented.

Unverified protection feeds the independent critical-health checkpoint. Working
or uncertain stops prevent FLAT, completed application publication, capital release
and final realized performance. Portfolio, application and reservation release
checks all include supplemental protection state. Dashboard/grant expiration
does not revoke independently authorized protective handling.

**Important limitation:** this is an offline-testable protection lifecycle, not
native stop/bracket transmission or actual broker enforcement. Positive unit
coverage is explicitly synthetic evidence under a fixture capability assertion;
`real_broker_protection_verified` remains false. The economic twin makes no such
assertion and reports native protection UNVERIFIED. It measures gaps and fences
acquisition rather than pretending a native stop exists. Stop placement, parent/
child partial activation, atomic replacement and OCA remain implementation and
broker-validation blockers. Full stop-execution normalization/history recovery
after contradictory evidence is intentionally unresolved.

### Market state and digital twin

`executor_market_state.py` records source, exact instrument key, generation/
sequence, source timestamp, receipt wall and monotonic time, both ages, missing
fields and quality. Missing source time stays missing; recent receipt cannot
refresh an old source quote. Duplicate, backward, frozen and invalid observations
are diagnosed. This model never independently authorizes an entry; the original
0.25-second acquisition source synchronization gate remains authoritative.

`executor_execution_twin.py` extends the original event-driven broker/simulator
with explicit ask depth and bounded BUY matching. SELL behavior and old defaults
remain compatible. It models ten synthetic CALL/PUT candidates, up to five active
strikes with explicitly listed irregular intervals, independent displayed/actual
liquidity, fills, price changes, callback/submission/ACK delays, cancellation and
pacing through the existing durable boundaries. Per-contract synthetic fees are
optional and default off for historical compatibility; Phase 10 uses $0.65 per
contract per side. Economic figures use recorded executions/commissions, not Greeks.

All tactics see identical books and the same explicitly scheduled operator
EXIT ALL. Tactics change HOW, not WHEN, strategy exits occur. Desired funding
always follows the original 25-contract ATM-first prefix; actual concurrent fills
can be incomplete and non-atomic. No blocked near strike is silently reallocated.

Seven labels exercise five distinct scheduler configurations: SINGLE, shorter-
deadline CONSERVATIVE_BOUND, FIXED_CHILD_5, the reused liquidity/adaptive engine
(LIQUIDITY_CHILD, ADAPTIVE_QUANTITY and PHASE8_ADAPTIVE are deliberately equivalent
aliases here), and explicit URGENCY_REPRICE_RESEARCH. Aliases are not claimed as
new independent algorithms. Extra aggressive repricing remains research-only.
Passive entry price seeking below the current ask is not enabled because the
existing marketable-limit dispatch gate would reject it.

The broker event engine is deterministic; the control driver observes at 120 ms
turns. First/target fill times are confirmed observations at that resolution,
not precise exchange latency. Queue position is unknown; executable realization
is a declared synthetic assumption. Gaps are discrete contract-second estimates,
not exact durations or guaranteed upper bounds. No self-learning, directional
model, queue-position prediction or licensed-data calibration was added.

## Economic benchmark evidence

`phase10_execution_quality_benchmarks.json` contains 84 main comparisons: seven
labels × six scenarios × 25/125 target contracts, seed 0; plus realization
sensitivity and halt/session-end cases. All records are SYNTHETIC_ONLY and are
excluded from actual trading history. Records include completion, first/target/
flat time, premiums/proceeds, slippage dollars, effective spread, waiting movement,
fees, cancellations/rejections, logical message count, residual paths and gaps.
Logical order/cancel counts are not total IBKR wire traffic. Effective spread is
twice signed cost versus recorded quote midpoint, not additional cash P&L.

Reproduction:

```sh
.venv/bin/python -B executor_execution_twin.py --output phase10_execution_quality_benchmarks.json
.venv/bin/python -B executor_phase10_benchmark.py --quality phase10_execution_quality_benchmarks.json
```

| Target / scenario / tactic | Acquired / closed / residual | Entry cost | Exit proceeds | Fees | Final synthetic net |
|---|---|---:|---:|---:|---:|
| 25 / deep / SINGLE | 25 / 25 / 0 | $25,000 | $24,750 | $32.50 | −$282.50 |
| 25 / deep / child 5 | 15 / 15 / 0 | $15,000 | $14,850 | $19.50 | −$169.50 |
| 125 / deep / SINGLE | 125 / 125 / 0 | $112,500 | $111,250 | $162.50 | −$1,412.50 |
| 125 / deep / child 5 | 75 / 75 / 0 | $67,500 | $66,750 | $97.50 | −$847.50 |
| 125 / thin / existing adaptive | 15 / 15 / 0 | $13,500 | $13,350 | $19.50 | −$169.50 |
| 125 / disappearing / existing adaptive | 15 / 0 / 15 | $13,500 | $0 | $9.75 | PENDING / UNRESOLVED |
| 125 / improvement offered / existing adaptive | 50 / 50 / 0 | $44,750 | $44,500 | $65 | −$315 |

**No economic improvement from slicing was demonstrated.** The smaller deep-book
dollar loss is caused by lower acquired exposure, not better execution: deep
SINGLE and child-5 each cost $11.30 per completed contract in this setup. Strict
protection recovery fences subsequent acquisition, so desired 125 does not imply
125 fills. Choosing a method because it simply fills less would be misleading.

Price improvement is an explicitly offered counterparty price shared by all
tactics, not a native algo accomplishment. Thin realization sensitivity changes
completion; it does not establish actual executable depth. All native protection
remained UNVERIFIED; reduced intent throughput did not eliminate protection gaps.
Halt and synthetic session end each retain 50 contracts, with no FLAT or final
profit figure. Disappearing liquidity similarly remains unresolved.

This twin does not reproduce exchange execution. Its fixed fee assumption omits
real broker/venue minimums, rebates and tiering. It does not prove exact queueing,
market impact, auction fills, complete depth, real protective coverage or real
fill speed. Licensed historical import requirements and feed synchronization are
specified in `mortificatio_phase10_broker_feasibility.md`; no licensed data or
vendor decoder was supplied.

## Performance evidence

The authenticated experiment uses real fixture passkey/grant/application checks,
durable capital reservation, automatic 125-contract allocation, fake acceptance,
fills, original SPX trailing trigger and verified terminal reconciliation. Both
prepared and unprepared paths enable the same optional protection policy. There
are ten samples per path: descriptive medians/maxima only, **not meaningful tail
certification**. Fake ACKs are synchronous callbacks; broker-side ACK/queue/fill
latencies are not measured. Fill-injection timings include fixture I/O and
reconciliation, not a real execution forecast. All samples are retained.

| Authenticated host stage, ms | Unprepared median / max | Prepared median / max |
|---|---|---|
| Allocation work after initial auth checks | 18.734 / 27.984 | 12.929 / 18.788 |
| Click → capital COMMIT | 70.899 / 84.068 | 56.171 / 82.692 |
| Click → fake adapter acceptance | 350.216 / 388.723 | 316.211 / 405.823 |
| SPX exit event → fake adapter | 154.236 / 497.366 | 158.947 / 209.362 |
| SPX exit event → confirmed fixture flat | 1014.411 / 1465.151 | 1021.394 / 1153.645 |

Dual preparation cost a median 52.005 ms off the click path. It reduced repeated
ranking/allocation work and observed median click-to-adapter time in this final
small run. **A reliable end-to-end latency or economic improvement is not
established**: only ten pairs were measured under shared load, and the prepared
maximum was higher. Authentication, configuration, durable state, repeated
mandatory validation, SQLite work and reconciliation dominate. No gate was relaxed.

The same Phase 9 ledger/adapter fixture was rerun for 50 samples on the existing
macOS 13.7.8 x86_64 / Python 3.9.6 host, concurrently with regression work:

| Comparable exit stage, ms, n=50 | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|
| Trigger → prepared batch | 10.238 | 29.189 | 67.149 | 67.149 |
| Trigger → durable synthetic adapter queue | 46.802 | 110.505 | 177.902 | 177.902 |
| Event receipt → adapter queue | 49.122 | 111.814 | 180.511 | 180.511 |

Phase 9's corresponding trigger-to-adapter p99 was 101.733 ms and its five-sample
authenticated-worker diagnostic was 117.678 ms. The rerun uses equivalent fixtures,
not a controlled identical host load or new exit algorithm; do not attribute the
observed difference to Phase 10 or interchange it with the new authenticated
application measurement. **The 5 ms goal remains unmet.** No result is real IBKR
routing/ACK/fill latency. Small sample percentiles in raw records are descriptive,
not acceptance evidence or a reason to remove durability.

The current evidence does not justify Rust/C++, a distributed system or new
SQLite batching. Existing persistent workers/Phase 9 rollback optimization are
preserved. Controlled supported-runtime profiling and crash proofs should precede
further transaction/process changes.

## Safety tests and acceptance boundary

Five new scripts add 32 targeted test methods. They cover dual directions, strict
funded prefixes, changed funds/reservations/data/generation, source/receipt clocks,
contract collisions, stale hints, double click and immediate opposite direction,
separate service authority, expired grants, event-driven worker shutdown, durable
partial-fill fences, five partial fills, stop rejection/identity conflicts,
stop/tactical overlap, cancel confirmation, unexplained fills, revalidation after
restart, actual post-reducer SQLite failure/replay, lost execution callbacks,
post-admission quote moves inside/outside funded caps, 125-contract
acquisition/liquidation, exact synthetic fees, disappearing liquidity,
halt/end-session residuals and seeded economic reproducibility.

Existing Phase 1–9 tests supply additional missing/delayed/duplicate/reordered
callback, partial replacement, pacing, emergency-during-entry, foundation/disk
outage, authentication/revocation, corruption/recovery, Blind Execution/history
and browser coverage. The heavy twin script gets a 180-second **test process**
budget; no strategy, quote, order or risk threshold changed to pass a test.

Final complete offline regression: **99 scripts passed, 0 failed; five
IBKR-connected scripts excluded**, using `.venv/bin/python -B
offline_test_runner.py` after the final post-admission quote fix. All 32 new
targeted methods are included in those scripts. The separate read-only order
boundary guard passed; `git diff --check` passed. No failure was waived.

The final benchmark artifact contains 84 main comparisons, two realization
sensitivity cases, two halt/session-end cases, 20 authenticated application
samples and 50 comparable exit samples. Artifact assertions verified zero
reported hard safety-invariant violations across the 88 economic/scenario
records and all application/exit samples. Runtime audit remains an explicitly
expected NO_GO diagnostic, not a claim of supported-runtime acceptance.

Zero detected hard execution-invariant violations is distinct from detected
protection gaps. Unverified protection, unfilled targets and residual exposure
are explicitly reported, not hidden in a zero-violation number. Finite fake
scenarios do not establish universal correctness or broker capability.

## Contract preservation

CALL / PUT / EXIT ALL remain the only trading actions; no UI, manual sizing,
strike controls, allocation toggles or extra confirmation was added. Original
same-day SPXW, approximately .48 actual-delta ATM ranking, actual listed intervals,
25-per-strike / 125 total, Decimal funding/reserves, trading-frequency/idempotency,
account isolation and separate authentication/grants remain unchanged.

The initial stop remains **option premium**: weighted actual entry minus 3.25.
The +4 arm / +1.25 protection and +5 uncapped three-point reversal remain **SPX
index movements**, symmetric for CALL/PUT. No fixed profit target, scale-out,
directional prediction or new strategy trigger was introduced. Chunking is only
execution sizing. Premium protection is retained after trailing activation.
Emergency fencing and independently authorized reduction remain separate from
ordinary entry grants. No automatic emergency rearm exists.

All new candidate/economic/protection detail is local privileged/research data,
not a browser-facing ledger API. Blind Execution sanitization remains intact;
completed performance still requires full reconciliation. The new unresolved-stop
checks strengthen finalization/capital release rather than bypass it.

## Remaining blockers and prioritized Phase 11 recommendation

1. **Protection first:** specify and implement complete native stop-intent,
   execution/correction, cancellation/replacement and full-history reconciliation
   against a fake broker, then seek separately authorized real validation.
   Parent-child, partial-fill activation, OCA serialization/overfill and disconnect
   persistence are unverified. No native protection is transmitted by Phase 10.
2. **Runtime/security:** supply reviewed supported Python/TLS and complete wheels;
   produce the actual hash lock and run all tests in a separate approved update.
   `mortificatio_phase10_runtime.md` gives exact commands. Existing Python 3.9.6,
   LibreSSL, setuptools/WebAuthn floors and fresh advisory clearance remain open.
3. **Data:** obtain licensed tick/quote/depth/index evidence and a validated
   authoritative timestamp/clock-domain solution for the unchanged 0.25-second
   gate. Do not mix feeds or treat local receipt as exchange time. Validate
   entitlements, contract calendars, halts and exact SPXW coverage.
4. **Execution evidence:** repeat matched-exposure comparisons under controlled
   load and calibrated replay; separate completeness from cost. Current results
   do not justify activating aggressive repricing, complex packages or broker algos.
5. **Operations:** retain independent checkpoints, secure sessions/recovery,
   permissions, watchdog/restore/alert/physical-key/HTTPS requirements. Profile
   persistent I/O and reconciliation before changing transaction authority.

Phase 11 is recommended only; **not begun**. Public-documentation feasibility is
not account approval. Broker-connected or order-enabled paper tests require
separate explicit authorization plus prerequisites. Live trading independently
remains NO-GO and needs successful paper evidence and operational/security review.

## Exact Phase 10 files

Created: `executor_entry_readiness.py`, `executor_market_state.py`,
`executor_execution_coordinator.py`, `executor_execution_twin.py`,
`executor_phase10_benchmark.py`, `executor_entry_readiness_test.py`,
`executor_protection_lifecycle_test.py`, `executor_market_state_test.py`,
`executor_phase10_lifecycle_test.py`, `executor_execution_twin_test.py`,
`mortificatio_phase10_acceptance.md`, `mortificatio_phase10_broker_feasibility.md`,
`mortificatio_phase10_runtime.md`, `phase10_execution_quality_benchmarks.json`,
`phase10_runtime_audit.json`.

Modified: `executor_allocation_service.py`, `executor_lifecycle.py`,
`executor_portfolio.py`, `executor_liquidation.py`, `executor_liquidity_benchmark.py`,
`executor_liquidity_simulator.py`, `offline_test_runner.py`, `order_boundary_test.py`.
All other pre-existing uncommitted work and historical Phase 8/9 artifacts remain
preserved. No actual broker execution boundary was enabled or replaced.
