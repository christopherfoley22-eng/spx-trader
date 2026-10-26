# Phase 14 — first read-only session checklist

**Not connected; not ready to connect yet. Paper and live execution remain disabled.** Current Mac: MacBookPro14,1 / Ventura 13.7.8. No Sonoma upgrade is supported for this model.

1. **Resolve broker-host support.** Ask IBKR whether a specific current Intel Gateway release is officially supported on Ventura. TWS's current published minimum is macOS 14; installed loader metadata does not override it. If there is no supported Ventura configuration, use an existing supported host. Do not buy hardware before checking this. Keep broker and executor on the same host; the current harness permits only localhost.
2. **Approve the isolated runtime installation.** The pending request covers official CPython 3.13.18 + OpenSSL 3.5.9 source builds, a new virtual environment, reviewed dependencies and current official SDK. No approval means no downloads/installations. The actual build commands, compatibility concerns and rollback are in `mortificatio_phase14_environment.md`. Preserve `.venv`. Phase 13's 3.13.17 recommendation is superseded by today's security release.
3. **Run the only currently available safe preflight command:**

```sh
cd /Users/tophergold/spx-trader
.venv/bin/python -B mortificatio_read_only_check.py
```

This command was verified locally. It makes no connection and correctly exits 1 with `LOCAL_PREFLIGHT_ONLY`, `connection_attempted: false` and runtime blockers. Repeating it does not repair them. After approved provisioning, `.venv-py31318/bin/python` is the planned replacement; that path does not exist yet. Require `pip check`, runtime audit, reviewed CA trust and the entire offline suite to pass in the new environment. Require SDK 1051.01 callback/mutation-boundary compatibility tests before connection; installed SDK 9.81 tests alone are insufficient.

4. **On the accepted host, manually open TWS/Gateway and select PAPER**, authenticate normally and complete IBKR Mobile authentication when requested. Confirm the exact client/build from Help → About. Installed launcher path on this Mac is `/Users/tophergold/Applications/Trader Workstation/Trader Workstation.app`; no command to launch it has been executed. Never send credentials or account identifiers in chat.
5. **Manually inspect API settings:** enable socket clients, keep **Read-Only API checked**, restrict to localhost/trusted `127.0.0.1`, and leave order binding disabled. Record the actual paper socket port and an unused nonzero client ID. The code allowlists 7497/4002; neither is a verified GUI setting. A different port requires review. Default client ID 5091 is not proof it is free. Do not enable order submission. The API cannot attest the read-only GUI checkbox.
6. **Verify entitlements rather than purchasing guesses:** Cboe Streaming Market Indexes for SPX; OPRA US Options coverage for SPXW bid/ask/sizes; both underlying and option coverage for Greeks. Confirm API agreements, user classification and paper-data sharing. OPRA does not include the underlying index. Contract/venue-specific depth remains unverified and optional for the first diagnostic. Missing required capacity evidence must continue to reject entries. [IBKR entitlement guide](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction).
7. **Prepare private local configuration** outside git, in a nonsymlink owner-controlled `0700` directory with a `0600` config file. Use the exact schema in `mortificatio_phase13_connection_checklist.md`; fill the approved paper identity locally, verified port and fresh timezone-aware approval/GUI-attestation times (at most ten minutes). Store no passwords, 2FA secrets or browser credentials. New capture and report paths must be distinct from config and must not overwrite files.
8. **Request explicit authorization for one read-only run**, including whether execution history may be requested. Only after approval and all prerequisites, use this planned command on this Mac (adjust repository/home paths on another accepted host):

```sh
/Users/tophergold/spx-trader/.venv-py31318/bin/python -B /Users/tophergold/spx-trader/mortificatio_read_only_check.py \
  --config /Users/tophergold/.mortificatio-readonly/config.json \
  --capture /Users/tophergold/.mortificatio-readonly/phase14-session-001.sqlite \
  --report /Users/tophergold/.mortificatio-readonly/phase14-session-001.json \
  --seconds 10 --client-id 5091 --include-execution-history \
  --authorize-read-only-connection
```

This connection command has **not** been executed. Replace the client ID with the verified unused value. The ten seconds bounds streaming collection, with additional bounded setup. Apply the approved runtime's CA trust configuration if required; never disable certificate verification.

9. **Review the captured evidence privately:** exact paper identity, funds, positions/open orders, qualified SPX and same-day SPXW, data type, bid/ask/sizes/Greeks, source versus receipt time, SDK/server versions and optional executions/commissions. Existing bounded harness selects three nearby strikes per direction, not all ten five-strike candidates. Depth has a separate opt-in method and is not requested automatically. History completion does not prove all old executions were returned. A successful process exit is not trading acceptance. Socket/subscriptions must close cleanly and connection failures must leave execution disabled.
10. **Review results before paper-order permission.** Watchlist streaming is aggregated (US indices 250 ms, options 100 ms); trade timestamps and local receipt cannot prove authoritative quote/Greek time. Missing timestamps must keep the 0.25-second gate closed. [IBKR streaming](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data). Before a supervised one-contract paper protocol: accepted runtime/host, verified paper account, usable provenance, reconciled broker state, explicit real capacity/risk inputs and an independently reviewed reduction-safe order/native-protection adapter and test protocol. Read-only cannot prove partial-fill stop activation, OCA races, cancel/replace or actual fill quality. Those are the purpose of separately authorized controlled paper tests.
