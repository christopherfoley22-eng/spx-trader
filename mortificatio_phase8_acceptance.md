# Mortificatio Phase 8 — offline execution acceptance

Review date: 2026-10-25. Architecture and authoritative strategy were inspected
against Phases 1–7 and `mortificatio_phase7_acceptance.md` before modification.
This work is synthetic/offline only. No IBKR connection, broker submission,
modification or cancellation, execution enablement, credential changes, deployment,
commit, push or Phase 9 was performed. Existing uncommitted work is preserved.

**Paper execution: NO-GO. Live execution: NO-GO.** Passing fake-broker tests is
not evidence of real account authorization, broker-held protection or actual
liquidity. The Phase 7 runtime/security and broker blockers remain applicable.

## Implemented boundary and unchanged strategy

`executor_adaptive_liquidity.py` provides immutable, versioned offline policy and
depth evidence, displayed-depth sizing, explicit price floors, optional research
trend analysis, advisory exit-capacity and contract-calendar interfaces.
`executor_liquidation.py` supplies a global coordinator and independent closing
checks over the existing hash-chained, FULL-synchronous portfolio journal.

Configuration is explicitly opt-in through privileged local
`OfflineExecutionLifecycle.configure_exit_execution`, requiring the existing
independent OBSERVE_RISK capability. `observe_exit_depth` uses the same boundary.
There is no browser endpoint to enable/configure the optimizer, no broker client,
and no acquisition authority. Existing entry scheduling is retained. An optimizer
plan has `executable=False`; dispatch binds only the existing simulated ledger.

The allocator remains unchanged: Decimal affordability, real listed intervals,
approximately 0.48 ATM delta, ATM-first funded targets, each nearer strike at 25
before farther strikes receive capital, maximum 25 per strike/125 overall. No
manual quantity/strike controls, redistribution or new entry gate were introduced.

The existing strategy alone triggers exits: weighted actual entry premium minus
3.25; SPX +4 arms +1.25 reversal protection; SPX +5 supersedes early protection
with an uncapped 3-point reversal from the continuously favorable peak. CALL/PUT
symmetry, partial-entry peak tracking, durable late-fill handling and emergency
precedence remain intact. Execution slicing liquidates owned exposure; it is not
a profit scale-out. The hard stop is retained after trailing activates.

## Coordination, uncertainty and protection

All affected strikes may prepare/dispatch together under one durable writer lock.
ATM does not have to finish closing before another strike starts. Durable intents,
idempotent order references, generation/account checks and original reconciliation
remain authoritative. The independent dispatch check verifies exact contract and
SELL direction, current quotes/source times, tick grid, immutable price floor,
original retry limits, pacing, available owned quantity and aggregate closing
reservations. It rechecks displayed participation/capacity independently of the
optimizer, including prior use of the current observation.

Working, prepared, submission-uncertain and cancel-pending quantities stay
reserved. A cancellation request is not terminal evidence. Replacement requires
terminal broker reconciliation and retains the original route quantity, retry
budget and arrival-price bound. Unknown submissions are not retried. Current
acknowledgments, fills and full snapshots establish order state; only existing
authoritative reconciliation establishes FLAT. Portfolio-ledger flat in a
benchmark is distinct from application/history finalization, which additionally
requires reconciled reservation release and the Phase 4 accounting requirements.

External protective-order evidence is a privileged, explicitly synthetic scope
with account, session generation, positions, freshness and exact contracts. Its
entire residual SELL quantity is reserved until terminal reconciliation. Invalid
scope invalidates authorization instead of retaining a formerly valid proof.
Overlapping protective and adaptive reservations are critical/unresolved. The
optimizer does not cancel external stops, assume OCA atomicity, remove protection
to improve a limit price, or infer cancellation from absence of a callback. Real
broker stop/OCA integration remains unverified and disabled.

Normal, protective and emergency modes have explicit bounded repricing intervals;
protective/emergency intent never waits for a forecast. Limits never breach the
original configured slippage floor. Retry/cancel budgets and the original
liquidation deadline produce durable risk alerts and unresolved residuals rather
than unbounded price chasing. A configured logical-request window reserves room
for emergency work. Backward wall-clock movement cannot reset its durable budget.
These sample policies are simulator inputs, **not asserted IBKR rate limits**.
They bound logical order/cancel requests, not every vendor transport message.

Missing/stale depth falls back only to a validated actual BBO with original
participation/capacity constraints. No synthetic levels are inserted. Missing
Greeks do not become invented Greeks; the existing reduction-only quote checks
remain separate from stricter acquisition evidence. Missing source timestamps,
invalid/locked/crossed quotes, halts or absent executable prices cause explicit
unresolved exposure/alerts. They do not authorize imaginary prices or fills.
The 0.25-second source synchronization gate and timestamp provenance are unchanged.

