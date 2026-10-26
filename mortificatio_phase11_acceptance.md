# Mortificatio Phase 11 — offline hard admission and broker reality preparation

Review date 2026-10-25. Phases 8–10 reports, benchmark structure/results, runtime
notes, allocation/readiness, protective strategy, journal/reconciliation,
independent checkpoint/security, market streaming and read-only transport were
inspected before modification. Existing uncommitted work was preserved.

**Paper: NO-GO. Live: NO-GO.** No broker connection, credentials, broker orders,
execution enablement, deployment, commit or push occurred. Phase 12 was not begun.
Public documentation research is not a broker observation. Runtime migration and
real broker protection/data validation remain unresolved.

## Implemented

### Mandatory hard exit-capacity admission

`executor_exit_capacity.py` is a separately inspectable pure Decimal model. The
original allocator still first establishes exact contracts, same-day expiry,
actual listed ATM/OTM ladder, approximately .48 delta ranking, financial/risk
room, fees, reserved capital and the 25-contract prefix. The new mandatory gate
then finds the largest permitted sub-prefix. An incompletely funded or capacity-
constrained nearer strike stops farther allocation; no redistribution occurs.

No production risk percentage or acceptable loss threshold was invented.
`ExitCapacityPolicy` must explicitly configure version, entry realization,
spread ceiling, modeled account-loss budget and ordered NORMAL/STRESSED/SEVERE
scenarios. Each scenario supplies a retained-liquidity fraction, adverse option-
premium movement, latency, additional adverse premium speed and loss ceiling.
Absent/invalid policy refuses acquisition. No browser settings or controls exist.

`CapacityContext` must supply fresh account/generation-bound reconciliation and
protection-feasibility references, existing modeled downside and an explicit
synthetic feasibility classification. These are trusted backend fixture inputs,
not client claims or signatures proving real broker protection. Actual broker
providers remain unsupported; no real execution can be authorized by this model.

Current bid/ask, displayed sizes and explicit estimated capacities are validated
using the unchanged original quote/source/size/Greek checks. Entry and stressed
exit capacities are distinct discounted constraints. Displayed depth is not
assumed executable. No replenishment or missing depth is manufactured. Quote
quality/provenance and the original 0.25-second synchronization gate remain intact.
An admitted target does not waive the original per-order participation, slice,
quote-consumption or deadline limits; an entry may remain partially filled.

The model charges permitted entry price minus stressed proceeds plus configured
fees; quantities beyond modeled liquidation capacity are charged full premium
exposure in the requested-size diagnostics. Such residuals cannot be admitted.
Existing disclosed modeled downside, external stop-risk encumbrance and local
held risk cannot disappear from the additional budget. The modeled downside is
not a new stop trigger and may exceed the initial stop under gap/deterioration.
A stop price is not guaranteed fill price.

The fixed linear assumptions permit exact per-strike Decimal capacity/loss bounds
without searching/recomputing all strikes for each additional contract. A separate
exhaustive 0..125 oracle verifies the bound on 200 seeded cases. This is a bounded
static conditional stress model, **not a calibrated exchange queue/market-impact
or future liquidity guarantee**. Recent history consists of the current validated
observation; no historical replenishment evidence was available. Real policy
approval needs licensed observations, sensitivity/calibration, broker feasibility
and an explicit account risk budget. Fixture policy numbers are not production
recommendations.

The frozen allocation plan contains admitted quantities, recalculated monetary
reservations and a canonical admission decision: direction/contracts, financially
requested/admitted vectors, binding constraint, entry-cost bounds, scenario
costs/residuals, downside, quality, clocks/generation and explicit reasons.
Failure before a financial size can be established records requested quantities
as unavailable, not invented. Admission denials are written to the private audit;
successful decisions are part of the existing immutable durable plan.

Checks run during background preparation, again at authenticated confirmation,
and before further acquisition planning/dispatch. Changed cache evidence cannot
be sent. Post-admission deterioration fences acquisition instead of silently
resizing, expanding or reallocating a working portfolio. Both provider snapshots
and independently streamed quotes must satisfy capacity. Replayed snapshots do
not overwrite newer streamed state; changed quote identities or backward provider
evidence fence acquisition without quarantining valid economic position evidence.
Current provider quotes must also pass the original validator. Capital is not released on a cancellation
request. Original session/grant/account/frequency/idempotency/emergency controls
remain authoritative. Existing legacy plans without this policy cannot silently
resume acquisition; protection and reconciliation remain independent.

