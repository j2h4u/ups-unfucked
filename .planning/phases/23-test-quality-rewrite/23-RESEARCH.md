# Phase 23: Test Quality Rewrite - Research

**Researched:** 2026-03-20
**Domain:** Python test quality — pytest patterns, mock anti-patterns, dependency injection
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Incremental fix per anti-pattern, not wholesale file rewrite — preserve working tests, modify only what each TEST-0X requirement targets
- Each requirement (TEST-01 through TEST-09) maps to a discrete set of changes in specific files
- All 555 tests must pass after each incremental change — no batch rewrites that break the suite
- Replace mock call sequence assertions with assertions on observable state or return values
- Keep mocks at I/O boundaries only: NUT client, disk writes, systemd/journald, sd_notify
- In test_monitor_integration.py: replace internal mocks with real SagTracker, SchedulerManager, DischargeCollector instances
- Private helper assertions (`daemon._some_method.assert_called`) replaced with checking the state that method was supposed to produce
- Split only tests that verify multiple unrelated behaviors
- Don't split tests that verify related aspects of a single behavior
- Add optional `output_path` parameter to `write_virtual_ups_dev()` — defaults to production path, tests pass explicit temp path
- Remove `Path` patching from all test_virtual_ups.py tests
- Monte Carlo test marked `@pytest.mark.slow` with comment documenting seed=42 and expected runtime (~2-3s for 100 trials)
- All test_motd.py tests marked `@pytest.mark.integration` with comment: "Environment-dependent: requires bash, scripts/motd/51-ups.sh, subprocess execution"

### Claude's Discretion
- Exact test function names after splits
- Whether to consolidate shared setup into new fixtures or keep inline
- Order of test functions within files
- Whether to add helper functions for common assertion patterns

### Deferred Ideas (OUT OF SCOPE)
- None — discussion stayed within phase scope
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TEST-01 | Mock call sequence replay replaced with outcome assertions (test_monitor.py) | Lines 93, 128, 161, 910, 976 are the primary targets — all asserting `.call_count` on mocked private methods. Replacement pattern: assert observable state (write count via real side effects, event_type transitions) |
| TEST-02 | Eager test split into focused single-behavior tests (test_monitor.py) | `test_signal_handler_saves_model` and `test_ol_ob_ol_discharge_lifecycle_complete` are the primary candidates — each verifies multiple unrelated behaviors in a single function |
| TEST-03 | Fragile Path patching replaced with dependency injection (test_virtual_ups.py) | `write_virtual_ups_dev()` currently hardcodes `Path("/run/ups-battery-monitor/ups-virtual.dev")` — needs optional `output_path` parameter. 5 test methods use identical `patch("src.virtual_ups.Path", side_effect=...)` pattern |
| TEST-04 | Private helper assertions replaced with outcome assertions (test_monitor.py) | Lines 93, 128, 161, 910, 976 — asserting call counts on `daemon._write_virtual_ups`, `daemon._handle_event_transition`, `daemon._compute_metrics` |
| TEST-05 | Integration tests use real collaborators instead of internal mocks (test_monitor_integration.py) | `mock_daemon` fixture patches `NUTClient` but has no mocks for `SagTracker`/`SchedulerManager`/`DischargeCollector` — these are already real. `TestPollOnceCallChain` uses real collaborators correctly. The anti-pattern is the remaining `MagicMock()` replacements for internal objects in other test classes |
| TEST-06 | Monte Carlo test marked slow with seed dependency documented | `test_monte_carlo_convergence` at line 396 of test_capacity_estimator.py — no `@pytest.mark.slow` marker, no comment on seed/runtime |
| TEST-07 | test_motd.py marked as integration test (environment-dependent) | 4 tests in test_motd.py run `subprocess.run(['bash', ...])` — none have `@pytest.mark.integration` |
| TEST-08 | Tautological assertion replaced with content assertion | Lines 368 (`set_peukert_exponent.assert_called()` with no argument check), 1105 (`mock_ce.assert_called_once()` with no arg check), 1240 (`has_converged.assert_called()` with no arg check) |
| TEST-09 | Assertion roulette fixed with descriptive messages | Multiple `assert X is True/False` without `msg=` parameter — when one fails in a multi-assert function, pytest output shows only the line, not what was being checked |
</phase_requirements>

