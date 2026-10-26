# Stage 4A: offline protection and emergency exits

This stage models broker-held protection using the existing paper ledger and
fake broker. It does not instantiate a broker client, connect to IBKR, transmit
orders or cancellations, or enable execution. Existing order-boundary checks
remain unchanged. All model actions and service results remain non-executable.

`executor_protective_exits.py` journals its control state through the existing
paper broker ledger, so execution history, correction identity, reservations,
session generations, and reconciliation remain authoritative. The manager is
enabled explicitly; pre-existing Stage 3A ledgers continue to work without it.
An enabled manager must be configured with the exact entry-intent group and
the authorized entry SPX value before processing that trade's observations.

For each owned quantity, desired protection is the active execution reports'
quantity-weighted entry premium minus **3.25 option-premium points**. SELL fills
do not reset that entry basis. Corrections replace the original BUY execution
in the existing ledger; they do not double count it. Invalid/nonpositive stops
cannot be planned as protection and latch emergency recovery for already-owned
contracts. New acquisition remains subject to the existing entry gates.

Coverage is reported as `BROKER_HELD_MODEL_ACKNOWLEDGED` only after a matching
STP identity, premium, remaining quantity, broker acknowledgement, and fresh
complete reconciliation. A planned, rejected, uncertain, stale, or wrong-size
stop is not reported as protection. Partial entry fills may have a protective
SELL for the quantity already owned while the remaining BUY is still working;
that exception does not authorize an ordinary profit or emergency SELL against
unresolved acquisition. Aggregate SELL reservations still cannot exceed ownership.

## Replacement and profit exits

The existing durable controller exports a shared pure evaluator for its SPX
profit rules: +4 arm/+1.25 floor, +5 trailing arm, and a three-SPX-point reversal.
Those thresholds and their semantics are unchanged. Both the controller and
protection manager use the same evaluator. The existing 0.25-second market-data
gate is unchanged. SPX profit observations retain the existing LIVE, freshness,
and source-order checks; no price movement or fill is invented from missing data.

Automatic profit scale-outs are **disabled in the active strategy**, even if
research targets are configured or recovered. Configuration validation, target
metadata, and partial-exit research helpers remain available for research; the
active `advance()` path never selects a target or creates a `PROFIT_PARTIAL`.
Safe liquidation chunks continue to mean exit ALL remaining contracts.

SPX profit tracking starts with the first confirmed entry fill. The controller
persists the entry baseline, favorable peak, early protection, and trailing arm
through further fills, entry completion, and restarts. Both replay paths evaluate
premium and SPX risk before additional acquisition. Any triggered exit halts
synthetic entry chunks; actual late fills remain broker facts to reconcile.
The hard premium stop continues to apply after either SPX protection arm.

The offline paper model cancels unresolved acquisition first, retaining any
existing protective stop until acquisition cancellation is reconciled. It then
uses the existing cancel-and-reconcile barrier for competing SELL intentions:

1. Persist model cancellation requirements for competing bound orders.
2. Wait for explicit cancellation/terminal records, execution history, positions,
   commissions, and fresh complete reconciliation to agree.
3. Reserve one full reduction or replacement stop for current owned quantity.

`PendingCancel`, empty open-order lists, disconnects, or uncertain submissions
do not release reserved quantities. Only a never-bound, provably local intention
can be abandoned locally. It cannot later be rebound. Before binding a new
protective identity, both quantity and premium are checked again against fills.

**Cancel-before-replace creates a modeled coverage gap.** The status exposes it
as `UNPROTECTED_OR_RESIZE_PENDING`; it never claims uninterrupted protection.
Real atomic replacement, OCA behavior, exchange tick rounding, stop triggering,
rejection semantics, and execution quality remain unverified external blockers.

## EXIT ALL and the local UI

`executor_emergency.py` provides an account-bound SQLite WAL/FULL emergency latch.
`POST /api/exit-all` accepts only a request UUID and uses the existing loopback,
cookie, Origin, and CSRF checks. Simulation and an explicitly injected offline
protective-model service support this route; observation and shadow modes do not
gain execution controls. `executor_protective_service.py` renders an injected
fake-broker manager through the existing UI and rejects execution endpoints.
No new broker-connected mode is launched by the CLI.

The simulation service persists the latch independently of its replay lock.
The EXIT ALL browser request bypasses the normal action queue, clears queued
entry/run actions, and disables entries immediately. The browser also retains
its emergency indication in session storage when available, including when the
acknowledgement is lost. Stale READY responses cannot re-enable its entry buttons.

The controller checks the latch before acquisition and at replay boundaries.
It also persists `EMERGENCY_REQUESTED` in its own journal, preventing entry after
restart even if an external latch object was not passed by a caller. Already
filled quantities remain owned until explicit exit-fill evidence and existing
broker reconciliation confirm zero positions and no unresolved orders.

In the paper model, EXIT ALL latches in the same durable ledger, blocks both new
BUY intentions and new BUY identity bindings, cancels unresolved acquisition and
competing SELLs through the modeled barrier, and then reserves liquidation.
Late fills are still recorded as facts and invalidate previous flat/coverage
claims. Repeated request IDs are idempotent; distinct repeated clicks do not
create duplicate exits. There is deliberately no automatic emergency rearm.

The UI never reports emergency FLAT from the latch flag alone. It requires the
controller's reconciled-flat audit or the paper manager's fresh reconciled-flat
evidence. Exhausted replay fixtures, missing quotes, rejected stops, disconnected
sessions, or unexplained late fills stay pending/recovery-required. Simulation
EXIT ALL consumes only supplied synthetic observations; it never manufactures
a last-price fill to force an exhausted fixture flat.

## Validation and remaining blockers

`executor_protective_exits_test.py` uses the Stage 3A independent fake broker;
`executor_emergency_test.py` covers controller replay, restart, HTTP, and service
concurrency. The existing Node UI timing test also exercises an EXIT ALL click
while an entry request is blocked. Run the complete suite with
`.venv/bin/python -B offline_test_runner.py`.

Real paper-session verification and broker transport remain absent. Account-wide
history completeness, real stop/OCA/cancel behavior, corrections and commissions
normalization, and Stage 2 source-timestamp/synchronization evidence still need
external validation. This offline model is not broker-held protection for an
actual position and cannot prevent external/manual broker orders from overselling.
