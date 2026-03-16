---
phase: 12-deep-discharge-capacity-estimation
plan: 04
name: CLI Flag Integration (CAP-05 Gap Closure)
subsystem: daemon/configuration
tags: [cli, argparse, integration, phase-13-handoff]
type: gap-closure
duration_minutes: 45
completed_date: 2026-03-16
dependency_graph:
  requires: [12-02, 12-03]
  provides: [user-signal-mechanism]
  affects: [phase-13-new-battery-detection]
tech_stack:
  added: []
  patterns: [argparse-integration, frozen-dataclass-pattern, atomic-persistence]
key_files:
  created: []
  modified:
    - src/monitor.py (parse_args function, MonitorDaemon.__init__ signature, CLI→daemon wiring)
    - tests/test_monitor.py (4 integration tests for flag wiring)
---

# Phase 12 Plan 04: CLI Flag Integration Summary

## Objective

Wire the CLI flag `--new-battery` to MonitorDaemon, closing **CAP-05 user signal mechanism** that was designed in Phase 12 Plans 01-03 but not yet integrated.

**Purpose:** Enable users to signal battery replacement to the daemon at startup; daemon persists flag in model.json for Phase 13 new battery detection logic to read.

---

## Changes Made

### 1. Argparse Integration (src/monitor.py)

**New function: `parse_args(args=None)`**
- Added standalone argument parser function (extracted from inline main())
- Accepts optional `args` list for testing (defaults to sys.argv[1:])
- Flag: `--new-battery` (action='store_true', boolean, no value required)
- Help text: "Signal that a new battery has been installed; daemon will use this for next discharge measurement"

**Updated: `main()` function**
- Now calls `parse_args()` to get CLI arguments
- Passes `new_battery_flag=args.new_battery` to MonitorDaemon constructor
- Handles graceful startup with or without flag

### 2. MonitorDaemon Parameter (src/monitor.py)

**Updated: `MonitorDaemon.__init__()`**
- Added parameter: `new_battery_flag: bool = False`
- Stores flag value in `model.data['new_battery_requested']` immediately at daemon startup
- Logs info message when flag is set (informational only, no warnings/errors)
- Flag persists via atomic `model.save()` (existing pattern from Phase 12 Plan 02)

**Key design decisions:**
- Flag stored in `model.data` (not Config) because Config is frozen dataclass
- Flag is NOT cleared by Phase 12; Phase 13 owns the detection logic and clearing responsibility
- No Phase 12 code checks or reacts to the flag; it's purely passed through for Phase 13

### 3. Integration Testing (tests/test_monitor.py)

**4 integration tests added:**

1. **test_new_battery_flag_false**
   - Verifies: `MonitorDaemon(config, new_battery_flag=False)` → `model.data['new_battery_requested'] = False`
   - Tests default behavior when flag not passed
   - Status: PASSING

2. **test_new_battery_flag_true**
   - Verifies: `MonitorDaemon(config, new_battery_flag=True)` → `model.data['new_battery_requested'] = True`
   - Tests behavior when flag is set
   - Status: PASSING

3. **test_new_battery_flag_persistence**
   - Verifies: Flag persists in model.json across save/reload cycle
   - Ensures Phase 13 can read flag even if daemon restarts
   - Demonstrates atomic persistence (model.save() + BatteryModel reload)
   - Status: PASSING

4. **test_cli_new_battery_flag**
   - Verifies: `parse_args()` correctly parses `--new-battery` flag
   - Tests with flag: `args.new_battery == True`
   - Tests without flag: `args.new_battery == False`
   - Integration test of argparse module
   - Status: PASSING

---

## Verification

### Automated Verification (All Passing)

```bash
python3 src/monitor.py --help | grep new-battery
# Output: "--new-battery  Signal that a new battery has been installed..."

python3 -c "from src.monitor import parse_args; args = parse_args(['--new-battery']); print(args.new_battery)"
# Output: True

python3 -c "from src.monitor import parse_args; args = parse_args([]); print(args.new_battery)"
# Output: False

python3 -c "import inspect; from src.monitor import MonitorDaemon; sig = inspect.signature(MonitorDaemon.__init__); print('new_battery_flag' in str(sig))"
# Output: True

pytest tests/test_monitor.py::test_new_battery_flag_false -xvs
pytest tests/test_monitor.py::test_new_battery_flag_true -xvs
pytest tests/test_monitor.py::test_new_battery_flag_persistence -xvs
pytest tests/test_monitor.py::test_cli_new_battery_flag -xvs
# All 4 PASSED
```

### Test Suite Status

- **New tests:** 4 integration tests added (all passing)
- **Total monitor tests:** 40 tests passing (36 existing + 4 new)
- **Full project suite:** 295 tests passing (291 existing + 4 new)
- **Regressions:** None detected

### Functional Verification

✓ CLI `--new-battery` flag parsed without error by argparse
✓ Flag value correctly passed to MonitorDaemon via constructor parameter
✓ Flag stored in `model.data['new_battery_requested']` at daemon startup
✓ Flag persists in model.json across daemon restarts (save/reload cycle)
✓ Help text documents `--new-battery` purpose clearly
✓ Default behavior (no flag) → flag = False
✓ Explicit flag → flag = True

---

## Implementation Quality

### Code Patterns

