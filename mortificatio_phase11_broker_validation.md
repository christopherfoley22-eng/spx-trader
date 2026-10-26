# Phase 11 broker reality: no connection, no observed broker capability

Reviewed 2026-10-25 using public official documentation. **Observed: none.**
Every callback/order/market test in this phase is synthetic. No broker credentials,
connection, order submission/modification/cancellation or permission change occurred.
Documented behavior is not account/product approval or observation.

| Capability | Documented | Implemented/simulated | Still unverified |
|---|---|---|---|
| Parent-child and staged bracket transmit | Attachments and Transmit sequencing exist. Legacy TWS guide describes activation after complete parent fill. | Existing obligations and synthetic stop states. No native transmission. | Partial activation, rejection, actual stop coverage during fills/disconnect. |
| Native stops | Order types/trigger fields exist. Venue/product conditions apply. | Weighted actual premium minus 3.25; durable recovery and reservation model. | Exact SPXW trigger, held/simulated location, persistence, replacement coverage. |
| OCA | Types include block/overfill protection; partial execution behavior varies by type. | Stops and tactical reductions share owned-quantity reservations. | Routing serialization, actual cancel/fill race behavior, safe replacement. |
| Orders/reconciliation | Client/order/perm identities and open-order observations exist. | Existing full position/order/execution/commission comparison, unknown acknowledgments remain reserved. New rich read-only stop observations. | Full native stop execution/correction/history normalization; reconnect completeness. |
| SPX/options BBO | Snapshot-style streaming; live entitlement required. | Existing exact qualification/stream validation plus size callbacks. | Actual SPXW availability, quote/size/Greek authoritative timestamp domains. |
| Depth | Separate request, venue/contract entitlement-dependent; incomplete coverage possible. | Explicitly authorized depth request method and bounded callback event capture; no invented full book. | Actual qualified contract support, entitlement, resets and source clocks. |
| Protection feasibility | Account/product-dependent. | Hard admission requires explicit synthetic feasibility reference. | A real feasibility provider; no synthetic reference may authorize real trading. |

