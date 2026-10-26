# Mortificatio Phase 5 — private dashboard and history interface

OFFLINE ONLY. No IBKR connection, actual broker orders, paper/live enablement,
deployment, commit, push or Phase 6 work is included. The interface uses the
sanitized Phase 1–4 APIs. It never reads portfolio journals or service tokens.

## Design and screens

No original ELIVO reference image was found in this repository. The design follows
the written direction, not a claim of exact visual fidelity: warm charcoal,
editorial local serif fonts, restrained sans-serif labels, champagne accents,
muted green/red controls, thin borders and a quiet mountain sidebar. The mountain
is an original local SVG; there are no remote images, webfonts or CDN dependencies.

`dashboard/index.html`, `dashboard/dashboard.css`, `dashboard/dashboard.js` and
`dashboard/mountains.svg` implement:

- Public sign-in/enrollment shell with no embedded private account/trade data.
- Authenticated dashboard with exactly CALL, PUT and EXIT ALL trading actions.
- Fixed read-only ATM through OTM+4 allocation, planned/owned quantities and
  execution state. Strikes/quantities come from the immutable server plan;
  unallocated or unavailable levels are never guessed.
- Direction, ownership/reconciliation status, conditional protection/quote health,
  independent supervision, synthetic broker evidence and emergency state.
- A separate Security page for fresh passkey trading authorization, session
  revocation and read-only safety boundaries. Login alone never authorizes entry.
- Trade History summaries, search/date/direction/state filters, page-size selection,
  pagination, selected-trade facts, per-strike premiums/gross/fees/net, exit reasons,
  order/execution evidence and recorded SPX chart evidence.

The single EXIT ALL button lives in the persistent top bar, including History
and Security. Clear separation, warning text and keyboard focus reduce accidental
activation without a confirmation dialog or a delayed hold requirement. CALL/PUT
require one activation after authorization. No START/CONFIRM trade controls,
manual strikes, contract quantities or allocation toggles exist. History rows per
page are a navigation setting, not position sizing.

## Blind Execution and realized results

Only sanitized application/history responses enter the browser. During active or
unresolved trades there is no unrealized P&L, live percentage return, raw fill
price, favorable peak or trailing milestone. The browser does not reconstruct
P&L from index movement or Greeks. Protection is labeled a conditional OFFLINE
model, with no claim of broker-held coverage. Stale broker evidence displays
unverified/last-recorded quantities rather than an unsupported flat claim.

The dashboard may reveal a realized result only after the history API reports
FINALIZED with the supported OFFLINE_FIXTURE origin. History tables show withheld
results for pending records. Finalized details use recorded Decimal strings;
financial display grouping does not calculate P&L using binary floating point.
Known finalization revocation clears visible cached figures. History/detail
refreshes reject older revisions and responses belonging to another authentication
context, and suppress figures when current verification fails.

Charts are recorded SPX observation points, not an invented continuous price path
or candles. Actual execution timestamps are plotted with a SPX Y-value only if the
API has exact source-time evidence; otherwise a separately labeled time-only marker
is used. Unavailable/ambiguous/withheld evidence shows explicit explanatory text.
Synthetic origin is visible on the shell, summaries, table and selected details.
Unsupported data origins are not presented as actual verified performance.

## Authenticated API additions

Phase 7 additionally provides authenticated credential management and recovery
with a pre-enrolled recovery-only passkey. Successful changes revoke all dashboard
sessions/grants; recovery does not log in or authorize trading. Security and public
recovery controls are separate from the three trading actions. The HTTPS adapter
now bounds concurrent workers and moves timed TLS handshakes off the accept loop.
See `mortificatio_phase7_acceptance.md` for the authoritative remediation status.

The existing FoundationAPI remains the only dynamic data boundary.

- `GET /auth/session` requires the secure existing session, exact host/transport,
  `Sec-Fetch-Site: same-origin` and `X-Mortificatio-Session: bootstrap`. It returns
  session expiry, a browser-memory CSRF token, an opaque non-authorizing command
  scope, independent grant status, offline broker-evidence status and fixed limits.
  It does NOT log in, reauthenticate, create a grant, verify IBKR or enable orders.
- Bootstrap issues additional random hashed CSRF tokens tied to the existing
  session and its expiry. Existing page/service-lease tokens remain valid, so a
  refresh cannot invalidate admitted acquisition just by changing CSRF. Tokens
  remain subject to session expiry/revocation, origin and CSRF checks. Hash-only
  `session_csrf_tokens` storage is bounded per session; no tokens are recoverable
  from the database. The primary login token retains its existing semantics.