## Synthetic environment and measurement limits

`executor_liquidity_simulator.py` extends the existing fake broker's durable
economic ledger with a seeded event book, independent realizable/displayed depth,
partial fills, withdrawals/replenishment, delayed acknowledgments/reports,
cancel/fill races, rejections, pacing and connectivity failures. It consumes only
observations already available in virtual time, never future books. Stable fill
effects are persisted before application and replayed idempotently after failure.
Broker effects and event acknowledgments can be recovered across their commit gap.
Restarts retain the durable virtual clock and actual economic records.

`executor_liquidity_benchmark.py` seeds actual synthetic acquisition fills at
different per-strike premiums with 25-prefix quantities. These are benchmark
inputs, not a replacement capital allocator or browser controls. All comparisons
use the same initial position, observation sequence, realization seed and policy.
The methods are SINGLE (one top-bid marketable limit per strike), FIXED (5-contract
slices, bounded by existing working-order slots), ADAPTIVE (visible-depth sweep)
and ADAPTIVE_REPRICE (sweep plus bounded cancel/reprice). SINGLE submits one
eligible order per strike per planning round; it cannot waive existing liquidity
limits to force a full 25-contract order. All baselines retain the same independent
safety constraints, including working-order and original price/retry bounds.

Estimates use displayed observations and Decimal prices; realized slippage uses
actual synthetic fills against the durable first valid exit-arrival quote.
If the trigger quote is unavailable, the first later valid quote is labeled as
such; there is no invented trigger price. Top-bid baseline estimates are
conditional on filling at their limit, not a claim of immediate depth execution.
Residual exposure/time series, partial-fill counts, logical request/cancel counts,
rejections and per-strike realized slippage are recorded. Slippage excludes fees;
actual recorded synthetic commissions remain in the existing economic ledger.
No simulator models
exact exchange queues, all competing participants, real vendor latency, every
venue event or guaranteed counterparties. This is a limited synthetic sweep,
not evidence of a universally optimal policy.
The reused privileged `OfflinePortfolioLedger.metrics()` exposes recorded
per-strike submission/acknowledgment timestamps, first/final fill receipt times,
weighted fills, fill percentage, arrival slippage, rejections, cancellation
requests/acknowledgments/reconciled cancellations and repricing counts, plus
portfolio entry/liquidation intervals. Source execution times remain in the
journal separately. Benchmark JSON summarizes cases; constructing `ExitSimulation`
with a retained private root keeps its complete source/order/event journals.
None of these internal prices or performance metrics enters the browser API.

Raw and summary results are in `phase8_synthetic_benchmarks.json`. Host handler
times use actual `perf_counter_ns`; broker dispatch/ACK/fill/flat timings use the
explicit virtual clock. Host percentiles concatenate raw samples instead of
averaging percentiles. Median/p95/p99/worst are reported. Flat-time distributions
include only confirmed-flat cases, with unfinished cases and residuals separately
reported. Delayed/out-of-order reports can invert observed callback order; actual
source event identity and execution time are retained separately.
The reported dispatch-to-ACK and ACK-to-fill intervals use application callback
receipt times; the synthetic broker's programmed ACK times are retained separately
in its durable event book. They must not be interpreted as real exchange timings.
Virtual market time does not advance during host SQLite work; wall-clock handler
latency is measured separately. These results cannot certify live quote freshness
or production end-to-end latency. The recorded workload includes the benchmark
process alongside the offline regression process, not a controlled load sweep.

## Acceptance criteria and verification

Offline invariants: zero unintended acquisition, overselling, duplicate bindings,
unauthorized dispatch or false-FLAT states in exercised scenarios; correct durable
restart/callback recovery, emergency fencing, partial accounting and Blind Mode.
Synthetic slippage must remain within the pinned original price bounds. A 250 ms
p99 budget per measured internal handler stage is an evaluation criterion on this
recorded host/load only. It is not a safety-threshold change, realtime guarantee,
complete API end-to-end latency measurement or IBKR performance promise.

New tests cover pure evidence/sizing and advisory-only policies; five-strike
concurrent planning; malicious optimizer quantity; external protective conflicts;
quote revalidation; bounded pacing/cancellations; disk failures; all required
sizes 1/9/25/35/50/60/75/85/100/125; seeded scenarios; duplicate and late fills;
out-of-order reports; source-event/fill commit gaps; source/ledger restart;
foundation outage; expired/revoked sessions; partial-entry peaks; emergency
repetition; retained reservations; authenticated capability isolation and sanitized
presentation. Existing Phase 1–7 adversarial, WebAuthn, accounting, strategy,
checkpoint and genuine Chrome tests remain in the complete offline suite.