The official legacy TWS submission guide says attached children activate after
complete parent fill; it is deprecated and must be checked against the installed
TWS/API. Bracket transmit sequencing alone does not prove partial coverage.
[IBKR submission](https://interactivebrokers.github.io/tws-api/order_submission.html),
[bracket example](https://interactivebrokers.github.io/tws-api/bracket_order.html).
Do not import behavior from a different API such as FIX into this TWS design.

OCA types have differing cancellation/reduction semantics and with-block routing
protection. These do not establish instantaneous atomic replacement or guarantee
all five strikes close concurrently.
[IBKR OCA](https://interactivebrokers.github.io/tws-api/oca.html),
[current Order fields](https://www.interactivebrokers.com/docs/tws-api/ref/order).
An undersized, rejected, pending-cancel or contradictory stop cannot become proof
of full protection. Cancel requests remain reserved until reconciled terminal proof.

Open orders are not execution/commission history. Client 0 binding behavior is
excluded: use a dedicated nonzero client and observation-only reqAllOpenOrders.
No auto-binding or order mutation is permitted in the harness.
[IBKR open orders](https://interactivebrokers.github.io/tws-api/open_orders.html).

IBKR watchlist streams aggregate snapshots (US indices 250 ms, US options 100 ms
in the current table). Callback recency does not timestamp a newly changed BBO.
A trade timestamp cannot timestamp bid/ask or Greeks. Missing source timestamp
has an unbounded upper age; the unchanged 0.25-second synchronization gate cannot
be proved from receipt alone.
[IBKR streaming](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data).

Depth is not guaranteed complete and is not supported for combos/calendar spreads.
A depth reset requires clearing/rebuilding; this diagnostic fails the session on
reset/unexpected error rather than pretending continuity. It retains bounded
incomplete event observations, not a production reconstructed depth book.
[IBKR depth](https://www.interactivebrokers.com/docs/tws-api/doc/md/market-depth).

Live Greeks require the option and underlying subscriptions; model Greeks are
not execution prices or timestamp proof.
[IBKR option computations](https://interactivebrokers.github.io/tws-api/option_computations.html).
Confirm SPX index entitlement (CBOE Streaming Market Indexes), OPRA-containing
options entitlement, professional classification, paper sharing and any separate
venue depth entitlement in the actual subscription manager. Do not assume L1
includes L2, or that a tick-by-tick feed supports each requested SPXW contract.
[IBKR subscription guide](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction).

## Read-only harness and blocked validation steps

`executor_broker_reality.py` extends the existing paper validation reducer;
`ibkr_phase11_read_only.py` extends the existing hard-disabled transport. Imports
open no socket. `run_read_only` defaults unauthorized, reuses the one-shot
allowlisted DU account/localhost paper-port/GUI read-only attestation, and ends
by disconnecting. Mutation methods remain disabled. Settings attestation is
operator evidence, not an API-readable proof of TWS read-only configuration.
No new command line, secret file, browser endpoint or execution permit was added.

Steps below are **BLOCKED pending separate explicit connection authorization**:

1. Manually authenticate the approved paper TWS/Gateway session, complete IBKR
   Mobile authentication as required, verify exact DU identity and paper port,
   enable API Read-Only, restrict trusted local access, record fresh settings
   verification. A connected socket is insufficient. Do not store 2FA secrets.
2. Instantiate fresh ApprovedPaperAccount/SettingsVerification and
   BrokerRealityValidation; call run_read_only with authorized=True only after
   separate authorization. Use a dedicated nonzero client; do not bind orders.
3. Qualify SPX and exact same-day SPXW C/P contracts via contract details. Capture
   full identities/expiry/multiplier and chain. Existing bootstrap samples near-ATM
   contracts, not a claimed complete ten-contract funded ladder. Extend only with
   explicitly qualified contract requests; never infer missing listed contracts.
4. Inspect BBO, bid/ask sizes, Greeks, marketDataType and errors during a live
   market. Retain each source timestamp only if genuinely supplied. Record receipt
   wall/monotonic times separately. Evaluate clock uncertainty and the original
   0.25-second gate; missing authoritative times remain a blocker.
5. During the active authorized diagnostic, call RealityProbe.inspect_depth with
   a registered exact option request/qualified SDK contract and separately
   explicit authorized=True. It checks identity, requests depth capabilities and
   at most five rows; it never sends an order. Capture entitlement errors and
   incomplete/reset observations. No depth request was made in Phase 11.
6. Compare read-only position and rich open-order observations with local journals.
   Record client/order/perm/ref, parent/type/qty/stop/status. An acknowledged STP
   alone is not adequate protection proof. Full execution/fee/correction history
   adapter and broker-native protective coverage remain separate blockers.
7. Disconnect/reconnect using new approval/validation objects; compare retained
   broker orders and positions. Never infer canceled/flat from a disconnected or
   empty callback. Broker-native stop activation and cancel/replace cannot be
   established by this order-free test: require later separately authorized
   broker order testing and approved operational procedure.

Reports are private diagnostic evidence, not browser data or order authority.
A fake callback test cannot be relabeled observed. Zero actual broker observations
were obtained. Paper and live remain **NO-GO**.

## Additional native-price and calendar blocker

Cboe specifies different single-leg SPX/SPXW price increments by premium level,
and expiration/half-day trading cutoffs differ from ordinary non-expiring hours.
Exact contract market rules and verified session calendars must be captured;
fixture `.05` ticks and irregular strike ladders are synthetic test inputs, not
universal SPXW specifications.
[Cboe SPX specifications](https://www.cboe.com/tradable-products/sp-500/spx-options/spx-specifications),
[Cboe SPX fact sheet](https://res-certification.cboe.com/resources/spx/spx-fact-sheet.pdf).

Weighted entry minus 3.25 can fall between quoting increments (for example,
10.00 minus 3.25 is 6.75); weighted partial fills can produce finer precision.
Whether the exact broker stop trigger/auxPrice is admissible must be verified
separately from quoting increments. No rounding or strategy-threshold change was
implemented. Native trigger/grid compatibility and any proposed representation
policy require explicit review rather than assuming every synthetic stop is legal.
