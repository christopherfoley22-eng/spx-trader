# Offline five-strike liquidity and execution scheduling

This is an isolated **offline model**, not a paper/live execution adapter. No
broker client, socket, submission/cancellation API, UI execution route, or live
mode was added. All returned actions have `executable: false`. The existing
single-contract executor, its 25-contract maximum, and all uncommitted work are
preserved. Five-strike / 125-contract capacity exists only in this model.

## Components and reuse

- `executor_portfolio.py`: `OfflinePortfolioLedger` extends the existing durable
  `PaperBrokerLedger`. It reuses its WAL/FULL journal, single-writer lock, approval
  binding, offline-only verified paper session, generations/nonces, economic
  execution and correction accounting, commissions, identity validation, and
  fault quarantine. A dedicated journal with a mode/policy marker is required;
  it refuses existing single-strike journals instead of migrating them.
- `executor_portfolio_liquidity.py`: explicit `ExecutionPolicy`, source-labeled
  `LiquidityFixture`, liquidity assessment, and tick-rounded limit-price policy.
- `executor_paper_reconciliation.py`: the public single-strike function still
  hardcodes one position. Its shared private core also serves the isolated
  portfolio with a five-position bound and additional LMT acknowledgment checks.
  Every asset still passes the existing per-contract/account/freshness validator.
- `executor_portfolio_test.py`: independent fake broker and actual fake-book
  liquidity, separate from displayed/estimated liquidity.
- `executor_portfolio_stress_test.py`: eight seeded partial-entry / 125-contract
  liquidation scenarios with disconnects, rejections, and disappearing liquidity.
- `order_boundary_test.py`: extended import/isolation checks; existing forbidden
  broker APIs and live/read-only/UI boundaries remain unchanged.

## Configuration and pre-trade checks

Configure one portfolio with 1–5 exact same-day SPXW contracts, **distinct
strikes, one direction**, and 1–25 contracts per strike. The maximum is therefore
125. Duplicate identities/strikes, wrong contracts, mixed directions, shorts,
and excess quantities fail closed. Existing contract validators are reused.

`ExecutionPolicy` requires every execution/risk limit explicitly; there are no
production slippage, liquidity, retry, or exposure defaults. Test parameters are
synthetic examples, not recommended trading settings. Configure:

- Maximum spread; entry and exit slippage in option-premium points.
- Display participation fraction; minimum/maximum slice and maximum working
  slices per strike. Whole-contract residuals can finish a configured target.
- Reprice step/count, acknowledgment timeout, working-order timeout, total entry
  deadline, and liquidation escalation deadline.
- Fee reserve per contract; gross premium cash, planned stop-risk estimate, and
  delta-notional caps.

For every enabled strike, BUY assessment checks bid/ask and spread, displayed
sizes, model-estimated entry/exit capacities and slippage, trading status, exact
contract identity, LIVE feed quality, source times, local receipts, explicit
Greeks timestamps/delta, tick grid, and uncertainty bounds. Reuse of the existing
one-second freshness and **0.25-second synchronization** gates is mandatory.
The portfolio also requires common SPX evidence and synchronized strike inputs.
All market fixtures must declare `OFFLINE_FIXTURE`; raw TradingView/IBKR callbacks
are not admitted or converted into source proof by this scheduler.

Displayed size is haircut by the configured participation fraction, limited by
modeled capacity and slice limits, and reduced by existing working reservations
and consumption of that quote ID. Repeated use of an unchanged quote cannot
manufacture more liquidity. Displayed size and modeled capacities are **never
guaranteed fills**. The fake broker's independent book can fill zero, a fraction,
or all of a marketable slice. Thin OTM data may reject the entire entry.

The central ledger reserves quantity and worst-case limit-band cash plus fees
**before** returning an entire multi-strike batch. It checks the configured full
portfolio's affordability, premium cash, planned stop risk, and delta exposure;
it does not silently shrink an unaffordable five-strike target. At dispatch,
fresh buying power and market evidence are checked again. Later plans see all
working orders and partial fills. Reservations survive pending cancellations,
uncertain submissions, disconnects, and restarts. No per-strike wallet can spend
cash already reserved by another strike.

`liquidity_report()` exposes each strike's assessment and aggregate owned /
working quantity, cash, modeled stop risk, and delta-notional estimate. Missing
exposure data is unknown, not zero. `status().last_pretrade_validation` is durable
and includes the last accepted all-strike checks and projected exposure. A stop
risk estimate is **not** a maximum loss guarantee: gaps and nonfills can cause
larger losses; long premium exposure is tracked separately.

## Scheduling and price protection

`schedule()` atomically returns all currently eligible strike/slice intentions.
`dispatch_batch()` durably binds the whole modeled batch before handing its
identities to the fake broker. There are no sleeps or acknowledgment waits
between strikes. Partial acknowledgments/fills arrive independently. The
original global uncertainty rule is retained between batches: unknown orders
must be reconciled, never blindly resent.

Every modeled order is an **LMT**, never an unbounded market order. Arrival price
is pinned separately for each strike and entry/exit side on the first intention.
New slices and replacements inherit that anchor; they cannot reset the slippage
band and gradually chase a moving market. Tick rounding must remain inside the
configured absolute bound. A stale never-dispatched limit can be locally
abandoned and replaced within the same bounded route. Bound orders require
cancellation **and complete reconciliation** before replacement. Minimum tick
is explicit fixture metadata; real market-rule/tick-band validation is still a
paper-integration prerequisite.

