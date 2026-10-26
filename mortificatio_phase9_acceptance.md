# Mortificatio Phase 9 — continuous exit readiness, offline acceptance

Date: 2026-10-25. Phase 7/8 acceptance, Phase 8 raw benchmarks, market stream,
portfolio/protective strategy, coordinator, safety boundary, durable checkpoint,
authentication and sanitized presentation were inspected before modification.
Existing uncommitted changes and historical results were preserved. No IBKR
connection, actual order operation, execution enablement, deployment, commit,
push, credential change or Phase 10 was performed.

**Paper execution: NO-GO. Live execution: NO-GO.** This phase is an offline
architecture implementation, not broker or supported-runtime acceptance.

## Continuous readiness and authoritative boundaries

`executor_exit_readiness.py:ContinuousExitReadiness` attaches to successful durable
portfolio commits. Each held strike has a frozen `PreparedExitTemplate`: exact
immutable contract identity, account/generation, SELL/LMT, owned and uncommitted
quantities, quote/source/receipt identity, risk epoch, depth and policy versions,
bounded prices and immutable proposed slices. It carries **no order ID, grant,
credential or submission authority**. Source timestamp and receipt time remain
distinct. Existing source/freshness checks determine expiry at selection/dispatch;
a hint cannot refresh its own evidence.

Quote/depth updates refresh only the affected strike. Order/fill callbacks update
the affected strike; global session/reconciliation/reservation/safety changes
refresh or invalidate the relevant scope. SPX events update the existing durable
strategy plus an internal strategy projection without recalculating five liquidity
plans. Per-strike premium-stop evidence comes from the original weighted-entry
calculation. No new strategy thresholds or directional predictions were added.

`OfflineExecutionLifecycle.enable_exit_readiness` requires the independent local
OBSERVE_RISK capability; it has no browser endpoint and does not authorize entry.
The opt-in enable bit is journaled. Templates are disposable and never used to
reconstruct orders, positions, IDs or capital. Restart loads the original hash-
checked ledger, marks old order uncertainty under existing rules, creates an empty
cache and rebuilds from fresh synthetic session/reconciliation evidence. Lost or
failed cache work falls back to the Phase 8 scheduler, not to volatile authority.

The Phase 8 coordinator first validates its global gate, then uses an exact-match
template when available. Changed positions, working/canceling orders, quote/depth
identity, account/generation, source freshness, policy or local risk epoch cause a
miss and the full original sizing fallback. Retry/replacement paths use the
original coordinator. Cache failure is not mistaken for failure of an already
committed event and does not abandon protection.

Orders are still reserved durably by PORT_PLAN, reconciled against the synthetic
broker, independently validated by `ClosingOrderSafety.batch`, and bound durably
by PORT_DISPATCH before the fake adapter queues a submission. The final independent
boundary retains exact account/contract, direction, ownership, working/protective
reservations, stale data, price floor, tick, idempotency, retry and pacing checks.
No cache is a durable order intent. Uncertain orders are not retried, cancellation
requests do not release holdings, and submitted orders do not imply fills or FLAT.

The actual existing sequence includes a planning gate before reservation and
independent final validation **after reservation/reconciliation**. It is retained
rather than relabeling it as a fictitious pre-reservation-only validation path.
Benchmark stages report this order explicitly; no negative or invented stage
duration substitutes for the requested pipeline.

## Streaming and emergency handling

`executor_exit_stream.py:OfflineProtectiveStream` is a bounded, capability-protected
synthetic event queue. Typed events contain kind, instrument, source/origin,
identity, sequence and the original payload with its source/receipt evidence.
SPX, held-option quote and depth callbacks use the existing lifecycle observation
methods. When an existing `ReadOnlyMarketStream` is supplied, its original
subscription manifest is reused; otherwise the manifest is explicitly synthetic
and transport-disabled. No real SPX contract qualification or subscription is
claimed. Real broker transport is unchanged and no socket is opened.

Individual strikes may update asynchronously. The opt-in local worker waits on a
Condition and wakes on enqueue; it does not poll prices. Authenticated startup,
bounded shutdown, failure stopping and a durable entry fence on worker shutdown
are provided. An existing independent supervisor still owns position recovery;
a stopped process/worker is not claimed to provide uninterrupted protection.
One worker preserves callback
ordering; global coordination still prepares/dispatches eligible strikes in one
batch without waiting for another strike's acknowledgment or fill. Sequence order
is per stream/instrument; durable event identities and source watermarks remain
authoritative across restarts. The bounded recent-ID cache is not the durable
duplicate-order defense. Broker reconnect still requires original session and
reconciliation, not an invented connected flag.

