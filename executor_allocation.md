# Mortificatio Phase 2 — automatic five-strike allocation (offline only)

No IBKR connection, network listener, order submission, deployment, commit or push
is introduced. No visual interface is modified. All market/account evidence and
broker callbacks used in validation are explicitly synthetic offline fixtures.
`submission_enabled` remains false throughout. This is not proof of actual IBKR
entitlements, liquidity, account identity, timestamps, or fill performance.

## Modules and integration

- `executor_allocation.py`: immutable allocation/evidence/policy models and a pure
  Decimal allocator. It reuses existing exact SPXW validation, liquidity/Greeks
  checks, account freshness and price-limit calculations.
- `executor_contract_selection.py`: shared nearest-listed-strike ordering and ATM
  delta ranking, extracted without changing existing ranking behavior.
  `executor_ibkr_observation.py` and `executor_market_stream.py` reuse it.
- `executor_allocation_service.py`: application admission, atomic durable capital
  reservations, persistent allocation/journal binding, and fake-only scheduler
  coordination. There is no broker client or actual submission method.
- `executor_application.py`: accepts a trusted optional `allocation_boundary`.
  A funded direction command creates its plan within the same SQLite transaction
  as Phase 1 command/frequency reservation. The existing API accepts only command
  identity, direction and epoch; no new manual allocation fields/routes exist.
- `executor_portfolio.py`: optional funded configuration binds targets, budget,
  original arrival quotes, full plan and digest in a single journal event. Legacy
  explicit-target offline research remains separate and unchanged in behavior.
- `mortificatio_protection.py`: the independent reduction-only service may now
  plan cancellations for a halted acquisition even before a strategy exit. It
  still cannot authorize new exposure and does not require dashboard lifetime.
- `order_boundary_test.py`: extends the existing no-client/no-broker-mutation
  checks to all new runtime modules.

The existing local simulator UI/server is NOT rewired to the unified authenticated
application. A trusted offline harness constructs `AllocationBoundary(policy,
provider)` and passes it to `UnifiedApplication` as the fourth argument. The
provider's `snapshot(direction)` returns a typed `AllocationEvidence`, never a
browser-supplied payload. The default broker boundary still denies admission;
no real provider or transport is enabled. A Phase 1-only application without this
boundary still queues unfunded offline commands, which cannot be bound through
the new funded scheduler bridge. Do not treat that legacy mode as Phase 2 entry.

## Deterministic allocation

1. Require an exact PAPER account/risk binding and generation, complete listed
   same-day SPXW chain, exact contracts, coherent real-time SPX/option observations,
   sizes and Greeks. Direction must match every contract. All source and receipt
   ordering/quality/freshness checks remain in force.
2. Find the three nearest listed strikes using the existing selection rule. Rank
   those by distance of absolute actual delta from 0.48, then SPX distance, strike
   and conId, preserving deterministic tie breaking. Select ATM before sizing;
   do not select a cheaper alternate when the selected ATM is unaffordable.
3. Starting at that ATM, choose the next four ACTUALLY LISTED strikes above it for
   CALL, below it for PUT. No fixed spacing or invented delta ladder is used.
   Require exact quotes for the ATM candidate universe and all five selected
   levels. The implementation conservatively validates every supplied quote and
   the full five-level universe, even if capital ultimately funds only ATM.
   Missing/untradeable/invalid evidence blocks the plan rather than skipping.
4. Calculate available capital and remaining aggregate risk conservatively.
   Allocate at most 25 at ATM, then move to OTM+1 only if ATM receives 25. Apply the
   same rule through OTM+4. The first strike with less than 25 closes allocation;
   every subsequent quantity is zero. Fewer than 25 ATM contracts are valid if
   the configured minimum entry and all other gates pass. The portfolio is at
   most 125 contracts. Risk constraints can reduce the affordable quantity too.
   This priority governs capital planning; the existing scheduler can process the
   funded strikes concurrently without assuming ATM actually filled first.
5. Persist the complete immutable plan and gross reservation before planning any
   simulated orders. Neither cheaper later quotes, better fills, reconciled
   cancellations, nor commissions can increase its quantities or reallocate its
   unused capital. All later acquisition is limited to the original targets and
   bounded initial acquisition/repricing sequence.

## Capital, fees, and risk accounting

All financial policy inputs and monetary arithmetic use Decimal; configuration
accepts exact strings, integers or Decimals, rejecting binary float money. Quote
observations retain existing float types and are converted using their decimal
string representations before any monetary arithmetic. Contracts must validate
with multiplier `100`; no alternate multiplier is silently assumed.

For each contract:

```
entry cap = floor((arrival ask + maximum entry slippage) / verified tick) * tick
reserved cash = entry cap * 100 + fee reserve per contract
estimated premium = arrival ask * 100
estimated fees = configured entry fee + configured exit fee
planned stop risk = (3.25 + configured exit slippage) * 100 + fee reserve
```