- `dashboard_scopes` stores stable opaque per-user/account/environment command
  namespaces. A scope is NOT a credential and grants no backend permission.
- `GET /api/commands/{uuid}` authenticates and checks user/account/environment
  ownership, then returns the existing sanitized command/plan. It never admits,
  allocates or dispatches another command. Unknown/unauthorized commands are 404.
- History list rows now include recorded entry/admission timestamps for the
  date/time column, retaining the API's existing timestamp basis distinction.
- Historical exit reasons are reconstructed from each strike's last economic
  closing execution, retaining the original cause through price corrections.
  A later portfolio emergency cannot relabel an already completed trade. This
  reporting change does not alter the current controller's emergency state.

WebAuthn uses the browser credentials API and the existing maintained server
verification library. The frontend only converts standard binary fields to/from
base64url; it performs no authentication cryptography and stores no private key.
Registration uses the existing one-time local-administrator bootstrap via a masked
input, cleared before its request. There is no remote owner/service provisioning
or credential recovery shortcut.

Session cookies remain Secure, HttpOnly, SameSite=Strict and __Host-scoped. CSRF
values remain in page memory; they are not placed in local/session storage, HTML
metadata, URLs or logs. Broker credentials and independent service capabilities
never enter browser requests/responses. Session bootstrap requires a non-simple
same-origin header and cannot be read through cross-origin CORS. Trading authority
still requires a separate fresh WebAuthn assertion, short lifetime and verified
permitted broker epoch. The server remains authoritative for every trading gate.

## Duplicate, refresh, expiry and emergency behavior

The browser persists only non-secret UUID/direction/epoch/trade-identity command
metadata in an opaque scope, and the stable emergency request UUID. There are no
stored CSRF/session tokens, credentials, quotes, raw ledgers or performance values.
A local user who can inspect that origin's browser profile may see this limited
command metadata; this is not a defense against a compromised host/profile.

Before an entry request, metadata is persisted; same-tab concurrent activation is
blocked and browser Web Locks coordinate mutations where supported. The backend's
UUID, entry epoch, account-wide reservations, active-trade and frequency gates
remain the authority across tabs/processes. A same-command human retry reuses its
original UUID. Refresh/startup NEVER automatically POSTs a trade: it resumes the
session and performs authenticated read-only reconciliation/lookups. An uncertain
record cannot silently become an opposite-direction/new-ID request. Absence from
a lookup is not treated as proof that an in-flight delivery cannot arrive.

EXIT ALL bypasses entry locks and does not depend on trading grants or broker
eligibility. A local emergency intent fences further UI acquisition before the
request; its UUID survives refresh and explicit retries. The backend's durable
emergency fence and reconciliation are unchanged. A recorded request is never
called a fill or FLAT. Existing in-memory CSRF is sufficient for emergency;
bootstrap is only a bounded recovery retry if security context changed.

Expiry or revocation clears private DOM/state and disables entry, without revoking
the independent protective service. Reauthentication is required for authenticated
emergency access; there is deliberately no unauthenticated emergency endpoint or
exposed service token. A failed emergency request remains visibly unresolved,
with entries locally fenced. No automatic rearm is introduced.

Sign-out hides private data immediately and blocks late response repaint. All
async detail/recovery flows are bound to authentication context and command
identity. Actual session revocation still happens on the backend; a lost network
response is called unconfirmed rather than claiming successful logout. Existing
absolute session expiry remains the fallback, not a fabricated revocation.

Authorization countdowns use the server's security-clock sample and elapsed local
monotonic time only for display. This is NEVER an exchange timestamp or a market
freshness substitute. All 0.25s market gates, exact contracts, stop/trailing rules,
25/125 caps and broker reconciliation remain on the backend unchanged.

## Local HTTPS adapter

`executor_dashboard_web.py` supplies `DashboardWebApplication(lifecycle)` and
`create_https_server(web_application, ('127.0.0.1', port), tls_context)`.
No server starts on import. Explicit operator-provided TLS server certificates
and a configured `https://localhost:port` authentication origin are required.
Use a separately provisioned OFFLINE localhost profile; do not repoint an existing
passkey RP, change live credentials, or silently migrate account authorization.

The adapter serves a fixed asset allowlist and delegates dynamic requests through
FoundationAPI. It never exposes repository files or direct ledger routes. It
requires actual TLS sockets and TCP peers, ignores forwarded identity/transport
assertions, rejects conflicting critical headers and transfer encoding, bounds
request bodies/timeouts, and binds loopback only. CSP allows same-origin assets
and connections, without inline/eval scripts or remote fonts/images. Additional
headers include no-store, no-referrer, nosniff, frame isolation and restricted
browser features. There is no permissive CORS or HTTP authentication fallback.

