# Mortificatio Phase 4 — durable history and Blind Execution services

OFFLINE ONLY. No IBKR connection, actual order submission, paper/live enablement,
visual dashboard change, listener, deployment, commit, push or Phase 5 work is
included. All results are explicitly OFFLINE_FIXTURE / synthetic. These are
recorded synthetic executions, not verified real IBKR fills or performance.

## Architecture

- `executor_trade_accounting.py`: pure Decimal accounting from replayed execution
  revisions, actual recorded prices/quantities, source fill times and commission
  reports. It imports no Greeks or pricing model.
- `executor_trade_history.py`: authenticated account/user-scoped history queries,
  read-only journal replay, finalization checks, versioned materialized reports,
  realized aggregates and chart evidence.
- `executor_blind_presentation.py`: explicit allowlists for all unified trading
  API responses. It does not alter internal strategy/protective state.
- `executor_lifecycle.py`: exposes the history service through the existing
  lifecycle instance. History materialization is NOT called from the protective
  loop; reporting failures cannot prevent that loop from enforcing exits.
- `executor_application_api.py`: bounded authenticated GET history routes and
  centralized Blind Execution projection for status, entry, duplicate-entry and
  emergency responses. Existing authentication and trading routes remain intact.
- `order_boundary_test.py`: extends existing no-broker-client/no-mutation checks
  to the new runtime modules.

There is no new visual interface or server. Use the existing transport-neutral
`FoundationAPI(lifecycle)` boundary. The isolated legacy local simulator/research
servers remain separate; they do not contain this lifecycle's managed trades and
must not be exposed as an alternative authenticated production dashboard.

## Actual accounting inputs and assumptions

A read-only replay uses existing `OfflinePortfolioLedger` reducers and verifies
every journal hash, sequence and head against the live journal before deriving
history. It does not open another journal writer or mutate risk/controller state.
The immutable order, execution, correction, commission, source-time and terminal
reconciliation records are the source of truth; cached reports are derived views.

For each funded strike:

```
entry premium value = sum(active non-busted BUY quantity * recorded premium)
exit premium value  = sum(active non-busted SELL quantity * recorded premium)
weighted averages   = each premium value / that side's total quantity
gross realized P&L  = (exit premium value - entry premium value) * 100
net realized P&L    = gross realized P&L - recorded applicable commissions/fees
```

Only exact SPXW/USD/100-multiplier contracts already admitted by the existing
validators are supported. All relevant strikes must have matching entry and exit
quantities; no partial-position P&L is promoted to a finalized trade. There is no
FIFO estimate or delta/gamma/theta estimate. Portfolio gross, fees and net are
computed as the sums of their per-strike amounts, and equality is checked.
Quantities in history are ACTUAL filled entry contracts, not planned targets or
entry-plus-exit volume. Strike count is the number of actually traded strikes;
funded but canceled zero-fill strikes may appear with zero quantities.

Fees are reported amounts, not estimated per-contract fees or allocation reserves.
A $0.65 commission report on a 25-contract execution is $0.65 in this ledger; the
history service does not multiply the report by 25. Negative explicit reports are
credits/rebates. Missing or non-USD applicable commission evidence blocks final
results. Unrecorded fee categories and currency conversions are not invented.

**Correction semantics:** this phase follows the EXISTING broker ledger model:
execution corrections supersede prior economic revisions; totals include active
execution revisions and the latest commission report for each such revision.
Superseded fills/reports remain in the audit history, but are not counted twice.
Commission corrections replace a report only through the existing explicit
correction chain. An active busted revision contributes no premium/quantity but
retains its applicable recorded fee. This replacement/fee-attribution contract
must be verified against real IBKR correction, bust and fee behavior before real
reporting is enabled. The existing schema does not establish account-wide charges
or independently retained charges outside this execution-report model.

All money is Decimal and serialized as decimal strings. Weighted-average and
aggregate ratios use 50-digit Decimal precision; rounded average premiums are
never used to calculate cashflow P&L. No binary floating-point monetary summation
is used. Durations use source execution timestamps; local admission/verification
times are labeled separately. Source fill time must exist and match the recorded
execution digest, including superseded execution audit records. Missing source
time is not replaced with callback receipt time.

## Finalization and revocation

A report becomes FINALIZED only if all of the following hold:

- Verified journal replay and exact allocation/account/risk binding.
- The synthetic account's current full reconciliation proof is valid, including
  all managed journal revisions and no queued, unprocessed broker callbacks.