---

## Summary

Phase 23 is a pure test quality improvement: no production code changes except the `output_path` DI parameter on `write_virtual_ups_dev()`. The nine requirements map cleanly to specific anti-pattern categories, each with a bounded scope. The codebase is in excellent shape for this work — the extractions in phases 19-21 already created the real collaborator classes (SagTracker, SchedulerManager, DischargeCollector) that TEST-05 needs. The `TestPollOnceCallChain` class in test_monitor_integration.py is already written in the correct style and serves as the reference pattern.

The most impactful changes are TEST-01/TEST-04 (mock call sequence assertions in test_monitor.py) and TEST-05 (the remaining internal mocks in test_monitor_integration.py). TEST-03 requires a one-line production change. TEST-06 and TEST-07 are purely additive marker decorators. TEST-08 and TEST-09 are message quality improvements.

**Primary recommendation:** Work requirement by requirement in the order TEST-07, TEST-06, TEST-03, TEST-08, TEST-09, TEST-01/TEST-04, TEST-02, TEST-05 — simplest changes first to establish momentum and keep the suite green throughout.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | current (project) | Test runner | Already used across entire test suite |
| unittest.mock | stdlib | Mocking at I/O boundaries | Already used; no additional install needed |
| tempfile / tmp_path | stdlib | File-based DI | Already used in multiple files |

### No New Dependencies
This phase requires zero new packages. All patterns use existing stdlib and pytest primitives already present in the project.

---

## Architecture Patterns

### Recommended Project Structure (no changes)
The existing structure stays. Only file content changes.

### Pattern 1: Observable State Assertion (replaces call count)

**What:** Instead of asserting a mock was called N times, assert the state change the method was supposed to produce.

**When to use:** Any time a test has `mock.call_count == N` on a private method that is mocked only to observe it.

**Example — current anti-pattern (TEST-01, lines 93-94):**
```python
# BAD: call_count on mocked private method
assert daemon._write_virtual_ups.call_count == 7, \
    f"Expected 7 writes (1 OL + 6 OB), got {daemon._write_virtual_ups.call_count}"
```

**Example — replacement using real side effects:**
```python
# GOOD: Track writes via a counter attached to the real file path or a list
write_log = []
daemon._write_virtual_ups = lambda: write_log.append(daemon.current_metrics.event_type)
# ... run polls ...
ob_writes = [e for e in write_log if e == EventType.BLACKOUT_REAL]
ol_writes = [e for e in write_log if e == EventType.ONLINE]
assert len(ob_writes) == 6, "Expected OB writes on every poll during blackout"
assert len(ol_writes) == 1, "Expected one OL write (poll 0 only)"
```

### Pattern 2: Dependency Injection for File Output (TEST-03)

**What:** Add `output_path: Optional[Path] = None` parameter to `write_virtual_ups_dev()`. When `None`, use production path. Tests pass explicit `tmp_path`.

**Current signature:**
```python
def write_virtual_ups_dev(metrics: Dict[str, Any], ups_name: str = "cyberpower") -> None:
    virtual_ups_path = Path("/run/ups-battery-monitor/ups-virtual.dev")
```

**New signature:**
```python
def write_virtual_ups_dev(
    metrics: Dict[str, Any],
    ups_name: str = "cyberpower",
    output_path: Optional[Path] = None,
) -> None:
    virtual_ups_path = output_path or Path("/run/ups-battery-monitor/ups-virtual.dev")
```

**Test replacement — current pattern (5 occurrences, test_virtual_ups.py lines 40-42, 84-86, 138-140, 185-187, 344-346):**
```python
# BAD: fragile Path class patching
with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
    test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
)):
    write_virtual_ups_dev(metrics)
```

**Replacement:**
```python
# GOOD: explicit DI parameter
test_file = tmp_path / "ups-virtual.dev"
write_virtual_ups_dev(metrics, output_path=test_file)
assert test_file.exists()
```

