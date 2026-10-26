# Mortificatio Phase 12 — protection lifecycle and fail-closed reconciliation

Review date: 2026-10-25. Work remained offline. **Paper: NO-GO. Live: NO-GO.**
No broker connection, credentials, real/paper order, execution enablement,
deployment, commit or push occurred. No Phase 13 work began.

## Prior evidence checked

Inspected Phase 9–11 reports/runtime notes, relevant broker findings, benchmark
JSON and actual order/allocation/protection/history/read-only source before edits.
Phase 11's raw JSON is complete, contains eight admission cases, ten application
samples and fifty exit samples, and reports trigger-to-adapter p99 71.941592 ms.
Its four new test files contain 32 methods, including the 200-case seeded sizing
oracle. The retained Phase 11 full-run log reports 103 passed/0 failed, five
connected scripts excluded. These establish recorded offline evidence, not actual
broker observation or production readiness. No real observation was found.

## Implemented architecture

### One economic order/fill ledger

`executor_native_protection.py` coordinates managed STP intentions inside the
existing portfolio journal and order dictionary. Native fills, corrections,
busts and fees use the same execution/commission records as ordinary orders.
There is no second position or realized-P&L ledger.

`OrderPhase` and `order_lifecycle` derive explicit PREPARED, SUBMISSION_PENDING,
BROKER_ACKNOWLEDGED, WORKING, PARTIALLY_FILLED, FILLED, CANCELLATION_PENDING,
CANCELED, REJECTED, EXPIRED and RECONCILIATION_REQUIRED views from durable facts.
They expose actual active executed quantity separately from reported quantities,
IDs, local observation revisions, remaining quantity, terminal reconciliation and
cancel/replace intent. A filled order does not mean the portfolio is flat.

Raw callbacks are not mapped one-to-one to these states. PreSubmitted does not
prove working protection. Inactive alone remains unresolved; a correlated synthetic
code-201 rejection can be recorded separately, without releasing quantity. Expired
requires an explicit reason after terminal reconciliation, not a local timer.
Cancellation pending never means canceled. `order_revision` is a local durable
observation revision, not an invented broker modification sequence.

Native stop plans reserve only confirmed, uncommitted long quantity after all
managed tactical/native and known external closing reservations. They use exact
contract identity and weighted actual entry premium minus 3.25. Plan references
are namespaced by journal identity and request identity; retries cannot bind an
already-bound uncertain intention. No outbound native order implementation exists.
`bind_synthetic` reserves a mock identity durably, not a real transmitted order.
Native intentions cannot bypass the ordinary limit-dispatch boundary.

### Mandatory protection readiness and fill obligations

Phase 11 hard capital/exit-capacity admission remains mandatory. It now additionally
requires an immutable ProtectionReadiness record bound to the approved account,
session generation and complete exact offered contracts, STP type, explicit
3.25 premium offset and FENCE_RECONCILE_PROTECT partial-fill policy. A feasibility
flag alone is rejected before capital reservation. The only accepted evidence
mode is OFFLINE_FIXTURE, with real-broker/working-confirmation flags false.

Each new funded portfolio activates native recovery before acquisition. Every
confirmed fill/correction durably recomputes ownership and required protection in
the same economic transaction. Missing protection fences further acquisition and
causes outstanding entries to be canceled/reconciled through existing independent
handling. Already accepted entries may still fill; they remain accounted for.
Coverage becoming synthetic-working does not automatically resume acquisitions.

Supported plan, prepared stop, reserved identity, acknowledgment and reconciled
working quantity remain distinct. Current coverage requires fresh full broker-
fixture reconciliation, current generation, matching IDs/contract/stop price,
Submitted state and sufficient residual protective quantity. All coverage remains
**synthetic**, never real_broker_verified. Partial/rejected/stale/ambiguous coverage
is critical/unresolved, not protected. Existing broker/market critical warnings
retain precedence; the native condition remains independently visible.

Obligations retain original execution identity/quantity/premium and correction
activity through restart. They now carry historical per-strike aggregate coverage
as of a reconciliation cycle/digest; those fields can legitimately refresh after
reconnect and are not additive per-fill quantities or permanent authority.
Current protection still requires the live freshness/generation checks.

