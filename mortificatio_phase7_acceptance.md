# Mortificatio — Phase 7 safety and security remediation

Review date: 2026-10-25. Authoritative checklist: `mortificatio_phase6_acceptance.md`.
Strictly offline: no IBKR connection, broker order submission/modification/cancellation,
execution enablement, deployment, commit, push, credential-setting changes or Phase 8.
Existing uncommitted work and historical reports were preserved. Public sources
already recorded in Phase 6 inform the migration assessment; no packages, runtimes
or vulnerability databases were downloaded in this phase.

## Decision

**IBKR paper execution: NO-GO. Live execution: NO-GO.** Offline research and fake
broker tests remain available. This report does not approve broker-connected
validation or assert production readiness. H3 and H4 now have tested offline
remediations; H1/H2 remain open in the actual installed environment.

## Protective-service reliability

`executor_protection_checkpoint.py` adds a separate, configuration-bound durable
checkpoint under the service root. It reuses the existing FULL-synchronous SQLite,
hash-chain, private-file and transaction infrastructure. Its inventory, next order
ID, callback cursor, health fence and integrity digest are independent of dashboard
authentication, reporting and the application database. No order/position ledger
is reconstructed from in-memory guesses. Economic records, source times, partial
fills, premium averages, favorable peaks and trailing state remain in the existing
validated portfolio journals. The synthetic broker book remains independent.

`executor_lifecycle.py` now:

- Registers the journal inventory before any simulated dispatch and confirms the
  bound journal before acquisition. The first migration imports the old durable
  counter/cursor with explicit initialization and counter checks. Subsequent IDs
  come from the protective checkpoint; foundation counters are reporting mirrors.
- Journals callbacks before advancing the independent cursor. A crash between the
  two replays the same callback identity without another economic fill or retry.
  Order IDs are durably reserved before binding; gaps are acceptable, reuse is not.
- Uses the independent inventory for ongoing supervision. Foundation reads/writes,
  publication and history are nonessential to protective risk decisions. A zero-wait
  application-writer probe avoids waiting for a held application write transaction
  in the tested protective cycle. This is not a universal scheduling-latency guarantee.
- Fences and seals acquisition when foundation storage is unavailable or integrity
  checks fail. It continues processing valid protective events, cancellation
  observations, reconciled owned exposure and bounded simulated reductions when
  the remaining independent stores, broker proof and market evidence permit.
- Reports `DEGRADED_ENTRY_FENCED`, `CRITICAL_UNRESOLVED_PROTECTION`,
  `CRITICAL_UNRESOLVED_BROKER` or `CRITICAL_UNRESOLVED_MARKET_DATA` as appropriate.
  Missing/stale protective quotes never become valid by substituting receipt time.
  Market/broker health may recover with new valid evidence; the entry fence does
  **not** automatically clear. No rearm endpoint was added.
- Exposes `protective_health(service_token)` for privileged local diagnosis without
  dashboard storage. If checkpoint durability cannot be verified, it explicitly
  reports that fact and refuses any volatile order fallback. An independent
  emergency latch is attempted as a second durable fence; a failed write is not
  represented as successful persistence.
- Withholds verified FLAT and reservation release when application reconciliation
  is unavailable. A requested exit, a planned reduction, an acknowledgement, or
  the absence of local positions alone is never claimed to be liquidation.

`executor_protection_recovery.py:recover_offline_protection` provides a local-only
cold-start path that requires an existing checkpoint/session journal, an approved
matching policy, independent service capability and the existing offline broker
fixture. It deliberately constructs **no dashboard authentication authority** and
denies application storage/login/trading grants. It does not create a replacement
foundation database or restore acquisition leases. A new explicitly verified
synthetic session and full reconciliation are still required. Real broker evidence
is not accepted as synthetic evidence and no real transport is added.

Missing managed journals are never recreated on recovery. Checkpoint, journal,
broker-book or authentication failures enter unresolved states, with exposure
retained. A process cannot continue protecting positions while stopped; the durable
path enables subsequent recovery, not uninterrupted coverage during machine outage.
An OS watchdog and actual broker-held protection remain operational prerequisites.

## Strong recovery and credential management

