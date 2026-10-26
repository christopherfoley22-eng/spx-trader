# Phase 13 — operational read-only handoff

2026-10-25. **Paper NO-GO. Live NO-GO.** No broker connection, orders, credential use, permission changes, downloads, installations, deployment, commit or push occurred. No new simulator or speculative optimizer was built.

## Working versus fixed

The repository has authenticated three-button command handling, separate trading grants, strict ATM-first 25-per-strike/125-total allocation, hard capacity admission, prepared entries/exits, a durable economic ledger, independent protective checkpoints, native-protection obligations, execution corrections and reconciled/Blind Execution history. These are offline-tested capabilities. None establishes actual IBKR execution/protection correctness.

The Phase 12 read-only capture already supports account-filtered execution requests, positions, open orders, status, commissions, quote/size/Greek callbacks and raw correction evidence. Phase 13 adds **`mortificatio_read_only_check.py`**, a small executable handoff to that existing infrastructure: no-connection default, runtime gating, separate explicit connection flag, bounded duration, private new report/capture paths and status-only stdout. The existing transport keeps order submission/modification/cancellation disabled. It disconnects at completion. A diagnostic exit code is not trading acceptance.

Also fixed **`mortificatio_runtime_audit.py`**: absent required distributions now produce explicit NO-GO issues. Previously only installed versions were tested against floors. Added four focused launcher tests and one runtime-audit regression; no strategy/execution/UI modifications.

## Runtime: blocked, not migrated

Observed: macOS 13.7.8 / Intel x86_64; `.venv` Python 3.9.6, LibreSSL 2.8.3; webauthn 2.7.1, setuptools 58.0.4, ibapi 9.81.1.post1; pyasn1-modules missing. No supported Python, Homebrew/uv installation or complete approved wheelhouse found. Local pip cache has only an old IBKR wheel. Existing `requirements-security.lock` is not an approved complete hashed target-environment lock.

TWS **is installed** under `~/Applications/Trader Workstation/Trader Workstation.app`; its bundle version `0.51.0` does not prove the running TWS/server build. It was not launched. Current IBKR published TWS minimum is macOS 14, above this Mac's observed OS. [IBKR requirements](https://portal.interactivebrokers.com/en/trading/tws-requirements.php).

Safe migration cannot be completed from local assets. It needs separately approved OS/runtime/package work. Target ordinary CPython 3.13.17 with project-required OpenSSL >=3.5; the official 3.13 changelog documents macOS OpenSSL 3.5.9, but local installation/TLS still needs verification. [Python changelog](https://docs.python.org/pt-br/3.13/whatsnew/changelog.html). Keep `.venv` intact; build `.venv-py313`, hash-lock reviewed compatible complete wheels and run the full suite. Exact reproducible commands and rollback boundary are in [connection checklist](mortificatio_phase13_connection_checklist.md). No new pins or hashes were invented. Current package-floor checks are not a complete vulnerability scan.

## Ranked blockers

1. **Supported host/runtime and reviewed dependency closure:** OS compatibility, Python/TLS, old authentication/packaging dependencies and missing package. Requires approval/physical action; cannot be repaired by an offline download-free code edit.
2. **No connection authorization or verified session setup:** manual paper login, 2FA when required, GUI read-only attestation, exact account/port/build and unused client ID must be provided locally. No actual account/settings/subscription configuration was read or altered. The harness only accepts the explicit approved single paper account and localhost allowlisted paper ports.
3. **Market provenance:** actual live SPX/SPXW/Greeks entitlements, completeness and source timestamps unobserved. Ordinary watchlist callbacks do not prove synchronized authoritative bid/ask/Greek time; the 0.25-second gate remains intact. A first diagnostic can establish the limitation even if it cannot resolve it.
4. **Real admission and broker protection:** capacity context and protection readiness still explicitly fixture-only. Modeled liquidity is not a calibrated real provider; displayed size is not guaranteed liquidity. Native SPXW stop/partial-fill/OCA/cancel-replace behavior is not observed. Actual order submission remains disabled.
5. **Operational acceptance:** SDK/current TWS callback compatibility, full snapshot/history coverage, private diagnostic review, trusted HTTPS and physical security-key checks remain. Existing read-only run qualifies three near-ATM strikes each direction, not all ten five-strike candidates; separately inspect the full selected ladder before any paper-order protocol.

## Exact next action and shortest credible path

**First approve provisioning a supported Mac/OS and a separate supported Python/TLS environment with reviewed package artifacts.** Confirm whether this Mac can run macOS 14+; use another supported Mac if it cannot. Do not authorize trading. Then manually verify the installed TWS build and PAPER/Read-Only API setup using the checklist, and separately authorize one read-only capture with a 10-second streaming window plus bounded setup.

The capture will read account identity/funds, qualified contracts, SPX/SPXW quotes/sizes/Greeks, timestamp provenance, positions/open orders and optionally execution/commission reports. It will not submit, modify or cancel orders, nor prove stop execution. Private account and order data remain local.

After reviewing that evidence, resolve only demonstrated compatibility/provenance/reconciliation gaps. Before requesting controlled paper-order permission, require supported-runtime regression acceptance, exact paper-account isolation, usable market-data evidence satisfying unchanged gates, reconciled broker state, reviewed real admission inputs/risk policy and a narrowly scoped independently safe order/protection adapter protocol. Start the separately authorized protocol at one contract and prove protection/reconnect/cancel races before scaling. No broad new offline phase is needed as a substitute for these operational steps.

## Changed files and verification

Phase 13 scope only; pre-existing uncommitted work preserved:

- `mortificatio_runtime_audit.py` — required-package absence rejection.
- `mortificatio_runtime_audit_test.py` — missing-package regression.
- `mortificatio_read_only_check.py` — guarded existing-harness launcher.
- `mortificatio_read_only_check_test.py` — four focused no-connection/privacy/path tests using fakes.
- `mortificatio_phase13_readiness.md` — this report.
- `mortificatio_phase13_connection_checklist.md` — Mac/runtime/TWS/action/authorization commands.
- `mortificatio_phase13_execution_audit.md` — lifecycle, capacity inputs and protection evidence matrix.

Targeted tests: launcher 4 passed, runtime audit 7 passed. Local preflight correctly returns NO-GO and `connection_attempted: false`. Full offline regression result: **107 scripts passed, 0 failed; five broker-connected scripts excluded**. This was run on the existing Python 3.9.6 environment, not a migrated runtime. The initial sandboxed run was 106 passed/1 failed solely because localhost binding was denied for the Chrome browser script; the complete permission-enabled rerun passed. No broker-connected test was run. No execution-path performance benchmark was rerun because execution behavior was unchanged.
