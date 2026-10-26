# Mortificatio — Phase 6 offline security and operational acceptance

Review date: 2026-10-24 America/New_York. Scope: existing Phases 1–5, their
offline broker fixtures, local HTTPS dashboard, and read-only diagnostic code.
All pre-existing changes were retained. No IBKR session was contacted, order
submitted/modified/canceled, credential changed, execution enabled, or deployment,
commit, push, or Phase 7 work performed. Public vendor documentation was consulted.

## Decision

**Paper trading: NO-GO. Live trading: NO-GO.** Offline research and isolated fixture
testing can continue. This review is not a penetration-test certification or a
claim of production readiness. Passing tests establish behavior under the tested
fixtures, not actual account authorization, fill speed, executable liquidity,
broker protection, market-data synchronization, or recovery under every failure.

The principal blockers are the unsupported runtime/build-tool exposure, unified
supervision's foundation-store dependency, incomplete credential/emergency recovery,
missing real broker-held protection, and lack of authoritative SPX/SPXW quote/Greek
timestamps satisfying the unchanged 0.25-second synchronization gate.

## Verification results

- New `executor_acceptance_test.py`: **14 tests passed, 0 failed**, with outbound
  socket connection methods patched to fail. Includes 80 seeded invalid-command
  variations, eight simultaneous opposite-direction commands, stale/missing source
  and Greek evidence, cross-account/user/environment ownership checks, forged
  cookies, ceremony replay, privilege escalation, partial exposure, emergency,
  revocation, restart, foundation outage, and false-empty reconciliation.
- `executor_dashboard_browser_test.py`: **2 scenarios passed, 0 failed** in real
  isolated Chrome with virtual WebAuthn keys and ephemeral localhost HTTPS.
  Added accessibility-tree checks for landmarks, named controls, private-content
  exclusion before login, trading-action names/count, and per-strike regions.
  Existing scenarios exercise one-press CALL/PUT, double clicks, refresh recovery,
  emergency liquidation, history filters/pagination, pending/withheld results,
  fees/details/charts, session expiry, and delayed responses after logout.
- Full regression: **79 offline scripts passed, 0 failed; 5 broker-connected
  scripts excluded**. Captured result:
  `/private/tmp/mortificatio-phase6-offline-suite.log`.
- `.venv/bin/python -m pip check`: **no broken requirements**. This checks package
  compatibility, not vulnerabilities or provenance.
- Metadata-only filesystem review: new foundation/protection fixtures and sidecars
  are private in tests. Existing research-state roots `.executor-local` and
  `.executor-shadow` are 0700; some legacy database/SHM files are 0644, so their
  parent-directory restriction is material. No existing state files were opened
  for content inspection, changed, or reset. No `.env`, PEM or key file was found
  by the scoped repository filename inventory (excluding tool credential directories,
  `.git`, `.venv`, and node_modules); this is not a complete secret/history scan.

Reproduce with:

```
.venv/bin/python -B executor_acceptance_test.py
.venv/bin/python -B executor_dashboard_browser_test.py
.venv/bin/python -m pip check
.venv/bin/python -B offline_test_runner.py
```

The browser test uses a temporary profile, virtual keys, a test-only certificate
exception, external request blocking, and localhost-only resolution. It does not
verify hardware keys or change the OS trust store. Five known broker-connected
scripts are excluded by `offline_test_runner.py`. Script counts are not individual
assertion counts; several older standalone scripts do not use unittest.

## Security review and coverage

