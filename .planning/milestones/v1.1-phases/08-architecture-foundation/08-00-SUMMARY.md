---
phase: 08-architecture-foundation
plan: 00
subsystem: test-infrastructure
tags: [fixtures, dataclass, testing, wave-0]
dependency_graph:
  requires: []
  provides: [current_metrics_fixture, config_fixture, test_stubs_for_arch_01_02_03]
  affects: [Wave 1 implementation (ARCH-01, ARCH-02, ARCH-03)]
tech_stack:
  added: [pytest.fixture decorators, dataclass type hints in docstrings]
  patterns: [fixture reuse pattern, skip-based wave progression]
key_files:
  created: []
  modified: [tests/conftest.py, tests/test_monitor.py]
decisions:
  - "Fixtures return dicts until Wave 1 (ARCH-01, ARCH-02 create dataclasses)"
  - "Test stubs use pytest.skip() to maintain Nyquist compliance (no phase gates)"
metrics:
  duration_minutes: 8
  completed_date: "2026-03-15"
  tasks_completed: 3
  files_modified: 2
---

# Phase 8 Plan 00: Wave 0 Test Infrastructure Summary

**One-liner:** Establish pytest fixture contracts and test stubs for Phase 8 dataclass refactoring, enabling Wave 1 tasks to focus on implementation.

---

## Objective

Prepare Wave 0 test infrastructure for Phase 8 dataclass refactoring (ARCH-01, ARCH-02, ARCH-03).

**Purpose:** Establish fixture contracts and test stubs so Wave 1 tasks can focus on implementation without building test infrastructure.

**Output:** Reusable fixtures + test stubs in place

---

## Tasks Completed

| Task | Name | Status | Commit |
|------|------|--------|--------|
| 0.1 | Create CurrentMetrics and Config fixtures in conftest.py | ✅ DONE | 0c6c800 |
| 0.2 | Create test stubs for dataclass validation (ARCH-01, ARCH-02, ARCH-03) | ✅ DONE | 0c6c800 |
| 0.3 | Commit Wave 0 test infrastructure | ✅ DONE | 0c6c800 |

---

## Detailed Work

### Task 0.1: Current Metrics and Config Fixtures

**File:** `tests/conftest.py`

Added two new pytest fixtures:

#### `current_metrics_fixture()`
Returns a CurrentMetrics-shaped dict with default test values:
- `soc`: 0.75 (typical mid-charge state)
- `battery_charge`: 75.0 (%)
- `time_rem_minutes`: 30.0 (typical runtime estimate)
- `event_type`: EventType.ONLINE
- `transition_occurred`: False
- `shutdown_imminent`: False
- `ups_status_override`: None
- `previous_event_type`: EventType.ONLINE
- `timestamp`: datetime.now()

This fixture is reusable across all test suites. Once ARCH-01 creates the `CurrentMetrics` dataclass in Wave 1, tests will import that class directly instead of using this dict.

#### `config_fixture(tmp_path)`
Returns a Config-shaped dict with typical test values:
- `ups_name`: "test-cyberpower"
- `polling_interval`: 10 (seconds)
- `reporting_interval`: 60 (seconds)
- `nut_host`: "localhost"
- `nut_port`: 3493
- `nut_timeout`: 2.0 (seconds)
- `shutdown_minutes`: 5
- `soh_alert_threshold`: 0.80
- `model_dir`: tmp_path / "test_model" (temporary, isolated per test)
- `config_dir`: tmp_path / "test_config"
- `runtime_threshold_minutes`: 20
- `reference_load_percent`: 20.0
- `ema_window_sec`: 120

This fixture leverages pytest's `tmp_path` fixture for test isolation. Once ARCH-02 creates the `Config` frozen dataclass in Wave 1, tests will import that class directly.

### Task 0.2: Test Stubs

**File:** `tests/test_monitor.py`

Added three test function stubs at the end of the file:

#### `test_current_metrics_dataclass()`
```python
pytest.skip("Wave 1: ARCH-01 — CurrentMetrics dataclass not yet created")
```
**Purpose:** Validate CurrentMetrics instantiation and field types once ARCH-01 creates the dataclass.

#### `test_config_dataclass()`
```python
pytest.skip("Wave 1: ARCH-02 — Config dataclass not yet created")
```
**Purpose:** Validate Config instantiation and field types once ARCH-02 creates the dataclass.

#### `test_config_immutability()`
```python
pytest.skip("Wave 1: ARCH-02 — Config dataclass not yet created")
```
**Purpose:** Verify `frozen=True` semantics—test that attempting to mutate a Config field raises `FrozenInstanceError`.

**Nyquist Compliance:** All stubs use `pytest.skip()` to prevent blocking Wave 0 gate. Running `pytest tests/test_monitor.py -v` shows all three tests as SKIPPED (expected).

### Task 0.3: Commit

**Commit:** `0c6c800`
**Message:** test(08-00): add test fixtures and stubs for Phase 8 dataclass refactoring

---

## Verification

### Checklist
- [x] `tests/conftest.py` has `current_metrics_fixture()` and `config_fixture()`
- [x] `tests/test_monitor.py` has three test stubs (all SKIPPED)
- [x] All files committed to git
- [x] `pytest tests/test_monitor.py::test_current_metrics_dataclass -v` shows SKIPPED
- [x] `pytest tests/test_monitor.py::test_config_dataclass -v` shows SKIPPED
- [x] `pytest tests/test_monitor.py::test_config_immutability -v` shows SKIPPED

### Test Output
```
tests/test_monitor.py::test_current_metrics_dataclass SKIPPED (Wave 1: ARCH-01 — CurrentMetrics dataclass not yet created)
tests/test_monitor.py::test_config_dataclass SKIPPED (Wave 1: ARCH-02 — Config dataclass not yet created)
tests/test_monitor.py::test_config_immutability SKIPPED (Wave 1: ARCH-02 — Config dataclass not yet created)
```

All stubs properly marked and no existing tests broken.

---

## Design Decisions

1. **Fixtures return dicts, not dataclasses (yet):**
   - Fixtures must work before ARCH-01/ARCH-02 create the dataclasses.
   - Using dict with typed field names and docstrings provides the schema contract.
   - When Wave 1 creates `CurrentMetrics` and `Config` dataclasses, tests will import them directly instead of using fixture dicts.

2. **Test stubs use `pytest.skip()` for Nyquist compliance:**
   - No pending assertions ("TODO:" comments).
   - No exceptions or failures that could block phase gates.
   - All three tests are SKIPPED (expected until Wave 1).
   - Ensures clean progression: Wave 0 → Wave 1 gate passes → ARCH-01/02/03 implement.

3. **Fixture names end in `_fixture()`:**
   - Clarifies that these are pytest fixtures returning test data.
   - Distinguishes from actual dataclass names (CurrentMetrics, Config) that will be created in ARCH-01/ARCH-02.

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Impact on Wave 1

Wave 1 (ARCH-01, ARCH-02, ARCH-03) will:

1. **ARCH-01:** Implement `CurrentMetrics` dataclass. Tests will then import it and use the fixture.
2. **ARCH-02:** Implement `Config` frozen dataclass. Tests will then import it and use the fixture.
3. **Test stubs** become active: replace `pytest.skip()` with assertions validating the dataclasses.

---

## Self-Check

- [x] File `tests/conftest.py` exists with two fixtures
- [x] File `tests/test_monitor.py` exists with three test stubs
- [x] Commit `0c6c800` exists in git history
- [x] No syntax errors (`python3 -m py_compile tests/conftest.py` passes)
- [x] All three tests are SKIPPED (expected for Wave 0)