`mortificatio_credential_recovery.py` uses the existing maintained WebAuthn verifier and standard
browser credentials API; it adds no custom authentication cryptography or password,
TOTP, recovery-code, admin-reset or anonymous emergency shortcut.

1. An authenticated owner requests ADD_LOGIN, ADD_RECOVERY or REVOKE using CSRF.
   A fresh UV/UP login-key assertion is bound to the user, session, exact operation,
   target and credential revision. A stolen session/CSRF token alone is insufficient.
2. Adding a key then requires fresh UV/UP registration with exact RP/origin and
   challenge verification. Duplicate credential identities are refused. Roles are
   explicit: LOGIN or RECOVERY. Recovery-only keys cannot log in, issue a trading
   grant, or authorize ordinary credential management.
3. Revoking an authenticator requires proof from a **different surviving LOGIN
   key**. The last login key cannot be removed through this route. Revocation and
   successful registration revoke every dashboard session and trading grant and
   invalidate in-flight credential changes. Independent protection remains authorized.
4. If all LOGIN keys are lost, a **previously enrolled RECOVERY key** supplies a
   fresh user-verified assertion. It permits only registration of a replacement
   login key. Completion atomically revokes previous login credentials, all sessions
   and grants, and invalidates concurrent flows. It returns LOGIN_REQUIRED, not a
   session, trading authority or broker identity. Fresh login, separate trading
   authorization, verified broker session and every risk/data gate remain required.
5. If all login **and** recovery keys are lost, automated recovery fails closed.
   There is no software identity-proofing shortcut. An external identity/recovery
   policy would require separately reviewed implementation. Pre-enroll and physically
   protect backup/recovery keys before relying on this design.

One-use ceremonies expire under the existing challenge TTL. Durable credential
epochs prevent a concurrent/revoked recovery from installing another key. Failures
roll back replacement/revocation atomically. A local-only bootstrap-renewal method
exists solely for an inactive, never-enrolled owner with no credentials or commands;
it cannot reset an established identity and is not an HTTP endpoint.

`mortificatio_auth.py` also rejects non-string attestation formats before maintained
verification, matching the input-validation hardening identified in Phase 6. This
does not turn the old installed library/runtime into a supported production stack.

Browser/API additions:

- `GET /auth/credentials`: authenticated role/credential identity list, no private
  keys or credential public-key material.
- POST `/auth/keys/options`, `/auth/keys/prove`, `/auth/keys/register`: authenticated,
  exact-origin, session/CSRF-bound credential changes.
- POST `/auth/recovery/options`, `/auth/recovery/prove`, `/auth/recovery/register`:
  same-origin, rate-limited, single-use WebAuthn recovery. Every replacement requires
  the pre-enrolled key proof and fresh registration. No trading endpoint is called.
- Security page key-management controls and public recovery workflow use those
  APIs. CALL/PUT/EXIT ALL remain the only trading-action controls. Logout/recovery
  clears private credential selectors. Unknown status disables entry and displays
  unresolved protection instead of retaining a stale healthy label.

These are software/virtual-authenticator validations, not proof of hardware-backed
identity or a tested physical lost-key procedure. Security-key inventory labels,
custody and fingerprint mapping must be reviewed by the operator.

## Operational hardening

- `mortificatio_store.py:private_database` now refuses shared/unowned leaf
  directories, application ancestor symlinks, unsafe writable ancestors, nonregular
  or multiply-linked files. Ancestor ownership must be root/current service user;
  the opened directory descriptor's owner, mode and inode must match the validated
  private directory. Descriptor checks and O_NOFOLLOW protect final opens;
  existing main/WAL/SHM files are made 0600 without resetting data. Shared parents
  are never chmodded. Root-owned `/tmp`, `/var`, `/etc` platform aliases and sticky
  root-owned temporary ancestors are explicitly handled. Deployment still needs
  a dedicated private service directory/account and controlled ancestor ownership.
- Paper ledger lock/database and emergency latch creation now use the private
  helper before SQLite sidecars are created. Existing application state was not
  opened/reset for this work. Same-UID/OS-administrator compromise, full database
  rollback and backup confidentiality are not solved by Unix permissions.