Legacy research journals and their conditional strategies are not silently
converted to native broker protection. New admissions require the new readiness
record; restart seals unfinished acquisition in old journals. Existing independent
application protection/reconciliation remains available. Adoption of legacy
external stops into a real managed-native adapter requires explicit review.

### Native stops versus tactical/emergency exits

Native intentions are SELL-only. They share the existing reservation book with
application stops, SPX trailing exits and EXIT ALL. A stop trigger execution
enters the existing INITIAL_STOP exit path; emergency reason retains precedence.
A native STP execution is recorded at its actual supplied price, including gaps,
rather than being incorrectly rejected as an LMT fill below a limit.

Replacement requests retain old stop reservations and return non-executable
cancellation descriptors. A replacement cannot be prepared against those contracts
until terminal cancellation is reconciled. Old stops can still execute during
cancellation; actual executed quantity reduces both ownership and reserved residual.
When a BUY correction makes working closing quantities exceed ownership, retain
the correction, request cancellation descriptors and report uncertainty. Never
invent cancellation, an OCA guarantee or a compensating trade.

The modeled cancel-confirmed-then-reduce path prevents overlapping sell authority
but cannot guarantee continuous broker protection during a cancel/new-order gap.
No real atomic replacement, bracket transmission, native stop dispatch or OCA
routing was enabled. Unfilled exposure/unknown scope remains unresolved. Broker
behavior and a production escalation/replacement policy still require validation.

### Authoritative reconciliation and corrections

Existing complete account/position/order/execution/commission comparison remains
authoritative. New managed stops participate in that same full comparison.
For native-enabled journals, a fully validated snapshot can supply a missing or
out-of-order status callback. It cannot supply fictitious executions: local active
execution quantity, full history/fee scope, exact IDs/account/contract, status and
remaining quantities must all agree. Duplicate permanent IDs are rejected.

Missing status facts are journaled before final reconciliation. A crash between
those commits leaves terminal proof/freshness unavailable and reservations held.
Identical status facts need not be rewritten: the required reconciliation event
still durably records the new complete snapshot digest. Economic executions and
corrections always retain the full rollback fallback; no safety commit was removed.

Corrections supersede explicit earlier execution revisions; busts remove their
quantity economically. Positions, weighted entry stops, current obligations,
source accounting and terminal evidence are reevaluated. A bust restoring closed
exposure revokes history finalization and reholds capital. Accounting uses recorded
execution prices/quantities and commissions, with Decimal, never Greek estimates.
The source fake now accepts zero-price zero-quantity bust evidence explicitly.

Contradictory corrections implying short/excess exposure are durably quarantined
with evidence rather than silently accepted, repaired or reported flat. Automatic
recovery of ambiguous multi-execution correction groups remains incomplete.
Uncertain/absent/zero-perm orders cannot be retried based on absence alone.

Normalized snapshot quantities are a strict internal contract (remaining equals
original quantity minus active fills). The real API's canceled/filled/remaining,
held and completed-order semantics still need a reviewed normalizer. The read-only
capture retains raw fields and does not pretend that mapping is already verified.

### Read-only execution evidence

`BrokerLifecycleValidation` adds private persistent hash-audited callback facts and
receipt times: order status/held reason, executions/families/source-time strings,
fees, open orders and broker errors. Duplicate executions are deduplicated;
contradictory payloads are retained as conflict records and fail validation.
Row/audit integrity is checked on read, including receipt fields. This archive is
not the authoritative position ledger or a complete-history attestation.

`LifecycleReadOnlyProbe` inherits every mutation-disabled method. A narrowly
allowlisted, one-shot, account-filtered reqExecutions observation is available
only after existing paper/session/settings gates and separate opt-in. The static
exception is confined to that guarded method; no mutation prohibition was relaxed.
No request or connection was made. execDetailsEnd does not establish all-intent
coverage; missing time-window/history/fees remain blockers. Execution time is kept
verbatim with unverified clock domain, never substituted for market quote time.

## Strategy, UI, security and recovery preserved

CALL / PUT / EXIT ALL remain the only trading actions. No UI controls, manual
quantities/strikes, extra confirmation, directional prediction or withdrawal was
added. Same-day SPXW, actual listed ladder, .48 delta targeting, strict ATM-first
funding, 25 per strike/125 total and configured capital/risk limits remain.

