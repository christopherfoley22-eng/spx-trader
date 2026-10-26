# Mortificatio Phase 3 — unified OFFLINE execution lifecycle

No IBKR connection, real broker order submission/modification/cancellation,
paper/live enablement, listener, deployment, commit or push is added. The visual
interface is unchanged. Every broker interaction in this phase is an explicitly
synthetic fixture operation. No mock is represented as authenticated IBKR.

## Components and boundaries

`executor_lifecycle.py` adds `OfflineExecutionLifecycle`, a backend facade around
Phase 1 `UnifiedApplication`, Phase 2 `AllocationBoundary`, the existing durable
`OfflinePortfolioLedger`, and `IndependentProtectionService`. Existing
`FoundationAPI(lifecycle)` supports the same authenticated command/authorization/
status/emergency contract without changes to the API adapter or legacy dashboard.
There is no additional confirmation, strike selector, or quantity input.

`executor_offline_fixture.py` adds a separate durable SYNTHETIC broker-side book.
It models acknowledgements and cancellation observations, preserves callback
ordering, and supplies full account snapshots. Fills are NEVER automatic: a test
must explicitly inject quantity, price, unique execution ID and source execution
time. Fees may be delayed/absent. The fixture has no socket, client, IBKR import,
actual order API or live execution flag. This is an integration-test source, not
an order adapter ready to connect to IBKR.

Existing modules changed only for narrow integration:

- `executor_portfolio.py`: durable verified-fixture session context, source fill
  time metadata, globally namespaced funded order references and optional central
  order-ID starts. Existing unfunded research paths retain their old references.
- `executor_allocation_service.py`: explicit global broker versus local journal
  generation mapping, optional global model order IDs, and background entry
  checks that do not touch dashboard idle activity.
- `mortificatio_auth.py`: optional `touch=False` for already authenticated service
  checks; all existing interactive callers retain the default touch behavior.
- `mortificatio_protection.py`: reduction-only fake binding method that rejects
  every BUY and requires an exit latch plus existing reconciliation/reservations.
- `order_boundary_test.py`: extends the existing no-IBKR-client/no-mutation guard.

## Construction and operation

The trusted local operator supplies the existing independent service capability,
explicit approved paper configuration, clocks, an allocation fixture provider,
synthetic broker admission evidence and a durable `OfflineBrokerFixture` with an
explicit history coverage start. No secrets/capabilities are auto-provisioned by
HTTP or persisted as dashboard credentials in lifecycle state.

Construct `OfflineExecutionLifecycle(application, private_state_root, approval,
fixture, service_token, wall_clock, monotonic_clock)`. The independent supervisor
starts by default and remains separate from dashboard lifetime. Tests may select
`start_worker=False`, reported as MANUAL_OFFLINE_HARNESS; this is a deterministic
offline testing mode, not a production execution setting. The worker can also be
started explicitly. There is no browser control to change this mode.

Before admission, explicitly call `verify_broker_session(service_token,
PaperSessionEvidence(...))`. The existing paper ledger validates the allowlisted
DU account, managed account set, PAPER mode, read-only flag, approval reference,
nonce, local verification time/expiry and isolated host/port configuration.
Live, ambiguous, unknown, expired and unverified evidence is rejected. The object
must be OFFLINE_FIXTURE. None of these tests verify an actual TWS session or API
Read-Only setting. A socket connection is never accepted as identity evidence.

The account anchor maintains a monotonic synthetic broker-session generation.
Each trade journal has its own local generation, explicitly bound to that global
generation and session nonce. This permits a new trade journal after reconnect
without resetting the account's authorization epoch or reusing an old grant.
Attaching an additional journal records local validation of the currently active
synthetic session; this is not an exchange timestamp, IBKR login, or 2FA claim.

Use only the facade as the external application boundary. Its CALL/PUT command:

1. Validates the dashboard session, CSRF, direction-only command identity and
   epoch. An identical command retrieves its durable decision and cannot redrive
   entry. Conflicting intent, stale epochs and concurrent competing trades fail.
2. Drains already available callbacks and reconciles the complete account before
   new admission. Queued late fills cannot be ignored in favor of a cached proof.
3. Requires independent supervision (in default background mode), verified
   synthetic broker evidence, current risk/market/funds checks and a separate
   fresh trading grant. Phase 1 account frequency remains at most two filled
   trades per New York day. Existing unreleased reservations remain a fence.
4. Atomically creates the immutable automatic five-strike allocation and capital
   reservation through the existing application/allocator transaction. Maximum
   25/strike and 125 total; no manual targets or changes to ATM-first rules.
5. Registers one durable trade journal, binds its funding/identity through Phase
   2, and delegates to the existing slicing/price-protected portfolio scheduler.
6. Reconciles before assigning centrally reserved model order IDs, persists the
   model bindings, then records synthetic acknowledgements independently per
   strike. This is SIMULATED dispatch, not actual broker order submission. A
   five-strike group is never assumed to fill atomically.

