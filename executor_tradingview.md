# TradingView SPX research integration — offline first

This integration never feeds market-data readiness, an entry authorization, a
broker ledger, or execution. It does not connect to IBKR. Existing strategy,
emergency, account isolation, reconciliation, 25-contract cap, and 0.25-second
IBKR source synchronization gates are unchanged. No public ingress or real
TradingView alert has been deployed or exercised during implementation.

## What works offline

- `executor_tradingview.py`: authenticated research inbox, exact `SP:SPX` symbol
  and configured stream identity, strict JSON schema, positive finite price,
  timezone-aware alert time, explicit event identity, durable duplicate handling.
- `executor_tradingview_web.py`: isolated loopback HTTP receiver on a separate
  listener. `POST /webhooks/tradingview` and authenticated `GET /status` only.
  No trading, emergency, or Executor UI endpoints. The existing UI's cookie and
  CSRF boundaries are not modified. Imports never start a listener.
- `executor_tradingview_replay.py`: uses **the active**
  `executor_dry_run.spx_profit_decision` for research observations. CALL/PUT
  symmetry, +4 early activation, +1.25 reversal before +5, +5 activation,
  favorable peaks, monotonically tightening three-point trailing thresholds,
  and full-exit signals are recorded. No upside cap or profit scale-outs.
- The existing historical TradingView CSV importer in
  `mortificatio_pre4_experiment.py` is reused for parsing only. Its historical
  SPX-based stop experiments/results are unchanged and are not used to evaluate
  this integration's active exits.

### Timestamp provenance and limits

`event_time` is TradingView **alert fire time** (`{{timenow}}`), not the exchange
or quote source timestamp. Optional `bar_time` is the TradingView candle opening
time (`{{time}}`). The inbox preserves original and normalized payloads,
separate local UTC / monotonic receipt times, and a process identity so monotonic
clocks across restarts are never compared as one clock. Exchange time is always
null. TradingView documents that `{{timenow}}` is rounded to the nearest second;
it cannot prove the existing 0.25-second IBKR synchronization requirement.

Alerts are sampled observations, not continuous tick coverage. An unobserved
peak or reversal may change actual strategy behavior. SPX-only replay does not
evaluate the **weighted-average option-premium minus 3.25** hard stop, option
fills, commissions, slippage, or P&L. Every result is research-only, non-ready,
non-executable, and labels the full trade outcome unknown without option-premium
and fill evidence. There is no synthesized SPX-based loss stop or option price.

