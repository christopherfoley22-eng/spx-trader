# Phase 10 broker and venue feasibility — research, not validation

Reviewed public official documentation on 2026-10-25. No broker connection,
credential use, account query or order operation was performed. “Documented”
does not mean implemented, entitled, enabled or validated for this account,
SDK, SPXW contract, trading session or routing combination.

## Feasibility matrix

| Capability | Documentation | Mortificatio status | Recommendation |
|---|---|---|---|
| Five-strike same-direction package | Cboe supports eligible complex orders; IBKR describes BAG/combo legs and ratios. | **Unverified** for five independent long SPXW options, especially unequal final-strike quantities. No combo dispatch exists. | Do not activate; obtain exact contract/routing/class eligibility and partial-fill tests. |
| Ordinary bounded single-leg LMT | Documented broker order type. | Offline ledger and economic simulator implemented; actual paper behavior unverified. | Preferred validation baseline. |
| Parent/child/bracket transmission | Attachment and staged transmit semantics documented. | **Partial-fill protection not established**; no actual attached-order implementation. | Validate every partial-fill, rejection and disconnect transition before acquisition enablement. |
| OCA with block | IBKR documents overfill-protected behavior and routing serialization. | Supplemental stop reservations modeled; actual OCA/cancel semantics unverified. | Never assume simultaneous stops/tactical exits are atomic. |
| Native STP | Broker order type documented, subject to exchange/product/trigger rules. | Synthetic acknowledged-stop lifecycle only; **not real broker-held protection**. | Verify trigger method, availability, persistence, partial executions and modification safety. |
| Adaptive priority algorithm | IBKR describes Urgent/Normal/Patient price-search priorities. | Not integrated or enabled; support for exact SPXW/routing/account unverified. | Compare against simple limits; not a promise of faster fills. |
| Options Minimize Impact | Broker catalog describes participation/slicing over time. | Not integrated; urgency fit unverified. | Research-only for entries; unsuitable to assume for mandatory immediate exits. |
| Prepared order templates | Static contract/side/type/price bounds can be held locally. | Immutable entry/exit hints implemented; **no pre-submission or preallocated broker IDs**. | Bind current auth, funds, source evidence, IDs and durable reservations at admission/dispatch. |
| IBKR quote/depth | Snapshot-style watchlist streams and separate depth API documented. | Existing read-only abstractions retained; no actual entitlements/source-time solution verified. | Preserve fail-closed 0.25-second source gate. |
| Direct/vendor index and options feeds | Cboe index and options book/trade products are documented. | No license, data or decoder supplied; no feed blending implemented. | Obtain licensed evidence and clock/identity mapping first. |
| Persistent workers | Offline event-driven workers exist. | Tested locally; not OS-supervised production workers. | Validate watchdog, queue admission, shutdown and restore. |
| SQLite/process optimization | Phase 9 structural rollback is preserved. | Real durability/snapshot work still dominates; no new batching/distribution introduced. | Controlled profiling and crash proofs before architecture expansion. |
| Rust/C++ helper | Technically possible, not broker capability. | No measured benefit demonstrated for remaining storage/reconciliation costs. | Not recommended now. |

## Complex orders are not five independent guaranteed long fills