Callbacks, market events, or periodic supervision continue the original bounded
acquisition sequence only while the original in-memory entry lease, dashboard
session, original trading grant, account, broker epoch and every existing gate
remain valid. Tokens/CSRF values are held in memory only. Price improvements,
partial cancellations, restart or a new login cannot increase the immutable
allocation or automatically restart a halted/completed entry sequence.

## Independent protection and emergency behavior

The protective service uses its own local capability for observation,
reconciliation and reduction. It never receives entry permission from that
capability. Background checks do not extend dashboard idle time. Logout, idle or
absolute session expiry, grant expiry/revocation and broker disconnects halt
further acquisition and retain unresolved reservations. The independent service
can continue cancellations, risk observation, reconciliation and exit planning.

Existing strategy thresholds/semantics are unchanged:

- Each owned strike uses its actual weighted-average entry premium minus 3.25.
  This protection remains applicable after profit protection/trailing activation.
- At +4 favorable SPX points, arm the existing +1.25 reversal protection.
- At +5, supersede that early rule with an uncapped best-favorable-SPX trail,
  exiting after a 3-point reversal. CALL/PUT direction remains symmetrical.
- There are no fixed profit targets, automatic scale-outs or position additions.
- The existing portfolio policy escalates a struck premium stop to portfolio
  liquidation. Stops are calculated per strike; this phase does not change that
  established portfolio-wide exit scope.

Valid SPX observations before the first confirmed fill do not arm or halt a
nonexistent position. Already available fill callbacks are drained before SPX
risk evaluation. Source ordering, exact contracts, live-feed quality, quote age,
Greeks and the 0.25-second synchronization requirement remain unchanged. Source
and local receipt times stay separate; missing source timestamps are never
replaced by receipt time. Bad market events halt acquisition rather than permit
fresh entries from old or fabricated evidence.

EXIT ALL persists the existing account admission fence before waiting for the
lifecycle lock or ordinary strategy work. The independent emergency latch remains
idempotent and cannot auto-rearm. The supervisor mirrors it to every managed
portfolio, cancels/reconciles acquisition, and plans SELL quantities only against
confirmed owned positions minus existing sell reservations. Cancellation intent
never proves cancellation or no late fills. Emergency does not depend on entry
eligibility, a trading grant, or ordinary quote freshness to block/cancel entry.
An actual exit still requires valid broker/quantity evidence and usable quotes;
no immediate fill or guaranteed liquidity is claimed.

**Protection is explicitly OFFLINE_CONDITIONAL_MODEL_NOT_BROKER_HELD.** This phase
does not create actual broker-held stops. A computed stop does not imply active
broker coverage. If a strike's exit quote is stale or unusable, its exposure
remains unresolved and alerts persist until validated evidence permits planning.
A process outage cannot guarantee application-managed stops or observe an unseen
SPX peak; actual broker-held coverage and gap handling remain deployment blockers.

## Reconciliation and durable recovery

The synthetic broker book is separate from portfolio journals and survives
service process death. All available callbacks are journaled before the durable
callback cursor advances. Source execution time is mandatory and persisted
separately from local execution callback receipt time. Duplicate executions,
callbacks and commission records use existing ledger identities/correction rules;
conflicting records quarantine entry rather than create duplicate exposure.

A complete account snapshot is validated against the UNION of all managed trade
journals before normalization into per-journal snapshots. Unknown positions,
orders or executions, duplicate identities, overlapping active trade ownership,
contract discrepancies, shorts, more than five strikes or quantity cap violations
fail closed. Existing validators then check each certified partition, retaining
the original component receipt times and history coverage. Global/local generation
mapping does not substitute market timestamps. Missing fees, uncertain orders,
incomplete history, stale/incoherent proofs and position disagreement block final
flat status and capital release.

A durable publication outbox bridges portfolio reconciliation to Phase 1 command
state. The lifecycle reserves each immutable revision/evidence identity before
delivery. A crash after the application accepts a revision but before local
acknowledgement replays the same evidence, not a conflicting new payload. Terminal
application state alone cannot release funds: the existing allocation bridge also
requires the exact pinned portfolio journal and fresh complete broker evidence,
zero positions, terminal orders and sealed acquisition.

The foundation SQLite database receives additive tables:
`execution_service` (pinned root and reporting mirrors of order-ID counter/callback cursor),
`execution_lifecycle` (journal, state, reason and revision), and
`execution_publications` (durable delivery outbox). Global model order IDs are
reserved before portfolio binding and are never reused, even after a crash.
Order references include the immutable trade ID. A process lock permits one
lifecycle owner for the pinned service root; portfolio writer locks still apply.

Phase 7 moves the authoritative global IDs, callback cursor, managed-journal
inventory and health fence into a separate hash-checked `protection.sqlite`
checkpoint in the service root. Engine journals remain the economic source of
truth. Foundation failures fence entry but do not gate essential callbacks,
protective market events, reconciliation or reduction-only simulated bindings.
`executor_protection_recovery.recover_offline_protection` can cold-start an
existing inventory without constructing dashboard authentication. It denies all
dashboard operations and acquisition, requires the independent local capability,
and still requires explicitly verified synthetic broker-session evidence. No
broker connection or actual order execution is added. See
`mortificatio_phase7_acceptance.md` for migration, critical conditions and runbooks.