| Boundary | Evidence and result | Relevant code/tests |
|---|---|---|
| Passkey verification | Maintained library performs cryptographic verification; exact RP/origin, challenge, UP/UV, ID/handle and counters enforced. Forged signatures, replay and cross-origin ceremonies rejected. Counterless keys rely on single-use challenges. | `mortificatio_auth.py`: `_client_policy`, `_take_flow`, `_verify_assertion`, `complete_registration`; `mortificatio_auth_test.py` |
| Session fixation/expiry/revocation | New random session after verified login; supplied old same-user session and grants revoked. Absolute/idle expiry and logout/revoke-all enforced. Cookie flags Secure/HttpOnly/SameSite=Strict/__Host. | `complete_login`, `require_session`, `logout`; `executor_application_api.py`: `cookie`; auth/API tests |
| Trading authority | Dashboard login creates no grant. Fresh WebAuthn step-up is bound to user/session/account/PAPER/risk version/broker generation, with short expiry. Replayed login proof cannot authorize trading. Configuration mutation routes disabled. | `begin_step_up`, `complete_step_up`, `require_grant`; application/acceptance tests |
| CSRF/origin | Exact host/origin, fetch metadata, bounded JSON and duplicate-field rejection. Resume CSRF requires same-origin custom header and session. Cross-site/null/prefix-confusion/forwarded-header attacks rejected. | `FoundationAPI.dispatch`, `DashboardWebApplication.dispatch`, `MortificatioSecurity.dashboard_session`; dashboard/API/acceptance tests |
| Account and data isolation | Foundation configuration pinned; command lookup and history join user/account/environment. Wrong accounts, LIVE environment, synthetic/live origin confusion and malformed session evidence rejected. Tests use separate stores and explicit metadata fault injection. | `UnifiedApplication._broker`, `command_status`; `TradeHistoryService._rows`; lifecycle and acceptance tests |
| Browser API | Only allowlisted projections expose status/commands. No ledger, service capability, broker credentials, raw session token or arbitrary order endpoint. Static allowlist, CSP and no-store. Sensitive strings rendered as text. | `executor_blind_presentation.py`, `executor_dashboard_web.py`, `dashboard/dashboard.js`, `order_boundary_test.py` |
| Replay/duplicates | Durable UUID, command intent binding, account entry epoch, central active-trade/frequency registry, global broker IDs and callback reconciliation. Refresh is read-only, not a dispatch retry. | `executor_application.py`, `executor_lifecycle.py`, lifecycle/recovery/acceptance tests |
| Rate limiting | Persistent peer/global authentication and user/account history limits verified. Authentication throttling does not disable authenticated emergency or service capability. General transport/entry/status throttling remains incomplete; see M2. | `MortificatioSecurity._rate`, `TradeHistoryService._authorized`, acceptance tests |
| Audit | Hash-chain and head verification detect tested changes; tokens/assertions absent from tested audit records. Denials persist. Rollback fails closed. External anchoring/delivery/recovery absent. | `FoundationStore.transact`, `verify_audit`, `audit`; auth/acceptance tests |
| Emergency | Valid session+CSRF required for dashboard EXIT ALL, but no grant/buying power/entry market gate. Independent local capability survives dashboard logout/expiry. No anonymous emergency/recovery shortcut. | `UnifiedApplication.emergency_exit_all`, `ProtectionAuthority.exit_all`, `IndependentProtectionService`; emergency/lifecycle/acceptance tests |

Application data contains public credential keys and token hashes, not passkey
private keys. Service capabilities are bearer secrets supplied by a trusted local
operator; safe OS storage, rotation, backup and provisioning are still operational
requirements. Same-user malicious code, a compromised browser/OS, or an administrator
able to rewrite all state remain outside software-only protection guarantees.

## Execution and financial acceptance

| Requirement | Offline evidence | Assessment |
|---|---|---|
| One direction, no manual overrides | Exact command field allowlist; 80 seeded manual/account/environment override attempts denied before order modeling. Concurrent CALL/PUT reserves only one trade. | PASS offline |
| 25 per strike / 125 total | `executor_allocation_test.py` includes 300 seeded capital/direction and 200 randomized price/fee/reserve/risk vectors. `executor_portfolio_stress_test.py` has eight seeded 125-contract entry/liquidation cases. Owned plus working BUY reservations and SELL reservations checked. | PASS for tested vectors |
| ATM-first capital | Actual listed intervals, actual delta ranking, Decimal ask×100+fees/slippage/reserves; partial ATM allowed; farther strikes require prior 25; no cheaper-strike skip. | PASS offline (`executor_allocation.py`: `build_allocation`) |
| Reservations | Held through partial fills, uncertain orders, cancellation and restart; released only after trusted terminal/flat reconciliation. False empty positions cannot release cash or finalize results. | PASS offline (`executor_allocation_service.py`, lifecycle tests) |
| No additional exposure after exit | Exit/halt latches, admission fence and acquisition cancellation; late/duplicate executions handled against owned quantities. No automatic rearm. | PASS offline; broker cancellation timing unverified |
| Reduction-only authority | `ProtectionAuthority.OPERATIONS` excludes acquisition. Reduction adapter cannot bind BUY intents. Unified supervisor can advance entries only using the separate, still-valid original entry lease/grant. | PASS offline; service capability alone is not entry authority |
| Strategy | Each strike WAP−3.25 premium stop; +4 SPX arms +1.25 floor; +5 supersedes with continuous favorable peak and 3-point reversal; no cap or automatic scale-out. Symmetry, +10/+20/+50, exact reversals, partial fills, duplicates and restarts covered. | PASS offline (`executor_active_exit_test.py`, `executor_premium_stop_test.py`, protective/lifecycle tests) |
| Fail-closed data/account gates | Missing/stale/wrong/frozen/delayed/crossed quotes, absent Greeks/source times, wrong account/environment, disconnect and generation changes rejected. .251s skew fails; no source-time substitution. | PASS offline; real source feasibility BLOCKED |
| Actual fills and realized results | Decimal execution prices, weighted averages, corrections, late commissions/rebates; per-strike sums equal portfolio. Missing evidence/fees or held reservations prevent FINALIZED; later evidence revokes finalization. | PASS offline (`executor_trade_history_test.py`, accounting/presentation tests) |
| Failure recovery | Existing tests include process death, publication outbox crash, uncertain dispatch, duplicate callbacks, restart peak retention, disconnects and late fills. New tests inject foundation outage and incorrect empty snapshots. | PARTIAL: H3 and broker blockers remain |