- Protective-health changes and credential/recovery events are hash-chained and
  placed in durable local alert outboxes. Tokens, assertions, cookie/CSRF values,
  raw credentials and arbitrary exception text are not added to audit records.
  External alert delivery/acknowledgement, retention, encrypted backups and external
  audit anchoring remain unimplemented and were not attempted offline.
- `executor_dashboard_web.py` moves TLS handshake processing off the accept loop,
  limits concurrent workers to 16, bounds handshake/header processing to 5 seconds
  and retains the existing bounded body reads. Excess connections close without
  adding workers. No internet binding, authentication bypass or automatic server
  start is introduced. Admission/per-API rate controls and sustained-load isolation
  remain incomplete; emergency local service capability is independent of HTTP.
- Startup still requires explicit approved dependencies. Close/join and writer locks
  remain enforced. Recovery never restores entry leases, retries uncertain orders,
  resets emergency state or releases reservations without reconciliation.

## Runtime migration and dependency reproducibility

The installed environment remains Python **3.9.6**, LibreSSL **2.8.3**, WebAuthn
**2.7.1**, setuptools **58.0.4**, and the existing **ibapi 9.81.1.post1** interface.
Only `/usr/bin/python3` was found; no supported replacement interpreter/uv/brew or
complete offline replacement wheelhouse was available. The cache contains ibapi
9.81.1.post1 and some unrelated/transitive wheels, but no required supported
interpreter or patched setuptools/new WebAuthn wheel set. No network installation or risky in-place upgrade was
performed. The actual vulnerable build-tool installation is therefore **not fixed**.

The migration target is a supported **CPython 3.13** environment with a maintained
TLS stack, created separately from `.venv`. Candidate versions identified from the
previous review are WebAuthn 3.0.1, cryptography >=49.0.0, pyOpenSSL >=26.3.0,
cbor2 >=6.1.2, pyasn1 >=0.6.2 and pyasn1-modules >=0.4.2. These are compatibility
candidates/minima, **not a validated lock or fresh vulnerability clearance**.
Setuptools must be at least 78.1.1 for the specific Phase 6 advisory; current
advisory review may require a newer release. Preserve/revalidate the existing
IBKR interface rather than silently changing SDK behavior during runtime migration.

`mortificatio_runtime_audit.py` performs no installation or network access. Its
default report fails with NO_GO on this environment (exit 1 is expected), flags
unsupported migration/TLS and old WebAuthn/setuptools floors, and never grants
execution readiness. It does not claim a complete CVE scan.

Its local-wheel lock generator requires a complete exact wheel set, rejects the
known obsolete/vulnerable policy floors, incompatible CPython/macOS tags and
Requires-Python, ambiguous wheels, missing/unsatisfied dependencies, URL/extras
dependencies and symlink artifacts. It hashes actual wheel bytes and writes exact
pins without overwriting an existing file. Synthetic wheel tests validate tooling,
not the compatibility of absent real artifacts. No fictitious hash lock was created.

When trusted offline artifacts and the target interpreter are separately available:

```
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target 3.13.0 --output /private/approved-py313.lock
# Use the operator-selected exact supported 3.13 patch version and matching wheels.
/private/approved-python3.13 -m venv /private/mortificatio-py313
/private/mortificatio-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --only-binary=:all: --require-hashes -r /private/approved-py313.lock
```

These commands were **not run**. Audit/verify the candidate artifact provenance and
current advisories, run the complete suite under that interpreter, test preserved
database migration/restore and SDK serialization, and perform virtual plus physical
key ceremonies before selecting it. The current `requirements-security.lock` remains
unchanged as the known-working offline compatibility lock, not production approval.

## Verification

- `executor_protection_reliability_test.py`: foundation connection/transaction
  outages, actual application write-lock contention, partial premium stop, peak
  tracking, independent reductions, restart without foundation, callback-cursor
  commit failure, checkpoint/journal disk errors, stale data, concurrent emergency,
  late duplicate fills, broker loss, missing/tampered journals and key recovery
  during open exposure. A real child process cold-starts without the foundation
  file and exits via `os._exit`; recovery does not duplicate its reduction.
- `mortificatio_credential_recovery_test.py`: real WebAuthn signatures for registration,
  role isolation, stolen sessions, distinct-key revocation, replay, origin/UV,
  expiry, revocation of in-flight recovery, restart, atomic failure, bootstrap
  restrictions and malformed attestation formats.