### Pattern 3: Real Collaborator Instantiation (TEST-05)

**What:** In integration tests, instantiate SagTracker, SchedulerManager, DischargeCollector with real config rather than assigning `MagicMock()`.

**Constructor signatures discovered from source:**

```python
# SagTracker (src/sag_tracker.py)
SagTracker(
    battery_model: BatteryModel,
    rls_ir_k: ScalarRLS,
    ir_k: float,
)

# SchedulerManager (src/scheduler_manager.py)
SchedulerManager(
    battery_model: BatteryModel,
    nut_client,           # NUTClient — can remain mocked (I/O boundary)
    scheduling_config: SchedulingConfig,
    discharge_handler,    # DischargeHandler
)

# DischargeCollector (src/discharge_collector.py)
DischargeCollector(
    battery_model: BatteryModel,
    config: Config,
    discharge_handler,    # DischargeHandler — for discharge_predicted_runtime handoff
    ema_filter,           # EMAFilter
)
```

**Key insight:** The `mock_daemon` fixture in test_monitor_integration.py already creates a real MonitorDaemon and therefore already instantiates real SagTracker, SchedulerManager, DischargeCollector as part of `__init__`. The TEST-05 work is about eliminating the lines that *replace* these with MagicMocks after construction:
```python
# BAD — found in various TestPollOnceCallChain tests:
d.discharge_collector._write_calibration_points = MagicMock()  # line 678 — this is I/O, OK to mock
```

The `_write_calibration_points` replacement at line 678 is actually correct — it's disk I/O through battery_model. What to look for: assignments like `daemon.sag_tracker = MagicMock()` or `daemon.scheduler_manager = MagicMock()` that replace an already-real collaborator with a mock.

**Finding from code review:** The main anti-pattern in test_monitor_integration.py is in the older `TestOrchestratorWiring` and `TestRLSCalibrationIntegration` classes, which use the `mock_daemon` fixture (patching only NUTClient) but then assign `MagicMock()` to `daemon.capacity_estimator` and `daemon.discharge_handler.*`. These are domain-level replacements, not I/O boundary mocks.

### Pattern 4: pytest.mark Registration

**Current pytest.ini markers section:**
```ini
markers =
    integration: marks tests requiring full daemon integration (deselect with '-m "not integration"')
```

**Required addition:**
```ini
markers =
    integration: marks tests requiring full daemon integration (deselect with '-m "not integration"')
    slow: marks tests as slow-running (deselect with '-m "not slow"')
```

### Pattern 5: Marker Application

**TEST-06 — Monte Carlo test (test_capacity_estimator.py, line 396):**
```python
# Before:
def test_monte_carlo_convergence(self, synthetic_discharge):

# After:
@pytest.mark.slow
def test_monte_carlo_convergence(self, synthetic_discharge):
    """
    Validation Gate 2: Monte Carlo convergence verification.
    ...
    Seed: random.seed(42) — deterministic, ~2-3s for 100 trials.
    Mark: @pytest.mark.slow — exclude from fast CI with '-m "not slow"'.
    """
```

**TEST-07 — MOTD tests (test_motd.py, 4 functions):**
```python
# Apply to all 4 test functions:
@pytest.mark.integration
def test_motd_capacity_displays(model_json_with_capacity):
    """MOTD integration: ...
    Environment-dependent: requires bash, scripts/motd/51-ups.sh, subprocess execution.
    """
```

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Counting writes during tests | Custom event bus / observer | Lambda side_effect that appends to list | Simpler, already the pattern for `call_order` tracking in test_f11_watchdog_after_critical_writes |
| Verifying write content | Re-implement NUT format parser in test | Write to tmp_path and read back | Already done in the better tests; DI makes this trivial |
| Marker filtering | Custom skip logic | `@pytest.mark.slow` + pytest.ini registration | One line, standard pytest |

---

## Common Pitfalls