These are simulated dispatches, cancellation observations and executions. Static
order-boundary guards and runtime tests confirm actual broker execution remains
disabled. No claim of instantaneous fills, guaranteed stop execution, or real OCA
overfill protection follows from these results.

## Findings and required remediation

### Critical vulnerabilities

No critical authentication bypass or browser-triggered actual broker order was
demonstrated within this scope. This does not mean no critical vulnerabilities
exist. Real protection and source-data blockers below are release-stopping even
though they are not demonstrated authentication exploits.

### High priority

| ID | Finding and evidence | Required remediation |
|---|---|---|
| H1 | Runtime Python **3.9.6** is unsupported upstream. TLS uses **LibreSSL 2.8.3**, separate from cryptography's bundled OpenSSL. The current WebAuthn compatibility pin cannot follow the current library line on this interpreter. | Provision a supported interpreter and TLS stack in a separate environment; revalidate persistence compatibility, signatures, cookies, CBOR parsing, browser ceremonies and every offline test before migration. Do not overwrite the only working environment or state. |
| H2 | Installed build tool **setuptools 58.0.4** is within the affected range for **CVE-2025-47273 / GHSA-5rjg-fvgr-3xxf** (PackageIndex path traversal; patched in 78.1.1). This is a build/install-time attack surface; no dashboard route invoking it was found and no exploit was run. | Replace vulnerable build tooling as part of a reviewed environment rebuild; use reviewed wheel-only installs and hash-verified locks including build tools. Remove package-index download functionality from the runtime deployment. |
| H3 | **Demonstrated foundation-store availability coupling:** `OfflineExecutionLifecycle.observe_spx` calls `_callbacks` before risk observation; `supervise`, `_ids`, and reconciliation/publication also require foundation transactions. Injected store failure prevents the new SPX event from advancing protection. The independent capability/latch survives, but the unified worker cannot complete its ordinary automatic path. | Design and test an independently available reduction/risk/reconciliation path and watchdog, with durable identity allocation, explicit unresolved state, operational escalation and evidenced broker-held protection. Preserve reconciliation before any reduction dispatch; do not bypass a failed integrity gate or fabricate flat state. Broader than an isolated acceptance fix. |
| H4 | Additional-key enrollment, lost-key recovery, credential-specific revocation and audited break-glass emergency access are incomplete. Initial bootstrap is one-use; failed enrollment has no supported retry/recovery workflow. Session expiry is handled safely, but loss of all credentials may block authenticated dashboard emergency access. | Approve and implement strong multi-key enrollment/recovery and OS-protected emergency procedures without dashboard bypasses. Drill loss of key, session revocation and independent service recovery before any broker execution. Never reset a ledger to regain access. |

