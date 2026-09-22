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

The normal view exposes only CALLS and PUTS. An accepted intent begins a
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

The fixture date and account label are synthetic. This interface never
connects to IBKR, cannot submit or preview broker orders, and cannot be used
as a live trading interface. A session ending with a simulated position still
open displays RECOVERY REQUIRED and blocks new intents.

For a fresh demo, stop the server and choose a new empty state directory with
`--state-dir PATH`. Keep the state directory private. Its contents are durable
replay evidence and should not be deleted during an active lifecycle.
