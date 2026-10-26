# Phase 11 supported-runtime migration — unresolved

Actual inventory: `phase11_runtime_audit.json`; audit status **NO_GO**. The host
still uses CPython 3.9.6 / LibreSSL 2.8.3, WebAuthn 2.7.1 and setuptools 58.0.4.
No Python 3.13/3.12/3.11 executable was found on PATH. No approved replacement
interpreter or complete wheelhouse was supplied. No download, installation,
package upgrade, lock rewrite or environment modification occurred.

The maintained migration target remains CPython 3.13 plus maintained OpenSSL,
subject to reviewed exact patch/artifact selection. Python's official supported
version table lists release lifecycles; 3.9 is no longer a supported production
choice. [Python version status](https://devguide.python.org/versions/).

The existing `requirements-security.lock` pins the legacy compatibility set:
webauthn 2.7.1, cryptography 48.0.1, pyOpenSSL 26.2.0, cbor2 5.9.0, pyasn1 0.6.4,
cffi 2.0.0, pycparser 2.23 and typing_extensions 4.16.0. Those versions are an
inventory, **not approval for the target runtime or fresh vulnerability clearance**.
ibapi 9.81.1.post1, pip 26.0.1 and setuptools 58.0.4 are also recorded in the
actual audit. No complete compatible target dependency pin/hash manifest can
honestly be supplied without reviewed target artifacts and validation.

Required artifacts: native x86_64 supported CPython/OpenSSL distribution with
checksums/provenance; pip/patched setuptools; maintained WebAuthn and full crypto/
CBOR/ASN.1/CFFI closure; compatible IBKR SDK; all browser/offline test dependencies.
Use the existing offline wheel auditor to verify tags, Requires-Python, dependency
closure, reviewed floors and actual wheel hashes. Its prior floors are not a
current security advisory scan. Reject incomplete or ambiguous wheel sets.

## Exact separately authorized migration sequence

Do not run until interpreter, complete wheels and exact versions are approved.
The paths below are specified inputs, not artifacts claimed to exist.

```sh
/private/approved-python/bin/python3.13 -c 'import platform,ssl,sqlite3; print(platform.python_version(), ssl.OPENSSL_VERSION, sqlite3.sqlite_version)'
MORTIFICATIO_TARGET_VERSION=$(/private/approved-python/bin/python3.13 -c 'import platform; print(platform.python_version())')
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target "$MORTIFICATIO_TARGET_VERSION" --output /private/approved-py313.lock
/private/approved-python/bin/python3.13 -m venv /private/mortificatio-py313
/private/mortificatio-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --only-binary=:all: --require-hashes -r /private/approved-py313.lock
/private/mortificatio-py313/bin/python -B mortificatio_runtime_audit.py
/private/mortificatio-py313/bin/python -B offline_test_runner.py
/private/mortificatio-py313/bin/python -B executor_phase11_benchmark.py --output /private/phase11-py313-benchmarks.json --samples 10 --exit-samples 50
```

Commands were **not run**. Validate target SQLite transaction/restore behavior,
FULL/WAL/private file permissions and application/journal identity, SDK callback
serialization, passkey enrollment/login/recovery, sessions/grants/revocation,
all market timestamp gates and full offline regression. Ensure subprocesses use
the new interpreter. Record actual complete lock/SBOM, reviewed advisories and
matched controlled-load timings before calling migration successful.

Rollback: keep the original environment untouched. Stop test workers cleanly;
retain consistent copies of all foundation, portfolio, source-event and independent
protective checkpoint state, including SQLite WAL/SHM as applicable, before trying
migration. Use SQLite-aware consistent backup/restore rather than copying a live
main database alone. Revert to the original environment and its corresponding
complete consistent state copy, never mix journals/checkpoints from different
runs or restore by discarding unresolved orders. Keep entries fenced through
reconciliation. Original unsupported runtime remains NO-GO even after rollback.

No operational data, real credentials or broker session may be used in the test
migration. Physical-key/trusted HTTPS, supervised independent workers, external
alerts/restore drills and current advisory clearance remain separate acceptance
requirements. Paper and live remain **NO-GO**.