### Pitfall 1: Splitting a Test That Has Related Assertions
**What goes wrong:** TEST-02 asks to split tests with multiple unrelated behaviors. If a developer splits a test that checks "before transition" and "after transition" state, they create two tests that each need more setup, and the narrative of the sequence is lost.
**Why it happens:** "One assertion per test" is a common misreading of the principle.
**How to avoid:** The rule is "one *behavior* per test." Checking buffer state before and after a single OB→OL transition is one behavior. Checking that a signal handler saves the model AND sets `running=False` AND is idempotent across multiple signals are three behaviors.
**Warning signs:** A split test that only makes sense when read alongside its sibling.

### Pitfall 2: Removing Mocks That Should Stay
**What goes wrong:** TEST-05 says "use real collaborators" — a developer removes the `patch('src.monitor.NUTClient')` wrapper and causes tests to attempt a real NUT TCP connection.
**Why it happens:** Overzealous application of the "no mocks" principle.
**How to avoid:** Keep mocks at I/O boundaries: NUTClient, sd_notify, time.sleep, write_health_endpoint, write_virtual_ups_dev, safe_save. Only remove mocks on domain-logic collaborators (SagTracker, SchedulerManager, DischargeCollector).
**Warning signs:** Test tries to connect to localhost:3493 and hangs or ConnectionRefused.

### Pitfall 3: output_path DI Breaks Symlink Guard
**What goes wrong:** The new `output_path` parameter bypasses the symlink check in `write_virtual_ups_dev()` if the guard uses the hardcoded path.
**Why it happens:** Guard reads `Path("/run/...")` before the parameter is applied.
**How to avoid:** Apply the symlink guard to `virtual_ups_path` (which is now `output_path or Path(...)`) — already the case if the assignment happens before the guard.
**Warning signs:** Symlink attack test (if it exists) fails after the refactor.

### Pitfall 4: pytest.mark.slow Warning Without Registration
**What goes wrong:** Applying `@pytest.mark.slow` without adding it to pytest.ini produces a `PytestUnknownMarkWarning` and the marker doesn't filter correctly.
**Why it happens:** pytest validates markers against the registered list.
**How to avoid:** Add `slow: ...` to pytest.ini `markers =` section before (or in the same commit as) adding the decorator.
**Warning signs:** `PytestUnknownMarkWarning: Unknown pytest.mark.slow` in test output.

### Pitfall 5: Assertion Roulette Fixes That Over-Specify
**What goes wrong:** Adding `msg=` to assertions in TEST-09 causes a developer to write assertions that embed the expected value, making the message wrong when the expected value changes.
**Why it happens:** Copy-paste from the assertion itself.
**How to avoid:** Messages should describe *what* is being checked ("online flag after OL poll") not what value is expected. The assertion already shows the expected value.

---

## Code Examples

Verified patterns from the existing codebase (reference implementations):

### Already-Correct Pattern: TestPollOnceCallChain (test_monitor_integration.py lines 697-810)
This class is the gold standard — I/O mocked, collaborators real, assertions on state:
```python
# Source: tests/test_monitor_integration.py lines 737-747
def test_ol_to_ob_starts_discharge(self, daemon):
    """OL→OB: discharge collection starts, cycle count increments."""
    self._poll(daemon, status='OL', voltage=13.0, input_voltage=230.0)
    initial_cycles = daemon.battery_model.get_cycle_count()

    self._poll(daemon, status='OB DISCHRG', voltage=12.0, input_voltage=0.0)

    assert daemon.current_metrics.event_type == EventType.BLACKOUT_REAL
    assert daemon.discharge_collector.discharge_buffer.collecting
    assert len(daemon.discharge_collector.discharge_buffer.voltages) == 1
    assert daemon.battery_model.get_cycle_count() == initial_cycles + 1
```
Note: no `assert_called`, no `call_count` — only state.

### Already-Correct Pattern: call_order tracking (test_monitor.py lines 1023-1081)
The `test_f11_watchdog_after_critical_writes` test uses a list to track ordering without asserting call counts on mocks:
```python
# Source: tests/test_monitor.py lines 1023-1031
call_order = []

def mock_health_endpoint(*args, **kwargs):
    call_order.append('health_endpoint')

def mock_virtual_ups(*args, **kwargs):
    call_order.append('virtual_ups')
```

