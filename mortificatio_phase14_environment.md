# Phase 14 — environment repair and connection readiness

Inspected 2026-10-25 (New York). **Paper NO-GO; Live NO-GO.** No broker session, login, connection, orders, account/TWS setting changes, downloads, installation, macOS upgrade, deployment, commit or push. Existing uncommitted work preserved. Installation approval was requested; no approval has been received at the time of this report.

## Verified local inventory

| Item | Observed |
|---|---|
| Model | `MacBookPro14,1`: 13-inch 2017 MacBook Pro, two Thunderbolt 3 ports |
| CPU / architecture | Intel i5-7360U @ 2.30 GHz, x86_64; four logical CPUs |
| Memory | 8 GiB (8,589,934,592 bytes) |
| OS | Ventura 13.7.8, build 22H730 |
| Available storage | Approximately 21 GiB on the boot APFS container at inspection |
| Python | Apple Command Line Tools Python 3.9.6; existing `.venv` also 3.9.6; no supported replacement found |
| TLS | `/usr/bin/openssl`: LibreSSL 3.3.6; Python `ssl`: LibreSSL 2.8.3; cryptography: independently bundled OpenSSL 4.0.1 |
| SQLite | Python sqlite3 3.39.5 |
| Dependencies | webauthn 2.7.1, setuptools 58.0.4, pip 26.0.1, cryptography 48.0.1, pyOpenSSL 26.2.0, cbor2 5.9.0, pyasn1 0.6.4, cffi 2.0.0, pycparser 2.23, typing-extensions 4.16.0; pyasn1-modules missing |
| IBKR SDK | ibapi 9.81.1.post1; query APIs exist, but current SDK compatibility is unverified. No `~/IBJts/source/pythonclient` installation |
| Browser/toolchain | Chrome 155.0.8059.85, Node 22.23.2, Apple clang 14.0.3 |
| Local cache | Only old ibapi 9.81.1.post1 wheel; no complete approved wheelhouse, supported Python, Homebrew or uv found |

Hardware was read without serial numbers. Apple identifies this model's newest compatible OS as **Ventura**, not Sonoma. No unofficial upgrade is proposed. [Apple model list](https://support.apple.com/en-gb/108052).

## What this Mac can support