### Protection obligations and reconciliation

Every recorded portfolio BUY execution now creates/updates an auditable obligation
inside the same durable economic journal transaction, even in legacy research
fixtures. Execution/ref/contract/quantity/premium, correction activity and receipt
are retained through replay/restart. Corrected/busted records remain in the audit.
Only the existing full reconciled-flat event can discharge remaining obligations.
An obligation is never affirmative broker-stop evidence.

The existing configured protection lifecycle continues fencing partial acquisition
fills atomically and retaining recovery obligations. Undersized, rejected,
pending-cancel and contradictory protective orders cannot prove coverage or
release their sell reservations. Native fixture reservations and tactical closing
orders share the existing independent reduction-only boundary. A cancel request
is not cancellation, and no uncertain order is retried without reconciliation.

Existing full reconciliation compares positions, executions/corrections,
commissions, open/terminal order identities/status and quantities; missing or
contradictory evidence blocks readiness and flat finalization. It remains the
single authoritative economic ledger. No second position ledger or callback-
status-as-fill accounting was introduced. New native-stop observations are
strictly diagnostic, not an alternate reconciliation authority.

**Still incomplete:** the legacy research paths without a configured strict
protection policy retain their existing acquisition behavior, while now recording
obligations. A complete enforced real native-protection acquisition lifecycle,
native stop intent/transmission, execution normalization, full-history correction
recovery and safe replacement remain unimplemented/unverified. The real broker
boundary remains disabled. Do not treat synthetic feasibility or obligations as
production protection or permission to enable these paths.
Exact weighted-entry-minus-3.25 trigger compatibility with contract/broker price
increments also remains unverified; no rounding or threshold change was made.

### Read-only broker reality harness

`executor_broker_reality.py` and `ibkr_phase11_read_only.py` reuse existing exact
qualification, paper account allowlisting, read-only GUI attestation, one-shot
connection gating and mutation-disabled transport. They add bid/ask-size callbacks,
bounded incomplete depth events/capability observations and rich open-order
identity/type/parent/stop/status observations. An explicit opt-in depth request
checks the registered exact contract and limits requested rows; no request was
made in this phase. Imports open no connection and no new executable CLI was added.

No callback creates a trading session, native protection proof or complete broker
execution/fee history. Missing source timestamp is explicit: upper source-age
uncertainty is unbounded, never substituted with receipt time. An unrelated SPX
callback cannot refresh an old option-size or option-price observation. All
actual read-only validation steps remain blocked pending separate authorization;
see `mortificatio_phase11_broker_validation.md` for documentation/capability matrix.

## Deterministic admission and economic findings

`phase11_exit_capacity_benchmarks.json` contains eight deterministic admission
cases, ten authenticated application samples (five per path), fifty comparable
exit samples, raw timings, summaries and one retained exit profile. Everything
is explicitly synthetic/offline, excluded from actual trade history.

The admission fixture has $700,000 buying power, $10 option ask, original $10.20
entry cap, $0.65 entry/exit fees and explicit 80% entry realization. Severe stress
retains 50% of modeled size and marks the option at $3.90 after configured gap and
latency deterioration. These are sensitivity assumptions, not actual SPXW evidence.

| Case | Admitted ATM → OTM+4 | Modeled severe downside |
|---|---|---:|
| All configured constraints permit full size | 25 / 25 / 25 / 25 / 25 | $78,912.50 |
| $31,565 modeled-loss budget | 25 / 25 / 0 / 0 / 0 | $31,565.00 |
| $47,347.50 modeled-loss budget | 25 / 25 / 25 / 0 / 0 | $47,347.50 |
| ATM displayed bid only 18, discounted to 9 | 9 / 0 / 0 / 0 / 0 | $5,681.70 |
| ATM entry capacity only 8 | 8 / 0 / 0 / 0 / 0 | $5,050.40 |
| All exit liquidity disappears | Refused | Unavailable / no acquisition |
| Protection feasibility unverified | Refused | Unavailable / no acquisition |
| Reconciliation absent | Refused | Unavailable / no acquisition |

