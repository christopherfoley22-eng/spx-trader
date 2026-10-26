# Phase 12 broker lifecycle — documented, simulated, observed, unverified

Reviewed 2026-10-25. **No IBKR connection or actual broker observation occurred.**
No paper/live order was submitted, modified or canceled. Native in the code means
managed broker-style STP intentions, not proof that Cboe hosts the stop natively.

| Capability | Documented | Simulated/implemented | Observed | Remaining validation |
|---|---|---|---|---|
| Stop orders | IBKR offers stops and may simulate order types server-side. | Managed STP intents, identity reservation, acknowledgment/working distinction, execution/correction accounting. | None | Exact SPXW/route/trigger/grid/session eligibility and persistence. |
| Parent-child/brackets | Attachment and staged Transmit documented; legacy TWS guide describes child activation on complete parent fill. | No assumption of automatic partial-fill coverage; each fill creates an obligation and entry fence. | None | Parent/child partial activation, rejection and disconnect behavior. No actual bracket transmission implemented. |
| OCA | Types include cancellation/reduction and with-block overfill protection. | All native/tactical residual sells share owned-quantity reservations; no assumed OCA atomicity. | None | Actual blocking/routing and fill/cancel behavior for the exact product/account. |
| Cancellation/replacement | PendingCancel is not cancellation confirmation. | Old stop remains reserved through request/ack uncertainty; replacement requires terminal reconciliation. | None | Safe production replacement policy and actual terminal/remaining semantics. |
| Executions/corrections | Distinct execution IDs; correction IDs change final revision suffix. | Active revisions/busts drive positions, obligations, fees and history. | None | Broker-native normalization of pending-price revisions, zero-perm rejections, bust/correction messages and complete history. |
| Status callbacks | Duplicate and missing callbacks are possible; executions matter. | Derived lifecycle plus validated full snapshot repair of missing status, never synthetic fills. | None | Installed SDK/server callback ordering and reconnect histories. |
| Read-only history | reqExecutions/execDetails/commissionReport and response completion are documented. | Account-filtered one-shot request hook and durable private callback capture; no connection attempted. | None | Actual available time window, completed orders, fees and all-intent coverage. |

## Official findings and their limits

