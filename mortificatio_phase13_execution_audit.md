# Phase 13 — execution evidence map

Inspection date: 2026-10-25. No real broker connection or observations. Prior report claims were checked against source/tests and saved benchmark artifacts; synthetic measurements remain synthetic. Phase 12 artifact reports 50 comparable exit samples, p99 48.22 ms, versus Phase 11 71.94 ms and Phase 10 177.9 ms under uncontrolled shared load. No causal speed claim. No execution-path change or new latency benchmark in Phase 13.

| Stage | Implemented evidence | External evidence / next action |
|---|---|---|
| CALL/PUT/EXIT ALL | `dashboard/index.html`, `dashboard/dashboard.js`, `executor_dashboard_web.py`; three actions, no manual allocation; browser/API regression | Physical passkey, trusted HTTPS and supported-device acceptance remain operational requirements |
| Authenticated command | `executor_application.py`, `executor_application_api.py`; passkeys, separate expiring grants, durable idempotency/account frequency, emergency fence | No real broker authorization asserted by offline evidence |
| Prepared candidate | `executor_entry_readiness.py`, `executor_allocation.py`; immutable direction plans, exact listed ladder, delta targeting, same-day, 25/125, strict ATM-first | Real SPX/SPXW/Greeks/provider provenance and approved source-time solution required |
| Hard capacity admission | `executor_exit_capacity.py`; explicit policy, stress assumptions, risk limits, data/reconciliation/protection readiness; missing evidence rejects | Context currently explicitly `OFFLINE_FIXTURE`. No calibrated real capacity provider or approved operational risk configuration |
| Capital reservation / acquisition scheduling | `executor_allocation_service.py`, `executor_portfolio.py`, `executor_lifecycle.py`; Decimal affordability, pending reservations, partial fills, revalidation, restart | Real account funds/existing-order reservations and audited adapter mapping unverified |
| Order preparation / dispatch | `executor_paper_broker.py`, `executor_paper_reconciliation.py`; durable order intents/IDs and fake broker | Actual execution methods disabled. No authorized real-paper order adapter proven |
| Acknowledgment / executions | `executor_native_protection.py`, `executor_broker_lifecycle_capture.py`; ack != working != filled, duplicate/correction handling, private callback archive | `ibkr_phase12_read_only.py` can inspect permitted callbacks but does not automatically convert raw observations into economic authority. SDK/TWS compatibility and full snapshot/history coverage unverified |
| Protection after fill | `executor_protection_recovery.py`, `executor_native_protection.py`; durable per-fill obligation, acquisition fencing, coordinated reduction reservations | No broker-verified native stop coverage. Partial activation, stop eligibility/trigger/price increments, OCA and safe cancel/replace require controlled paper tests |
| Strategy / exit coordination | `executor_protective_exits.py`, `executor_protective_service.py`, `executor_liquidation.py`, `executor_exit_readiness.py`; independent checkpoints, reduction-only safety | WAP option premium minus 3.25 remains active; SPX +4/+1.25, +5 uncapped 3-point reversal unchanged. Actual native order behavior/slippage unavailable |
| Verified flat / history | `executor_trade_history.py`, `executor_trade_accounting.py`, `executor_blind_presentation.py`; positions/orders/reservations/corrections reconcile before final result; blind active projections | Actual complete broker evidence required, zero position with working sells is not flat |

## Capacity inputs: what can actually be established

| Input | Reality / required evidence |
|---|---|
| Exact contract chain, expiry, multiplier, conId | Read-only contract qualification; today's listing/venue evidence must be observed |
| SPX and SPXW bid/ask/sizes | IBKR L1 with correct entitlements. Sizes are displayed estimates, not executable guarantees |
| Greeks | Broker option computation callbacks with underlying/option entitlements; missing values fail closed |
| Authoritative quote/Greek timestamp | Not established by normal callbacks. Local wall/monotonic times and last-trade time are distinct. Unrelated callbacks do not refresh an option's source evidence |
| Depth and recent liquidity | Contract/venue/API/entitlement dependent and unverified. Additional licensed historical quote/trade/depth sources could support calibration, but none is integrated or licensed here |
| Cash/buying power and actual exposure | Account summary/positions/open orders/executions can be read; completeness, currency and reservation reconciliation must be proved |
| Executable entry/exit capacity, stress losses, latency deterioration | Currently explicit synthetic policy/model assumptions. No guarantee of future liquidity and no empirical calibration |
| Protection feasibility | Offline supported-plan model, not broker working-order confirmation |

