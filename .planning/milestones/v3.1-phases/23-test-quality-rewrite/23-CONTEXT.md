# Phase 23: Test Quality Rewrite - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Rewrite test suite to assert observable outcomes via dependency injection. Eliminate mock call sequence replay, private method assertions, and tautological checks. No new features, no behavior changes — pure test quality improvement. Scope: test_monitor.py, test_monitor_integration.py, test_virtual_ups.py, test_motd.py, and the Monte Carlo test in test_capacity_estimator.py.

</domain>

<decisions>
## Implementation Decisions

### Rewrite Scope & Strategy
- Incremental fix per anti-pattern, not wholesale file rewrite — preserve working tests, modify only what each TEST-0X requirement targets
- Each requirement (TEST-01 through TEST-09) maps to a discrete set of changes in specific files
- All 555 tests must pass after each incremental change — no batch rewrites that break the suite

### Mock Replacement Approach (TEST-01, TEST-04, TEST-05)
- Replace mock call sequence assertions (`assert_called_once`, `call_count`, `assert_called_with`) with assertions on observable state or return values
- Keep mocks at I/O boundaries only: NUT client, disk writes, systemd/journald, sd_notify
- In test_monitor_integration.py: replace internal mocks with real `SagTracker`, `SchedulerManager`, `DischargeCollector` instances — these modules were extracted in phases 19-21 specifically to enable this
- Private helper assertions (`daemon._some_method.assert_called`) replaced with checking the state that method was supposed to produce

### Test Splitting (TEST-02)
- Split only tests that verify multiple unrelated behaviors (e.g., a test checking both SoH update AND buffer clearing AND model save in one function)
- Don't split tests that verify related aspects of a single behavior (e.g., checking voltage samples before and after a transition is one behavior)
- Each resulting test function exercises a single behavior with a descriptive name

### Dependency Injection for Virtual UPS (TEST-03)
- Add optional `output_path` parameter to `write_virtual_ups_dev()` — defaults to production path, tests pass explicit temp path
- Remove `Path` patching (`patch("src.virtual_ups.Path", side_effect=...)`) from all test_virtual_ups.py tests
- Backward compatible — existing callers without the parameter get production behavior

### Test Markers (TEST-06, TEST-07)
- Monte Carlo test (`test_monte_carlo_convergence`) marked `@pytest.mark.slow` with comment documenting seed=42 and expected runtime (~2-3s for 100 trials)
- All test_motd.py tests marked `@pytest.mark.integration` with comment: "Environment-dependent: requires bash, scripts/motd/51-ups.sh, subprocess execution"

### Tautological & Assertion Quality (TEST-08, TEST-09)
- Replace tautological assertions (assert X == X, assert True, assert mock.called after explicit call) with content assertions that verify actual behavior
- Add descriptive messages to assertion roulette cases — tests with multiple assertions that lack context about which check failed

### Claude's Discretion
- Exact test function names after splits
- Whether to consolidate shared setup into new fixtures or keep inline
- Order of test functions within files
- Whether to add helper functions for common assertion patterns

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` §Tests — TEST-01 through TEST-09 define each anti-pattern to fix

### Target test files
- `tests/test_monitor.py` — 1829 LOC, ~42 tests. Primary target for TEST-01 (mock sequences), TEST-02 (eager splits), TEST-04 (private assertions), TEST-08 (tautological), TEST-09 (assertion roulette)
- `tests/test_monitor_integration.py` — 948 LOC, ~27 tests. Primary target for TEST-05 (real collaborators)
- `tests/test_virtual_ups.py` — 439 LOC, ~16 tests. Primary target for TEST-03 (DI for path)
- `tests/test_motd.py` — 325 LOC, 4 tests. Primary target for TEST-07 (integration marker)
- `tests/test_capacity_estimator.py` line 396 — Monte Carlo test. Primary target for TEST-06 (slow marker)

### Real collaborator modules (for TEST-05 DI)
- `src/sag_tracker.py` — Extracted in Phase 19, has own unit tests
- `src/scheduler_manager.py` — Extracted in Phase 20, has own unit tests
- `src/discharge_collector.py` — Extracted in Phase 21, has own unit tests

### Existing test patterns (reference for consistency)
- `tests/test_discharge_event_logging.py` — Uses `@pytest.mark.integration` marker pattern
- `tests/test_health_endpoint_v16.py` — Uses `@pytest.mark.integration` marker pattern
- `tests/conftest.py` — Shared fixtures including `daemon_config`

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `conftest.py` `daemon_config` fixture — already used by `make_daemon`, can be extended for real-collaborator integration tests
- `@pytest.mark.integration` — already registered and used in 22 tests across 3 files
- Real collaborator classes (`SagTracker`, `SchedulerManager`, `DischargeCollector`) — all have clean constructors accepting Config, suitable for direct instantiation in tests
- `tempfile.TemporaryDirectory` — already used in multiple test files for file-based tests

### Established Patterns
- Integration tests use `@pytest.mark.integration` decorator (test_discharge_event_logging.py, test_health_endpoint_v16.py, test_sulfation_persistence.py)
- File-based tests use `tmp_path` or `tempfile.TemporaryDirectory` fixtures
- MOTD tests use subprocess with `HOME` env override — this pattern stays, just gets the marker
- `make_daemon` fixture creates MonitorDaemon with patched NUT/systemd — this stays for unit tests, integration tests use a different fixture with real collaborators

### Integration Points
- `write_virtual_ups_dev()` in `src/virtual_ups.py` needs `output_path` parameter added (TEST-03)
- `conftest.py` may need a new fixture for integration-mode daemon with real collaborators (TEST-05)
- `pyproject.toml` or `conftest.py` may need `@pytest.mark.slow` registration

### Anti-Pattern Inventory (from codebase scout)
- 28 mock call sequence assertions in test_monitor.py (`assert_called_once`, `call_count`, etc.)
- 9 mock/patch usages in test_monitor_integration.py (despite being "integration" tests)
- Path patching via `side_effect` lambda in test_virtual_ups.py (fragile, 3+ occurrences)
- Monte Carlo test at line 396 in test_capacity_estimator.py — no `@pytest.mark.slow`
- test_motd.py — 4 subprocess-based tests, no `@pytest.mark.integration`
- 20 boolean assertions in test_monitor.py (`assert X is True/False/None`)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

### Reviewed Todos (not folded)
- "Anti-sulfation deep discharge scheduling for battery longevity" — relevance score 0.2, out of scope for test quality phase (belongs to v3.0 domain)

</deferred>

---

*Phase: 23-test-quality-rewrite*
*Context gathered: 2026-03-20*
