---
phase: 14-capacity-reporting-metrics
plan: 01
subsystem: MOTD Capacity Reporting
tags: [motd, capacity-display, convergence-status, integration-testing]
dependency_graph:
  requires: [Phase 12 (capacity estimation), Phase 13 (SoH recalibration)]
  provides: [RPT-01 requirement satisfied, MOTD capacity display with convergence status]
  affects: [MOTD module (51-ups.sh), test suite (tests/test_motd.py)]
tech_stack:
  added: []
  patterns: [bash+jq for JSON parsing, Python subprocess for numerical accuracy, color codes for status badges]
key_files:
  created: []
  modified:
    - scripts/motd/51-ups.sh (61 insertions, 24 deletions)
    - tests/test_motd.py (264 insertions, 13 deletions)
decisions:
  - Color-coded status badges: GREEN (✓ LOCKED), YELLOW (⟳ MEASURING), DIM (? UNKNOWN)
  - Convergence definition: locked when CoV < 0.10 AND sample_count >= 3
  - Confidence display: integer percentage (0-100%), derived from 1-CoV formula
  - Graceful degradation: MOTD exits cleanly (exit 0) even if capacity_estimates missing
metrics:
  completed_at: "2026-03-16T17:02:36Z"
  duration_minutes: 12
  task_count: 2
  test_count: 4
  commits: 2
  files_modified: 2
---

# Phase 14 Plan 01: Extend MOTD with Capacity Convergence Display Summary

**One-liner:** MOTD now displays measured capacity with convergence status badge, sample count, and confidence percentage — users see real-time measurement progress on every login.

## Objective

Extend MOTD module (51-ups.sh) to display capacity measurement progress with convergence status and confidence percentage. Users see "Capacity: X.XAh (measured) vs Y.YAh (rated) · STATUS_BADGE · N/3 samples · NN% confidence" on every login, providing real-time visibility into capacity measurement convergence without CLI queries.

## Execution Summary

**All tasks completed successfully. 4 tests created and passing. Requirement RPT-01 fully satisfied.**

### Task 1: Extend 51-ups.sh with convergence status display

**Status:** COMPLETED (Commit `87b56c8`)

**Changes:**
- Extended Python subprocess within 51-ups.sh to compute convergence status (locked/measuring/unknown) based on:
  - Sample count >= 3 AND CoV < 0.10 → "locked"
  - Sample count < 3 OR CoV >= 0.10 → "measuring"
  - Errors → "unknown"
- Added status_badge formatting after IFS parsing:
  - Green "✓ LOCKED" when converged
  - Yellow "⟳ MEASURING" when collecting samples
  - Dim "? UNKNOWN" on errors
- Changed output format from "Z/3 deep discharges" to "N/3 samples"
- Compute confidence as integer percentage (0-100%) from convergence_score = max(0, min(100, int((1 - CoV) * 100)))
- Added color variables (GREEN, YELLOW, DIM, NC) for ANSI codes
- Graceful degradation preserved: exit 0 even if capacity_estimates missing

**Verification:**
- Bash syntax check: PASSED (`bash -n scripts/motd/51-ups.sh`)
- status_badge references: 5 occurrences in script ✓
- Convergence logic: 1 "cov < 0.10" threshold + status determination ✓
- Status badge types: LOCKED, MEASURING, UNKNOWN all present ✓

### Task 2: Create comprehensive MOTD test coverage

**Status:** COMPLETED (Commit `cc57a3a`)

**Tests Created:** 4 tests (3 new + 1 existing pre-Phase-14)

1. **test_motd_capacity_displays** (NEW)
   - Setup: Creates model.json with 3 capacity_estimates (6.9Ah, 7.0Ah, 6.95Ah)
   - Execution: Runs 51-ups.sh via subprocess with HOME override
   - Assertions:
     - Output contains "Capacity:", "6.95Ah", "7.2Ah"
     - Contains "3/3 samples" format
     - Contains status badge (LOCKED/MEASURING/UNKNOWN)
     - Contains confidence percentage with "%"
   - Expected CoV: ~0.008 (3 values with low variance) → confidence ~99% → status LOCKED ✓

2. **test_motd_handles_empty_estimates** (NEW)
   - Setup: Creates model.json with empty capacity_estimates array
   - Execution: Runs 51-ups.sh via subprocess
   - Assertions:
     - Exit code is 0 (no crash)
     - No error messages
     - No "Capacity:" in output (graceful fallback)
   - Also tests completely missing model.json scenario ✓

3. **test_motd_convergence_status_badge** (NEW)
   - Setup: Creates model.json with 2 estimates (6.8Ah, 7.1Ah)
   - Execution: First run with 2 samples
   - Assertions:
     - Exit code is 0
     - Output contains "2/3 samples"
     - Status badge is "MEASURING" (not LOCKED, since count < 3)
   - Setup: Adds 3rd estimate (6.95Ah) with low variance
   - Execution: Re-runs 51-ups.sh
   - Assertions:
     - Output contains "3/3 samples"
     - Status badge changes to "LOCKED" ✓