Targeted new coverage: **35 test methods** across five scripts (7 pure policy/
analytics, 10 coordinator/boundary, 8 simulator, 3 stress, 7 lifecycle). Subtests
exercise all ten required quantities, 11 full-size adverse scenarios and eight
seeded randomized size/policy cases. The stress script's 180-second process budget
accommodates durable synthetic fault/recovery work; it changes no market-data,
price, strategy or execution safety threshold.

The initial and final complete runs each passed **89 offline scripts, 0 failed**,
excluding all five broker-connected scripts. The final run includes all 35 new
test methods, 11 full-size adverse subcases, all required position sizes and the
existing Chrome/virtual-passkey scenarios. No failure was waived. Final output:
`/private/tmp/mortificatio-phase8-offline-suite-final.log`.

## Final synthetic benchmark results

Command: `.venv/bin/python -B executor_liquidity_benchmark.py --output phase8_synthetic_benchmarks.json --seeds 1`.
The completed artifact contains **44 cases**, each starting with 125 contracts,
across four policies and 11 scenarios; horizon 4 virtual seconds, seed 0.
**Zero detected safety-invariant violations.** This finite sweep has no statistical
confidence claim. Randomized invariant tests supplement it; they do not establish
real liquidity, fill speed or an optimal execution policy.

| Policy | Flat cases / 11 | Median / p95 / p99 / worst flat time (virtual s) | Average realized slippage (premium points) | Logical requests / cancels / rejects | Residual contracts across all cases |
|---|---:|---|---:|---|---:|
| SINGLE | 8 | 0.9 / 2.2 / 2.2 / 2.2 | 0.001797 | 152 / 21 / 1 | 262 |
| FIXED | 8 | 1.55 / 3.2 / 3.2 / 3.2 | 0 | 326 / 39 / 4 | 262 |
| ADAPTIVE | 9 | 0.6 / 2.6 / 2.6 / 2.6 | 0.037688 | 154 / 12 / 1 | 181 |
| ADAPTIVE_REPRICE | 9 | 0.6 / 3.1 / 3.1 / 3.1 | 0.037605 | 171 / 23 / 1 | 181 |

Slippage is quantity-weighted over actual **filled** contracts, excludes fees and
unfilled exposure, and is not directly comparable as full-portfolio cost when
completion populations differ. Time-to-flat excludes unfinished cases; their
residuals are explicitly retained. Worst individual fill slippage was **0.30
premium points**, within the pinned existing bound. There were 1,179 partial-fill
executions across the case set; raw rows include residual-over-time series and
each policy's per-case outcomes.

- Deep books: SINGLE/ADAPTIVE/ADAPTIVE_REPRICE 0.2 virtual seconds, FIXED 1.2;
  all had zero realized slippage. Splitting did not improve this case.
- Moderate books: adaptive methods 0.4 seconds with 0.0336 average premium-point
  slippage versus SINGLE 0.6 seconds at zero; FIXED 1.3 seconds at zero.
- Partial liquidity: SINGLE/FIXED retained 32 contracts; ADAPTIVE closed all in
  2.6 seconds; ADAPTIVE_REPRICE in 3.1 seconds with extra traffic.
- Severe acceleration: SINGLE/FIXED retained 115 contracts; adaptive methods 86.
  Permanent disappearance: SINGLE/FIXED retained 115; adaptive methods 95.
  **None of those residual cases was reported FLAT.**
- Delayed ACK, disconnect/reconnect and rejected-request scenarios completed in
  all policies under their stated synthetic assumptions. This does not verify
  actual broker recovery, pacing or cancellation semantics.

Advanced repricing did not improve completion counts and approximately doubled
adaptive cancellation traffic (23 versus 12); its worst completed-case latency
was worse. **Recommend against activating ADAPTIVE_REPRICE.** Keep it research-only
and preserve the opt-in boundary. Simpler depth-aware sizing may merit further
controlled comparison, but costs more slippage in these examples. No policy is
automatically selected, retuned or enabled from these results; forecasts remain
disabled and entry restrictions/ATM-first allocation remain unchanged.

## Measured internal latency — performance target not met

Host: macOS 13.7.8 x86_64, Python 3.9.6, concurrently running the offline regression
suite. These are actual host wall-clock measurements in milliseconds; they are
separate from synthetic broker callback times. First decision-to-preparation
includes intervening fixture market-evidence/reconciliation work. Preparation-to-
dispatch includes intervening cancellation/reconciliation and durable fake dispatch
binding/queueing, rather than timing only a dispatch function call.

