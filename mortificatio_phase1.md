# Mortificatio Phase 1 — offline application and security foundation

Status: OFFLINE ONLY. No broker connection, order submission, listener, deployment,
commit, or push. This foundation does not dispatch queued commands. The default
broker boundary denies authorization and admission. Mock evidence is explicitly
labeled OFFLINE_FIXTURE; it never represents a verified IBKR connection.

## Corrected allocation specification (future stage; not implemented here)

Allocate affordable ATM contracts first, up to 25. Fewer than 25 ATM contracts are
allowed when eligible capital is insufficient, provided configured minimum-entry,
liquidity, and risk requirements pass. Do not allocate OTM+1 until ATM has 25;
apply that same prerequisite successively through OTM+4. Cap each strike at 25
and the portfolio at 125. Include executable asks, multiplier 100, fees, slippage,
capital reserves, working orders, and partial fills in affordability. Never fund
the same capital twice. Do not skip an underfunded ATM for cheaper strikes without
an explicitly validated policy. Never expose manual strike/quantity controls.
Do not add contracts automatically after the initial sequence finishes.

This document corrects the intended allocation specification; the existing
explicit-target portfolio simulator has not been converted into an allocator.
No Phase 2 allocation, dashboard redesign, or history implementation is included.

## Application boundaries and shared models

- `mortificatio_models.py`: immutable account/environment/risk binding, application
  frequency policy, direction-only UUID commands, typed admission and lifecycle
  evidence. PAPER DU identity checks are configuration constraints, not proof of
  an authenticated broker session.
- `mortificatio_store.py`: one shared SQLite registry per permitted account and
  environment, WAL/FULL transactions, private database creation, configuration
  binding, rollback-clock checks, hashed tokens, hash-chained security audit and
  a durable local alert outbox. Expected denials commit their audit/failure counts
  and consume challenges where verification was attempted; unexpected errors
  roll back. Do not create independent registries for separate browser sessions.
- `mortificatio_auth.py`: WebAuthn enrollment/login and fresh user-verified step-up
  via Duo py_webauthn. The application does not implement signature verification
  or authenticator cryptography. It requires exact HTTPS origin/RP, rejects
  cross-origin ceremonies, and persists single-use challenges and counters.
- `executor_application.py`: authenticated dashboard boundary, separate
  short-lived trading grants, durable commands, account-wide daily frequency
  reservations, lifecycle ingestion and emergency intent. The default broker
  provider denies; fixtures can exercise admission only. There is no dispatcher.
- `executor_application_api.py`: transport-neutral API contract, not an HTTP
  server. A future TLS adapter must normalize headers and derive peer identity and
  secure transport from the connection, never untrusted forwarding headers.
- `mortificatio_protection.py`: separately provisioned, explicitly revocable local
  service capability scoped only to risk observation, reconciliation and EXIT ALL.
  It adapts the existing OFFLINE portfolio ledger; it cannot authorize entries or
  dispatch broker orders. Its separate emergency latch survives dashboard expiry.

## Enrollment, sessions, and trading authorization

A trusted OS administrator creates a single-use owner bootstrap through the local
Python API. There is no remotely callable bootstrap/service provisioning endpoint.
Deliver the bootstrap through a protected channel; never place it in URLs, logs,
browser storage, shell history, or command-line arguments. It expires after 60s.
Initial enrollment activates one operator credential, without creating a session
or trading grant. Platform passkeys and hardware security keys are supported by
WebAuthn; all ceremonies require user verification. Offline tests use generated
P-256 signatures and real library verification, not a mock verification bypass.

Login creates a new random session and CSRF token, with a 15-minute absolute and
5-minute idle lifetime. The cookie is __Host-, Secure, HttpOnly, SameSite=Strict,
Path=/; responses disable caching. Tokens are hashed in storage and omitted from
audit records. Keep the CSRF value in frontend memory in a future adapter.
Logout, expiry and fresh-authenticated revoke-all invalidate associated grants.
Reauthentication rotates the presented prior session. Other sessions can be
revoked with a fresh WebAuthn proof.

Login alone never authorizes trading. A separate fresh WebAuthn assertion issues
an at-most-180-second grant bound to user, session, account, PAPER environment,
risk-policy version and broker connection generation. Reconnects require a new
grant. Configuration and execution-enabling routes remain disabled; their future
implementation must require fresh authentication and independently validated policy.
No broker password, IBKR 2FA secret, or broker credential is stored here.

## Commands and frequency enforcement

The direction command accepts exactly command_id (canonical UUIDv4), CALL/PUT,
and entry_epoch. No quantity, strike, account override, or broker evidence is
accepted from the browser. BEGIN IMMEDIATE serializes account reservation, grant,
frequency and epoch checks. Accepted commands persist as QUEUED_OFFLINE before
returning. Duplicate IDs with identical intent return the existing decision;
conflicting reuse is rejected. Concurrent distinct IDs cannot open overlapping
managed trades. A stale epoch prevents browser retries from opening another trade
once the original completes. No pending command automatically executes on restart.
Future dispatch must revalidate current authorization, emergency status and all
existing broker/risk/market gates immediately before any submission.

The account-wide limit remains at most two filled trades per New York calendar
day (configurable downward only). Pending initial commands reserve a slot and
block conflicting acquisition. First actual entry fill establishes the counted
trade date; partial fills count once. A canceled zero-fill command releases its
reservation only after complete trusted reconciliation, execution history and
commission evidence. Filled trades retain their daily count. Session rotation or
process restart cannot reset this registry. Late exposure after a terminal state
blocks the account for recovery; no automatic rearm route exists.