### Tautological → Content Assertion

**TEST-08, line 368 — current:**
```python
daemon.battery_model.set_peukert_exponent.assert_called()
```
**Replacement (check the value was actually set):**
```python
call_args = daemon.battery_model.set_peukert_exponent.call_args
assert call_args is not None, "set_peukert_exponent was not called"
exponent_set = call_args.args[0] if call_args.args else call_args.kwargs.get('value')
assert 1.0 <= exponent_set <= 1.4, \
    f"Peukert exponent {exponent_set} outside physical bounds [1.0, 1.4]"
```

**TEST-08, line 1105 — current:**
```python
mock_ce.assert_called_once()
```
**Replacement:**
```python
assert mock_ce.call_count == 1, "CapacityEstimator should be instantiated exactly once"
call_args = mock_ce.call_args
# Verify it was called with config-derived params, not arbitrary values
```

**TEST-08, line 1240 — current:**
```python
daemon.capacity_estimator.has_converged.assert_called()
```
**Replacement:**
```python
# has_converged() was called to make a convergence decision; verify the decision's effect
convergence_status = daemon.battery_model.get_convergence_status()
assert convergence_status is not None, "Convergence check should update model state"
```

---

## Anti-Pattern Inventory

Exact locations of each anti-pattern for the planner:

### TEST-01 / TEST-04: Mock call sequence assertions on private methods (test_monitor.py)
| Line | Mock target | Anti-pattern type |
|------|------------|-------------------|
| 93-94 | `daemon._write_virtual_ups.call_count` | private method call count |
| 128-129 | `daemon._handle_event_transition.call_count` | private method call count |
| 161-162 | `daemon._write_virtual_ups.call_count` | private method call count |
| 910-911 | `daemon._compute_metrics.call_count` | private method call count |
| 976-978 | `daemon._handle_event_transition.call_count` | private method call count |
| 368 | `set_peukert_exponent.assert_called()` | no-arg assert_called (TEST-08 also) |
| 439 | `battery_model.save.assert_called_once()` | assert_called_once (acceptable if verifying persistence was triggered — verify with content check) |
| 602-603 | `add_soh_history_entry.assert_called_once()` | mixed: call count + no content check |
| 657 | `add_soh_history_entry.call_count == 2` | call count on domain mock |

**Distinction:** `assert_called_once()` on `battery_model.save` is borderline — it's checking persistence happened. Acceptable if reformulated as "model was persisted", e.g., by checking `model_path.exists()` or that model state was updated. But the context determines whether this is really needed.

### TEST-01 / TEST-04: test_monitor_integration.py
| Line | Mock target | Anti-pattern type |
|------|------------|-------------------|
| 761 | `discharge_collector._write_calibration_points.call_count` | private method call count |
| 783 | `_update_battery_health.assert_called()` | private mock assert (acceptable mock since _update_battery_health is explicitly mocked as a subsystem boundary in this fixture) |

**Note:** Line 783 (`daemon._update_battery_health.assert_called()`) is actually legitimate — the fixture comment says `_update_battery_health` is explicitly mocked as a complex subsystem boundary. This one should be retained as-is or converted to a state check (buffer was cleared, which the side_effect already does).

### TEST-02: Tests with multiple unrelated behaviors (test_monitor.py)
| Function | Multiple behaviors | Split into |
|----------|-------------------|-----------|
| `test_signal_handler_saves_model` (lines 417-451) | 1) SIGTERM saves model and stops, 2) Multiple signals are idempotent | `test_signal_handler_saves_model_and_stops`, `test_signal_handler_idempotent` |
| `test_ol_ob_ol_discharge_lifecycle_complete` (lines 454-657) | 1) OL→OB starts collection, 2) OB samples accumulate, 3) OB→OL triggers health update + clears buffer, 4) Second cycle starts fresh | Already very granular assertions — check if the whole function is really one scenario or truly multi-behavior. The TestPollOnceCallChain already covers most of these behaviors with real collaborators. May be better to mark this test for replacement by referencing TestPollOnceCallChain equivalents. |