- All managed quantities are zero; each relevant intention is terminal/proven,
  with no uncertainty or quarantined ledger faults.
- Acquisition is sealed. The capital reservation was RELEASED through the
  existing reconciliation path, not by a local request or an application flag.
- The application command is COMPLETED (or a proven no-fill rejection), the
  lifecycle registry is FLAT_CONFIRMED, its terminal fingerprint proves zero
  positions/orders and complete executions/commissions in the current broker
  generation, and no lifecycle publication is awaiting acknowledgement.
- Actual quantities, source execution times and applicable fee evidence permit
  closed accounting without guessing.

Otherwise the service returns PENDING_RECONCILIATION with `totals: null`, no
average premiums, no fill prices, no realized result, no chart/markers and only
coarse reconciliation reasons/allowed execution information. A previously final
result is revoked on the next validated read if late/corrected/unprocessed
records, lost proof, held reservations or other uncertainty invalidate it. Pending
results contribute nothing to finalized performance metrics. Once fresh evidence
resolves the issue, a new revision can be finalized with recalculated amounts.

This is intentionally conservative: disconnect, restart or stale current account
proof can temporarily revoke visible historical finalization until reconciliation
is restored. These services do not serve an old cached profit figure as currently
verified while the lifecycle reports uncertainty. Results carry an `as_of_utc`,
revision and finalization time; they are not a guarantee against future broker
corrections.

Confirmed canceled requests with no economic fills are CANCELLED_NO_FILL, not
completed trades or fabricated $0 winners. Their audit record is queryable, with
no displayed final profit. Completed-trade aggregates exclude these records and
are NOT an account-wide cash/billing statement, including any separately incurred
charges on unfilled/busted requests.

## Durability and access

Two additive tables in the private foundation database hold `trade_history`
(current derived report) and `trade_history_revisions` (append-only report
revisions with previous-head/report hashes). Security audit entries anchor report
revision digests. Repeated identical reads do not create new economic revisions.
Every read checks projection integrity and reconstructs current evidence rather
than trusting a stale cached final result. Economic changes, finalization and
revocation create durable new revisions; old invalid figures are not returned by
any revision-export endpoint (none exists).

Materialization is lazy on authenticated history reads and independent of risk
supervision. GETs never supervise, dispatch, cancel, reconcile, change capital
reservations or grant trading authority. Session activity, security rate counters
and the derived history/audit projection may be persisted as read bookkeeping.
A broken history projection fails the history request closed while independent
premium/SPX protection continues. No reporting exception is added to the risk
service's control flow.

Queries require a valid Mortificatio session; user, account and environment are
selected on the backend. A valid different user cannot read another user's trade
or aggregates, even on the same account. Unknown and unauthorized trade IDs both
return 404 after authentication. Expired, revoked or missing sessions cannot read
history. GET history access does not create or extend a trading grant. History
request rates are independently bounded per user and account using the existing
security policy; throttling history cannot throttle the independent risk service.

## Read-only API

All routes use the existing secure-cookie, exact-host/origin, no-cache, and
transport-verified API adapter. No listener or TLS termination is added.

- `GET /api/history`: completed trades by default.
- `GET /api/history?state=PENDING_RECONCILIATION`: unresolved records.
- `GET /api/history?state=ALL`: visible completed, pending and no-fill records.
- `GET /api/history/summary`: finalized realized performance only.
- `GET /api/history/{trade_id}`: authorized details/per-strike execution history.
- `GET /api/history/{trade_id}/chart`: completed chart evidence or explicit
  unavailable/withheld status.

List/summary filters: `direction=CALL|PUT`, inclusive New York `start_date` and
`end_date` in YYYY-MM-DD format, bounded literal `search`, and
`source=OFFLINE_FIXTURE`. Search covers trade identity, date, direction, strikes
and a finalized exit reason; active profit-trigger reasons are not searchable.
List pagination uses one-based `page`, default `page_size=25`, maximum 100, stable
reverse date/admission-time/trade-ID ordering. Duplicate/unknown filters, invalid
ranges, account overrides and unsupported origins are rejected. Summary ignores
state/pagination for performance computation and always aggregates ONLY finalized
matching trades.

Summary includes completed count, wins (net > 0), losses (net < 0), breakevens,
win rate percent, total and average net realized P&L. Empty history returns count
0, total 0, and undefined average/win rate as null; it does not invent performance.
Every result is labeled OFFLINE_FIXTURE / synthetic. REAL_IBKR/LIVE or mixed-source
queries are unsupported and rejected; no real-data statistics are silently
combined with fixtures.

