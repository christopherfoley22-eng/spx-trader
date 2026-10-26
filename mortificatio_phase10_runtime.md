# Phase 10 runtime — remediation still blocked

The actual offline audit is `phase10_runtime_audit.json`. It remains NO_GO on
Python 3.9.6 / LibreSSL 2.8.3, with WebAuthn and setuptools below the existing
reviewed floors. An audit exit code of 1 is the expected blocker signal, not a
waived test failure. No supported interpreter, approved complete wheelhouse or
separately authorized environment update was available. No package was downloaded
or upgraded; the working `.venv` and `requirements-security.lock` were preserved.

The audit JSON contains the complete observed installed dependency manifest.
It is **not** a supported target lock, SBOM provenance attestation or current CVE
clearance. The existing security compatibility lock covers the legacy environment,
not production approval. The Phase 9 supported CPython 3.13 / maintained OpenSSL
candidate remains the migration target pending exact patch/artifact review.

Once a separately approved native interpreter and complete reviewed wheels exist:

```sh
/private/approved-python/bin/python3.13 -c 'import platform, ssl; print(platform.python_version(), ssl.OPENSSL_VERSION)'
MORTIFICATIO_TARGET_VERSION=$(/private/approved-python/bin/python3.13 -c 'import platform; print(platform.python_version())')
.venv/bin/python -B mortificatio_runtime_audit.py --wheelhouse /private/approved-wheels --target "$MORTIFICATIO_TARGET_VERSION" --output /private/approved-py313.lock
/private/approved-python/bin/python3.13 -m venv /private/mortificatio-py313
/private/mortificatio-py313/bin/python -m pip install --no-index --find-links /private/approved-wheels --only-binary=:all: --require-hashes -r /private/approved-py313.lock
/private/mortificatio-py313/bin/python -B mortificatio_runtime_audit.py
/private/mortificatio-py313/bin/python -B offline_test_runner.py
/private/mortificatio-py313/bin/python -B executor_readiness_benchmark.py --output /private/phase10-py313-exit.json --repeats 50
/private/mortificatio-py313/bin/python -B executor_execution_twin.py --output /private/phase10-py313-quality.json
/private/mortificatio-py313/bin/python -B executor_phase10_benchmark.py --quality /private/phase10-py313-quality.json
```

These migration commands were **not run**. The supplied wheel set must include
the complete maintained WebAuthn/cryptographic dependency closure, patched
setuptools/pip, compatible IBKR SDK and test dependencies. The existing offline
lock generator hashes actual wheel bytes, checks target tags/Requires-Python and
dependency closure, and refuses incomplete/ambiguous artifacts. Do not fabricate
hashes from installed RECORD files or interpret prior minimum versions as fresh
advisory clearance. Obtain a current trusted advisory/provenance review separately.

Use a new private environment and copied consistent state, including WAL/SHM,
checkpoint and order journals; never test migrations against operational data.
Verify every subprocess/browser uses the target interpreter. Test restore,
permissions, SDK serialization, passkeys/recovery, source timestamp gates and all
regressions. Record complete actual locks/SBOM and controlled-load benchmarks.
No TLS, passkey, broker authentication or permission check may be bypassed to
make an unsupported stack appear ready. Physical keys/trusted HTTPS, watchdogs,
external alerts and actual account/protection validation remain open.

Supported-runtime acceptance was **not achieved**. Paper and live remain NO-GO.