- `mortificatio_credential_recovery_api_test.py`: API cookies/CSRF/origin, no automatic
  authentication/grant after replacement, no privilege fields or reset endpoints.
- `mortificatio_hardening_test.py`: permissions, symlink/hardlink/ancestor rejection,
  directory-owner/descriptor-swap fault injection, unchanged bytes, bounded transport
  workers, handshake failure and capacity release.
- `mortificatio_runtime_audit_test.py`: exact actual-artifact hashes, deterministic
  locks, unsafe floors, missing/duplicate/incompatible wheels, dependency closure,
  symlinks and honest readiness reporting.
- Updated original acceptance/recovery tests now require continued protective
  processing under a foundation outage and no recreation of missing journals.
  Nonessential publication failure leaves its durable outbox pending instead of
  halting protective processing; uncertainty and holdings remain visible.
- Existing broker/account/data/strategy, sizing, reconciliation, P&L, browser and
  execution-boundary regressions are retained. No safety threshold was relaxed.
- A third genuine Chrome/virtual-key scenario covers recovery-only enrollment,
  replacement, fresh login with no trading grant, backup login-key enrollment and
  distinct-key revocation. It pauses session refresh and mutates the selector to
  verify revocation stays bound to the click-time credential. The historical
  `mortificatio_recovery.py` position-recovery API is retained unchanged; credential
  recovery lives in a separate module.

Final full-suite result: **84 offline scripts passed, 0 failed; 5 broker-connected
scripts excluded**. All three Chrome scenarios passed in that run. The final
focused reliability and history-presentation checks also passed (14 and 13 tests).
New coverage: 49 targeted test methods (14 reliability, 15 credential recovery,
5 recovery API, 9 hardening, 6 artifact/runtime) and one additional genuine Chrome
scenario, for **50 new methods/scenarios**. The browser file has three scenarios.
The recovery browser scenario additionally passed five consecutive isolated runs
after correcting lost/offline virtual-device custody in the harness; server
authentication and safety gates were not relaxed. The first broad regression
found a historical module-name collision, which was corrected by separating the
new credential module and restoring the historical position-recovery API unchanged.
An intermediate browser run failed during rapid multi-key sign-in; the final
device-custody model and explicit asynchronous revocation race test pass in the
complete suite. These virtual-key results do not replace physical-key acceptance.
Final-source suite output: `/private/tmp/mortificatio-phase7-offline-suite-acceptance.log`.

## Phase 6 checklist reassessment

| Finding | Phase 7 status | Remaining work |
|---|---|---|
| H1 unsupported runtime/TLS | **Partially resolved**: assessed, target/policy and offline locking tests implemented. | Actual supported interpreter/TLS provisioning and full migration acceptance unresolved. |
| H2 vulnerable setuptools | **Unresolved in installed environment**; new artifact tooling refuses the known vulnerable floor. | Supply reviewed patched wheels, rebuild separately, validate current advisories and remove vulnerable runtime/build installation. |
| H3 foundation dependency | **Resolved and verified offline for tested outages/restarts**; durable independent inventory/cursors/IDs, risk processing, reductions, cold recovery and explicit critical conditions. | Real broker-held coverage, OS watchdog, all-machine/storage failure and worst-case scheduling guarantees unverified. |
| H4 credential/recovery gap | **Resolved and verified offline** for pre-enrolled backup/recovery credentials and revocation. | Physical-key/custody/recovery drills; total loss without a recovery key intentionally has no automated bypass. |
| M1 private state | **Resolved for tested foundation/protection/paper/emergency paths** with stricter private creation. | Dedicated service identity/filesystem deployment, legacy research inventory and OS-level testing remain. |
| M2 transport/backpressure | **Partially resolved**: bounded workers and pre-HTTP TLS/header timeouts. | Per-API admission/fairness, stress/load and emergency overload exercises. |
| M3 audit/alerts/backup | **Partially resolved**: durable protective/recovery audit and local alert outboxes. | External anchoring/delivery, monitored acknowledgements, retention and encrypted restore drills. |
| M4 lock/latency | **Partially resolved**: essential path avoids foundation writer wait in targeted fault test. | Reporting/history can still contend for the lifecycle lock; no sustained-load or hard latency guarantee. |
| M5 legacy simulator | **Unresolved deployment concern**, execution boundary retained. | Isolate/disable alternate research listeners before deployment; no broker coupling. |
| M6 complete environment lock | **Partially resolved**: actual-artifact hash/closure tooling with tests; no invented target lock. | Complete reviewed wheelhouse, real target lock/SBOM, runtime and advisory validation. |
| L1 manual/browser/hardware | **Unverified** beyond Chrome virtual-authenticator/browser regressions. | Physical keys, VoiceOver, touch, Safari/Firefox/Edge and recovery drills. |
| L2 stale UI/profile metadata | **Partially resolved**: unavailable status clears healthy entry eligibility and reports unresolved protection. | Workstation/profile controls and network-loss display acceptance. |

