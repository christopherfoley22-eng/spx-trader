# Mortificatio — reviewed GitHub transfer

Prepared from `main` at `9a303ef0315f0f1b68bb8531e6606b72bce84942`. No commit or push has been performed. Approval is required before either.

## Transfer scope

188 changed/new files: 23 modified tracked files, 164 existing new project files, and this transfer report. The 187 pre-existing changed/new files are all preserved, including earlier-phase dependencies and completed Phase 10–14 source, tests, reports, runtime instructions and synthetic benchmark evidence. No existing work was deleted. The complete project tree after transfer contains 264 tracked source/evidence files, including 76 files unchanged since the base commit.

`.gitignore` is the only implementation-adjacent edit in this transfer task: it now also excludes alternate local virtual environments, runtime build/download staging, dependency trees/caches, private diagnostics, logs and additional private key/database containers. No trading, sizing, exit, authorization or broker behavior changed.

## Security review

Examined all 263 pre-existing publishable project files, not just the change list, plus this report before staging. Checked key blocks, common provider tokens, embedded URL credentials, credential-like literal assignments, broker account patterns, high-entropy strings, filenames, binary/symlink presence and remote URL credential embedding. No actual credentials, private keys or real broker account identifiers were identified in the transfer set. Account-like values and static tokens were traced to synthetic fixtures; high-entropy candidates were public documentation URLs. No sensitive values are reproduced here. This is a targeted review, not a guarantee against every possible encoded secret.

Ignored local state (`.executor-local/`, `.executor-shadow/`, `.venv/`) is retained on the Mac and excluded from Git. It must not be force-added or copied into Codespaces. Future private read-only captures/configuration, build artifacts and alternative virtual environments are also excluded. Intentionally preserved phase benchmark/runtime-audit JSON files are historical project evidence, not credentials or operational broker configuration.

## Verification

Full offline regression: **107 scripts passed, 0 failed; five broker-connected scripts excluded**. The run included localhost HTTPS/Chrome tests with the necessary sandbox permission, on the existing Python 3.9.6 environment. This does not constitute Codespaces or supported-runtime validation. Five broker-connected scripts remain excluded by the existing runner. No broker connection, trades, downloads or installation were performed. `git diff --check` passes.

## Codespaces handoff limitations

The regression result belongs to the current Mac/Python environment; it does not certify Linux/Codespaces. Do not transfer the Mac virtual environment. `requirements-security.lock` remains the historical Python 3.9 compatibility lock, not a supported-runtime/security approval. Preserve Phase 14's migration blockers and separate authorization boundaries.

The existing browser test uses a macOS Chrome executable path and `/private/tmp`; those need platform-appropriate setup before the complete suite can pass in Codespaces. Node/Chrome and the reviewed Python/SDK dependency closure are not provisioned by this transfer. No devcontainer or automatic broker startup has been introduced. Use `offline_test_runner.py`, not an indiscriminate runner that could invoke the five broker-connected scripts.

No Codespaces TWS/Gateway session has been provisioned or validated by this transfer. Existing broker admission requires an approved localhost paper endpoint; do not change it to a remote gateway, forward broker ports publicly, or enable order submission as part of moving files. Paper and live remain NO-GO.

## Approval and publication

The reviewed file set will be staged explicitly. Before committing, compare staged paths/content with the reviewed manifest, inspect the staged diff, and check that the remote branch has not advanced. If it has diverged, stop for review; do not reset, rebase or force-push. Only after explicit approval, use a normal commit and normal push to `origin main`. Git `user.name` and `user.email` are currently unset; obtain the approved author identity (prefer GitHub noreply), then configure it locally or use per-command identity. Do not invent an author or publish a private email accidentally.

Suggested commit title: `Mortificatio: preserve execution and safety work through Phase 14`.

## Every changed or created file since 9a303ef

`M` = modified tracked file; `A` = new file. This report is included as the final new file.