- **Argparse:** Standard Python argparse with action='store_true'
- **Parameter passing:** Frozen dataclass Config → new_battery_flag parameter → model.data storage
- **Persistence:** Leverages existing atomic model.json save pattern from Phase 12 Plan 02
- **Logging:** Info-level message when flag set (no errors, normal user workflow)

### Testing Strategy

- **Unit coverage:** 4 integration tests covering:
  - Flag default (False when not passed)
  - Flag set (True when passed)
  - Flag persistence (survives save/reload)
  - CLI argument parsing (end-to-end)
- **No mocking of core logic:** Tests use real Config/BatteryModel with minimal systemd/NUT mocking
- **Deterministic tests:** No randomness, all reproducible

### Design Constraints (Maintained)

- Phase 12 only stores flag; does NOT clear it (Phase 13 responsibility)
- Flag stored in model.data (not Config) because Config is frozen
- Flag defaults to False (safe for existing daemon behavior)
- No Phase 12 code reads or reacts to flag (pure pass-through)

---

## Phase 13 Handoff

### What Phase 13 Will Read

Field: `model.data['new_battery_requested']` (boolean)

**Usage in Phase 13:**
1. On first discharge after daemon startup with `--new-battery` flag set
2. Measure current discharge capacity
3. Compare to stored estimates (from Phase 12 capacity_estimates array)
4. If >10% capacity jump detected: prompt user "New battery installed? [y/n]"
5. If user confirms: rebaseline SoH using new measured capacity
6. Clear flag: `model.data['new_battery_requested'] = False`

### Integration Points Ready

- ✓ `model.data['new_battery_requested']` field persisted and readable
- ✓ Flag wired from CLI argument to daemon initialization
- ✓ Flag survives daemon restarts (stored in model.json)
- ✓ No Phase 12 code clears the flag (Phase 13 owns that responsibility)

---

## Requirements Completion

**CAP-05: User Signal Mechanism**

| Requirement | Satisfied By | Status |
|-----------|-------------|--------|
| User can pass `--new-battery` CLI flag | parse_args(), argparse integration | ✓ COMPLETE |
| Flag recognized and parsed without error | test_cli_new_battery_flag | ✓ PASSING |
| MonitorDaemon receives flag value | MonitorDaemon.__init__(new_battery_flag) | ✓ COMPLETE |
| Flag stored in model.data | `model.data['new_battery_requested']` | ✓ COMPLETE |
| Flag persists across daemon restarts | test_new_battery_flag_persistence | ✓ PASSING |
| Phase 13 can read flag from model.data | Field exists, wiring verified | ✓ READY |
| Phase 13 owns flag clearing (not Phase 12) | Architecture maintained | ✓ COMPLETE |

**Phase 12 Requirements Status:**
- CAP-01 (Coulomb counting): ✓ COMPLETE (Phase 12 Plan 01)
- CAP-02 (Voltage anchoring): ✓ COMPLETE (Phase 12 Plan 01)
- CAP-03 (Confidence tracking): ✓ COMPLETE (Phase 12 Plan 01)
- CAP-04 (Atomic persistence): ✓ COMPLETE (Phase 12 Plan 02)
- CAP-05 (User signal mechanism): ✓ COMPLETE (Phase 12 Plan 04) ← **This Plan**
- VAL-01 (Quality filters): ✓ COMPLETE (Phase 12 Plan 03)
- VAL-02 (Peukert fixed at 1.2): ✓ COMPLETE (Phase 12 Plan 01)

**Phase 12 Complete:** All 7 requirements satisfied.

---

## Deviations from Plan

None. Plan executed exactly as written:
- Argparse integration implemented as specified
- MonitorDaemon parameter added as specified
- Flag stored in model.data['new_battery_requested'] as specified
- 4 integration tests added as specified
- All tests passing with no regressions

---

## Metrics

| Metric | Value |
|--------|-------|
| Files modified | 2 (src/monitor.py, tests/test_monitor.py) |
| Lines added | ~190 (argparse + tests) |
| Tests added | 4 integration tests |
| Tests passing | 295/295 (100%) |
| Regressions | 0 |
| Duration | ~45 minutes wall-clock |

---

## Next Steps

**Phase 13: SoH Recalibration & New Battery Detection** (soft-depends on Phase 12 capacity convergence)

Phase 13 will:
1. Read `model.data['new_battery_requested']` flag from model.json
2. Implement detection logic: on next discharge, compare measured capacity to stored estimates
3. If >10% jump detected and flag set: prompt user for confirmation
4. If confirmed: rebaseline SoH and capacity_ah_ref using measured value
5. Clear flag: `model.data['new_battery_requested'] = False`

Phase 12 provides:
- ✓ CLI flag wiring (this plan)
- ✓ Model.data storage for flag (this plan)
- ✓ Capacity measurements and convergence tracking (Plans 01-03)
- ✓ Validation gates and discharge handlers (Plans 01-03)

---

## Self-Check

**Files created/modified:**
- [x] src/monitor.py — parse_args() function + MonitorDaemon.__init__() signature verified
- [x] tests/test_monitor.py — 4 integration tests added and passing

**Commits:**
- [x] feat(12-04): add --new-battery CLI flag and MonitorDaemon parameter integration
- [x] test(12-04): add integration tests for --new-battery CLI flag

**Test verification:**
- [x] 4 new tests PASSING
- [x] 295/295 total tests PASSING
- [x] 0 regressions detected

✓ **PLAN COMPLETE**
