# Phase 9 supported-runtime migration — blocked, not verified

The offline inventory found only the existing CPython 3.9.6 environment with
LibreSSL 2.8.3. No `python3.13`, `python3.12`, `python3.11`, uv, brew or Rust
toolchain was on PATH; no framework interpreter was found under
`/Library/Frameworks/Python.framework/Versions`. No complete approved replacement
wheelhouse or interpreter was supplied. No network provisioning was attempted.

`phase9_runtime_audit.json` records the actual environment. The existing offline
auditor exits 1 / NO_GO as expected: Python/TLS migration, WebAuthn and setuptools
reviewed floors fail. This expected diagnostic exit is not a regression-test
failure. The existing compatibility `requirements-security.lock` is unchanged.
It is not a complete supported-runtime hash lock or a vulnerability clearance.

The Phase 7 candidate remains CPython 3.13 with maintained OpenSSL. Current exact
patch versions, artifact provenance and fresh vulnerability advisories were not
verified offline. The existing auditor's package floors are prior review floors,
not a claim that meeting them removes every known vulnerability. Setuptools
78.1.1 is the previous specific-advisory minimum, not approval of that exact
version for deployment. A newer reviewed release may be required.

## Exact offline provisioning procedure once approved artifacts exist

Supply a separately reviewed native interpreter at
`/private/approved-python/bin/python3.13`, and a complete matching wheelhouse at
`/private/approved-wheels`. Include pip, patched setuptools, WebAuthn and its full
dependency closure, the existing IBKR interface and every test dependency.
Preserve the existing `.venv` and private state. Do not run against operational
databases while testing a migration; use consistent encrypted backups/copies,
including WAL/SHM and the independent protective checkpoint/journals.

```sh
/private/approved-python/bin/python3.13 -c 'import platform, ssl; print(platform.python_version(), ssl.OPENSSL_VERSION)'
MORTIFICATIO_TARGET_VERSION=$(/private/approved-python/bin/python3.13 -c 'import platform; print(platform.python_version())')
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target "$MORTIFICATIO_TARGET_VERSION" --output /private/approved-py313.lock
/private/approved-python/bin/python3.13 -m venv /private/mortificatio-py313
/private/mortificatio-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --only-binary=:all: --require-hashes -r /private/approved-py313.lock
/private/mortificatio-py313/bin/python -B mortificatio_runtime_audit.py
/private/mortificatio-py313/bin/python -B offline_test_runner.py
/private/mortificatio-py313/bin/python -B executor_readiness_benchmark.py --output /private/phase9-py313-benchmark.json --repeats 50
```

These provisioning/migration commands were **not run**. The existing wheel-lock
tool validates compatible tags, actual wheel-byte hashes, Requires-Python,
dependency closure, ambiguous packages and reviewed floors; it refuses incomplete
or unrelated artifacts and refuses to overwrite a lock. Do not manufacture hashes
from installed files, use unreviewed network indexes, or silently upgrade the
working environment. The target interpreter must actually have maintained TLS.

Verify every subprocess/browser/backend uses the new interpreter; obtain any
missing test dependencies from the reviewed wheelhouse and regenerate the complete
lock. Record a full environment/SBOM and trusted advisory review. Verify SQLite
integrity/restore, history equality, permission checks, passkey/recovery ceremonies,
unchanged source timestamp gates and SDK serialization without connecting to IBKR.
Repeat timing under controlled load. Physical key/trusted HTTPS and operational
acceptance remain required. Only after real supported-runtime regression may this
blocker be called resolved. No order enablement or broker validation is authorized
by these instructions.