An emergency event authenticates the existing EXIT_ALL capability and durably
latches the independent global fence **before queue admission**. Emergency events
have queue priority. Overflow, changed/reordered events, invalid payloads or feed
disconnect invalidate hints and persist an entry/critical fence. Overflow does
not pretend an emergency has dispatched or filled; the durable latch remains for
the independent supervisor. Existing protective state/reservations remain intact.
Feed loss is not falsely represented as broker disconnection. There is no network
listener, extra trading button, dashboard shortcut or unattended live worker.

## Python optimization and rollback safety

Initial paired measurements showed that caching alone did not improve the full
path. In one profiled cache-only dispatch, 17,461 recursive `deepcopy` calls used
about 27 ms; journal append, synthetic snapshot/reconcile and SQLite connection
work dominated simple sizing. The original cache-only run is retained as
`phase9_readiness_baseline_benchmarks.json`, including both contemporaneous PHASE8
and READY samples. It is not a modification of the historical Phase 8 artifact.

The opt-in readiness path now uses explicit rollback write sets for PORT_SPX,
PORT_EXIT, PORT_PLAN, PORT_DISPATCH and enumerated adaptive metadata operations.
Top-level/scalar assignments, new intent maps, affected bindings, arrivals and
append-only metadata lists are detached as required. Existing reducer behavior
is unchanged. Unknown operations, economic executions/corrections, reconciliation
and ordinary non-readiness ledgers retain the full-deepcopy fallback.

This changes rollback snapshot cost, **not durability**: transaction admission,
FULL-synchronous WAL, stable identities, reducer validation, hash chain, journal
head and COMMIT-before-dispatch remain. Real SQLite triggers abort INSERT after
reducer mutation in the new tests, asserting full state equality, unchanged IDs,
reservations, pacing, sequence and head. Any future reducer mutation change must
update its declared write set/tests or retain full copying. No new asynchronous
reservation, unjournaled execution or in-memory recovery fallback was introduced.

Additional aggressive repricing is now **disabled by default** (`ADAPTIVE`).
Explicit research ADAPTIVE_REPRICE policies remain testable. This is the Phase 9
instruction, not automatic tuning from live outcomes. Existing retry/slippage
bounds remain unchanged; no forecast delays an authorized exit.

## Preserved strategy, allocation, security and privacy

The existing strategy remains authoritative: weighted entry premium minus 3.25
for each strike, CALL/PUT symmetric; SPX +4 arms +1.25 reversal protection; SPX +5
supersedes early protection with an uncapped three-point adverse reversal from
the best favorable peak. No exit occurs merely at +4/+5. Premium stops continue
after trailing activation. Partial entry, late-fill acquisition fencing and
emergency precedence remain unchanged. Exit chunking is not profit-taking.

No allocator code changed: actual listed strikes, delta selection, Decimal costs,
fees/reserves/risk, ATM-first funded 25-contract prefixes and maximum 125 across
five strikes remain. No manual controls, farther-strike redistribution or new
entry-liquidity gate was added. The 0.25-second acquisition source synchronization
gate remains unchanged; local receipt time never becomes an exchange timestamp.

Authentication, grants, account isolation, independent protective checkpoints,
foundation-outage recovery, permission hardening, audits and revocation are
retained. Templates, prices, peaks and timing evidence are internal-only. No
browser-facing API or visual dashboard change was made. CALL/PUT/EXIT ALL remain
the only trading actions; the existing sanitized presenter and finalization/P&L
rules still apply. Dashboard expiry cannot revoke independent reduction authority.

## Performance experiment and limits

Reproduction: `.venv/bin/python -B executor_readiness_benchmark.py --output phase9_readiness_benchmarks.json --repeats 50`.
The experiment compares the original non-readiness Phase 8 coordinator/full-copy
path against the opt-in Phase 9 path with identical synthetic holdings, quotes,
depth, policies and broker response conditions. Expensive synthetic book ingestion
and initial reconciliation occur **before** the trigger in both paths. Thus this
isolates readiness/rollback changes instead of crediting Phase 9 for simply moving
the benchmark's fixture construction out of one side's timing interval.