## Requirements before any further trading stage

Paper execution remains NO-GO until runtime remediation, operational recovery,
trusted HTTPS and physical-key acceptance, verified actual paper account/session,
market-data entitlements, an authoritative solution for the unchanged **0.25-second
source gate**, complete real order/execution/correction/commission reconciliation,
and real protective stop/cancel-replace/OCA validation are established. Ordinary
receipt timestamps cannot solve the source-time gap. All actual order methods
remain disabled. No real-session validation was attempted.

Live execution is separately NO-GO and additionally requires explicit authorization,
approved paper outcomes, independent operational/security review, controlled real
liquidity/slippage/risk validation and a tested escalation process. Software cannot
guarantee fills, stop execution or safety on a compromised machine/account.

## Local operational runbooks

- Provision an owned private 0700 state directory and separate service identity;
  keep main/WAL/SHM/lock/checkpoint artifacts together in encrypted, consistent
  backups. Do not copy only the main file while WAL writes are live. Preserve
  independent capability secrets outside browser storage and source control.
- For foundation failure: stop acquisition, inspect privileged protective health,
  preserve journals/checkpoints/book and reservations, and restore the foundation
  without reset. If cold-start is required, use the capability-authenticated
  protection-only factory with the exact existing policy, fixture and fresh
  verified synthetic session. Dashboard remains unavailable, not implicitly trusted.
- For protective disk, integrity, broker or data failure: treat critical exposure
  as unresolved. Do not bypass verification, retry uncertainty, release funds,
  invent connectivity or claim automatic liquidation. Escalate through a separately
  approved operator path; actual broker emergency procedures are not validated here.
- For lost login key: use a surviving login key or the pre-enrolled offline recovery
  key; replace credentials, then log in and establish new independent authority.
  Revoke compromised keys using a different surviving login key. Never reset a
  ledger/owner database to regain access. Protection must continue independently.
- Local HTTPS still requires an explicitly configured exact origin/RP and trusted
  operator-owned certificate. No insecure HTTP fallback or test trust exception
  belongs in deployment. Test two physical keys, lost-key/stolen-session drills,
  real user verification and separate broker authentication before any approval.

## Files changed

Created: `executor_protection_checkpoint.py`, `executor_protection_recovery.py`,
`executor_protection_reliability_test.py`, `mortificatio_credential_recovery.py`,
`mortificatio_credential_recovery_test.py`, `mortificatio_credential_recovery_api_test.py`,
`mortificatio_hardening_test.py`, `mortificatio_runtime_audit.py`,
`mortificatio_runtime_audit_test.py`, `mortificatio_phase7_acceptance.md`.

Modified: `executor_lifecycle.py`, `executor_lifecycle_recovery_test.py`,
`executor_acceptance_test.py`, `executor_application_api.py`,
`executor_blind_presentation.py`, `executor_dashboard_web.py`,
`executor_paper_broker.py`, `executor_emergency.py`, `mortificatio_auth.py`,
`mortificatio_store.py`, `order_boundary_test.py`, `dashboard/index.html`,
`dashboard/dashboard.js`, `executor_lifecycle.md`, `executor_dashboard.md`,
`executor_dashboard_browser_test.py`, `executor_dashboard_browser_test.js`.

Historical Phase 6 findings remain intact. **Phase 8 has not begun.**