### TEST-03: Path patching occurrences (test_virtual_ups.py)
All 5 occurrences follow the same pattern:
- Line 40-42: `test_write_to_tmpfs`
- Line 84-86: `test_passthrough_fields`
- Line 138-140: `test_field_overrides`
- Line 185-187: `test_nut_format_compliance`
- Line 344-346: `test_monitor_virtual_ups_integration`

All can be mechanically replaced with `output_path=tmp_path / "ups-virtual.dev"` after the DI parameter is added. The fixture `tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_")` can be replaced with pytest's built-in `tmp_path`.

### TEST-05: Internal mocks in test_monitor_integration.py
The `mock_daemon` fixture (line 57-65) patches only `NUTClient` — this is correct. The collaborators (SagTracker, SchedulerManager, DischargeCollector) are real. The anti-pattern is in test methods that then do:
```python
daemon.capacity_estimator = MagicMock()
daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
```
These are domain-level object replacements, not I/O mocks. For tests that need to control capacity estimation output (e.g., `test_handle_discharge_complete_calls_estimator`), the correct approach depends on whether the test is verifying orchestration (use real estimator with synthetic input) or boundary contract (mock at the interface).

### TEST-08: Tautological assertions
| Location | Current | Problem |
|----------|---------|---------|
| test_monitor.py:368 | `set_peukert_exponent.assert_called()` | No argument check — doesn't verify correct value |
| test_monitor.py:1105 | `mock_ce.assert_called_once()` | No argument check |
| test_monitor.py:1240 | `has_converged.assert_called()` | No argument check — doesn't verify convergence decision flowed |

### TEST-09: Assertion roulette (no descriptive messages)
The primary candidates are in the multi-assertion tests like `test_health_endpoint_capacity_fields` (lines 1672-1689) which has 10+ assertions without `msg=`. Pattern to apply:
```python
# Before:
assert 'capacity_ah_measured' in data

# After:
assert 'capacity_ah_measured' in data, "capacity_ah_measured field missing from health endpoint JSON"
```

---

## State of the Art

| Old Approach | Current Approach | Reason |
|--------------|------------------|--------|
| Mock all collaborators to isolate unit | Mock only I/O boundaries, real collaborators | Extracted modules (phases 19-21) are now independently tested — redundant mocking adds fragility |
| `Path` class patching for file output | `output_path` DI parameter | DI is explicit, refactor-safe, and doesn't intercept all Path calls |
| `assert_called_once()` to verify side effects | Assert the state/output the method was supposed to produce | State assertions survive internal refactors; call count assertions couple tests to implementation |

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (version from pyproject.toml dev deps) |
| Config file | `pytest.ini` |
| Quick run command | `pytest tests/ -x -q --tb=short` |
| Full suite command | `pytest tests/ -v --tb=short` |
| Slow tests excluded | `pytest tests/ -m "not slow"` |
| Integration excluded | `pytest tests/ -m "not integration"` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TEST-01 | test_monitor.py assertions check state not call counts | unit | `pytest tests/test_monitor.py -x -q` | ✅ (modifying) |
| TEST-02 | Single-behavior test functions | unit | `pytest tests/test_monitor.py -x -q` | ✅ (splitting) |
| TEST-03 | write_virtual_ups_dev accepts output_path | unit | `pytest tests/test_virtual_ups.py -x -q` | ✅ (modifying) |
| TEST-04 | No private method assertions | unit | `pytest tests/test_monitor.py -x -q` | ✅ (modifying) |
| TEST-05 | Real collaborators in integration tests | integration | `pytest tests/test_monitor_integration.py -x -q` | ✅ (modifying) |
| TEST-06 | Monte Carlo marked @pytest.mark.slow | unit | `pytest tests/test_capacity_estimator.py -x -q` | ✅ (modifying) |
| TEST-07 | MOTD tests marked @pytest.mark.integration | integration | `pytest tests/test_motd.py -x -q` | ✅ (modifying) |
| TEST-08 | Content assertions on previously bare assert_called() | unit | `pytest tests/test_monitor.py -x -q` | ✅ (modifying) |
| TEST-09 | Descriptive messages on multi-assert tests | unit | `pytest tests/test_monitor.py -x -q` | ✅ (modifying) |