Fee reserve must cover both estimated entry and exit fees. Any additional reserve
is retained as a cushion. The scheduler's existing execution policy must match
exactly, so it cannot use a different slippage/fee envelope. Delta notional uses
actual supplied Greeks and SPX, conservatively rounded UP to cents per contract
for reservation storage; it is not a model of future P&L or a fixed delta ladder.
Nominal stop-risk budgeting cannot guarantee losses in a gap or illiquid market;
full-premium funding and the separate capital cap remain enforced.

Initial eligible capital is the nonnegative minimum of:

- Configured capital limit (also capped by existing max-premium-cash policy), less
  outside owned cost, outside working reservations and central held reservations.
- Conservative fresh broker cash/funds, capped independently by buying power,
  less the cash reserve, outside working reservations and central held reservations.

Account buying power is never interpreted as permission to spend the entire
account. Existing external stop risk/delta and central held risk/delta reduce the
portfolio policy limits independently of cash affordability.

**Provider accounting contract:** `ExposureEvidence` includes gross external owned
and working exposure and EXCLUDES ALL exposure already covered by this central
reservation ledger. Its `excludes_local_reservations` and `complete` proofs must
be explicit. Outside owned cost reduces the capital budget, not already-net broker
cash a second time. Outside working cash is conservatively subtracted even if the
broker summary may already net those orders; this can underallocate, but cannot
spend those funds twice. A future real provider must document and verify the
summary/reservation decomposition, including unrelated account positions/orders.
Unknown accounting basis must fail closed; do not set completeness flags by guess.
An explicit complete position/order inventory must also prove there is no SPXW
quantity outside central reservations. Any positive, negative, working or unknown
outside SPXW exposure blocks a fresh allocation instead of guessing ownership or
merging it into a new trade. Other outside account exposures still consume budget.

A plan's full gross reservation stays HELD through all partial fills, terminal
entry cancellations, exits and fees pending. During its own acquisition, confirmed
spend reduces its remaining cash need; the original gross capital/risk envelope
still remains reserved. Other held plans are conservatively deducted in full.
There is no early reservation release or automatic reuse of price improvements.
An unreleased prior allocation blocks a new funded command even if its application
lifecycle record is terminal and cash appears sufficient. Only ledger-confirmed
release removes that fence. This prevents an unresolved old portfolio from
overlapping a new plan and bypassing the 25/125 quantity limits.

## Durable plan and reservation contents

The canonical immutable plan records direction, exact expiration/identities,
all five strike levels and quantities (including zero levels), executable asks,
price caps, premium costs, estimated fees, reserved cash, total contracts,
remaining eligible capital, nominal stop risk/delta, risk-policy version,
allocation reasons, policy, original market evidence, account evidence, external
encumbrances and prior central reservations. Authoritative source timestamps and
local monotonic receipt timestamps remain separate. Plan creation time is not a
replacement market timestamp. Reports are detached JSON copies; changing a report
cannot mutate its plan. Stored plan hashes are checked before reuse.

Admission/frequency/plan/reservation share a BEGIN IMMEDIATE transaction. Raced or
replayed commands cannot allocate twice. Duplicates return the existing immutable
decision without fetching new market data. Failed allocation reserves no trade or
cash; configuration metadata and security denials may persist intentionally.

Four additive tables are initialized in the existing foundation database:
`allocation_policy`, `allocation_plans`, `allocation_reservations`, and
`allocation_bindings`. Policy is persistently pinned per account/environment/risk
version; changing it requires a future reviewed configuration/migration flow.
No reset/rearm route or permissive migration bypass was added.

## Offline scheduler lifecycle

`bind_engine` requires fresh Phase 1 authentication/grant and a matching verified
fixture broker generation. It persists a random durable portfolio journal identity
and centrally pins the plan to that exact journal path AND identity before
configuring targets. A crash between central registration and configuration can
park the reservation but cannot clone it into another journal. A lost/recreated
journal with a new identity cannot take over the existing binding. Rebinding the
same journal/plan is idempotent. Binding metadata must survive recovery.

`advance` rechecks the original admitted session/grant, account, generation,
emergency flags, reservation and exact plan/journal. It rechecks fresh financial
exposure and current target quotes/Greeks, then delegates to the existing portfolio
scheduler. `simulate_dispatch` performs the same checks before assigning ONLY
fake ledger order IDs to BUY intentions. It never calls a broker. Protection/
liquidation remains on the independent reduction-only service, not this entry API.