IBKR may keep a stop in its own systems rather than at the exchange. Product-
dependent trigger rules can differ from a simple single bid crossing. A broker-
managed simulated stop, exchange-native order and application stop are three
separate mechanisms. [IBKR order catalog](https://www.interactivebrokers.com/en/trading/ordertypes.php),
[simulated types](https://www.interactivebrokers.com/en/trading/simulatedOrderTypes.html),
[official trigger-method guidance](https://ibkrguides.com/traderworkstation/define-order-preset-value.htm).
No precise SPXW route-specific native-stop support was established by a broker
observation. Catalog availability is not proof of account acceptance or protection.

Attachment/transmit documentation does not prove each partial fill is protected.
The legacy TWS description says activation after complete parent fill; behavior
from another API such as FIX must not be imported into TWS assumptions.
[Current attachment guide](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/adding-a-profit-taker-and-stop-loss),
[legacy TWS submission guide](https://interactivebrokers.github.io/tws-api/order_submission.html).

OCA variants and with-block routing protection do not create a universal atomic
cancel/replace guarantee. A pending cancellation can still fill; remaining owned
quantity and every outstanding sell must reconcile first.
[IBKR OCA](https://interactivebrokers.github.io/tws-api/oca.html),
[Order fields](https://www.interactivebrokers.com/docs/tws-api/ref/order).

The execution identifier's last revision component can indicate a correction;
partial fills each have execution IDs. This does not make an arbitrary different
ID a safe independently additive fill. Explicit correction chains and active
revision accounting are required.
[Current Execution reference](https://www.interactivebrokers.com/docs/tws-api/ref/execution),
[execution/commission guide](https://interactivebrokers.github.io/tws-api/executions_commissions.html).
The latter is marked deprecated; actual installed-version behavior needs testing.

Order-status callbacks alone cannot establish fills or final flatness. Duplicate
statuses and missing status transitions are documented concerns. `Inactive` is
not used as conclusive rejection or terminal release; code 201 correlated to a
known native intent can record rejection evidence, while quantities remain held
until terminal reconciliation.
[Submission/status guide](https://interactivebrokers.github.io/tws-api/order_submission.html),
[official error codes](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/error-codes).
A market-data bust notification such as 10225 is not automatically an identified
trade bust. No execution/quantity is invented from a generic error code.

Execution query history is bounded by SDK/server/TWS settings. The legacy client
reference describes current-day executions; the guide describes a TWS Trade Log
setting for a longer available window. `execDetailsEnd` is response completion,
not proof of coverage back to all durable local orders.
[EClient reference](https://interactivebrokers.github.io/tws-api/classIBApi_1_1EClient.html),
[history guide](https://interactivebrokers.github.io/tws-api/executions_commissions.html).
Missing older history or commissions must keep recovery unresolved.

Weighted entry minus 3.25 can fall between quoting increments. Whether that exact
auxPrice/trigger is legal for the intended broker/route must be validated. No
rounding or strategy threshold was changed. Exact SPXW calendars, expiration,
RTH/extended-session handling and early closes also remain validation inputs.
[Cboe SPX specifications](https://www.cboe.com/tradable-products/sp-500/spx-options/spx-specifications).

## Offline coordination policy

- A supported immutable protection-readiness plan is mandatory for admission,
  alongside Phase 11 capital/exit-capacity/data/risk conditions. It binds account,
  generation and exact contract identities, STP type and the 3.25 premium offset.
  The only accepted mode is explicitly OFFLINE_FIXTURE. It does not assert working
  broker coverage or enable any real order boundary.
- Every economic fill/correction recomputes current ownership and protection
  requirements within the same existing durable journal transaction. Any owned
  exposure fences additional acquisition until recovery; there is no automatic
  acquisition rearm, including after synthetic coverage becomes working.
- Prepared stops reserve confirmed owned quantity. Only explicit synthetic identity
  binding and later matching acknowledgment plus full reconciliation can establish
  modeled working protection. PreSubmitted alone is not treated as working proof.
- Native stops are managed closing intents in the original ledger, not external
  positions or a second accounting ledger. All tactical/native/external quantities
  share sell reservations. A full stop fill is not portfolio-flat evidence.
- Cancel-confirmed-then-reduce is the modeled coordination path. A replacement
  request keeps old quantity reserved. This avoids overlapping sell authorization
  but cannot promise continuous real protection during a cancel/new-order gap.
  Real atomic/OCA replacement has not been enabled or verified.
- If a BUY correction makes existing closing orders oversized, retain the economic
  correction, fence acquisition, request cancellation descriptors, and hold those
  sells until reconciled cancellation. No extra closing order is authorized.
- Corrections that restore formerly closed exposure revoke flatness/finalized
  history and rehold reservations. Actual recorded prices/fees drive recalculation.
  Contradictory corrections implying short/oversized exposure are quarantined with
  raw evidence; they are not silently repaired or reported flat. Automatic repair
  of ambiguous multi-execution correction groups remains a blocker.
- Restart/disconnect invalidate working-proof generation and entry authority;
  full positions, executions, fees, open/terminal orders and IDs must agree again.
  No absence-only non-submission proof or blind uncertain-order retry exists.

## Read-only harness: prepared, not run

`BrokerLifecycleValidation` stores private hash-audited callback observations,
including exec IDs/families, quantities/prices, raw execution time, order/perm/
client/ref identities, status, held reasons, fees and broker errors. Duplicate
executions do not create another economic fact; contradictory observations are
retained separately and fail validation. Capture is not the execution ledger.
No timestamp timezone, source clock precision or complete history is invented.

`LifecycleReadOnlyProbe` inherits mutation-disabled methods and requires the
existing explicit paper-account/localhost-port/read-only-settings authorization.
The sole new outbound capability is an account-filtered, bounded-ID, one-shot
read-only execution-history query after verified session identity and separate
opt-in. The static prohibition on reqExecutions was narrowed only for this guarded
method in this probe; every mutation prohibition and all other files remain guarded.
No query was sent in this phase.

After separate connection authorization, approved settings/artifacts and manual
TWS/Gateway login/IBKR Mobile authentication as required:

1. Create fresh ApprovedPaperAccount and SettingsVerification, then private
   BrokerLifecycleValidation(capture_path=...) using the exact approved DU account.
2. Run `run_lifecycle_read_only(..., authorized=True, include_execution_history=True)`
   only after that authorization. Use a dedicated nonzero client; no order binding.
3. Compare positions and open-order observations with local IDs; inspect raw
   orderStatus, execDetails, commissionReport and execDetailsEnd evidence. Preserve
   account isolation, pending-price flags and source-time uncertainty.
4. Verify available history coverage, terminal records and commission completeness
   before designing a real normalizer into the authoritative reconciliation layer.
   The present capture deliberately never produces a trading/reconciliation permit.
5. Reconnect with a new validation object; compare persistent orders, executions,
   identities and positions. Missing/unexpected data stays unresolved. Querying
   older execution/terminal history may require additional reviewed mechanisms.
6. Native activation/partial protection, trigger rules, rejection, cancellation,
   OCA and replace races cannot be proved without actual orders. Those require a
   separately approved controlled paper-order protocol; this phase grants none.

Actual broker observations: **0**. Paper and live: **NO-GO**.
