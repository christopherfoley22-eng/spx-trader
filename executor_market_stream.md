# Stage 2: read-only SPX/SPXW market integration

This stage is offline validated. It adds no broker connection, EClient, socket,
order method, execution endpoint, or automatic launch path. Existing simulation
and Stage 1 option-premium stops are unchanged. Older diagnostic scripts that
default to port 7496 are not used by this integration.

`ReadOnlyIBKREvidenceAdapter` remains the authority for resolved SPX identity,
completed SPXW chain evidence, and exact contracts. Its new metadata helpers
choose the current **New York** expiration, propose the three nearest listed
strikes (configurable from one through seven), and reject ambiguous identities.
Proposed strikes still need exact contract resolution for that date/right.

`executor_market_stream.py` consumes SDK-shaped `marketDataType`, `tickPrice`,
and `tickOptionComputation` callbacks. SPX uses its index last-value tick;
options require both bid and ask, never last or mark as replacements. Model
computations (tick 13) supply delta, gamma, vega, theta, implied volatility,
option reference price, underlying reference price, and dividend value. Bid,
ask, and last computation channels are ignored, not mixed into model Greeks.
Delayed computations, invalid/sentinel values, and wrong-sign deltas are rejected.

Each callback bridge is bound to one session generation. Reconnect requires
ending the previous session and registering new request IDs; old quotes,
Greeks, and source proofs are discarded. Adapter resets invalidate the stream.
Unknown/old callbacks fail closed. Continuous callback diagnostics are bounded.

The subscription manifest describes streaming `reqMktData` requests with
market-data type 1, `snapshot=False`, and no special generic ticks. It is data,
not a transmitted request. A future approved paper-only transport must resolve
contracts and translate this manifest into SDK contracts; no transport is
enabled here. Greeks are included by default in option market-data requests:
[IBKR Options & Greeks](https://www.interactivebrokers.com/docs/tws-api/doc/md/option-greeks).

The existing `ReadOnlyMarketIngestion` is reused per exact option/SPX pair.
`rank()` requires source-proven SPX, atomic option bid/ask, and source-proven
Greeks, including live request classification, freshness, and the unchanged
0.25-second synchronization/uncertainty limits. It requires all nearest-strike
candidates to be qualified and quoted; incomplete discovery is blocked.
Candidates are ranked by `abs(abs(delta) - 0.48)`, then distance from ATM,
strike, and conId. Existing conservative account-funds validation and
`safe_sizing.py` cap affordable quantities at 25. Ranking never authorizes entry.

`LocalReadOnlyObservationService(adapter=adapter, market_stream=stream)` can
expose this state through the existing read-only status view. Its `ready` and
all `executable` flags remain false. No POST or trading control is enabled.

## Remaining blockers

Raw `tickPrice` and `tickOptionComputation` provide local receipt times, not
exchange event times. These callbacks therefore record `source_time=None` and
`exchange_time=None`. Receipt freshness is displayed as a **diagnostic** only;
it does not establish source freshness or synchronization. Raw updates clear
corresponding old proven evidence, preventing fallback to an earlier proof.

The existing normalized proof methods are exercised only with explicit synthetic
fixtures in the offline tests. The inbound IBKR bridge never calls them or
manufactures a source timestamp. Native quote/Greek callbacks cannot currently
satisfy the existing source gate, so real-feed ranking remains blocked even
with paid subscriptions. Tick-by-tick availability, timestamp precision, and
request-specific classification limitations remain documented in
`executor_market_ingestion.py`. Subscriptions alone cannot resolve these.

A separately validated timestamped source/normalizer and a reviewed paper-only
read-only socket transport remain necessary. This stage makes no claim that
real quotes, entitlement availability, or paper-account identity were verified.

## External setup for a future authorized paper-only read-only validation

- Subscribe to **CBOE Streaming Market Indexes** for SPX and **OPRA (US Options
  Exchanges)** for SPXW options, with API use covered for the relevant username.
  Both underlying and option subscriptions are required for Greeks. A snapshot
  subscription alone does not provide the required streaming feeds.
  [IBKR subscription coverage](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction).
- Enable the **Market Data API Acknowledgement** in Client Portal, Settings,
  Market Data Subscriptions. Confirm entitlement activation after relogin.
  [IBKR acknowledgement instructions](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/compliance-requirements-for-api-market-data/market-data-api-acknowledgement).
- Share the entitled live username's subscriptions with its associated paper
  username; only the paper username would be used for a future socket session.
  [IBKR market-data sharing](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-users/market-data-sharing).
- In the **paper** TWS session, enable **ActiveX and Socket Clients**, keep
  **Read-Only API checked**, and restrict connections to localhost. Verify the
  actual paper session/account manually; port defaults do not prove identity.
  Typical paper defaults are TWS 7497 and IB Gateway 4002. No connection is made
  or made available by this stage.
  [IBKR API setup](https://interactivebrokers.github.io/tws-api/initial_setup.html).

Run the new synthetic tests with `.venv/bin/python -B executor_market_stream_test.py`
and the entire offline suite with `.venv/bin/python -B offline_test_runner.py`.