Python's official lifecycle lists 3.9 EOL as 2025-10-31.
[Python support status](https://devguide.python.org/versions/).
The maintainer advisory documents the setuptools affected range and fix.
[Setuptools advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf).

### Medium and low priority

| ID / priority | Finding | Required remediation / limit |
|---|---|---|
| M1 / medium | `mortificatio_store.py:private_database` creates new files 0600/new parents 0700, but does not validate existing parent ownership/mode or all ancestor symlinks, and checks/open/chmod are path-based. Some legacy SHM/DB files are 0644 under currently 0700 roots. | Require a dedicated private, owned state directory; validate ancestors and link/race handling before use. Keep legacy research state isolated. Do not blindly chmod shared/repository parents. |
| M2 / medium | Authentication/history limits exist, but status/session resume/command denial traffic lacks comprehensive admission/backpressure. `ThreadingHTTPServer` has unbounded worker creation; TLS handshake/header processing precedes the handler-body timeout. Local slow-client/resource exhaustion remains possible. | Bound connections, parsing/handshake durations and work queues. Test overload while preserving a separately available authorized emergency/protective path; do not put emergency behind ordinary entry throttling. Loopback binding limits remote exposure but is not host isolation. |
| M3 / medium | Hash chains are entirely local; a full rollback/rewrite can preserve internal consistency. `security_alerts` is a durable outbox with no external delivery/ack monitoring. Retention, encrypted backups and restore drills incomplete. | External append-only audit anchoring, alert delivery and failure monitoring, bounded retention, encrypted backups and recovery drills. |
| M4 / medium | Audit-chain verification on transactions and history replay across all trades occur under shared locks. Long histories/denial floods could delay supervision; no hard scheduling-latency budget or sustained-load acceptance was established. | Measure worst-case supervision lag under load/large history; isolate reporting work and bounded audit verification without losing integrity. A passing .25s source gate is not a scheduling guarantee. |
| M5 / medium | `executor_local_web.py:make_handler` remains an alternate legacy **simulator** with locally issued cookie/CSRF controls, not Mortificatio passkey authentication. It is separate from the unified boundary and must not become a broker path. | Keep it inaccessible outside isolated research, inventory/disable alternate listeners in the deployment plan, and prohibit coupling it to broker submission. No actual order bypass demonstrated. |
| M6 / medium | `requirements-security.lock` pins security dependencies only, without artifact hashes, interpreter/build tooling/IBKR/browser locks, a complete SBOM or automated advisory review. Current WebAuthn 3.0.1 includes non-string attestation-format rejection absent from the 2.7.1 compatibility pin; impact here was not established. | Reviewed supported-runtime dependency migration, full hash-verified environment lock, malformed-attestation tests, SBOM and scheduled advisory review. Do not characterize this partial review as a clean vulnerability scan. |
| L1 / low | Only Chrome desktop/tablet automation is established. Physical passkeys, VoiceOver, touch devices, Safari/Firefox/Edge and multi-device recovery unverified. | Complete the manual/browser matrix below before accepting UI/security compatibility. |
| L2 / low | Session loss is detected by UI polling/local expiry; network loss can delay visible revocation/state updates. Existing browser profile contains limited nonsecret command metadata. | Treat server authorization as authoritative; document workstation/profile protections and delayed/offline display behavior. No automatic entry replay. |

No production runtime/security/strategy module or dependency was changed in this
phase. The findings require deliberate remediation, not a speculative acceptance
rewrite. New tests document both working boundaries and the H3 limitation.
No session, risk, or market-data gate was weakened.

## Dependency and environment assessment

| Component | Observed version | Assessment |
|---|---|---|
| Python / sqlite3 | 3.9.6 / 3.39.5 | Unsupported interpreter; DB migration/backup compatibility needs testing. |
| Python HTTPS TLS | LibreSSL 2.8.3 | Old platform stack; must revalidate a maintained TLS runtime, not infer health from cryptography's library. |
| webauthn | 2.7.1 | Existing compatible pin; current maintainer line requires Python >=3.10 and has newer parsing behavior. |
| cryptography / pyOpenSSL | 48.0.1 / 26.2.0 | Lock matches installed versions; cryptography backend reports OpenSSL 4.0.1 (9 Jun 2026). Not the Python HTTPS stack. No comprehensive advisory clearance asserted. |
| cbor2 / pyasn1 | 5.9.0 / 0.6.4 | Locked; current WebAuthn dependency requirements differ. Revalidate CBOR and registration fixtures on migration. |
| cffi / pycparser / typing_extensions | 2.0.0 / 2.23 / 4.16.0 | Locked; verify wheel/hash/platform compatibility in rebuild. |
| pip / setuptools | 26.0.1 / 58.0.4 | Dependency consistency passes; setuptools vulnerable-range finding H2. Neither is included in the security lock. |
| ibapi | 9.81.1.post1 | Old installed SDK; negotiated TWS/server compatibility is unverified. No SDK upgrade/connection performed. |
| Node | 22.23.2 | Browser-test tooling; 22 remains an upstream LTS line as reviewed. No npm dependencies/downloads added. |
| Chrome / Safari driver | 155.0.8059.85 / Safari 18.6 driver 18621.3.11.19.1 | Chrome tested; Safari installed/driver version inspected only. |

The current WebAuthn maintainer requirements and 3.0.1 change are documented at
[upstream requirements](https://github.com/duo-labs/py_webauthn/blob/master/pyproject.toml)
and [release 3.0.1](https://github.com/duo-labs/py_webauthn/releases/tag/v3.0.1).
Node branch status: [official release table](https://nodejs.org/en/about/previous-releases).
No package was upgraded or downloaded. No full fresh CVE database or automated
pip-audit run was available; the review combined installed metadata, local source,
dependency consistency and selected maintainer advisories. Absence of other
reported CVEs here is not a statement that the remaining packages are vulnerability-free.

## Browser/accessibility matrix

| Check | Result |
|---|---|
| Chrome desktop 1440px and tablet 900px | Automated layout overflow, screenshots and functional workflows pass. |
| Exactly three trading actions; no manual sizing | DOM has exactly CALL/PUT/EXIT ALL; active dashboard accessibility tree exposes three named actions. Hidden private dashboard controls excluded before login; global EXIT ALL is disabled while signed out. |
| One-press, duplicates, refresh and emergency | Genuine virtual-key enrollment/login/step-up; one command on double activation; no POST replay after refresh; emergency visible across private pages. |
| Privacy and logout | Active results withheld; pending chart/details withheld; logout clears private nodes; held response cannot repaint after logout; API denies revoked cookie. |
| History/details | Search/date/direction/pagination APIs and UI direction/page controls, per-strike fees/details, synthetic labels, recorded/missing charts covered by combined tests. |
| Keyboard / labels / contrast | CALL→PUT Tab order; focus-visible/reduced-motion CSS; accessible main/regions and nonempty control names; selected foreground/background pairs >=4.5:1. This is not a complete WCAG audit. |
| Safari | Installed, driver version observed; no Safari session launched or automation preferences changed. Rendering/WebAuthn compatibility NOT VERIFIED. |
| Firefox / Edge / other engines | Not exercised; Firefox not installed and no Playwright package present. No support certification claimed. |
| VoiceOver, real keys, biometrics, touch, zoom/high contrast | Manual/physical acceptance NOT EXECUTED. AX-tree checks do not substitute for screen-reader interaction. |

Manual acceptance must include enrollment/login/step-up/revocation with two physical
keys or supported passkeys; lost-key drill; screen-reader navigation/status announcements;
200–400% zoom; contrast modes; touch accidental-action review; trusted TLS; browser
restart/history/back navigation; multiple tabs; and expiry/network loss during an open
synthetic trade. Do not enable OS/browser automation or real broker access implicitly.

## IBKR readiness — documentation versus empirical evidence

| Area | Documented capability / current implementation | Remaining validation |
|---|---|---|
| Login/2FA | Operator authenticates TWS/Gateway independently with IBKR's supported second factor. Mortificatio stores no IBKR credentials/2FA secrets. | No real login tested. A socket/port/account prefix alone is insufficient; exact approved paper identity and session evidence required. |
| Session lifecycle | IBKR documents daily automatic restart options that can retain authentication. | Do not assume every reconnect/order prompts a phone approval. Require a reviewed manual-auth/restart configuration and fresh bound evidence; no restoration automation added. |
| API configuration | Enable socket clients, restrict localhost, keep Read-Only API checked; approved paper defaults 7497/4002 in diagnostic allowlist. | Actual GUI settings, endpoint, SDK/server compatibility unverified. Port alone is not account proof; API read-only checkbox has no independently verified socket attestation in this code. |
| Data entitlements | CBOE Streaming Market Indexes for SPX; OPRA US options (or explicit including bundle) for SPXW; underlying+option subscriptions needed for Greeks. | API market-data acknowledgements, eligible account, paper sharing, line limits, competing sessions and current real-time callbacks must be observed. No subscriptions purchased or account settings changed. |
| Timestamp feasibility | Ordinary quote/Greek callbacks do not carry bound authoritative subsecond source times. Last-trade timestamps and local receipt timestamps are separate diagnostics. IBKR tick-by-tick excludes options and limits indices to CME. | **BLOCKED:** exact SPX/SPXW/Greeks source synchronization <=0.25s cannot be established by this path. An independently validated authoritative solution is required; do not weaken the gate or invent source stamps. |
| Protection | Existing durable protective controller models stop acknowledgement/quantity, cancel-before-replace, competing exits, late fills and recovery with fake broker evidence. Unified portfolio reports `OFFLINE_CONDITIONAL_MODEL_NOT_BROKER_HELD`. | No real per-strike stop or broker-held coverage verified. Quantity/WAP/tick rounding, partial entries, rejected/late stops, atomic amendments and outage coverage must be validated. `executor_protective_exits.md` explicitly identifies the modeled cancel-before-replace coverage gap. |
| OCA/cancel/replace | IBKR defines OCA with-block types that route only one group order at a time, and no-block proportional reductions. | These semantics do not prove SPXW protection/atomic replacement, nor support unconditional concurrent stop+exit quantities. Test exact account/contracts/order types and reconcile cancel/late-fill races before orders can ever be authorized. |
| Reconciliation | Offline ledger tracks orderId/permId/orderRef/execution IDs/commissions/corrections, account-wide inventory, uncertain intents and restart. Real diagnostic callbacks remain separate. | Full real account/open/completed orders, execution/correction/commission completeness, manual/other-client order scope and crash/reconnect evidence unverified. Never retry uncertainty or declare FLAT based on a callback alone. |
| Liquidity/performance | Bounded marketable-limit plans and deterministic slice/reservation tests exist. | Real fill percentages, ack/fill times, slippage, rejection/pacing and 125-contract entry/exit cannot be inferred from fixtures or displayed sizes. |

Sources for the specific operational claims:

- [IBKR Mobile authentication](https://ibkrguides.com/brokerportal/sls/using-two-factor.htm),
  [session restart FAQ](https://www.interactivebrokers.com/docs/third-party-integrations/general-third-party-frequently-asked-questions).
- [TWS API setup](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/),
  [market-data coverage](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction).
- [Greeks callback and entitlement requirements](https://www.interactivebrokers.com/docs/tws-api/doc/md/option-greeks),
  [tick-by-tick limitations](https://www.interactivebrokers.com/docs/tws-api/doc/md/live-ticks).
- [OCA order fields](https://www.interactivebrokers.com/docs/tws-api/ref/order).

IBKR paper fills use top-of-book simulation and stops/complex orders are simulated;
paper success would not establish live liquidity or equivalent stop behavior.
[IBKR paper limitations](https://www.ibkrguides.com/orgportal/aboutpapertradingaccount.htm).

## Operational handoff and remediation sequence

1. Keep order submission disabled and the dashboard loopback-only. Inventory and
   isolate legacy/demo listeners. Retain private copies of all existing ledgers;
   never fix a recovery incident by deleting state, releasing held reservations
   manually, trusting an empty callback, or retrying an uncertain intent.
2. Review a supported-runtime/dependency rebuild plan (H1/H2/M6) with pinned
   artifacts, private directory provisioning, encrypted backups and migration/restore
   tests. No dependency upgrade was undertaken in Phase 6.
3. Specify and test protection availability during foundation failure, disk full,
   lock contention, supervision stall and reporting overload (H3/M2/M4), without
   bypassing integrity/account/reconciliation gates. Add separately monitored
   reduction authority and a tested operator emergency runbook.
4. Complete strong recovery/key management, external audit/alert delivery and
   manual hardware/browser/accessibility acceptance (H4/M3/L1). An expired dashboard
   must block new entries while protection continues; if all keys are lost, do not
   provision an anonymous emergency route or expose service capabilities.
5. Only after separate authorization, conduct real **read-only** paper-session
   validation using the existing opt-in diagnostic. It must not place/cancel orders,
   and cannot resolve missing source-time evidence merely by observing LIVE data.
6. An authoritative timing solution, real account-wide reconciliation and approved
   protective-order validation design are prerequisites to any separately authorized
   paper execution stage. Live consideration would additionally require controlled
   paper results, real liquidity/risk validation, operational sign-off and new explicit
   authorization. Neither trading mode is approved by this report.

Files changed in Phase 6: new `executor_acceptance_test.py` and this report;
modified `executor_dashboard_browser_test.js` for accessibility-tree assertions.
All prior uncommitted files remain preserved. **Phase 7 has not begun.**