On restart, entry tokens/leases are NOT restored. Every recovered acquisition is
durably sealed. An explicit new synthetic session and full reconciliation are
required before any model execution capability returns. Owned quantities,
execution identities, pending/uncertain orders, premium cost, favorable peak and
trailing state are replayed. Reduction authority remains independent of login.
A missing/replaced bound journal is recovery-required, never silently recreated.
A reserved command interrupted before journal configuration stays visibly
unresolved and retains funds; no automatic speculative release or retry is added.
Configured orders with no model bindings can close only with complete zero-fill
account reconciliation. A binding with no broker-side order is uncertain:
absence is not non-submission proof and the lifecycle never retries it.

Late fills after a completed trade restore the old reservation/fence and trigger
reconciliation/reduction rather than a new entry. The account remains blocked;
normal trading does not automatically rearm after emergency or recovery faults.
When multiple journals would ambiguously own the same contract, the whole-account
comparison blocks rather than inventing ownership or risking duplicate sells.

## Read-only status and integration limits

`status(session)` remains authenticated. Independent `supervise(service_token)`
returns service-scoped structured status without a dashboard session. Status
reports synthetic origin, actual execution disabled, supervisor mode/health,
broker connectivity, account proof health, direction, allocation, owned counts,
pending orders, reservation/reconciliation/recovery state, computed premium stops,
quote health, peak, early protection and trailing thresholds, and emergency state.
Unknown journal quantities remain unknown, not zero. No active unrealized dollar
P&L, percentage return, fabricated realized result, or completed-trade statistics
are exposed. FLAT_CONFIRMED requires current broker evidence plus terminal
application state and released allocation reservation; empty local quantities or
intent alone cannot display flat. Stale/disconnected evidence reports pending.

The existing legacy simulation web server/dashboard is not modified or secured
by this facade. Future transport/UI integration must use `FoundationAPI(lifecycle)`
and remove alternate externally callable entry paths. No HTTP listener, TLS
adapter or visual dashboard work is included in Phase 3.

Security/operational limitations remain:

- No real broker login, TWS read-only verification, IBKR account/buying-power
  verification, actual fees, entitlement, fill speed/slippage or broker-held stop
  coverage has been tested. Authoritative SPX/SPXW timestamps capable of satisfying
  the 0.25s gate remain an unresolved IBKR integration blocker. Execution stays off.
- Source fixture history/market inputs are explicit test assertions. They are
  not cryptographic proof of broker truth or a real market-data entitlement.
- Existing Python/library modernization, HTTPS/browser/passkey integration,
  secure key recovery, external alert delivery and external audit anchoring remain
  incomplete. Trusted local capabilities require OS/process/file isolation.
- SQLite hash chains do not protect against a compromised administrator rewriting
  all state/anchors. The root lock is single-host coordination, not distributed
  fencing. Do not operate independent copies of the central account registry.
- Unconfigured reservations, unknown/absent uncertain orders, missing journals,
  ambiguous ownership and broker corrections may require explicit operator-led
  recovery. This phase adds no permissive reset, rearm, or lost-journal bypass.
- A continuously available real supervisor, source callback routing and bounded
  broker-held protection/cancel-replace safety require separate validation.
  The synthetic fixture is not a substitute for those tests. Performance,
  retention and operational monitoring need validation before deployment.

## Offline tests

`executor_lifecycle_test.py` covers CALL/PUT entry, allocation/partial fills,
existing stops/trailing, emergency during entry, grant/session expiration,
disconnect/reconnect, late/duplicate fills, concurrent commands, wrong broker
account/environment/read-only evidence, missing commissions, structured blind
status, and no network/order boundaries. `executor_lifecycle_recovery_test.py`
covers publication crash windows, partial synthetic acceptance, unresolved
reservations, actual child-process death, global IDs across trades/reconnects,
idle expiration, automatic/independent supervision, stale reconciliation, source
execution times, premium protection after trailing, and closed-service fencing.

Run:

```
.venv/bin/python -B executor_lifecycle_test.py
.venv/bin/python -B executor_lifecycle_recovery_test.py
.venv/bin/python -B offline_test_runner.py
```

Phase 4 has not begun. Next work requires separate authorization; no paper/live
execution or dashboard deployment is enabled by this phase.

## Final verification results

Final complete offline regression: **74 standalone scripts passed, 0 failed**;
all **5 IBKR-connected scripts excluded**. New Phase 3 tests: **19 lifecycle +
20 recovery/failure-injection = 39 passed, 0 failed**. Existing Phase 1/2,
strategy, market-data, portfolio, protection, replay and broker-boundary scripts
passed. The new tests include an actual terminated child process and a running
independent supervisor. No actual broker connection or order mutation occurred.
No dashboard changes, deployment, commit or push occurred. Phase 4 has not begun.