### Sampling Rate
- **Per task commit:** `pytest tests/ -x -q --tb=short -m "not slow"` (skips Monte Carlo)
- **Per wave merge:** `pytest tests/ -v --tb=short`
- **Phase gate:** Full suite green (`pytest tests/ --tb=short`) before `/gsd:verify-work`

### Wave 0 Gaps
None — existing test infrastructure covers all phase requirements. The only infrastructure change is adding `slow` to pytest.ini markers.

---

## Dependency Order

The nine requirements have the following dependency structure:

```
TEST-07 (motd markers)        — independent, additive only
TEST-06 (slow marker)         — requires pytest.ini update first
TEST-03 (DI for virtual_ups)  — requires src/virtual_ups.py change first, then test changes
TEST-08 (tautological asserts) — independent per occurrence
TEST-09 (assertion messages)   — independent per occurrence
TEST-04 (private assertions)   — overlaps with TEST-01; do together
TEST-01 (call sequence replay) — same lines as TEST-04; do in one pass
TEST-02 (test splits)          — after TEST-01/TEST-04 so split tests are already clean
TEST-05 (real collaborators)   — last; after all unit test work settled
```

Recommended wave grouping:
- **Wave 1:** pytest.ini + TEST-07 + TEST-06 (markers, pure additions, zero risk)
- **Wave 2:** TEST-03 (production change + test rewrites)
- **Wave 3:** TEST-08 + TEST-09 (assertion quality, low risk)
- **Wave 4:** TEST-01 + TEST-04 (mock replacement, medium risk)
- **Wave 5:** TEST-02 + TEST-05 (structural changes, highest risk)

---

## Open Questions

1. **test_ol_ob_ol_discharge_lifecycle_complete (TEST-02)**
   - What we know: 200-line test with 6 polling scenarios and 12+ assertions across two full OB cycles
   - What's unclear: Whether to split this test or recognize that `TestPollOnceCallChain` already covers these behaviors and this test should be *deleted* rather than split
   - Recommendation: Check if TestPollOnceCallChain.test_full_blackout_cycle + test_ob_accumulates_samples + test_ob_ol_ob_resumes_collection together cover the same ground. If yes, delete the large test rather than split it.

2. **TEST-05 scope: which mocks to remove in test_monitor_integration.py**
   - What we know: `capacity_estimator = MagicMock()` and `discharge_handler.capacity_estimator = ...` assignments appear in TestCapacityEstimatorIntegration class (in test_monitor.py, not test_monitor_integration.py)
   - What's unclear: The CONTEXT.md says TEST-05 targets test_monitor_integration.py — the TestCapacityEstimatorIntegration class is actually in test_monitor.py
   - Recommendation: Planner should verify: `grep -n "capacity_estimator = MagicMock" tests/test_monitor_integration.py` to confirm scope before planning

---

## Sources

### Primary (HIGH confidence)
- Direct code inspection of all specified test files (test_monitor.py 1829 LOC, test_monitor_integration.py 948 LOC, test_virtual_ups.py 439 LOC, test_motd.py 325 LOC, test_capacity_estimator.py line 396)
- Source files: src/virtual_ups.py, src/sag_tracker.py, src/scheduler_manager.py, src/discharge_collector.py
- pytest.ini — confirmed `integration` marker registered, `slow` marker NOT registered
- pyproject.toml — no pytest configuration there, all config in pytest.ini

### Secondary (MEDIUM confidence)
- CONTEXT.md — user decisions and canonical references verified against code

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new dependencies, all patterns from existing code
- Architecture: HIGH — patterns verified directly from production source and existing good tests
- Pitfalls: HIGH — derived from direct code inspection, not speculation
- Anti-pattern inventory: HIGH — exact line numbers from grep and manual read

**Research date:** 2026-03-20
**Valid until:** Stable (no fast-moving libraries; all Python stdlib + pytest patterns)