Host timings use `perf_counter_ns`, normal GC and the existing Python runtime.
Warm paths have 50 samples each, cold-cache and 10 ms database-contention paths
10 each; all ten sizes have paired samples, plus CALL/PUT protective/emergency
samples. No slow samples were discarded. Small cold/mode populations are not
robust statistical p99 confidence. Contention holds an actual SQLite writer lock;
durability waits are included. The workload runs alongside the offline regression
process, not a controlled dedicated-host load sweep.

Cold means empty hints with broker reconciliation already established; it does
not hide restart verification inside a fast cache authorization. Actual restart
and foundation recovery use the full lifecycle in integration tests, not a 1 ms
microbenchmark or a claimed startup deadline.

The dispatch endpoint is the durable **synthetic adapter queue**, not socket
transmission, IBKR acknowledgment or fill. The 125-contract moderate-depth initial
wave dispatches five independently safe orders (105 contracts under the unchanged
participation limits), retaining remaining exposure for the original coordinator.
It does not claim instantaneous submission/fill of all 125. All-size tests assert
reservations never exceed holdings, no actual fills are claimed by dispatch timing,
and no portfolio is reported FLAT from a submission.

The Phase 8 historical **491.285 ms decision-to-preparation p99** included
intervening fixture evidence/reconciliation work. It is a different measurement
from new trigger-to-adapter dispatch and must not be presented as an interchangeable
before/after number. The current paired baseline provides comparable stages.

## Final measurements — target not achieved

Final source/instrumentation: `phase9_readiness_benchmarks.json`, 164 dispatch
samples, all retained. The normal strategy is armed **before** timing; receipt
marks the single reversal event that actually triggers exit. The earlier
cache-only diagnostic artifact included the arming tick in its receipt window;
it is retained for diagnosis and is not substituted for the final paired run.

The final host was macOS 13.7.8 x86_64 / Python 3.9.6, with normal GC, concurrent
offline regression and focused-test activity. The OS/workload are not deterministic
or isolated; sequential paired paths, small populations and scheduler/storage
noise limit causal and p99 confidence. No samples were removed to improve a tail.

| Comparable warm metric (ms), n=50 each | Phase 8 median / p95 / p99 / max | Phase 9 median / p95 / p99 / max |
|---|---|---|
| Trigger recognized → first prepared batch | 19.681 / 31.646 / 77.057 / 77.057 | 12.148 / 21.640 / 40.934 / 40.934 |
| Trigger recognized → durable synthetic adapter queue | 60.980 / 88.286 / 148.374 / 148.374 | 49.938 / 91.941 / 101.733 / 101.733 |
| Single event receipt → adapter queue | 62.974 / 90.398 / 230.420 / 230.420 | 52.022 / 95.580 / 103.118 / 103.118 |

Historical Phase 8 decision-to-preparation p99 was **491.285 ms**. The new
comparable trigger-to-preparation p99 is **40.934 ms**, but these use different
timing windows; the 491 figure is not an apples-to-apples speedup denominator.
The contemporaneous paired preparation baseline is **77.057 ms**. Phase 9 warm
trigger-to-adapter p99 **101.733 ms fails 5 ms, 2 ms and 1 ms**. Performance
acceptance is **NOT MET**, despite lower median and some lower tails. In particular,
warm p95 dispatch was slightly worse; do not claim every percentile improved.

| Phase 9 warm stage (ms), n=50 | Median | p95 | p99 | Max |
|---|---:|---:|---:|---:|
| Receipt → trigger recognized | 2.127 | 5.508 | 9.500 | 9.500 |
| Trigger → first template/fallback selection | 4.773 | 8.900 | 16.728 | 16.728 |
| Selection → durable quantity reservation | 5.512 | 11.750 | 16.700 | 16.700 |
| Reservation → final validation completed, including reconcile | 23.966 | 43.789 | 56.114 | 56.114 |
| Independent final validation duration | 2.674 | 5.896 | 16.092 | 16.092 |
| Reservation → adapter queue | 38.554 | 65.727 | 89.416 | 89.416 |
| Trigger → adapter queue | 49.938 | 91.941 | 101.733 | 101.733 |

Rows overlap and their percentiles must not be summed. The planning gate precedes
reservation; independent final validation follows reservation and reconciliation.
The experiment does not remove that ordering to fabricate a simpler stage chart.
Per-order source IDs and durable reservations remain mandatory.