```text
M .gitignore
A dashboard/dashboard.css
A dashboard/dashboard.js
A dashboard/index.html
A dashboard/mountains.svg
A executor_acceptance_test.py
A executor_active_exit_test.py
A executor_adaptive_liquidity.py
A executor_adaptive_liquidity_test.py
A executor_allocation.md
A executor_allocation.py
A executor_allocation_service.py
A executor_allocation_service_test.py
A executor_allocation_test.py
A executor_application.py
A executor_application_api.py
A executor_application_api_test.py
A executor_application_test.py
M executor_autonomous_local_test.py
A executor_blind_presentation.py
A executor_broker_lifecycle_capture.py
A executor_broker_lifecycle_capture_test.py
A executor_broker_reality.py
A executor_broker_reality_test.py
A executor_contract_selection.py
A executor_dashboard.md
A executor_dashboard_browser_test.js
A executor_dashboard_browser_test.py
A executor_dashboard_test.py
A executor_dashboard_web.py
M executor_dry_run.py
M executor_dry_run_lifecycle_test.py
M executor_dry_run_recovery_test.py
A executor_emergency.py
A executor_emergency_test.py
A executor_entry_readiness.py
A executor_entry_readiness_test.py
A executor_execution_coordinator.py
A executor_execution_twin.py
A executor_execution_twin_test.py
A executor_exit_capacity.py
A executor_exit_capacity_test.py
A executor_exit_readiness.py
A executor_exit_readiness_lifecycle_test.py
A executor_exit_readiness_test.py
A executor_exit_stream.py
A executor_exit_stream_test.py
A executor_history_presentation_test.py
M executor_ibkr_observation.py
A executor_ibkr_read_only.md
A executor_ibkr_read_only.py
A executor_ibkr_read_only_test.py
A executor_lifecycle.md
A executor_lifecycle.py
A executor_lifecycle_recovery_test.py
A executor_lifecycle_test.py
A executor_liquidation.py
A executor_liquidation_lifecycle_test.py
A executor_liquidation_test.py
A executor_liquidity_benchmark.py
A executor_liquidity_simulator.py
A executor_liquidity_simulator_test.py
A executor_liquidity_stress_test.py
M executor_live_observation_service.py
M executor_local_service.py
M executor_local_ui.html
M executor_local_ui.md
M executor_local_ui_race_test.js
M executor_local_web.py
M executor_market_ingestion.py
A executor_market_state.py
A executor_market_state_test.py
A executor_market_stream.md
A executor_market_stream.py
A executor_market_stream_test.py
A executor_native_protection.py
A executor_native_protection_test.py
A executor_offline_fixture.py
A executor_paper_broker.md
A executor_paper_broker.py
A executor_paper_broker_test.py
A executor_paper_reconciliation.py
A executor_paper_types.py
A executor_phase10_benchmark.py
A executor_phase10_lifecycle_test.py
A executor_phase11_benchmark.py
A executor_phase11_lifecycle_test.py
A executor_phase11_protection_test.py
A executor_phase12_benchmark.py
A executor_phase12_lifecycle_test.py
A executor_portfolio.md
A executor_portfolio.py
A executor_portfolio_liquidity.py
A executor_portfolio_stress_test.py
A executor_portfolio_test.py
A executor_premium_stop_test.py
A executor_protection_checkpoint.py
A executor_protection_lifecycle_test.py
A executor_protection_recovery.py
A executor_protection_reliability_test.py
A executor_protective_exits.md
A executor_protective_exits.py
A executor_protective_exits_test.py
A executor_protective_service.py
A executor_readiness_benchmark.py
A executor_readiness_benchmark_test.py
A executor_readiness_rollback_test.py
A executor_readiness_worker_benchmark.py
M executor_replay.py
M executor_replay_fixtures.json
M executor_replay_format.md
M executor_replay_process.py
M executor_replay_sessions.py
M executor_replay_test.py
A executor_trade_accounting.py
A executor_trade_history.md
A executor_trade_history.py
A executor_trade_history_test.py
A executor_tradingview.md
A executor_tradingview.py
A executor_tradingview_replay.py
A executor_tradingview_replay_test.py
A executor_tradingview_test.py
A executor_tradingview_web.py
A ibkr_paper_read_only_validation.py
A ibkr_phase11_read_only.py
A ibkr_phase12_read_only.py
A mortificatio_auth.py
A mortificatio_auth_test.py
A mortificatio_credential_recovery.py
A mortificatio_credential_recovery_api_test.py
A mortificatio_credential_recovery_test.py
A mortificatio_hardening_test.py
A mortificatio_models.py
A mortificatio_phase1.md
A mortificatio_phase10_acceptance.md
A mortificatio_phase10_broker_feasibility.md
A mortificatio_phase10_runtime.md
A mortificatio_phase11_acceptance.md
A mortificatio_phase11_broker_validation.md
A mortificatio_phase11_runtime.md
A mortificatio_phase12_acceptance.md
A mortificatio_phase12_broker_lifecycle.md
A mortificatio_phase12_runtime_readiness.md
A mortificatio_phase13_connection_checklist.md
A mortificatio_phase13_execution_audit.md
A mortificatio_phase13_readiness.md
A mortificatio_phase14_connection_ready.md
A mortificatio_phase14_environment.md
A mortificatio_phase6_acceptance.md
A mortificatio_phase7_acceptance.md
A mortificatio_phase8_acceptance.md
A mortificatio_phase9_acceptance.md
A mortificatio_phase9_runtime.md
A mortificatio_pre4_experiment.py
A mortificatio_pre4_experiment_test.py
A mortificatio_protection.py
A mortificatio_read_only_check.py
A mortificatio_read_only_check_test.py
A mortificatio_risk_budget.py
A mortificatio_risk_budget_test.py
A mortificatio_risk_controls.py
A mortificatio_risk_controls_test.py
A mortificatio_runtime_audit.py
A mortificatio_runtime_audit_test.py
A mortificatio_shadow.py
A mortificatio_shadow_test.py
A mortificatio_shadow_web_test.py
A mortificatio_store.py
A mortificatio_test_authenticator.py
M offline_test_runner.py
M offline_test_runner_test.py
M order_boundary_test.py
A phase10_execution_quality_benchmarks.json
A phase10_runtime_audit.json
A phase11_exit_capacity_benchmarks.json
A phase11_runtime_audit.json
A phase12_protection_reconciliation_benchmarks.json
A phase12_runtime_audit.json
A phase8_synthetic_benchmarks.json
A phase9_compiled_feasibility.json
A phase9_readiness_baseline_benchmarks.json
A phase9_readiness_benchmarks.json
A phase9_runtime_audit.json
A phase9_worker_benchmarks.json
A requirements-security.lock
M simulation_evidence.py
A mortificatio_github_transfer.md
```