| Stage | Samples | Median ms | p95 ms | p99 ms | Worst ms |
|---|---:|---:|---:|---:|---:|
| Market-event handler receipt → exit decision | 44 | 4.311 | 9.404 | 13.320 | 13.320 |
| Decision → first prepared order batch | 44 | 141.904 | 171.211 | 491.285 | 491.285 |
| Prepared batch → durable simulated dispatch | 122 | 51.043 | 88.339 | 204.138 | 237.771 |
| Coordinator planning cycle | 773 | 6.145 | 47.441 | 194.652 | 462.292 |

The documented **250 ms per-stage p99 target FAILS** for decision-to-preparation.
The worst observation occurred in the partial-liquidity SINGLE case. No safety
gate or threshold was weakened to hide it. Phase 7 M4 lock/I/O and scheduling-tail
work remains open; these figures do not isolate database, scheduler and concurrent
load causality. Profile under controlled load before making a latency claim.
Even stages whose p99 passed are not realtime guarantees. Broker ACK/first/final
fill receipt distributions are retained per case in the JSON; no real IBKR latency
was measured. Offline safety invariants passed in exercised cases; **offline
performance acceptance is incomplete**, and paper/live execution remain NO-GO.

## Remaining blockers and recommended Phase 9 scope

- Phase 7 H1/H2 remain: installed Python 3.9.6/LibreSSL 2.8.3 and obsolete
  setuptools. No interpreter, packages or dependency lock was silently upgraded.
  Provision reviewed supported runtime/wheels, reproducible complete lock/SBOM
  and current advisory validation, then run migration/regression acceptance.
- Authoritative SPX/SPXW quote/size/Greek timestamps and entitlements for the
  unchanged 0.25-second source gate remain unverified. Local receipt time cannot
  solve that gap. Full depth is optional and unavailable/incomplete depth must
  not be represented as exchange liquidity. Greeks are not manufactured.
- Real approved paper-session identity, manual IBKR/2FA lifecycle, API settings,
  pacing semantics, marketable limit behavior, order/execution/correction/fee
  reconciliation, disconnect recovery, broker-held stops, cancel/replace and OCA
  remain broker-validation requirements. No connection was attempted.
- Offline repricing may retain exposure when the floor, retries, data or broker
  evidence is exhausted. A separately reviewed actual-broker emergency/escalation
  policy is required; no bounded-price policy guarantees immediate liquidation.
- Expiration/early-close/calendar interfaces are advisory and require exact
  verified contract/session evidence. No real verified exchange calendar or
  newly activated expiration-liquidation policy is supplied. Halt handling
  retains unresolved exposure rather than inventing a fill.
- Forecasting remains disabled; pre-entry exit-capacity analysis is research
  only. Neither changes direction, allocation or entry eligibility. Enabling
  either or changing execution parameters requires separate evidence/approval.
- Dedicated service identity, OS watchdog, complete-storage/machine failure,
  encrypted restore drills, trusted HTTPS, physical passkeys, manual broker
  recovery, external alert delivery/anchoring and multi-browser/device acceptance
  remain Phase 7 operational/security work. History/reporting can still contend
  for lifecycle locks. These tests do not provide hard scheduling guarantees.

Recommended Phase 9, **not begun**: supported-runtime/operational remediation and
independent review of the measured offline policy, followed by a separately
authorized read-only IBKR provenance/account/depth/pacing observation protocol.
Order-connected paper validation would need separate explicit authorization and
all existing prerequisites. Live trading independently remains NO-GO, requiring
approved paper evidence, operational/security acceptance and explicit permission.

## Exact Phase 8 files

Created: `executor_adaptive_liquidity.py`, `executor_liquidation.py`,
`executor_liquidity_simulator.py`, `executor_liquidity_benchmark.py`,
`executor_adaptive_liquidity_test.py`, `executor_liquidation_test.py`,
`executor_liquidity_simulator_test.py`, `executor_liquidity_stress_test.py`,
`executor_liquidation_lifecycle_test.py`, `phase8_synthetic_benchmarks.json`,
`mortificatio_phase8_acceptance.md`.

Modified: `executor_portfolio.py`, `executor_lifecycle.py`,
`executor_blind_presentation.py`, `dashboard/dashboard.js`,
`order_boundary_test.py`, `offline_test_runner.py`, `offline_test_runner_test.py`.
Other pre-existing uncommitted files are not Phase 8 changes. Historical Phase 6/7
reports and research strategy results remain unchanged.