- **Technically possible:** a maintained CPython source build and isolated application environment on Intel Ventura. Existing compiler, SQLite and architecture are plausible prerequisites; an actual build and full dependency validation have not occurred.
- **Offline development:** this machine can remain the development/test computer. Existing regressions run here. A newer computer is not required merely to repair Python.
- **Official TWS support:** current published minimum is macOS 14, Intel i5/M1, 8 GB RAM, 2K display and broadband. This Mac meets CPU/RAM minima but cannot officially upgrade to the required OS. Its display/network suitability was not independently established. [IBKR requirements](https://portal.interactivebrokers.com/en/trading/tws-requirements.php).
- **Gateway:** available for Intel Mac and uses fewer resources. IBKR says Gateway and TWS have equal usage/support levels; no explicit current Ventura exemption was found. Do not infer support from an installer that launches. Obtain IBKR confirmation of a specific currently supported Gateway build on Ventura before deciding hardware is necessary. [Gateway/TWS documentation](https://www.interactivebrokers.com/docs/tws-api/doc/trader-workstation-and-ib-gateway/download-tws-or-ib-gateway).
- **Broker-connected validation on this Mac:** presently NO-GO. Runtime repair alone cannot establish supported TWS/Gateway or adequate OS security maintenance. If IBKR cannot confirm a supported Ventura configuration, use an existing supported host rather than buying hardware automatically. A supported Mac needs macOS 14+ and the IBKR minima above; use a currently security-maintained OS. Keep the executor and broker server together to preserve the existing localhost boundary.
- **Eventual live execution:** NO-GO regardless of hardware. No actual broker/data/native-protection evidence or real execution adapter is verified.

## Installed TWS: precise evidence

Path: `/Users/tophergold/Applications/Trader Workstation/Trader Workstation.app`.
Bundle version `0.51.0`, build `20260914163739`, `LSMinimumSystemVersion=10.15`.
`twslaunch-1051.jar` identifies **Workstation Launcher**, implementation `10511a`, built 2026-09-09. Bundled launcher Java is `17.0.14.0.101`.

These establish the installed **launcher**, not the actual downloaded/running TWS client or server build. No full TWS engine JAR was identified in the launcher JAR directory. The loader's 10.15 minimum explains technical launchability on Ventura but does not override IBKR's current macOS 14 support policy. It was not launched. No Gateway app was found in `/Applications` or `~/Applications`. Actual client/build verification remains a manual Help → About step on an accepted broker host, without placing orders.

IBKR requires current Stable/Latest TWS or Gateway and API, with Python >=3.11. The official API page currently lists Stable 1050.02 and Latest 1051.01; the latter includes the Python API. Installed 9.81 is not the current supported SDK. Review the replacement's callback signatures, dependencies and mutation surface before connection. [API requirements](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/requirements), [official SDK downloads](https://download2.interactivebrokers.com/installers/tws-api/index.html).

## Read-only harness audit

Inspected `mortificatio_read_only_check.py`, `ibkr_paper_read_only_validation.py`, `ibkr_phase11_read_only.py`, `ibkr_phase12_read_only.py` and their reducers/tests. Default launcher operation opens no socket. Explicit one-shot authorization, fresh paper/GUI evidence, an exact single approved paper account and localhost paper-port allowlist are required. The legacy base module has a live-port constant, but the guarded paper path passes only its explicitly validated paper endpoint; do not invoke legacy diagnostic CLIs.

The query allowlist includes account summary, positions, all-open-order observation, contract details/chain and market data. Order submission, cancellation, global cancellation, option exercise, account modification and order binding raise in the existing probe. Quote/size/Greek and source/receipt-time capture exists; missing authoritative times remain missing. Private new report/capture destinations prevent ordinary overwrite and sensitive stdout disclosure. Failure/timeout paths disable the session and close transport in `finally`; reconnect needs fresh approval. Offline tests cover these boundaries, not actual cleanup at IBKR. Current SDK protobuf-era callbacks/mutation methods still require review after approved installation. The existing diagnostic never grants execution authority.

## Runtime installation boundary — outcome B, not a completed migration

The conservative project-compatible target is **CPython 3.13.18, ordinary GIL build, OpenSSL 3.5.9 LTS**, user-owned prefix plus a new `.venv-py31318`. Today's 3.13.18 security release supersedes Phase 13's 3.13.17 installer recommendation and is source-only. It blocks compromised CA keys. Python 3.14.10 is also a security release, but changing minor version is unnecessary for this project's existing 3.13 artifact tooling. [Python release](https://www.python.org/downloads/release/python-31318/), [OpenSSL sources](https://www.openssl-library.org/source/).

Approval is required to download/build/install anything. No source, wheel or global interpreter was downloaded/installed. The requested scope is:

1. Official `Python-3.13.18.tar.xz`, SHA256 `abb0102e6d4ae027509bcb82327d9dd85515b44a3ba8ad538cfc5e9847029f68`, from Python.org; official OpenSSL 3.5.9 archive and publisher checksum/signature from the OpenSSL release page. Verify provenance before extraction; do not bootstrap with the old Python's TLS.
2. Reviewed wheel candidates: pip 26.2.2, setuptools 84.2.1, webauthn 3.0.1, cryptography 50.0.2 and pyOpenSSL 26.4.0, a reviewed certifi CA bundle (at least the 2026.10.19 CA update), plus complete compatible transitive closure and official IBKR API 1051.01 Python source. Existing remaining package pins are compatibility evidence only. Exact final SDK package version, dependencies, platform wheels and hashes require inspecting approved artifacts; no complete new lock is claimed.
3. Build below the repository, no `sudo`, no framework/system Python changes, no Homebrew, no OS change. User-owned staging only; never make SDK directories world-writable.

Why upgrade the coupled crypto pair: current cryptography embeds OpenSSL 4.0.1; upstream 50.0.2 wheels embed 4.0.3, and pyOpenSSL 26.4.0 supports cryptography 50.x. Validate API compatibility before adoption. Existing setuptools is below its documented path-traversal fix; current pip also predates the reviewed CA update. This is a targeted advisory review, not a full CVE clearance. [Cryptography changelog](https://cryptography.io/en/latest/changelog/), [pyOpenSSL changelog](https://www.pyopenssl.org/en/latest/changelog.html), [setuptools advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf).

### Exact build/install procedure, only after approval and verified artifacts

The source/staging paths below are **planned**, not installed or verified. Obtain/extract approved archives into `.phase14-approved/source` first. Record all source/wheel hashes. A trusted CA bundle and its provenance must be configured for the private OpenSSL installation; do not disable certificate verification or use `--trusted-host`.

```sh
# Run from /Users/tophergold/spx-trader after approval.
MORTIFICATIO_RUNTIME_ROOT=/Users/tophergold/spx-trader/.runtime-py31318
cd /Users/tophergold/spx-trader/.phase14-approved/source/openssl-3.5.9
./Configure darwin64-x86_64-cc --prefix="$MORTIFICATIO_RUNTIME_ROOT/openssl" --openssldir="$MORTIFICATIO_RUNTIME_ROOT/openssl/ssl"
make -j2
make test
make install_sw
cd /Users/tophergold/spx-trader/.phase14-approved/source/Python-3.13.18
./configure --prefix="$MORTIFICATIO_RUNTIME_ROOT/python" --with-openssl="$MORTIFICATIO_RUNTIME_ROOT/openssl" --with-openssl-rpath=auto --with-ensurepip=install
make -j2
make altinstall
cd /Users/tophergold/spx-trader
.runtime-py31318/python/bin/python3.13 -c 'import sys,ssl,sqlite3,ctypes; print(sys.version,ssl.OPENSSL_VERSION,sqlite3.sqlite_version); print(ssl.get_default_verify_paths())'
.runtime-py31318/python/bin/python3.13 -m venv .venv-py31318
# After the SDK wheel and complete reviewed closure are staged locally:
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse .phase14-approved/wheels --target 3.13.18 --output requirements-py31318.lock
.venv-py31318/bin/python -m pip install --no-index --only-binary=:all: --find-links .phase14-approved/wheels --require-hashes -r requirements-py31318.lock
.venv-py31318/bin/python -m pip check
.venv-py31318/bin/python -B mortificatio_runtime_audit.py
.venv-py31318/bin/python -B offline_test_runner.py
```

Abort on any failed command, missing extension/CA trust, resolver conflict or regression. These commands have **not** been executed. Build prerequisites and actual compatibility remain to be verified. Preserve `.venv`, original lock and databases; test against synthetic/copy state. Rollback means stop new workers and return to the original environment/state pair; that rollback remains unsuitable for broker operation. No live database migration is needed or authorized here.

## Repairs and verification

Changed only `mortificatio_runtime_audit.py`, `mortificatio_runtime_audit_test.py` and the two Phase 14 reports. Known Python security floors now reject 3.13 before 3.13.18 and 3.14 before 3.14.10; pip floor is 26.2.2. Added one focused runtime regression and extended the existing unsafe-wheel rejection check. Maintained all earlier runtime, authentication and execution gates. No trading architecture, strategy, UI or broker transport changes.

Targeted checks: runtime audit **8 passed**, launcher **4 passed**. Full offline suite: **107 scripts passed, 0 failed; five broker-connected scripts excluded**. The full run used the necessary localhost/Chrome sandbox permission. No additional exclusions were made. The suite includes security/grants, hard admission, native protection, recovery and actual localhost Chrome/dashboard tests. Five broker-connected scripts remain excluded. Tests are on the existing Python 3.9.6 environment, not a migrated runtime. No warning suppression or failing-test exclusion was introduced.

## Immediate user action

Respond to the pending isolated-runtime download/build approval request. Separately ask IBKR: “Is any current Stable/Latest Intel IB Gateway build officially supported on MacBookPro14,1, macOS Ventura 13.7.8? If yes, which exact build and supported API version?” No message has been sent on your behalf. If the answer is no, choose an already available supported host. Do not buy a computer or upgrade this Mac unofficially based on an assumption.

The next milestone is an explicitly authorized read-only broker session after those concrete prerequisites pass. See `mortificatio_phase14_connection_ready.md`.