Initial hard stop remains **option premium WAP minus 3.25**. Early +4 arm / +1.25
reversal and +5 uncapped three-point trailing reversal remain **SPX index points**,
symmetrical for CALL/PUT. No fixed target/automatic scaleout was activated.

WebAuthn, separate grants, CSRF, account isolation, command idempotency and trade
frequency remain authoritative. A duplicate authenticated command can return its
recorded result even during native recovery; it cannot dispatch again. Protection
planning/binding simulation requires independent reduction-service capabilities,
not the dashboard session or trading grant. Grant expiry/foundation outage does
not abandon known exposure. No native prices/proof details enter browser DTOs;
only existing sanitized risk-health signaling was strengthened. Realized results
remain hidden until verified complete flatness and reservation release.

Restart/disconnect invalidate native generation proof and preserve obligations,
working quantities and uncertain IDs. There is no in-memory-only recovery, blind
retry, automatic emergency rearm or broker credential storage. Independent
checkpoints and FULL/WAL journal safeguards remain intact.

## Tests and adversarial evidence

Three new scripts contain **38 targeted methods**: 23 native coordination, eight
authenticated lifecycle/accounting and seven read-only capture tests. Native
benchmark JSON repeats all 23 deterministic native cases with assertions.
The original 200-case sizing oracle and prior regressions remain in the full suite.

Coverage includes all requested scenarios: partial fill before protection ack;
rejection; acknowledged-not-working; insufficient quantity; execution duplicates;
missing status; late fills during cancellation; stop during EXIT ALL; replacement
before cancellation; conflicting positions; cold restart; unexpected orders;
quantity corrections; bust restoring flat exposure; simultaneous triggers; SQLite
reconciliation failure; expired grants; residual liquidation; zero-position report
with working sells; five stops during emergency. Additional tests cover blind
accounting revocation/re-finalization, exact-readiness refusal, private-capture
restart/tampering, account isolation and disabled mutation APIs.

All cases are **synthetic**. The old canceled-stop/helper is not used to invent a
second fill while also filling the newly authorized close. A contradictory broker
fill remains a quarantine case, not permission to locally oversell.

Initial full run: 101 passed/5 failed. Code was corrected so
recorded duplicates stay idempotent and existing critical market alerts retain
priority. Legacy assertions were updated to verify the stronger entry fence,
unchanged capital and refresh/session behavior. A subsequent 105/1 run isolated an
old whole-obligation equality assertion; it now preserves economic facts while
allowing validated proof cycle/digest refresh. No safety threshold was weakened.
Final full regression: **106 offline scripts passed, 0 failed**, with all five
broker-connected scripts excluded. Command: `.venv/bin/python -B
offline_test_runner.py`; output retained in
`/private/tmp/mortificatio-phase12-offline-suite-handoff.log`. This includes all
38 targeted methods and prior regression/browser/virtual-passkey tests. No
failure was waived. `git diff --check` and the read-only order-boundary guard
passed. Artifact assertions verified 23 adversarial cases, 50 equivalent exit
samples, zero reported hard safety violations, and the native exercise's empty
position book/FLAT_CONFIRMED only after full reconciliation.

## Performance evidence

`phase12_protection_reconciliation_benchmarks.json` contains raw host samples,
23 passing adversarial cases, native persistence/obligation/coverage measurements,
fifty equivalent Phase 11 exit samples and five prepared authenticated application
samples. All synthetic; no actual broker dispatch/ack/queue/fill latency measured.
Host: Python 3.9.6 / macOS 13.7.8 x86_64, concurrent offline testing, uncontrolled
load. Virtual broker clock is fixed to isolate host cost, not certify real freshness.

| Operation, ms | N | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|---:|
| Broker order event ingestion | 50 | 2.182 | 3.301 | 4.028 | 4.028 |
| Durable duplicate execution receipt | 50 | 2.040 | 3.705 | 4.245 | 4.245 |
| Complete position/order reconciliation | 50 | 5.115 | 6.893 | 17.173 | 17.173 |
| Coverage projection | 50 | .258 | .463 | .570 | .570 |
| Obligation update within economic event | 10 | .025 | .035 | .035 | .035 |
| Equivalent exit trigger → synthetic adapter | 50 | 38.658 | 43.503 | 48.221 | 48.221 |