## Blind Execution contract

Every unified user-facing trading response is projected through explicit
allowlists, including fresh entries, duplicate replies, status and EXIT ALL.
Unknown newly added nested fields fail closed; this is not a denylist limited to
known P&L spellings. During active or unresolved trades, responses permit direction,
selected contract/strike identities, quantities, execution/reconciliation state,
generic protection/quote health, broker/supervisor health, risk health and emergency
status. They suppress:

- Unrealized dollar P&L, percentages and modeled/estimated profit.
- Favorable peaks, early/trailing activation and numeric trailing thresholds.
- Current bid/ask, raw market evidence, Greeks and average entry/exit premiums.
- Premium stop prices that reveal average entry, profit-trigger exit reasons,
  raw execution/commission history and chart evidence before finalization.

Protection health reports model/quote status, not an assertion of broker-held
coverage. The internal lifecycle and capability-protected risk service retain
all prices, stops, peaks, trailing state and source evidence, unchanged. Finalized
realized details are revealed only through the authenticated history services.
The visual dashboard and historical research calculations are unchanged.

## Chart evidence

The chart interface returns RECORDED_SPX_OBSERVATIONS with source timestamps,
OFFLINE_FIXTURE attribution and original evidence IDs, plus actual active
execution markers with recorded option premiums/quantities and source execution
times. SPX marker Y-values are present only when an unambiguous recorded SPX
observation has the EXACT execution timestamp. Otherwise they are null; no
interpolation or guessed index price is used.

No candles are manufactured from sparse ticks. `candles` is null with an explicit
unavailable reason. Fewer than two distinct source observation times make history
unavailable. Conflicting prices at one source timestamp make chart ordering
ambiguous/unavailable; such observations must not be rendered as an authoritative
continuous series. Marker Y-values then remain null. While a trade is pending,
all chart evidence is WITHHELD_BLIND_EXECUTION.

## Tests and remaining limitations

`executor_trade_history_test.py` covers one/five strikes, partial and weighted
fills, actual premiums, fee/credit corrections, winning/losing trades, per-strike
portfolio equality, late fills, execution revisions, unresolved fees/reservations,
revocation, stale/disconnected proof, restart, missing/ambiguous charts, projection
integrity and reporting failure isolation. It also checks 100 seeded Decimal
cashflow vectors. `executor_history_presentation_test.py` covers authenticated
read-only routes, ownership, filters/search/pagination, source isolation, active
response allowlists, pending result suppression, session expiry, rate limits and
independent risk behavior.

Remaining work before production includes:

- Real IBKR session/identity, actual fill/fee correction semantics and complete
  account reconciliation remain unverified. Authoritative SPX/SPXW timestamps for
  the unchanged 0.25s gate remain an unresolved broker-data limitation.
- No actual broker-held protection, executions, subscriptions, real performance,
  account-level tax/billing statements or currency conversions are implemented.
- Read-only projection queries replay durable journals under lifecycle coordination
  for consistency. Indexing, retention, reporting-worker isolation and latency
  under large histories need profiling before live/paper deployment. User/account
  query limits are bounds, not a distributed denial-of-service solution.
- Existing legacy-runtime/dependency modernization, HTTPS/browser integration,
  passkey recovery, external alerts and externally anchored audit/backup security
  are still required. Local hash chains cannot defeat a compromised administrator
  rewriting all state. State and service capabilities require OS isolation.
- The legacy simulator/research UI is still separate from the authenticated unified
  application. Phase 5 should, if authorized, integrate a single secure dashboard
  with the three trading actions, blind status and finalized history, using these
  APIs, plus browser/accessibility/security acceptance tests. Keep it offline and
  execution-disabled until separately authorized validation resolves broker gates.

Phase 5 has not begun.

## Final verification

Complete offline regression: **76 standalone scripts passed, 0 failed**; all
**5 IBKR-connected scripts excluded**. New Phase 4 tests: **18 accounting/history
+ 13 presentation/security = 31 passed, 0 failed**, including **100 seeded Decimal
cashflow vectors**. Existing strategy, protection, authentication, allocation,
reconciliation, lifecycle, replay and disabled-execution boundary scripts passed.
No visual dashboard, real broker connection/order, deployment, commit or push was
performed. Phase 5 has not begun.