| Trigger → adapter (ms), n=10 each | Phase 8 median / p95 / p99 / max | Phase 9 median / p95 / p99 / max |
|---|---|---|
| Empty hint cache | 67.292 / 92.160 / 92.160 / 92.160 | 55.000 / 70.962 / 70.962 / 70.962 |
| SQLite writer contention | 68.369 / 81.081 / 81.081 / 81.081 | 65.614 / 82.389 / 82.389 / 82.389 |

Small n means p99 equals the largest observation. Contention is a configured
10 ms writer hold, not a timing guarantee. The final complete event receipt-to-
adapter p99 under contention is **84.787 ms** for readiness; a wait incurred before
recognition must not be hidden by only reporting trigger-to-dispatch.

The separate `executor_readiness_worker_benchmark.py` diagnostic reuses offline
authentication test fixtures and is never imported by application services.
`phase9_worker_benchmarks.json` measures five actual authenticated lifecycle/queue
worker samples holding 125 contracts, with durable independent IDs/reservations
and all five closing orders. Receipt-to-adapter median/p95/p99/max:
**109.056 / 127.271 / 127.271 / 127.271 ms**. Trigger-to-adapter:
**100.513 / 117.678 / 117.678 / 117.678 ms**. Receipt-to-recognition:
**9.511 / 9.691 / 9.691 / 9.691 ms**. This includes local capability checks,
queue wakeup, independent service work and fake adapter acceptance; it is not
WebAuthn/HTTP request latency or real broker latency. No closing fills or FLAT were claimed.
Five samples are functional timing evidence, not stable tail certification.

## Profiling and execution quality

The final isolated cProfile dispatch window records roughly **20.6 ms synthetic
sync**, **11.7 ms SQLite connection close**, **11.1 ms reconciliation wrapper**,
**10.7 ms append wrapper** and **6.1 ms / 3,005 recursive deepcopy calls**.
These cumulative times overlap and include profiling overhead; they are not
additive production stage latencies. The earlier 27 ms / 17,461-call profile
included an arming tick as well as different host load. Structural copying reduces
work, but comparing those two profiles alone is not a controlled causal speedup.
The final paired unprofiled samples are the performance evidence.

Remaining costs include FULL commits, full-copy economic/reconciliation rollback,
fake broker snapshot/connection churn, repeated mandatory evidence validation,
global lifecycle lock contention, scheduling and GC/serialization. No unverified
batch reservation or asynchronous COMMIT shortcut was added. A future safe
transaction/state redesign needs separate crash/rollback proofs and controlled
load/runtime testing. Compiling a small helper does not remove those requirements.

Execution-quality pairs use identical synthetic book observations and broker
responses for four virtual seconds. **All compared economic fields matched**:
fills, residuals, confirmed-flat time, realized slippage, logical submissions,
cancellations and rejections. Means below are premium points per actually filled
contract; unfilled exposure is not assigned invented execution cost.

| Scenario, 125 initially owned | Filled / residual, each path | Virtual confirmed-flat time, each path | Average actual slippage, each path |
|---|---|---|---|
| Moderate | 125 / 0 | 0.4 s | 0.0336 |
| Partial | 125 / 0 | 2.6 s | 0.0468 |
| Severe price acceleration | 39 / 86 | Unresolved | 0.107692 |
| Permanent liquidity disappearance | 30 / 95 | Unresolved | 0.05 |
| Disconnect/reconnect | 125 / 0 | 0.6 s | 0.0336 |

There were **zero detected safety-invariant violations** in the 164 ledger
dispatch samples, five full-worker samples and paired quality cases. This does
not imply universal correctness. Finite synthetic tests cannot establish actual
fill speed, depth completeness, actual pacing or executable future liquidity.

## Verification

The complete offline regression suite passed **94 scripts, 0 failed**, with all
five broker-connected scripts excluded. No failed safety test was waived. Output:
`/private/tmp/mortificatio-phase9-offline-suite-final.log`.
Final focused checks also passed after the single-event instrumentation and
optional worker/shutdown integration: stream 8 tests and benchmark 3 tests.
Static broker-boundary verification is rerun against all new sources.

New targeted coverage totals **24 methods**: 8 readiness, 8 streaming/worker,
2 independent lifecycle, 3 actual-boundary benchmark tests, 3 structural rollback.
Subtests include every required size, actual post-reducer SQLite failures,
tampered hints, wrong/stale evidence, duplicates/order changes, disconnection,
cache faults, cold restart, durable 125-contract foundation-outage recovery,
session revocation, emergency priority/backpressure and no price/peak disclosure.
Existing Phase 1–8 strategy, randomized/fault, trade history, protective recovery,
passkey/CSRF/account and genuine Chrome regressions remain intact.