Lifecycle evidence is accepted only from a trusted local protective capability,
with monotonic revisions/generations and durable duplicate identities. The Phase 1
interface accepts offline fixtures only. A real reconciliation provider and
migration/import of existing managed exposure into the central registry remain
required before future execution work. API clients cannot assert they are flat.

## Independent protection and emergency continuity

Dashboard expiry, logout, grant expiration, and revoke-all do not revoke the
protective-service capability or clear its independent emergency latch. The
adapter can monitor existing SPX risk rules and reconcile an existing simulated
portfolio without a dashboard session. Reduction planning is allowed only after
an exit latch; it rejects acquisition actions and exposes no dispatch API.

Dashboard EXIT ALL requires an authenticated session plus CSRF but no trading
grant, broker readiness, buying power, or ordinary entry-data eligibility. It
first durably blocks account admission in its authorization transaction, then
persists the independent emergency latch and reports PENDING_RECONCILIATION.
Failure writing that latch cannot reopen admissions. With an expired
dashboard session, the separately authenticated service remains able to persist
emergency intent, monitor protection, and reconcile. Repeated emergency requests
are idempotent and never rearm trading. No new code claims actual orders canceled,
protective broker coverage, or positions flat.

Existing strategy code, per-strike 25 cap, portfolio 125 cap, -3.25 premium stop,
+4/+1.25 protection, +5 activation and uncapped 3-point SPX trailing, broker paper
isolation, exact contract validation, and 0.25-second source-data gate remain
unchanged. Admission fixture booleans are test interfaces, not replacements for
those validators or fabricated exchange timestamps.

## Dependencies and offline verification

The existing environment is Python 3.9 on Intel macOS. `requirements-security.lock`
pins its compatible WebAuthn library and transitive dependencies. Install with:

```
.venv/bin/python -m pip install --only-binary=:all: -r requirements-security.lock
.venv/bin/python -B mortificatio_auth_test.py
.venv/bin/python -B executor_application_test.py
.venv/bin/python -B executor_application_api_test.py
.venv/bin/python -B offline_test_runner.py
```

The current WebAuthn release line needs newer Python. Upgrade the runtime and
revalidate current dependency releases before production; this compatibility pin
is not a claim of production security clearance. A dependency vulnerability review,
hash-verified supply-chain lock, and update policy remain required.

New tests cover real signature verification, bad registration/assertions, origin,
RP, UV/UP, counters, replay and expired challenges, rate limits, session rotation,
revocation, CSRF, grant expiry/binding, command concurrency, process death/restart,
frequency limits, complete reconciliation, late fills, API payload restrictions,
secure cookies/private SQLite sidecars and independent protection after expiry.
`order_boundary_test.py` also forbids broker client imports/mutations in the new
runtime components and excludes test authenticators from product imports.

## Remaining security and operational limits

- This is a foundation, not a deployed authenticated application. The legacy
  `executor_local_web.py` / `executor_local_ui.html` simulation interface is
  unchanged and is NOT secured by these new boundaries. Do not expose it or treat
  it as a production trading path. Integrating one frontend/service path requires
  a later explicitly authorized stage, eliminating any alternate entry path.
- Real TLS termination, reverse-proxy trust configuration, header normalization,
  browser/hardware-key ceremonies, and cross-device behavior need integration
  testing. No real hardware key or IBKR identity was verified in these tests.
- Secure additional-key enrollment, lost-key recovery, recovery-code procedures,
  credential management, and unusual-access detection remain incomplete. There
  is deliberately no password/TOTP/admin recovery shortcut. Failed initial
  bootstrap may require trusted local administrative recovery; no auto-reset.
- Alerts are a durable local outbox, without external delivery or monitoring.
  Audit hashes detect local inconsistency, not a complete database rollback or
  rewrite by a compromised OS administrator. External append-only anchoring,
  retention limits, encrypted backups and monitored recovery remain needed.
- Use a dedicated private state directory and service OS account. Protect local
  service capability material independently of the dashboard. SQLite/file
  permissions cannot protect against a compromised host, administrator or process.
  No network listener or fund-transfer capability is added.
- Account-wide enforcement requires all future admission paths to share this
  registry and trusted broker evidence. The separate legacy simulator and an
  operator-created second database are outside that enforcement boundary.
- Real IBKR account/session verification, buying power, full portfolio recovery,
  order dispatch, subscriptions and authoritative sub-0.25s market timestamps
  remain unvalidated. Execution stays disabled. No actual protective order or
  fill-speed claim is made.

Next recommended authorized stage: deterministic ATM-first allocation and a
reservation interface consuming this command registry, followed by unified
frontend integration. Preserve exact capital/liquidity accounting and run all
work offline. Do not begin Phase 2 without the operator's authorization.

## Verified results for this implementation

Final complete offline run: 70 standalone scripts passed, 0 failed; all 5
IBKR-connected scripts excluded. New Phase 1 cases: 17 authentication, 19
application/protection, 12 API tests — 48 passed, 0 failed. `pip check` reports
no broken requirements. The final suite includes the durable emergency-admission
fence and its simulated latch-write failure test. No IBKR connection, broker
order mutation, deployment, commit, or push was performed. Phase 2 has not begun.