Affordability alone no longer determines the admitted target. In the 75-contract
budget case, the modeled full-125 downside would be $31,565 above that permitted
amount. This is **modeled risk avoidance**, not realized savings or a guarantee.
No new real-fill/economic advantage from slicing was demonstrated; Phase 10's
counterfactual findings remain applicable. Missing liquidity can still prevent
closing owned contracts: the unchanged exit price bounds do not guarantee flat.

Reproduce:

```sh
.venv/bin/python -B executor_phase11_benchmark.py --output phase11_exit_capacity_benchmarks.json --samples 5 --exit-samples 50
.venv/bin/python -B offline_test_runner.py
```

## Performance and bottleneck evidence

Host remains Python 3.9.6 / macOS 13.7.8 x86_64. Monotonic perf_counter_ns timing,
same Phase 9/10 ledger/adapter fixtures, shared and uncontrolled host load. Fake
adapter dispatch/ACK/fill injection is not actual IBKR transport/exchange latency.
All slow samples, including one profiled exit, are retained.

| Final exit fixture stage, ms | N | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|---:|
| Trigger → prepared | 50 | 8.318 | 14.517 | 20.929 | 20.929 |
| Trigger → durable synthetic adapter | 50 | 38.315 | 54.562 | 71.942 | 71.942 |
| Receipt → adapter | 50 | 39.832 | 58.354 | 74.529 | 74.529 |
| Repeated hard-admission checks | 60 | 3.191 | 5.050 | 5.648 | 5.648 |

Prepared authenticated application samples (N=5): click-to-capital commit median
49.120 ms / max 72.896; click-to-adapter 254.426 / 438.818; exit event-to-adapter
108.774 / 171.041; exit event-to-confirmed fixture flat 671.155 / 1133.166.
Raw p95/p99/max and all other recorded stages are in JSON. Five samples do **not**
establish tail acceptance. Repeated admission checks are not 60 independent trades.
Candidate lookup/final validation, protection-obligation creation and complete-
flat processing are included in aggregate application/fill paths; they are not
independently instrumented per-line latency guarantees.

Phase 10 exit p99 was 177.902 ms; the final equivalent-fixture rerun was 71.942 ms.
Earlier Phase 11 diagnostic runs varied with host load. These variations do not establish
a reliable exit improvement: there is no controlled identical-load paired exit
experiment or changed exit algorithm. The 5 ms target remains unmet. Phase 10
application samples were N=10/path versus N=5/path here, so they are descriptive
comparisons, not evidence of production speedup.

The one measured exit profile shows SQLite/source-fixture synchronization rather
than an expensive numerical prediction loop: snapshot lock wrapper cumulative
31.333 ms, source synchronization 19.244 ms, fixture DB context 17.096 ms, and
SQLite connection close alone 12.272 ms self time. These overlapping cumulative
figures must not be summed. This identifies concrete storage/synchronization work
to investigate, not proof of the cause of every historical 177.9 ms tail. No safety
commit, reconciliation check or checkpoint was removed. Rust/C++ is not justified.

## Verification

New tests: 32 methods in four scripts: 11 capacity, nine authenticated lifecycle,
eight fake broker-reality callbacks and four protection. The capacity suite also
runs 200 seeded cases with an independent exhaustive oracle. Focused suites pass.
Final complete regression: **103 offline scripts passed, 0 failed**, with five
broker-connected scripts excluded. Command: `.venv/bin/python -B
offline_test_runner.py`; final output retained in
`/private/tmp/mortificatio-phase11-offline-suite-handoff.log`. This final run
includes all 32 new targeted methods and the last read-only bootstrap fix.
The prior post-remediation complete run also passed 103/0. No failure was waived.
The order-boundary guard and `git diff --check` passed. Artifact checks confirmed
eight admission cases, ten application samples and fifty exit samples, valid
25/125 ATM-first vectors, zero admitted quantities on rejection, and zero reported
hard safety-invariant violations in the benchmark cases. This is finite offline
evidence, not proof of real broker execution or universally safe liquidity.