## Runtime and compiled feasibility

Runtime migration remains **unresolved**, as allowed when offline provisioning is
unavailable. `phase9_runtime_audit.json` is the actual NO_GO diagnostic;
`mortificatio_phase9_runtime.md` supplies exact separate-environment, actual-wheel
hash-lock, no-index installation and supported-runtime acceptance instructions.
No supported interpreter/TLS or complete approved wheelhouse was available. No
hashes were fabricated and the compatibility lock/environment was not overwritten.
The complete suite in this phase runs on 3.9.6, **not** a supported replacement.
Setuptools/WebAuthn reviewed floors and current advisory validation remain open.

`phase9_compiled_feasibility.json` records 1,000 pure sizing calls separately,
with GC enabled and all samples retained. This is explicitly a non-acceptance
microbenchmark. Rust was unavailable and no compiled implementation was built.
The pure 25-contract sizing call measured median **0.0205 ms**, p95 **0.0470 ms**,
p99 **0.0888 ms**, worst **0.4046 ms**, n=1,000. This does not include validation,
reservation, authentication, storage or dispatch, and cannot certify 1 ms execution.
The profiled bottleneck is authoritative-state rollback/journaling, reconciliation
and storage/scheduling, not the small immutable sizing helper. A Rust/C++ helper
could reduce some CPU work but cannot remove required COMMIT/reconciliation or
establish a 1–5 ms end-to-end guarantee. Do not move identity/account/authentication
or authoritative state into an unreviewed compiled boundary. No rewrite is
recommended from the current evidence; profile a supported runtime first.

## Remaining blockers and next scope

Paper and live execution remain separately NO-GO: actual supported runtime,
reviewed complete dependency artifacts/advisories, trusted HTTPS/physical keys,
watchdog/restore/alert drills, real account/session/2FA evidence, market-data
entitlements and authoritative source timestamps for the unchanged 0.25-second
gate, broker pacing, actual order/fill/correction/commission reconciliation,
broker-held stops and cancel/replace/OCA semantics remain unverified. Cached
templates do not cure missing authoritative timestamps or broker uncertainty.
No bounded-price policy guarantees a fill during disappearance/price gaps.

Phase 7 lock/I/O tail and full-machine/disk failures remain operational concerns.
The synthetic clock does not advance during real host computation; measured wall
latency is separate, so these experiments cannot certify live freshness/fill speed.
Real calendar/depth/queue behavior, complete load stress and physical security
acceptance remain outside the verified evidence. Finite tests are not universal
correctness or a production-readiness claim.

Recommended Phase 10, **not begun**: provision and verify the supported offline
runtime/artifacts; profile controlled-load durable state/reconciliation costs;
review any proposed batching/immutable-state design with crash/rollback proofs
before changing authority boundaries. Then design separately authorized read-only
IBKR account/provenance observations. Broker-connected/order-enabled paper tests
would require separate explicit authorization and all prerequisites. Live testing
independently requires approved paper evidence and explicit permission.

## Exact Phase 9 files

Created: `executor_exit_readiness.py`, `executor_exit_stream.py`,
`executor_readiness_benchmark.py`, `executor_readiness_worker_benchmark.py`,
`executor_exit_readiness_test.py`,
`executor_exit_stream_test.py`, `executor_exit_readiness_lifecycle_test.py`,
`executor_readiness_benchmark_test.py`, `executor_readiness_rollback_test.py`,
`phase9_readiness_benchmarks.json`, `phase9_readiness_baseline_benchmarks.json`,
`phase9_runtime_audit.json`, `phase9_compiled_feasibility.json`,
`phase9_worker_benchmarks.json`,
`mortificatio_phase9_runtime.md`, `mortificatio_phase9_acceptance.md`.

Modified: `executor_paper_broker.py`, `executor_portfolio.py`,
`executor_liquidation.py`, `executor_adaptive_liquidity.py`, `executor_lifecycle.py`,
`executor_liquidation_test.py`, `order_boundary_test.py`.
Historical Phase 7/8 reports/benchmarks and other pre-existing uncommitted changes
were preserved. No authentication cryptography, visual controls or trading/risk
thresholds were changed.