Sources: [TradingView placeholders](https://www.tradingview.com/support/solutions/43000531021-how-to-use-a-variable-value-in-alert/),
[TradingView time semantics](https://www.tradingview.com/pine-script-docs/concepts/time/).

### Journal and admission

Use a dedicated journal path, never an existing Executor/broker database.
SQLite WAL/FULL commits complete before successful acknowledgement. A receipt
hash chain and stored head detect partial edits/truncation; this is **not** an
external tamper-proof archive and cannot detect rollback of the complete DB to a
previous self-consistent copy. The journal is mode 0600 and refuses unrelated
schemas or changed stream/policy bindings.

Every valid authenticated receipt, duplicate, conflict, and quarantine has its
own durable receipt sequence. Original valid JSON payloads are retained. Invalid
authenticated payloads retain their body hash and rejection reason, not arbitrary
raw credential-bearing text. Authentication headers/tokens are never persisted
or logged. Unauthenticated requests cannot write to the journal.

Research admission defaults to at most 60 seconds of alert age and one second
of future clock tolerance. These are **research inbox bounds**, not trading or
IBKR freshness thresholds. Stale/future events are quarantined. Distinct IDs with
equal timestamps are unordered; older event times are late. Exact duplicate IDs
with unchanged normalized content are idempotent even on a later stale retry.
Changed duplicates dispute that identity. Any invalid/quarantined/disputed
stream blocks ordered replay until an operator reviews the data; nothing is
silently filtered into a supposedly complete sequence. Raw records remain
available through `audit()` for review. There is no automatic history rewrite.

## Alert payload and authentication

Example for a **once-per-one-minute-bar-close** SPX alert:

```json
{
  "version": 1,
  "source": "TRADINGVIEW",
  "stream_id": "spx-1m-close-v1",
  "event_id": "spx-1m-close-v1:{{time}}",
  "symbol": "{{exchange}}:{{ticker}}",
  "event_time": "{{timenow}}",
  "event_time_kind": "ALERT_FIRE",
  "bar_time": "{{time}}",
  "price": "{{close}}"
}
```

Only `SP:SPX` is admitted; SPY, futures, calculated TVC symbols, delayed-suffixed
symbols, and other aliases are not silently treated as the same source. Verify
the chart/feed is actually `SP:SPX`. The sender's symbol string still does not
prove real-time entitlements or quote provenance.

The example identity is unique only for the specified once-per-bar stream.
For intrabar alerts, generate a stable per-event unique identity in the sending
script, including a stream/script-instance identifier and sequence; do not use
second-resolution `{{timenow}}` alone. Distinct events sharing a second-resolution
time will be quarantined because their true ordering is not established. Bar
close alerts are research samples and will miss intrabar strategy events.

The receiver requires `Authorization: Bearer <private relay token>` with a
random 32–128-character URL-safe token configured locally through
`MORTIFICATIO_TV_RELAY_TOKEN`. It accepts no body/query-string password or broker
credential. A **separately configured and validated HTTPS ingress** must verify
TradingView's presented client certificate (trusted chain plus expected identity,
not a self-declared forwarding header), then inject the private relay token and
forward to loopback. Strip any inbound Authorization/identity forwarding headers
at that edge. Keep the relay token out of TradingView alert messages, URLs, source
control, public logs, and the Executor UI. A local test token authenticates a
local test sender; it does not establish real TradingView origin.

This repository does not configure TLS certificates, expose a public port, or
claim that the public-edge verification has been completed. The production
certificate trust/rotation and relay deployment remain setup blockers. Existing
browser CSRF restrictions are not bypassed to receive external webhooks.

TradingView documents HTTPS client-certificate authentication, ports 80/443,
a three-second request limit, possible delivery failures, and two-factor
authentication for webhook alerts. Use HTTPS 443 at the validated external edge.
The local listener is HTTP **loopback only**; never expose it directly. Requests
are body-bounded, socket-read-bounded, and durably acknowledged without running
strategy replay in the HTTP request. SQLite lock timeout is 0.25 seconds; a
persistence error yields 503 rather than a successful acknowledgement. That
storage timeout has no relationship to market synchronization. Public edge rate
limits, storage growth, and end-to-end acknowledgement latency under load still
require validation; the journal currently verifies its chain on each request.

Sources: [webhook configuration and delivery limits](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/),
[webhook authentication](https://www.tradingview.com/support/solutions/43000680459-webhook-authentication/).

After configuring a private relay token locally, a future local listener can be
started with:

```sh
.venv/bin/python -B executor_tradingview_web.py --journal /private/path/tradingview.sqlite
```

No listener was started during implementation; HTTP tests use in-memory sockets.

## Candle and alert replay

For one-minute exported **standard OHLC** SPX candles (not Heikin-Ashi, Renko, or
other transformed charts), provide an explicit historical entry time, direction,
and SPX entry price. The CSV alone does not prove its symbol/provider; verify its
origin yourself. The result explicitly marks CSV symbol provenance unproven.
Timestamp units are selected, never guessed:

```sh
.venv/bin/python -B executor_tradingview_replay.py --csv /path/SPX.csv --direction CALL --entry-spx 7355.25 --entry-time 2026-10-23T14:30:00+00:00 --timestamp-unit unix_seconds
```

Candle processing does not assume a favorable high/low sequence. If the range
allows a protection trigger, it reports `INTRABAR_AMBIGUOUS`, stops the primary
trajectory, and supplies high-first/low-first paths as **non-exhaustive examples**.
Interior oscillations may produce additional outcomes even when those two paths
agree. Guaranteed crossings by the close are identified where provable, while
intrabar event times and execution prices stay unknown. Harmless bars can advance
protection/peaks with interval-only timing. A reversal observed at the next bar
open can record an SPX exit signal, without inferring an execution fill. Mid-bar
entry and missing time windows produce unknown outcomes rather than invented
pre/post-entry peaks. Invalid OHLC and duplicate/backward bars are rejected.

Ordered inbox data can be replayed programmatically with:

```python
from executor_tradingview_replay import replay_alert_events
result = replay_alert_events(inbox.replay_events(), "CALL", "7355.25", entry_time)
```

## Closed-market testing versus live validation

**Markets closed:** run both new offline test scripts or the complete
`offline_test_runner.py`; exercise authenticated requests, duplicates/conflicts,
restart/crash persistence, concurrency, bad payloads, clock separation,
CALL/PUT boundaries, monotonic trailing, CSV import, and intrabar ambiguity.
Historical CSV research works while closed. Synthetic callback tests are not
empirical trading evidence.

**Needs real delivery / live markets:** deploy and independently validate the
HTTPS/client-certificate edge, confirm alert plan/2FA and source entitlements,
verify symbol and chart type, event IDs and alert cadence, observe delivery
latency/reordering/loss and sender-clock behavior, and compare TradingView SPX
observations with independently validated IBKR data. Demonstrating availability
does not establish continuous tick coverage or exchange timestamps. Exact SPXW
option quotes, Greeks, source synchronization, paper-account verification,
reconciliation, and broker-held protection remain IBKR-side requirements.
No TradingView event alone can satisfy or override any of those requirements.