`CapacityContext` rejects non-fixture origin; no safe real-provider switch exists. Buying power cannot by itself authorize 125. Missing required policy/context/source-time/quantity/protection evidence rejects admission. This is a deliberate real-execution blocker, not a reason to loosen the gate.

## Native protection: evidence boundary

| Behavior | Official documentation | Read-only session | Controlled paper test | Limited live validation |
|---|---|---|---|---|
| Parent/child/bracket transmission | Describes API construction and transmission | Existing relationships/status only | Partial fills and activation timing | Real routing/fill differences |
| Native SPXW stop | Generic stop API is documented; exact contract/routing eligibility not proven | Inspect existing STP contract/status | Eligibility, trigger, tick rounding, gaps and reject recovery | Genuine slippage/market behavior |
| OCA | Grouping/reduction semantics documented; not a universal atomic guarantee | Existing fields only | Partial fill/cancel races and overfill behavior | Venue-dependent execution limits |
| Cancel/replace | Requests/status lifecycle documented | Observe changes already made elsewhere | Old-stop fills, replace failures, residual coverage | Actual routing timing |
| Execution corrections/busts | Execution identity/correction behavior documented | Capture events only if supplied; historic coverage limited | Correction ingestion/recovery protocol, possibly broker-assisted | Real correction occurrence cannot be manufactured |
| Disconnect / persistence | API reconnect/open-order requests documented | Inspect actual retained state across authorized reconnection | Working protection through disconnect/restart | Real operational persistence/venue conditions |

Documentation is not observation. Paper tests also cannot prove live execution quality. See [IBKR bracket orders](https://interactivebrokers.github.io/tws-api/bracket_order.html), [OCA](https://interactivebrokers.github.io/tws-api/oca.html), [order lifecycle](https://interactivebrokers.github.io/tws-api/order_submission.html), [executions](https://interactivebrokers.github.io/tws-api/executions_commissions.html). These official legacy SDK pages are marked deprecated by IBKR; recheck the selected current SDK documentation during environment approval. Exact SPXW stop behavior remains unverified.

## Concrete defects versus setup limitations

Fixed: runtime audit previously omitted absent required packages. Fixed: Phase 12 callback capture had no guarded private-output executable handoff; Phase 13 launcher provides one. Existing older diagnostic CLI still prints full private reports to stdout; use the new launcher and do not redirect raw output to shared logs. Existing read-only harness is a diagnostic, not production authorization or complete five-strike validation. SDK 9.81 callback compatibility with a current TWS/Gateway build must be checked after approved runtime/package selection.

Setup blockers: supported OS/runtime/artifacts; actual TWS/Gateway installation/session; manually verified read-only setting/account/port; entitlements; separate connection authorization. No settings or permissions were changed.

Concrete offline regression evidence: `executor_application_test.py` and `executor_dashboard_browser_test.py` cover authenticated commands/UI; `executor_allocation_test.py`, `executor_allocation_service_test.py`, `executor_entry_readiness_test.py`, `executor_exit_capacity_test.py` and `executor_phase11_lifecycle_test.py` cover prepared sizing/admission; `executor_market_stream_test.py` and `executor_broker_reality_test.py` cover market evidence; `executor_native_protection_test.py`, `executor_phase12_lifecycle_test.py` and `executor_broker_lifecycle_capture_test.py` cover native obligations/corrections/callback capture; `executor_lifecycle_recovery_test.py` and `mortificatio_lifecycle_crash_test.py` cover recovery; `executor_trade_history_test.py` and `executor_history_presentation_test.py` cover reconciliation/privacy. These remain synthetic/offline evidence. Phase 13 changes, complete regression outcome and environmental exceptions are recorded in `mortificatio_phase13_readiness.md`.

“Broker-native” must not be read as “exchange-native.” An IBKR-held/simulated stop and an exchange-held order have different dependencies; exact hosting/trigger/persistence for SPXW must be established for the selected route. Neither a stop trigger nor paper fills guarantee an execution price. No existing protection is removed by Phase 13.