If entry liquidity, freshness, or affordability fails, acquisition is durably
halted. Outstanding BUY intentions are cancelled/locally abandoned as applicable,
owned positions remain tracked, and normal risk monitoring can still trigger a
full exit. There is no automatic rearm or indefinite repricing.

## Exit priority and escalation

The model reuses the active premium and SPX rules: weighted-average BUY premium
minus 3.25; SPX +4/+1.25 early protection; SPX +5 arms unlimited three-point
favorable-peak trailing. Tracking begins with confirmed fills and persists through
partial acquisition and recovery. Automatic profit scale-outs are not introduced.
Any full-exit trigger blocks acquisition portfolio-wide.

Emergency requests first persist through the existing independent
`EmergencyLatch`, without waiting for the portfolio scheduling lock. Scheduling
and dispatch check that latch; emergency overrides an ordinary exit reason,
persists across restart, and never automatically rearms.

Cancellation actions for outstanding BUYs come first. A strike with reconciled
acquisition cancellation can liquidate its owned quantity while another strike
is still cancelling. Working SELLs reserve owned quantity, so competing premium,
SPX, and emergency triggers cannot create duplicate reduction orders. Late fills
invalidate prior reconciliation/flat evidence and are accounted for before
additional reductions. Missing/stale SPX or Greeks do not prevent an emergency
request or liquidation using independently valid exact option/size evidence;
missing option liquidity still prevents unsafe pricing.

Escalation is explicit and durable:

1. Missing acknowledgment: record an uncertainty risk alert and require
   cancellation/reconciliation; never infer non-submission or automatically retry.
2. Working timeout/rejection: request cancellation and retain reservations until
   positions, terminal order evidence, executions/corrections, and commissions
   agree. Rejected `Inactive` status alone is not terminal cancellation proof.
3. Replacement: at most the configured number of reprices for the same residual
   route, always inside the original strike's price band.
4. Exhausted reprices, unavailable liquidity, or nonmarketable price bound:
   retain owned exposure and a durable risk alert; no market-order fallback,
   price-band widening, or fresh route to evade retry limits.
5. Liquidation deadline: escalate to `UNFILLED_EXIT_DEADLINE_OPERATOR_REVIEW_REQUIRED`.
   Continue reconciliation with entry blocked. Operator attention is required;
   the deadline does not declare a fill, release a reservation, or report FLAT.

**Price protection cannot guarantee immediate liquidation in a fast or illiquid
market.** The model may remain EXITING with unfilled exposure and risk alerts.
It reports `FLAT_CONFIRMED` only after fresh complete broker-fixture evidence
confirms zero owned positions and every intention is terminal/provably local.
Empty open-order lists, receipts alone, or a local zero estimate cannot do that.
Unexpected short/excess executions are quarantined, not normalized away or
reported as safe positions. A complete broker-wide snapshot must agree with
independent execution history before any new intentions are bound.

## Metrics

`metrics()` is explicitly `OFFLINE_SIMULATED_RECEIPT_METRICS`. It records each
strike's modeled submission-attempt timestamps, first broker acknowledgment,
first/final fill receipts, active/corrected average fill price, fill percentage,
quantity-weighted slippage versus the pinned arrival quote, rejection history,
cancellation requests/acknowledgments/reconciled confirmations, and replacement
attempts. Portfolio entry time ends only when its full configured entry is
reconciled; liquidation time ends on reconciled flat evidence. Partial portfolios
and unknown submission outcomes do not receive invented completion times.
These are simulated local receipt UTC times, never exchange timestamps or real
IBKR fill-speed measurements. Callback and commission accounting is inherited;
corrections and duplicates do not double count active economic fills.

## Validation and remaining real-market requirements

Run `.venv/bin/python -B offline_test_runner.py`. Focused scripts are
`executor_portfolio_test.py` and `executor_portfolio_stress_test.py`.

Offline coverage includes 125 entry/exit, configurable slices, limited actual
liquidity despite large displayed size, widening quotes, stale/delayed/missing
or wrong data, cash/risk limits, concurrent scheduling, partial/late fills,
rapid repricing, canceled/uncertain orders, rejected exits, disconnect/restart,
competing triggers, persistent emergencies, and disappearing liquidation
liquidity. Assertions cover caps, reservations, unique identities, no modeled
shorts, bounded prices/retries, and no false FLAT.

Before any real paper orders: separately authorize and implement a paper-only
transport for this mode; prove real account/history completeness, contract/tick
rules, feed trading status, price/size/Greeks source timestamps, and the unchanged
0.25-second gate. Stage 5A ordinary IBKR callbacks currently cannot establish all
that source evidence. Validate actual marketable limits, order/ack latency,
commissions, cancellation/fill races, and slippage under real paper liquidity.

This new portfolio models **conditional local protective decisions**, not actual
broker-held STP/OCA protection. The existing single-strike protective manager is
unchanged and is not automatically attached to the portfolio. Safe per-strike
broker-held coverage, atomic amendments/OCA semantics, replacement gaps, and
broker-side oversell protection must be designed and validated before enabling
paper execution. External/manual broker orders are outside this model's control.
No real fill speed, guaranteed executable depth, or guaranteed stop loss is
claimed. Controlled live measurements would require separate explicit approval.
