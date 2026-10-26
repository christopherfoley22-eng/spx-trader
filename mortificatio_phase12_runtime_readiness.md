# Phase 12 runtime readiness — NO-GO

The fresh local audit is `phase12_runtime_audit.json`: CPython 3.9.6, LibreSSL
2.8.3, WebAuthn 2.7.1 and setuptools 58.0.4 remain below the existing reviewed
runtime/security floors. Expected audit exit is 1 / NO_GO. No interpreter,
package, SDK, credential, lock or deployed environment was changed or downloaded.
A complete approved replacement interpreter/wheelhouse was not supplied.

The supported migration target remains reviewed CPython 3.13 with maintained
OpenSSL, pending exact patch, artifact provenance and current advisory review.
The actual installed dependency manifest is in the JSON audit. The existing
legacy `requirements-security.lock` is compatibility evidence, not a complete
supported-runtime hash lock or fresh vulnerability clearance.

Native coordination uses standard-library Python plus existing libraries; no
new dependency or compiled component was added. The read-only extension uses the
installed IBKR SDK's ExecutionFilter/callback surface; compatibility with the
intended TWS/Gateway/server/SDK version is unverified. Do not assume current
protobuf-era documentation is identical to SDK 9.81.1.post1 behavior.

## Separately authorized migration, exact inputs and commands

Supply reviewed native interpreter `/private/approved-python/bin/python3.13`
and complete target-compatible wheels `/private/approved-wheels`, including pip,
patched setuptools, maintained WebAuthn/crypto closure, SDK and all test/browser
dependencies. Verify checksums, provenance, Requires-Python, platform tags and
closure with the existing offline wheel auditor. It must refuse incomplete or
ambiguous artifacts; no fabricated hashes or unreviewed downloads.

```sh
/private/approved-python/bin/python3.13 -c 'import platform,ssl,sqlite3; print(platform.python_version(), ssl.OPENSSL_VERSION, sqlite3.sqlite_version)'
MORTIFICATIO_TARGET_VERSION=$(/private/approved-python/bin/python3.13 -c 'import platform; print(platform.python_version())')
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target "$MORTIFICATIO_TARGET_VERSION" --output /private/approved-py313.lock
/private/approved-python/bin/python3.13 -m venv /private/mortificatio-py313
/private/mortificatio-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --only-binary=:all: --require-hashes -r /private/approved-py313.lock
/private/mortificatio-py313/bin/python -B mortificatio_runtime_audit.py
/private/mortificatio-py313/bin/python -B offline_test_runner.py
/private/mortificatio-py313/bin/python -B executor_phase12_benchmark.py --output /private/phase12-py313-benchmarks.json --samples 50
```

Commands above were not run. Validate native stop replay/corrections, SQLite FULL/
WAL atomicity, private DB/journal permissions, audit integrity, SDK serialization,
passkey/recovery/session/grant security, blind history revocation and all regression
scripts on the actual supported environment. Record a complete lock/SBOM and fresh
advisory/provenance evidence; prior package floors are not current CVE clearance.

Rollback: preserve the original environment. Use SQLite-aware consistent copies
of foundation, portfolio, source, read-only capture and protective checkpoint
journals, including WAL/SHM as applicable. Never copy only a live main DB or mix
state from different runs. Test migration against copies, never operational state.
On failure, stop workers and restore the matching complete state/environment pair;
keep entries fenced until full reconciliation. A rollback to this unsupported
runtime does not make trading safe.

Trusted local HTTPS/physical security keys, watchdogs, independent protective
identity, external alerts, complete-machine/disk-failure restore drills and actual
broker/data/protection validation remain operational requirements. Runtime and
broker readiness are separate. Paper and live remain **NO-GO**.