4. **test_motd_shows_new_battery_alert** (EXISTING - pre-Phase-14)
   - Verifies new battery detection flag triggers alert display ✓

**Test Infrastructure:**
- Fixtures: `model_json_with_capacity` (tmp_path-based), `temp_model_json` (tmp_path-based)
- Test directory structure: tests use HOME override pointing to tmpdir for isolation
- Subprocess timeout: 5 seconds per test call
- Color code handling: Tests strip ANSI escape sequences before assertions

**Test Results:**
```
tests/test_motd.py::test_motd_capacity_displays PASSED
tests/test_motd.py::test_motd_handles_empty_estimates PASSED
tests/test_motd.py::test_motd_convergence_status_badge PASSED
tests/test_motd.py::test_motd_shows_new_battery_alert PASSED

====== 4 passed in 0.25s ======
```

## Acceptance Criteria — All Met

- [x] scripts/motd/51-ups.sh has no syntax errors (bash -n passes)
- [x] Python subprocess computes CoV and returns "{status},{count},{pct}" format
- [x] Status badge variables (status_badge, status_color) defined after IFS parsing
- [x] Output line contains all elements: measured Ah, rated Ah, status_badge, sample_count, confidence_pct
- [x] Graceful handling: if capacity_estimates missing, script exits 0
- [x] Color variables (GREEN, YELLOW, DIM, NC) defined in script
- [x] Three test functions exist with exact names (grep -c "def test_motd_" = 4 including pre-existing)
- [x] test_motd_capacity_displays asserts output contains required elements
- [x] test_motd_handles_empty_estimates checks exit code 0 and graceful fallback
- [x] test_motd_convergence_status_badge verifies status badge state changes
- [x] All tests run via pytest and produce green output
- [x] Test imports include subprocess, json, pytest
- [x] Tests override HOME environment variable to use tmpdir config directory

## Deviations from Plan

None. Plan executed exactly as written. All acceptance criteria satisfied without auto-fixes needed.

## Requirement Traceability

| Requirement | Plan Task | Implementation | Status |
|-------------|-----------|-----------------|--------|
| RPT-01 | 14-01 Task 1 | MOTD capacity display with convergence status badge, sample count, confidence % | ✓ SATISFIED |
| RPT-01 | 14-01 Task 2 | Comprehensive test coverage (3 new tests + existing alert test) | ✓ SATISFIED |

## Notable Implementation Details

1. **Convergence Score Calculation:** Uses population standard deviation (÷N, not ÷N-1) consistent with Phase 12 design. CoV calculated only for sample_count >= 1, confidence = 0 for count < 3 (immature estimates).

2. **Status Badge Color Codes:** ANSI escape sequences used for terminal display. Tests strip codes before assertions (replace `\033[...m` with empty string).

3. **Python Subprocess Pattern:** Embedded Python heredoc in bash script avoids subprocess overhead for simple cases while maintaining numerical accuracy for CoV calculation. Falls back gracefully on Python errors (prints "unknown,0,0").

4. **Home Directory Override:** Tests use `HOME=/tmpdir` environment variable to redirect model.json path. MOTD script reads from `${HOME}/.config/ups-battery-monitor/model.json`, making it fully testable without root/user home modification.

5. **Sample Count Format Change:** Old format "Z/3 deep discharges" replaced with "N/3 samples" for clarity. This is accurate since each capacity_estimate represents one deep discharge event (enforced by VAL-01 gate in Phase 12).

## Files Modified

| File | Changes | Lines |
|------|---------|-------|
| scripts/motd/51-ups.sh | Extended with convergence status computation, color badges, confidence display | +61, -24 |
| tests/test_motd.py | Added 3 new test functions with fixtures, color-code handling, tmpdir isolation | +264, -13 |

## Commits

1. `87b56c8` feat(14-01): extend MOTD with convergence status badge and confidence display
2. `cc57a3a` test(14-01): create comprehensive MOTD test coverage for capacity display

## Self-Check

**Files Verified to Exist:**
- [x] scripts/motd/51-ups.sh (exists, 121 lines)
- [x] tests/test_motd.py (exists, 325 lines)

**Commits Verified to Exist:**
- [x] 87b56c8 in git log
- [x] cc57a3a in git log

**Test Results:**
- [x] All 4 MOTD tests passing
- [x] No syntax errors in 51-ups.sh
- [x] Expected behavior confirmed: MOTD shows capacity, convergence status, sample count, confidence

## Next Steps

Phase 14 Plan 02 (journald capacity events logging) and Plan 03 (health endpoint extension) are unblocked. Phase 14-01 provides the foundation for capacity visibility; subsequent plans add structured event logging and metrics export.

---

**Summary created:** 2026-03-16 17:02:36 UTC
**Executed by:** Claude Code (Haiku 4.5)
**Duration:** 12 minutes
**Quality Gate:** PASSED (all acceptance criteria met, comprehensive test coverage, no deviations)