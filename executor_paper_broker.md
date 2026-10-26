# Stage 3A: offline paper broker infrastructure

Stage 3A is an isolated broker ledger and reconciliation layer. It does not
connect to IBKR, instantiate EClient, submit or cancel orders, expose an
execution permit, or replace the existing Executor. All submission/executable
flags remain false. No existing simulation, premium-stop, market-ingestion,
or 0.25-second synchronization gate is bypassed or changed.

Files:

- `executor_paper_types.py`: normalized immutable approval, session, order,
  execution, commission, position, and reconciliation evidence.
- `executor_paper_broker.py`: single-writer SQLite WAL/FULL journal, account
  binding, session generations, intentions, identities, and economic history.
- `executor_paper_reconciliation.py`: complete-cycle comparison, reusing the
  existing broker snapshot coherence and account/quantity/freshness validators.
- `executor_paper_broker_test.py`: independent fake broker and adversarial tests.

## Paper identity boundary

An explicit `ApprovedPaperAccount` pins the full paper account ID, approval
reference, expiry, and localhost paper endpoint. A DU prefix or port alone is
insufficient. Every session additionally requires that exact account as the
only managed account, a matching verification reference/endpoint, PAPER mode,
read-only confirmation, a fresh timestamp, an unexpired approval/session, and
a never-reused nonce. Generation numbers and nonce history survive restarts.

**Only OFFLINE_FIXTURE session evidence is accepted in Stage 3A.** This tests
the verification boundary without pretending that a TWS server's environment
has been independently authenticated. No real IBKR paper account was verified.
A future trusted session verifier and paper-only read-only transport are still
required. Production/live modes, unknown origins, mixed accounts, unknown
accounts, and unverified sessions are rejected. No broker connection method
exists here, including for paper accounts.

## Durable intentions and callbacks

An intention with an exact validated current-day SPXW contract, BUY/SELL side,
whole quantity, and unique intent/orderRef is committed before an order ID may
be reserved. Reservation also requires fresh reconciliation. Order IDs are
client-scoped, permIds are unique, and an intention cannot be rebound to a new
order ID. A reserved identity is **unconfirmed/uncertain**, not submitted.
There is deliberately no operation that transmits it.

Order observations carry orderId, clientId, permId, orderRef, account, contract,
quantities, and status. Initial permId=0 is retained as unconfirmed evidence;
it cannot satisfy reconciliation until a positive permId is observed. Duplicate
orderStatus callbacks do not create executions. Actual quantities are derived
only from unique execution IDs. Repeated IDs are idempotent; changed callback
or execution identities are quarantined and block planning.

Execution corrections require an explicit link to the active execution being
replaced and a newer IBKR-style final numeric suffix within the same execution
family. Identity changes, unexplained same-family reports, missing/out-of-order
correction history, and unverified busts fail closed. The previous record stays
in the journal but no longer contributes to quantity or active commissions.
IBKR documents correction IDs in its
[execution reference](https://www.interactivebrokers.com/docs/tws-api/ref/execution).

Commission reports can precede executions. They are indexed by execution ID;
orphaned or missing active-execution commissions prevent complete accounting.
Explicit corrections replace the prior report rather than adding a second
fee; finite USD rebates are retained. Raw fees and execution prices persist
in the private ledger, never an application log.

## Reconciliation and exit limits

Reconciliation requires a new generation-bound cycle for the approved account,
completed positions/open orders/completed orders/executions/commissions, fresh
coherent receipt times, and declared history covering **all persisted intents**.
API-visible-only order scope or truncated execution history is insufficient.
Unknown orders/positions are not adopted. Every reported execution must already
have been replayed into the ledger; reported positions and order fill counts
must agree with active executions.

An empty open-order snapshot does not prove an uncertain order was never sent.
A known bound order must be found as working or explicitly terminal with matching
execution history and positions. Cancel requests/PendingCancel/Inactive do not
release reservations. Cancellation releases remaining quantity only after a
complete matching cycle. A late execution invalidates the previous reconciliation.

Prepared, unconfirmed, and working SELL quantities all reserve ownership.
Aggregate remaining exits cannot exceed the reconciled long position; concurrent
exit intentions serialize. Unresolved entry acquisition blocks exit planning.
Corrections that reduce ownership beneath pending exits block reconciliation.
A reported short/overfilled execution is quarantined without changing the last
valid nonnegative local projection. A negative broker position is retained as a
fault, never treated as flat. This cannot prevent manual/external broker activity;
it prevents this infrastructure from accepting or planning an oversell, and no
orders can be sent at all in Stage 3A.

Disconnect/restart discards session/reconciliation authority and marks bound
identities uncertain. A new verified fixture session and full reconciliation are
required. Uncertain orders never receive another order ID or an automatic retry.
Hard contradictions retain rejected evidence in a durable fault quarantine and
require operator review; there is no automatic fault reset or unsafe state repair.

The journal has a hash chain and independently checked head/sequence, durable
transactions, private DB permissions, and an exclusive writer lock. It detects
ordinary corruption, changed payloads, missing tails, and account rebinding.
It is not a cryptographic defense against an attacker replacing the entire DB
and its metadata; external backup/rollback attestation remains future work.

## Remaining external blockers

- A trusted real paper-session verifier and read-only broker transport.
- Proven complete account-wide order and execution history; API visibility and
  broker history retention may not cover every persisted intention after outages.
- An explicit normalizer for real SDK executions, corrections, busts, and fee
  versions, including incomplete/pending price revisions.
- The existing real SPX/SPXW source-timestamp, classification, and synchronization
  blockers from Stage 2. A matched broker ledger is not market-data readiness.
- Execution, broker stops, cancellation transmission, and emergency liquidation
  remain disabled and belong to a later separately authorized stage.

Run `.venv/bin/python -B executor_paper_broker_test.py` for Stage 3A tests and
`.venv/bin/python -B offline_test_runner.py` for the complete offline suite.
