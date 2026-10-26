# Stage 5A: real IBKR paper-session read-only validation

Implementation and fake-callback validation only. **No real connection was
attempted in this stage.** Strategy rules, the 25-contract cap, source-proof gate,
0.25-second synchronization, execution boundaries, and existing journals remain
unchanged. This is an isolated diagnostic, never an execution permit.

## Architecture and guarantees

- `executor_ibkr_read_only.py` reuses `ReadOnlyIBKREvidenceAdapter`,
  `ReadOnlyMarketStream` / `IBKRMarketCallbacks`, exact SPXW contract validators,
  conservative sizing, `ApprovedPaperAccount`, and optionally an existing
  `ReadOnlySnapshotMachine`. It has no SDK or socket capability.
- `ibkr_paper_read_only_validation.py` inherits the existing `ReadOnlyProbe` with
  its order/account mutation methods hard disabled. Only account summary,
  position/open-order observation, contract/chain lookup, LIVE market-data type,
  and streaming market-data requests are allowed. Client ID must be positive;
  client 0 and automatic order binding are not used. Socket disconnect is the
  only cleanup operation: it does not cancel broker orders.
- Import and default CLI invocation are non-connecting. Real startup requires
  an explicit authorization flag plus current account/settings verification.
  Only the explicitly approved DU account and localhost paper endpoint
  (7497 TWS / 4002 Gateway) are accepted. A port or DU prefix alone is not paper
  verification: the operator must verify the specific logged-in paper session
  in the GUI first. The sole returned managed account must then match exactly.
  Unknown/live/multiple accounts, unexpected callbacks, errors, or disconnects
  stop validation. There is no retry/reconnect; start a newly verified session.
- API requests begin only after account identity and handshake agree. Contract
  lookup requires SPX IND / CBOE / USD and a current-day SPXW chain. A fresh LIVE
  SPX receipt may propose the three nearest *listed* strikes for exact CALL and
  PUT lookup. This lookup bootstrap has no proven source timestamp and is never
  an authorized ATM selection. Each returned expiry, right, strike, conId,
  currency, multiplier, class, and exchange is validated before subscribing.
- Real quote/Greeks callbacks enter only the existing **ordinary diagnostic**
  ingestion path. They never call any `proven_*` method. Delayed/frozen, invalid,
  stale, crossed, incomplete, and missing/wrong-sign Greeks remain blocked.
  Rank/sizing results retain the existing fail-closed source gate and 25 cap.
- Funds remain in memory and account identifiers are not printed. The report
  shows tags received, current freshness, and whether the original summary was
  fresh at completion. A one-shot summary can become stale during observation;
  it is never silently refreshed or accepted beyond the existing one-second
  freshness limit. Exact option identities, quotes, Greeks, callback receipt
  times, and broker error *codes* are included; raw broker error text is omitted.

## Timestamp and synchronization result

Ordinary `tickPrice` and `tickOptionComputation` have no per-quote or per-Greeks
source time. Reported `source_time` / `exchange_time` remain null. `tickString`
45/48/77/85/88 are recorded separately as trade timestamps, with local UTC and
monotonic receipt times and age diagnostics. They never stamp an option bid/ask,
SPX update, or Greeks event. Whole-second last-trade timestamps do not prove
subsecond synchronization or bind separate callbacks into a quote.

`measured_source_skew_seconds` remains null and the required source gate remains
0.25 seconds. Local receipt spread is labeled **diagnostic only**. Close local
receipt times cannot make `source_pair_valid`, a ranked selection, or execution
ready. IBKR documents that real-time tick-by-tick is unavailable for options and
indices other than CME indexes; requesting unsupported SPX/SPXW tick-by-tick is
not a workaround. Real source synchronization therefore remains **unproven**
with this ordinary TWS API path, even if entitlements and LIVE callbacks work.