Cboe's current complex-order page describes eligible orders up to 16 legs,
specified ratio bounds and a 100 ms COA auction. Being within a numerical leg
limit is not sufficient evidence that this all-long five-strike package qualifies.
Partial ATM-first allocations also create unequal ratios; each intended quantity
must remain within its funded per-strike cap. Package execution cannot be assumed
to preserve arbitrary independently sized fills. [Cboe complex handling](https://www.cboe.com/markets/us/options/trading/complex-orders)

The technical complex-book process lists five-or-more-option-leg orders among
simple-book legging restrictions and describes SPX-family exceptions for some
non-conforming strategies. That makes an assumed “sweep five independent books
atomically” particularly unsafe. [Cboe complex book process](https://www.cboe.com/document/tech-spec/document/technical-specifications/cboe-titanium-u.s.-options-complex-book-process)

IBKR's complex-order lesson describes combo construction and venue-dependent
limitations. It does not establish acceptance, fill ratios or urgency for our
exact all-long package. Broker confirmation and explicit paper tests would be
needed; no combination support is invented here. [IBKR complex-order lesson](https://www.interactivebrokers.com/campus/trading-lessons/python-complex-orders/)

## Protection during partial acquisition

The current IBKR attachment documentation describes attached profit/stop orders;
the official legacy submission guide says children are held until the parent is
completely filled. Neither is evidence that every partial acquisition already
has active protection. Bracket transmit sequencing addresses accidental early
transmission, not all rejection, partial-fill and disconnection gaps.
[Current attachment documentation](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/adding-a-profit-taker-and-stop-loss),
[official legacy submission guide](https://interactivebrokers.github.io/tws-api/order_submission.html),
[official bracket example](https://interactivebrokers.github.io/tws-api/bracket_order.html)

IBKR's OCA documentation distinguishes cancellation/reduction variants and
overfill protection “with block”; that can serialize routing. It must not be
equated with five concurrent independent exits or instantaneous cancel/replace.
[IBKR OCA](https://interactivebrokers.github.io/tws-api/oca.html)

PendingCancel is not cancellation confirmation and executions can still arrive
while cancellation is pending. Actual executions and complete reconciliation are
needed before releasing close quantities or declaring flat.
[IBKR order status](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/understanding-order-status-message)

Mortificatio therefore models UNVERIFIED, synthetic acknowledged coverage,
recovery, cancellation-pending and terminal reconciliation separately. A partial
fill fences further acquisition in the same durable economic event when the
Phase 10 policy is configured. A boolean synthetic capability assertion is not
proof of an actual exchange-held stop. No stop transmission, bracket guarantee,
automatic risk rearm, or OCA-native safety claim was added. Unexpected/contradictory
stop evidence stays durably uncertain; full broker stop-history reconciliation
is a remaining adapter requirement rather than an invented recovery success.

## Native algorithms and price improvement

Adaptive exposes urgency choices and price-search behavior; “Urgent” is not an
immediate-fill guarantee. Product/exchange/account/API acceptance and observed
cost must be tested, particularly for combos. [IBKR Adaptive](https://interactivebrokers.github.io/tws-api/ibalgos.html)

The broker order catalog includes options-oriented Minimize Impact and many
conditional order types. Availability in a catalog does not validate SPXW use
or suitability for a triggered protective exit. Passive price improvement is
also incompatible with the current dispatch requirement that a new entry limit
be marketable; that gate was not weakened. The twin's “improvement offered” case
offers the same explicitly synthetic better counterparty price to every tactic,
not a special advantage assigned only to an advanced algorithm.
[IBKR order catalog](https://www.interactivebrokers.com/en/trading/ordertypes.php)

## Data provenance, entitlements and licensed replay

IBKR documents aggregate watchlist snapshots rather than tick-by-tick updates:
US indices at 250 ms and US options at 100 ms in the current table. A recent API
receipt is not a newly timestamped exchange BBO. Last-trade time cannot silently
serve as bid/ask/Greek time. [IBKR streaming data](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data)

Depth is a separate facility, not guaranteed to show every price, and book reset
events must invalidate accumulated depth. Contract/venue availability must be
queried in a separately authorized session; combo depth is not provided as an
ordinary single-leg book. [IBKR depth](https://www.interactivebrokers.com/docs/tws-api/doc/md/market-depth)

IBKR lists CBOE Streaming Market Indexes for SPX index L1 and OPRA-containing
options streaming packages. Exact account entitlement, professional classification,
API/paper sharing and separate depth coverage must be verified in the subscription
manager. No subscription was purchased or account setting changed.
[IBKR subscription guide](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction)

Cboe's index feed distributes SPX values; options PITCH and historical quote/trade
products are separate evidence sources. Vendor-specific SPXW venue coverage,
timestamp semantics, redistribution rights and complete licensing must be checked.
No price, queue position, full depth or license is presumed from a marketing label.
[Cboe index feed](https://www.cboe.com/data/global-indices-feed),
[Cboe PITCH specification](https://www.cboe.com/document/tech-spec/document/technical-specifications/cboe-titanium-u.s.-equitiesoptions-multicast-pitch-specification),
[Cboe historical options products](https://datashop.cboe.com/options)

### Licensed historical import specification (not an implemented vendor decoder)

- Manifest: schema version; license/owner attestation; permitted research use;
  provider/product/venue; checksum of original files; capture dates; timezone;
  source clock domain/resolution/uncertainty; timestamp meaning; corporate/index
  license restrictions; verified exchange/session calendar; decoder version.
- Records: immutable event ID, source generation and per-instrument sequence;
  exact symbol/secType/tradingClass/expiry/right/strike/multiplier/currency and
  vendor-to-qualified-contract mapping; quote/trade/depth/reset/halt/session event
  kind; authoritative event time if available; separately captured receipt wall
  and monotonic observation time if available. Missing times remain missing.
- Quotes: independent bid/ask and sizes; condition/tradability; delayed/frozen
  status; Greek model/source and timestamps when actually supplied. Trades are
  not executable quotes. Candle intervals are insufficient to infer intrabar
  queue ordering or subsecond stop execution.
- Depth: side, level/order identity as actually supplied, operation, sequence,
  venue and reset/gap flags. Never invent levels, order priority or guaranteed
  counterparties from L1. Apply resets before subsequent deltas.
- Replay: preserve recorded arrival/order semantics without look-ahead or
  interpolation. Explicitly identify gaps, missing fields and unknown queue
  position. Keep historical, synthetic and actual broker data separate.
- Feed mixing requires a reviewed clock-domain mapping, measured uncertainty and
  the unchanged source synchronization gate. No mapping means research-only /
  blocked acquisition; receipt time is not a substitute. Keep price improvement,
  realization fractions and queue assumptions independently parameterized.

Licensed raw data, a vendor decoder and empirical calibration were not supplied.
The implemented twin remains synthetic and does not reproduce exchange execution.
