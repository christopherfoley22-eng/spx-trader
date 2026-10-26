# Phase 13 — read-only connection checklist

No connection has been attempted. Paper session only. No order permission is granted by this checklist.

1. **Approve the environment update first.** This Intel Mac has macOS 13.7.8, Python 3.9.6 and LibreSSL 2.8.3. IBKR currently lists macOS 14 minimum for TWS; check Software Update for a supported upgrade, or use another supported Mac. Back up before any OS change; keeping `.venv` preserves application rollback but does not roll back macOS. TWS exists at `~/Applications/Trader Workstation/Trader Workstation.app`. Its bundle advertises `0.51.0`, which is not sufficient evidence of the actual TWS API/server build. Confirm Help → About manually; do not reinstall or launch it automatically. Use either official TWS **or** IB Gateway, not both; TWS is preferable for the first supervised diagnostic. Do not use IBKR Desktop as the API server. OS/software installation requires separate approval/physical action. [IBKR system requirements](https://portal.interactivebrokers.com/en/trading/tws-requirements.php).
2. **Approve a separate supported Python environment and reviewed artifacts.** Target ordinary CPython 3.13.17, Intel/universal2, with stdlib OpenSSL >=3.5. Python's installer supports this OS; the official 3.13 changelog records the macOS update to OpenSSL 3.5.9. Verify the installed result rather than treating documentation as local evidence. [Python TLS changelog](https://docs.python.org/pt-br/3.13/whatsnew/changelog.html). Do not replace `.venv`. Obtain a reviewed complete macOS cp313 wheelhouse containing `webauthn>=3.0.1`, `setuptools>=78.1.1`, cryptography, pyOpenSSL, cbor2, pyasn1, pyasn1-modules, cffi, pycparser, typing-extensions, ibapi and pip, plus their dependencies. These floors are existing review floors, **not current vulnerability clearance**. Select an officially supported SDK/TWS pairing and inspect callback compatibility before approving its exact pins. The only local wheel found is old ibapi 9.81.1.post1; it is insufficient. [Python release](https://www.python.org/downloads/release/python-31317/).
3. **After artifacts are separately approved**, use the commands below. `/private/approved-wheels` and the interpreter path are placeholders for reviewed local artifacts; do not run download commands. Abort if TLS, dependency closure or tests fail. Retain `.venv` and existing databases unchanged for rollback.

```sh
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -c 'import sys,ssl; print(sys.version); print(ssl.OPENSSL_VERSION)'
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv .venv-py313
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target 3.13.17 --output requirements-py313.lock
.venv-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --require-hashes -r requirements-py313.lock
.venv-py313/bin/python -m pip check
.venv-py313/bin/python -B mortificatio_runtime_audit.py
.venv-py313/bin/python -B offline_test_runner.py
```

The existing lock tool inspects artifacts without installing them. Exact new pins/hashes cannot be supplied honestly before those artifacts are selected. Browser regression also requires the existing local Node/Chrome setup. A successful runtime audit still requires full security acceptance; it is not permission to connect or trade.

4. **Manually log into PAPER TWS/Gateway**, complete IBKR Mobile authentication when requested, and verify the paper account in the GUI. Do not send passwords, recovery codes or account identifiers in chat. Verify there is exactly one permitted account. Confirm SPX index and same-day SPXW options are visible.
5. **Manually configure API access:** enable socket clients, keep **Read-Only API checked**, restrict access to localhost/trusted `127.0.0.1`; do not enable automatic order binding. Record the actual configured paper socket port. Code currently accepts only 7497 or 4002; these are allowlisted conventional paper ports, **not observed settings**. A different configured port needs review, not silent fallback. Choose an unused nonzero client ID; launcher default 5091 is not proof it is unused. Record TWS/Gateway build and SDK version. Read-only GUI setting cannot be attested through this API. [IBKR API setup](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/).
6. **Confirm entitlements in Client Portal:** real-time Cboe Streaming Market Indexes coverage for SPX and OPRA options coverage (standalone or qualifying bundle) for SPXW, API access agreements, professional/nonprofessional classification, and paper-account sharing eligibility. Names describe required coverage; actual subscriptions/status are unknown. Do not buy a guessed bundle. Additional depth is optional and contract/venue-specific; OPRA L1 does not establish full executable depth. [IBKR subscription guide](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction).
7. **Create a local private diagnostic directory** (`0700`) and configuration file (`0600`), outside tracked files (for example `mkdir -m 700 "$HOME/.mortificatio-readonly"`, then create `config.json` locally and `chmod 600` it before entering private values). If the directory already exists, inspect its ownership/permissions rather than overwriting files. Use `ibkr_paper_read_only_validation.load_config`'s exact schema below. Replace placeholders locally; ISO times must be timezone-aware and fresh, GUI attestation lifetime at most ten minutes. Never add broker credentials. Use the port actually verified in step 5.

```json
{
  "account": "<approved paper account entered locally>",
  "verification_ref": "<local approval reference>",
  "approved_until": "<future ISO timestamp>",
  "host": "127.0.0.1",
  "port": "<replace with verified integer paper port>",
  "settings": {
    "reference": "<GUI inspection reference>",
    "verified_at": "<current ISO timestamp>",
    "expires_at": "<ISO timestamp at most 10 minutes later>",
    "paper_login_verified": true,
    "read_only_api_checked": true
  }
}
```

8. **Request separate explicit authorization for one bounded read-only session (10-second streaming window plus bounded handshake/qualification)**, including whether to inspect account-filtered execution history. Without it, run only `mortificatio_read_only_check.py` with no arguments (local preflight, no socket). After approval and supported-runtime acceptance, the exact command is:

```sh
.venv-py313/bin/python -B mortificatio_read_only_check.py \
  --config /Users/tophergold/.mortificatio-readonly/config.json \
  --capture /Users/tophergold/.mortificatio-readonly/session-unique.sqlite \
  --report /Users/tophergold/.mortificatio-readonly/session-unique.json \
  --seconds 10 --client-id 5091 --include-execution-history \
  --authorize-read-only-connection
```

Replace paths/client ID with verified local values; capture/report must be new files in a real, nonsymlink `0700` directory. Config, capture and report must be distinct. Raw diagnostics contain private account/order data: keep local; stdout exposes status only. Shell history need not contain any account identifier.

9. **Inspect evidence, not just exit code.** The diagnostic reads account identity/funds, positions, existing open orders, exact qualified SPX/SPXW identities, chain/quotes/sizes/Greeks and optional execution/commission callbacks. Current bounded subscription selects three near-ATM strikes per direction, **not the complete five-strike ladder per direction**. Depth inspection exists but is separately authorized and not automatically requested by this launcher. Require explicit completeness/freshness checks; absence of old executions is not proof of absence. It disconnects after the bounded run; `--seconds` bounds streaming collection, not total setup time. It cannot submit/modify/cancel orders or enable execution; legacy order methods raise. No subscriptions, permissions or broker authentication settings are changed automatically.
10. **Repeat in liquid market hours** for quote evidence; a closed-market identity/reconciliation test cannot prove live data. Standard watchlist streaming is aggregated (US indices 250 ms, options 100 ms), not exchange tick-by-tick. Receipt time and trade timestamps cannot establish authoritative bid/ask/Greek timestamps. Keep the existing 0.25-second source synchronization gate blocked if evidence is absent. [IBKR streaming documentation](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data).

Before requesting paper-order testing: supported runtime and full regression accepted; paper-only identity/read-only capture validated; actual contract and entitlement evidence reviewed; authoritative synchronization solution validated; account positions/orders/executions reconciled; explicit calibrated risk policy and real-data capacity/protection-readiness mapping reviewed; and a narrowly scoped, separately approved order adapter/native-protection protocol with oversell-safe cancellation/recovery must exist. Read-only observation alone cannot satisfy the last requirement. Initial controlled paper protocol should use one contract, not 125; its outcome cannot prove live fill quality.
