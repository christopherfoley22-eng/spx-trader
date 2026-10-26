# Executor local dry-run interface

Launch after reviewing the uncommitted changes:

```sh
.venv/bin/python executor_local_web.py --port 8765
```

Open `http://127.0.0.1:8765/` on the same computer. The server binds only to
loopback, requires a generated in-memory local session cookie and a CSRF token
for every POST, validates the browser origin, and does not enable remote phone
access. The `.executor-local/` state directory is ignored by Git and contains
the synthetic controller journal, replay cursor, and request-id registry.

The normal view exposes CALLS, PUTS, and an offline emergency EXIT ALL button.
An accepted intent begins a
synthetic replay using the selected canonical fixture. Its first observation
is processed immediately; the development panel can advance one observation
or run the remaining synthetic session. These controls supply market evidence
only. The existing dry-run controller decides entry, strategy, and full exit.
Mutating controls stay disabled while a request and the following status read
are pending. During an intent request, the Run remaining location becomes a
queue control. A tap queues one Run request visibly; it is sent only after a
fresh status read proves an active replay. If an independent HTTP client sends
Run remaining before an intent is accepted, the server returns a retryable
conflict instead of consuming it.
An exhausted fixture with an active simulated position reports recovery required;
it never implies confirmed flat.

EXIT ALL bypasses the normal browser action queue and persists an emergency
latch independently of the replay lock. It blocks acquisition immediately and
retains that block after restart. The simulator can complete an exit only from
supplied observations and reconciled fill evidence; exhausted fixtures remain
pending. There is no automatic rearm. The separately injected protective-model
service uses the same UI for fake-broker stop acknowledgements and cancellation
barriers. See [Stage 4A protection and emergency exits](executor_protective_exits.md).

The fixture date and account label are synthetic. This interface never
connects to IBKR, cannot submit or preview broker orders, and cannot be used
as a live trading interface. A session ending with a simulated position still
open displays RECOVERY REQUIRED and blocks new intents.

The active dry-run controller uses an initial **option-premium** stop at the
quantity-weighted average of committed simulated BUY fills minus 3.25. It
monitors the exact contract's fresh LIVE bid, including during partial entry;
SPX movement alone cannot trigger this stop. Invalid/nonpositive stop prices
block authorization or the proposed simulated fill. Explicit simulated fill
prices may improve on, but cannot exceed, the authorized ask; existing quote,
account, contract, and affordability checks remain in force. A missing fill
price retains the simulator's ask-fill convention.

The +4 SPX profit arm, +1.25 SPX floor, +5 SPX trailing arm, and three-SPX-point
reversal remain unchanged. The premium stop remains a protective floor after
these arms. These are offline decisions, not broker-held protection or a
guarantee of the simulated exit price. Stale/missing option quotes cannot
evaluate the premium stop; SPX-only profit exits remain available.

Active journals created before premium-stop version 1 require recovery and are
not silently migrated. Keep their files intact. Historical standalone engines
and research baselines retain their old rules; the local UI and both Executor
replay runners use `executor_dry_run.py` and the premium stop.

For a fresh demo, stop the server and choose a new empty state directory with
`--state-dir PATH`. Keep the state directory private. Its contents are durable
replay evidence and should not be deleted during an active lifecycle.

## Final active exit rules

The active controller uses weighted-average option entry premium minus 3.25 as
the hard stop, including partial entry and after SPX profit protection arms.
SPX +4 arms early protection without selling; reversal to +1.25 before +5
exits all remaining quantity. SPX +5 permanently supersedes that floor with a
three-point reversal from the best favorable SPX level, without an upside cap.
CALL peaks rise and PUT peaks fall. No automatic profit targets or scale-outs
are active. Liquidation chunks do not change the full-exit decision.

The position panel displays the computed premium stop, evidenced early and
trailing activation, favorable SPX peak, and current trailing SPX threshold.
Those strategy indicators do not certify a broker-held stop; the injected
protective model reports acknowledgement/coverage separately. Emergency status
and its persistent entry block retain precedence. Missing fresh data never
creates an executable entry or synthetic emergency fill.