The initial complete run found seven failing scripts (96 passed). Regression
investigation identified provider snapshots overwriting newer streamed quotes,
then correctly triggering duplicate-identity quarantine. This was fixed by
independent validation of both evidence paths, retained stream state and
acquisition-only fencing of bad provider evidence. Two targeted tests cover
streamed deterioration hidden by a snapshot and conflicting provider identity
without economic quarantine. The original no-chase test now expects explicit
hard-admission refusal while retaining its fence/capital invariants. Live fixture
updates carry distinct synthetic observation identities; no duplicate guard was
weakened. The full suite was rerun after these fixes.
An additional fake-callback test verifies that index size messages arriving before
option registration do not abort the read-only bootstrap or become entry evidence.

Existing tests continue supplying late/duplicate/out-of-order callbacks,
execution-before-status, lost callbacks, emergency/cancel races, five partial
fills, foundation/disk outages, contention, restart, grant expiration/revocation,
account mismatches, Blind Execution/history, UI/passkeys and reconciliation.
The new hard-gate tests add spread expansion, current evidence deterioration,
post-admission liquidity failure, full/limited funding/risk/capacity and the
mandatory stale-option-with-unrelated-receipts case. Protection tests retain
undersized/rejected obligations, pending-cancel reservations and contradictory
identities. Real halts, book/queue dynamics and broker-native failures are not
verified by these finite synthetic scenarios.

## Preservation and remaining requirements

No visual/trading controls were changed: CALL, PUT, EXIT ALL only. Same-day SPXW,
strict ATM-first five-strike allocation, 25/125 limits, no discretionary scaleouts
or additions. Initial stop is **weighted actual option premium minus 3.25**;
early +4 arms +1.25 favorable **SPX-index** reversal protection; +5 activates the
uncapped three-SPX-point peak reversal and supersedes early protection. Emergency
fencing/independent reduction, persistent recovery, account isolation, passkeys,
grants and Blind Execution remain intact. No new internal model/protection prices
were added to browser-facing projections. Realized results still require full
reconciliation and released reservations.

Highest-value Phase 12 recommendations, **not implementation authorization**:

1. Complete native stop intent/execution/correction and full-history reconciliation,
   mandatory production protection-policy enforcement and cancel/replace/OCA
   recovery against fakes; never enable native acquisition from a feasibility flag.
2. Supply reviewed supported runtime/wheels, complete hash lock and current
   advisory clearance; run actual migration/regression/security acceptance.
3. Separately authorize narrow read-only paper diagnostics to establish actual
   account/entitlements/source timestamps/depth/session recovery. No broker order
   tests are authorized by this report. Keep entries blocked if authoritative
   source-time uncertainty cannot satisfy the unchanged 0.25-second requirement.
4. Obtain licensed observations; validate/tune explicit account-approved loss,
   liquidity and deadline assumptions. Current BBO haircuts are not a calibrated
   liquidation-capacity guarantee. Review fee minima and nonlinear impact.
5. Profile persistent SQLite/source operations under controlled load with crash
   proofs before optimizing transactions. Retain independent protective checkpoints,
   secure permissions/recovery/HTTPS/physical-key/supervision and external alerts.

Paper and live independently remain **NO-GO**. Passing offline tests cannot
establish enforceable native stops, real account permission, executable liquidity,
actual protection gaps, real fill speed or supported-runtime security.

## Exact Phase 11 files

Created: `executor_exit_capacity.py`, `executor_broker_reality.py`,
`ibkr_phase11_read_only.py`, `executor_phase11_benchmark.py`,
`executor_exit_capacity_test.py`, `executor_phase11_lifecycle_test.py`,
`executor_broker_reality_test.py`, `executor_phase11_protection_test.py`,
`mortificatio_phase11_acceptance.md`, `phase11_exit_capacity_benchmarks.json`,
`mortificatio_phase11_broker_validation.md`, `mortificatio_phase11_runtime.md`,
`phase11_runtime_audit.json`.

Modified: `executor_allocation.py`, `executor_allocation_service.py`,
`executor_entry_readiness.py`, `executor_execution_coordinator.py`,
`executor_portfolio.py`, `executor_allocation_test.py`, `executor_lifecycle_test.py`,
`executor_allocation_service_test.py`, `order_boundary_test.py`.

The two existing fixture modules now explicitly provide synthetic capacity policy
and account-context evidence. No default production budget was introduced and no
historical Phase 8/9/10 benchmark artifact was rewritten. All unrelated pre-existing
uncommitted work was retained. No broker execution boundary was enabled.