The portfolio scheduler preserves configured slicing/concurrency, displayed-size
participation estimates, spread/quality gates, target limits, partial fill
accounting, terminal cancellation proof, original arrival-price bounds, bounded
repricing and retry/deadline behavior. A five-strike plan is never treated as an
atomic broker fill. Quotes can move within bounds without resizing the plan;
prices/risk/funds outside the authorized envelope halt further acquisition and
retain existing reservations pending reconciliation.

Dashboard/grant expiration, logout/revocation, broker generation changes,
emergency/account fencing and financial-gate failures block further acquisition.
The entry bridge durably halts its acquisition on authorization/financial denial;
there is no automatic resumption with another login/grant. The independent
protective service can plan outstanding-entry cancellations even without a normal
strategy exit and continues to monitor owned exposure. Existing emergency fencing
and premium/SPX strategy rules remain unchanged. No UI session is needed by the
reduction service, and its capability cannot authorize BUY plans.

`release_reconciled` requires the independent reconciliation capability, the same
pinned journal, fresh complete ledger reconciliation, zero owned positions and
no unproven intentions, PLUS a terminal reconciled application lifecycle state.
It durably seals acquisition before marking a reservation RELEASED. Repeated
release does not credit money twice. A cancellation request, bare application
completion assertion, missing commission, stale snapshot, or visible zero quantity
alone cannot release it. Reconciliation/correction/late-fill faults remain fail
closed; no process claims FLAT from local intent or cancellation acknowledgement.

## Tests and limitations

New `executor_allocation_test.py` covers ATM/partial ATM/full ATM, partial OTM,
all five levels/125 cap, capital/minimum-entry limits, fees, multiplier, reserves,
existing exposure, risk/delta ceilings, missing strikes, CALL/PUT direction,
actual delta ranking, missing/frozen/delayed/stale/crossed/wrong-identity quotes,
source synchronization and immutable plans. It includes 300 seeded capital/
direction cases and 200 varied-price/fee/reserve/risk cases.

New `executor_allocation_service_test.py` uses real Phase 1 WebAuthn verification,
SQLite transactions, and fake broker callbacks. It covers one-press API plans,
duplicates/concurrent requests, atomic failed admission, account/environment/
generation isolation, emergency races, partial fills, quote bounds, cheaper quotes
without additions, session/grant expiry, independent cancellation, recovery,
process death, journal binding, cancelled orders, missing commissions, late fills,
confirmed liquidation/release, and enforced no-network/no-order boundaries.

Remaining blockers and tradeoffs:

- No real IBKR or market-data provider is enabled. Existing authoritative 0.25s
  timestamp/entitlement limitations remain unresolved. Raw local receipt times
  cannot satisfy the source gate. All observations here are offline fixtures.
- Production correctness needs broker-wide ownership/reservation decomposition,
  verified external exposure, source-proven quotes/Greeks, actual fees, and cash
  reconciliation. Fees can exceed estimates: cash/risk checks halt acquisition,
  but an estimate is never a guaranteed charge or fill price.
- Gross reservations are intentionally retained until fully reconciled flat;
  this underuses capital during partial cancellations/holding and uncertain fees.
- Restart does not automatically acquire remaining targets. Broker reconciliation
  and unresolved acquisition handling are mandatory; independent exits continue.
  There is no automatic reauthorization/rearm or lost-journal replacement.
- A real continuously running coordinator must deliver broker corrections/late
  fills promptly to both portfolio and application reconciliation. The existing
  Phase 1 recovery fence blocks an account on late exposure after completion.
  Fixtures are trusted backend inputs, not a cryptographic guarantee of broker truth.
- Providers must serve already captured, immutable snapshots without network I/O
  or acquiring portfolio locks inside the application admission transaction.
  SQLite serializes admissions; it is not a distributed multi-host execution bus.
- Existing Phase 1 security limitations remain: legacy UI isolation, HTTPS adapter,
  runtime/library modernization, key recovery, external alert delivery and audit
  anchoring. Local OS/database compromise remains outside token/file protections.

Recommended Phase 3 (requires separate authorization): integrate the authenticated
application, funded allocator and independent protective worker into one offline
service lifecycle with the three-action interface and read-only allocation/status.
Complete authorization/recovery, security adapter and account-wide reconciliation
acceptance tests before considering separately authorized read-only IBKR validation.
Do not enable execution or start Phase 3 under this authorization.

## Final verification

Final complete offline regression: **72 standalone scripts passed, 0 failed**;
all **5 IBKR-connected scripts excluded**. New allocation tests: **18 pure
allocation + 26 application/scheduler integration = 44 passed, 0 failed**,
including **500 seeded randomized invariant scenarios**. The final run includes
the unresolved-prior-reservation fence and explicit outside-SPXW inventory proof.
Existing authentication, strategy, market-data, protective exit and paper-ledger
regression scripts passed. No visual interface was changed, no broker connection
or real submission occurred, and Phase 3 has not begun.