Sources: [streaming callback fields / timestamps](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data),
[tick-by-tick instrument limitations](https://www.interactivebrokers.com/docs/tws-api/doc/md/live-ticks).

## Prerequisites BEFORE an explicitly authorized connection

1. Log in interactively to **IBKR PAPER** TWS or IB Gateway. Verify the exact
   approved paper account and paper session in the GUI. Do not provide passwords,
   tokens, or two-factor codes to this tool or store them in this repository.
2. In TWS Global Configuration > API > Settings, enable ActiveX/Socket clients,
   keep **Read-Only API checked**, restrict connections to localhost, and confirm
   the configured paper port matches approval. Do not disable Read-Only API or
   enable automatic order binding. The socket API cannot independently attest
   the Read-Only API checkbox: current operator GUI verification is required and
   the report explicitly says it is not independently API-verified. Neither
   broker rejection experiments nor test orders are used to probe this setting.
3. Obtain live SPX index entitlement (**Cboe Streaming Market Indexes**) and live
   SPXW US option quotes (**OPRA**, or a bundle explicitly including OPRA).
   Underlying and option data subscriptions are needed for live option Greeks.
   Complete applicable API market-data acknowledgements and share the entitled
   live username's data with the associated paper username where supported.
   Resolve competing sessions, subscription/line limits, and options permissions
   as diagnosed by IBKR; requested LIVE type alone is not entitlement proof.
   Do not purchase subscriptions automatically.
4. Run during an open SPX/SPXW session with today's expiration listed. Weekends,
   holidays, closed markets, and missing Greeks may prevent meaningful validation.
5. Create a private **0600**, non-symlink JSON config outside version control with
   the following exact fields (replace placeholders with verified values):

```json
{
  "account": "<approved DU paper account>",
  "verification_ref": "<paper-account GUI verification reference>",
  "approved_until": "<future timezone-aware ISO timestamp>",
  "host": "127.0.0.1",
  "port": 7497,
  "settings": {
    "reference": "<paper-login and API settings GUI verification reference>",
    "verified_at": "<timezone-aware ISO timestamp>",
    "expires_at": "<within 10 minutes of verified_at>",
    "paper_login_verified": true,
    "read_only_api_checked": true
  }
}
```

Config preflight only, **no connection**:

```sh
.venv/bin/python -B ibkr_paper_read_only_validation.py --config /private/path/paper-read-only.json
```

Only after the user separately authorizes the real connection:

```sh
.venv/bin/python -B ibkr_paper_read_only_validation.py --config /private/path/paper-read-only.json --authorize-read-only-connection --seconds 10
```

No real configuration was created or credentials retrieved during implementation.
The resulting report is a moment-in-time diagnostic captured before socket
cleanup. CLI success means the approved diagnostic session was observed; it does
**not** mean source synchronization, reconciliation, or execution is authorized.

Sources: [API setup](https://interactivebrokers.github.io/tws-api/initial_setup.html),
[market-data subscriptions](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction),
[paper data sharing](https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/?retakeFinal=1).

## Reconciliation and remaining blockers

An injected existing read-only snapshot machine can consume account, position,
and open-order callbacks against an existing local controller. It requires the
original freshness/coherence/completion checks; it never fabricates a local flat
baseline. The CLI does not assume or create a local position journal, and reports
reconciliation required. API-visible open-order scope is explicitly limited.

The Stage 3A paper broker ledger and reconciliation layer remain **offline
fixture only**. Real callbacks do not change their session-origin restriction,
write execution intentions, or enable any order/cancel API. Complete account-wide
execution/correction/commission history and paper ledger reconciliation are not
validated by this diagnostic. Existing stop replacement coverage gaps and broker
OCA/atomic amendment behavior remain separate prerequisites before execution.

The installed SDK inspected during implementation is `ibapi 9.81.1-1`, maximum
client protocol 157. The report includes SDK and negotiated server versions.
Compatibility with the actual running TWS/Gateway has not been empirically
validated. No SDK upgrade or dependency change was made.

Current blockers, pending actual authorization and observation:

- No approved real-session account/settings configuration or authorization yet.
- Installed SDK versus actual TWS/Gateway compatibility is not yet validated.
- Real connectivity, contract identities, funds, entitlements, spreads, and
  Greeks availability are not yet empirically verified.
- Read-Only API is operator-verified; no independent socket attestation exists.
- Per-quote/Greeks source timestamps and the 0.25-second source gate remain
  unproven; do not substitute receipt time or trade timestamps.
- Full real paper-ledger reconciliation and protective-order behavior remain
  unvalidated; all execution stays disabled.

Offline validation: `.venv/bin/python -B executor_ibkr_read_only_test.py`, then
`.venv/bin/python -B offline_test_runner.py`. Fake transport tests patch socket
creation and exercise the real callback facade without any IBKR connection.
