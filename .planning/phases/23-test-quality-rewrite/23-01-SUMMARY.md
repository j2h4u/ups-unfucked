---
phase: 23-test-quality-rewrite
plan: "01"
subsystem: testing
tags: [pytest, markers, slow, integration, monte-carlo, motd]

requires:
  - phase: 22-naming-docs-sweep
    provides: stable module naming after decomposition — tests reference final names

provides:
  - pytest.ini slow marker registration
  - "@pytest.mark.slow on Monte Carlo test (test_monte_carlo_convergence)"
  - "@pytest.mark.integration on all 4 test_motd.py functions"

affects:
  - CI pipelines (can now filter with -m "not slow" or -m "not integration")
  - 23-02 and subsequent plans in phase 23

tech-stack:
  added: []
  patterns:
    - "Marker-based test filtering: slow tests excluded with '-m \"not slow\"', integration excluded with '-m \"not integration\"'"
    - "Environment-dependency comment on integration tests: comment co-located with marker"

key-files:
  created: []
  modified:
    - pytest.ini
    - tests/test_capacity_estimator.py
    - tests/test_motd.py

key-decisions:
  - "grep -c 'pytest.mark.slow' returns 2 (decorator + docstring mention) — functionally correct, decorator at line 396 is the actual marker"

patterns-established:
  - "Integration test pattern: @pytest.mark.integration + '# Environment-dependent: requires bash, scripts/motd/51-ups.sh, subprocess execution'"
  - "Slow test pattern: @pytest.mark.slow + docstring noting seed and ~runtime"

requirements-completed: [TEST-06, TEST-07]

duration: 4min
completed: "2026-03-20"
---

# Phase 23 Plan 01: Test Marker Registration Summary

**pytest.ini slow marker registered, Monte Carlo test marked @pytest.mark.slow, all 4 MOTD tests marked @pytest.mark.integration — enabling -m "not slow" (554/555) and -m "not integration" (529/555) filtering**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-03-20T14:04:34Z
- **Completed:** 2026-03-20T14:08:00Z
- **Tasks:** 1 of 1
- **Files modified:** 3

## Accomplishments
- `slow` marker registered in pytest.ini alongside existing `integration` marker — no more PytestUnknownMarkWarning
- `@pytest.mark.slow` applied to `test_monte_carlo_convergence` with docstring noting `random.seed(42)` and ~2-3s runtime
- `@pytest.mark.integration` + environment comment applied to all 4 test_motd.py functions: `test_motd_capacity_displays`, `test_motd_handles_empty_estimates`, `test_motd_convergence_status_badge`, `test_motd_shows_new_battery_alert`
- Filter verification: `-m "not slow"` collects 554/555, `-m "not integration"` collects 529/555

## Task Commits

1. **Task 1: Register slow marker and apply TEST-06 + TEST-07 markers** - `14f2294` (feat)

**Plan metadata:** _(docs commit follows)_

## Files Created/Modified
- `pytest.ini` — added `slow: marks tests as slow-running` marker registration
- `tests/test_capacity_estimator.py` — added `@pytest.mark.slow` decorator + docstring note to `test_monte_carlo_convergence`
- `tests/test_motd.py` — added `@pytest.mark.integration` + environment comment to all 4 test functions

## Decisions Made
- Docstring comment for `@pytest.mark.slow` includes both seed and runtime information per plan spec — placed at end of existing docstring as two distinct lines

## Deviations from Plan

None — plan executed exactly as written.

**Note:** Pre-existing failure in `tests/test_monitor.py::test_per_poll_writes_during_blackout` (introduced by parallel agent 23-02 changing `write_virtual_ups_dev()` signature) was observed but is out of scope for this plan. Logged to `deferred-items.md`.

## Issues Encountered
- `grep -c "pytest.mark.slow"` returns 2 instead of the expected 1 from the acceptance criteria — second match is in the docstring text (`Mark: @pytest.mark.slow — exclude from fast CI...`). The decorator itself is correctly applied at line 396. Filter verification (`-m "not slow"` = 554/555) confirms functional correctness.

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- Marker infrastructure in place for test quality rewrite work in 23-02, 23-03, 23-04
- Pre-existing test failure in test_monitor.py needs attention from 23-02 plan (parallel agent scope)

---
*Phase: 23-test-quality-rewrite*
*Completed: 2026-03-20*