A typical explicit integration, after the existing lifecycle dependencies and
independent capability are provisioned locally:

```
import ssl
from executor_dashboard_web import DashboardWebApplication, create_https_server

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(operator_certificate_path, operator_key_path)
server = create_https_server(
    DashboardWebApplication(offline_lifecycle), ('127.0.0.1', configured_port), context
)
# An explicit local operator may serve_forever(); no automatic listener/deployment.
```

No runtime test certificate, live credentials, broker connection, synthetic fill
controls or service capability is auto-created by this adapter. The existing
legacy simulator/research web service is separate; use this authenticated path
for Phase 1–5 integration rather than exposing the older demo service.

## Verification and screenshots

`executor_dashboard_test.py` tests authentication, same-origin bootstrap, hashed
session-bound/revoked CSRF, unchanged admitted acquisition after refresh, no login-
only trading, readonly recovery, active-data suppression, missing broker evidence,
emergency independence, restart, exact three actions, static CSP/traversal guards,
and core text-contrast/focus support.

`executor_dashboard_browser_test.py` / `.js` launch an isolated real headless
Chrome profile against ephemeral localhost HTTPS. Browser WebAuthn virtual keys
exercise genuine enrollment/sign-in and fresh authorization through the existing
server verifier. Calls to external origins are blocked; the resolver excludes all
hosts except localhost, and no IBKR scripts/credentials are used. Only this TEST
browser uses a temporary self-signed certificate trust exception. No trust store,
real browser profile or product TLS validation is changed.

The browser scenarios cover CALL and PUT one-press behavior, double activation,
five-strike readonly allocation, partial fills, private status, refresh without
replay, emergency liquidation, two finalized synthetic trades, filters/pagination,
pending reconciliation with withheld details, per-strike fees/results, recorded
and missing chart evidence, expiration, keyboard
navigation, responsive layouts and a delayed history response released after
logout. Synthetic fills are injected by the Python test's private IPC, NEVER a
browser-accessible test endpoint. Screenshots are written to
`/private/tmp/mortificatio-phase5-previews/` for visual review and are not real
trading results.

Run:

```
.venv/bin/python -B executor_dashboard_test.py
.venv/bin/python -B executor_dashboard_browser_test.py
.venv/bin/python -B offline_test_runner.py
```

The browser test requires the locally available Chrome executable. A sandbox may
require permission for its isolated browser/localhost test server. There are no
npm downloads or external visual assets. The five broker-connected scripts remain
excluded by the regression runner.

Final Phase 5 verification: **78 offline scripts passed, 0 failed**, with the
five broker-connected scripts excluded. Phase 5 adds 16 dashboard/API test methods,
two real-browser scenarios and one historical exit-reason regression (19 new test
methods/scenarios). The browser uses synthetic evidence and virtual authenticators,
not a verified real IBKR session or a physical security key. The captured full-suite
result is `/private/tmp/mortificatio-phase5-final-regression.log`.

## Remaining limitations and next phase

- Original ELIVO artwork is unavailable. Layout follows the written specification;
  no exact visual-fidelity claim is made. Platform serif fallback can vary.
- Desktop/tablet browser layouts, keyboard focus and core text contrast were
  checked. A full manual VoiceOver/screen-reader/switch/touch and cross-browser
  accessibility audit, hardware security-key testing and formal certification are
  not complete. Long Decimal/large histories may require horizontal table scrolling.
- A trusted local TLS certificate, OS/service isolation, passkey enrollment/recovery,
  dependency/runtime modernization and external security alerts/audit anchoring
  remain operational requirements. This is not a hardened internet deployment.
- Real IBKR login/2FA/session identity, entitlements, authoritative SPX/SPXW source
  timestamps for the unchanged 0.25s gate, broker-held protection, live fees/fills
  and correction semantics remain unvalidated. No actual execution is enabled.
- The interface polls sanitized state and history; it cannot promise instantaneous
  delivery during network failure or immediate liquidation in an illiquid market.
  Unknown exposure remains unresolved, and independent protection is explicitly
  an offline conditional model, not confirmed broker-held protection.
- Phase 6 should be a separately authorized offline operational/security acceptance
  stage: manual accessibility/hardware-key/browser testing, trusted localhost TLS,
  recovery and load/latency validation, and planning the remaining read-only IBKR
  validation blockers. Do not enable orders or begin Phase 6 without authorization.