Native stop planning/binding sample counts are five each; complete-flat proof has
one sample. Their raw descriptive distributions are included but cannot establish
tail acceptance. The measured five-stop execution-ingestion-to-confirmed-flat
sequence was 40.117 ms; it excludes actual market wait/routing. Prepared authenticated
click-to-adapter median was 268.525 ms, max 271.997 ms, N=5. See raw JSON for all
stages and persistence-kind distributions; nested samples are not independent
trades and overlapping stage timings must not be summed.

Phase 11's equivalent exit p99 was 71.942 ms. The current 48.221 ms rerun uses the
same ledger fixture, not identical controlled host load. It does not establish a
causal speed improvement. The native model/application paths are different workloads,
not interchangeable latency numbers. **The 5 ms objective remains unmet.** No
compiled rewrite, real-time guarantee or removed durable check was justified.

The synthetic 125-contract stop exercise records $10 entry, $6.50 exit, $162.50
explicit per-contract round-trip fees: gross −$43,750; net −$43,912.50. This shows
gap/accounting behavior, not market forecasting or real economic performance.
No new slicing advantage or real protection/fill-speed benefit was demonstrated.

Reproduce:

```sh
.venv/bin/python -B executor_phase12_benchmark.py --output phase12_protection_reconciliation_benchmarks.json --samples 50
.venv/bin/python -B offline_test_runner.py
```

## Remaining blockers and next scope

Paper and live remain **NO-GO**. No real broker observations were obtained.
Native intent/transmission and automatic acquisition/protection orchestration are
not enabled. The current model fences unresolved partial entry and does not
promise full target completion or continuous real coverage through replacement.
Exact stop grid/trigger method, exchange-native versus broker-simulated handling,
parent/child/OCA, zero-perm rejection, raw remaining/terminal normalization,
execution/fee history coverage and ambiguous correction recovery require broker
validation and an approved production adapter/policy. See the broker lifecycle
matrix; read-only testing alone cannot prove stop execution/cancel/replace behavior.

Runtime/security migration remains blocked: see `phase12_runtime_audit.json` and
`mortificatio_phase12_runtime_readiness.md`. Authoritative quote/Greek timestamps,
entitlements, exact calendars, trusted HTTPS/physical keys, external alerts,
supervised service/restore drills and operating procedures remain prerequisites.

Recommended next phase, not begun: complete supported-runtime acceptance and
reviewed real-history normalization/adoption tests, then separately authorize
narrow paper read-only captures. Design controlled paper-order acceptance only
after that evidence and explicit separate permission. Keep automatic real
submission disabled until native protection/recovery and all prior gates are proven.

## Exact Phase 12 files

Created: `executor_native_protection.py`, `executor_broker_lifecycle_capture.py`,
`ibkr_phase12_read_only.py`, `executor_native_protection_test.py`,
`executor_phase12_lifecycle_test.py`, `executor_broker_lifecycle_capture_test.py`,
`executor_phase12_benchmark.py`, `phase12_protection_reconciliation_benchmarks.json`,
`mortificatio_phase12_acceptance.md`, `mortificatio_phase12_broker_lifecycle.md`,
`mortificatio_phase12_runtime_readiness.md`, `phase12_runtime_audit.json`.

Modified: `executor_exit_capacity.py`, `executor_allocation_test.py`,
`executor_lifecycle_test.py`, `executor_entry_readiness_test.py`,
`executor_phase10_lifecycle_test.py`, `executor_portfolio.py`,
`executor_paper_reconciliation.py`, `executor_liquidation.py`, `executor_lifecycle.py`,
`executor_blind_presentation.py`, `executor_offline_fixture.py`,
`order_boundary_test.py`, `executor_allocation_service_test.py`,
`executor_dashboard_test.py`, `executor_lifecycle_recovery_test.py`,
`executor_phase11_lifecycle_test.py`.

Other pre-existing uncommitted work and historical benchmark artifacts were
preserved. No actual broker-order boundary was enabled or replaced.
