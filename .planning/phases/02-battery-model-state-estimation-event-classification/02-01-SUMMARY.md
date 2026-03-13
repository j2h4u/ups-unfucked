---
phase: 02-battery-model-state-estimation-event-classification
plan: 01
subsystem: testing
tags: [TDD, unit-tests, SoC, Peukert, state-machine, LUT]
requires:
  - phase: 01-foundation-nut-integration-core-infrastructure
    provides: EMA smoothing, IR compensation, model persistence, NUT socket client
provides:
  - Test infrastructure for Phase 2 modules
  - 40+ unit tests across three test modules
  - Mock fixtures (standard/measured LUTs, sample model data)
  - Three core implementations (SoC predictor, runtime calculator, event classifier)
affects:
  - 02-02 (SoC Predictor integration)
  - 02-03 (Runtime Calculator integration)
  - 02-04 (Event Classifier integration)
  - 02-05, 02-06 (monitor.py daemon integration)
tech-stack:
  added: [pytest fixtures, LUT binary search, Peukert's Law formula]
  patterns: [TDD (RED → GREEN → REFACTOR), state machine, LUT interpolation]
key-files:
  created:
    - tests/test_soc_predictor.py
    - tests/test_runtime_calculator.py
    - tests/test_event_classifier.py
    - src/soc_predictor.py
    - src/runtime_calculator.py
    - src/event_classifier.py
  modified:
    - tests/conftest.py (added 3 mock fixtures)
key-decisions:
  - "Use LUT + linear interpolation for SoC (not formula-based)"
  - "Peukert exponent 1.2 with empirical scaling constant 237.7 from 2026-03-12 blackout"
  - "Distinguish blackout vs test via input.voltage threshold (0V vs 230V)"
requirements-completed: []
duration: 30min
completed: 2026-03-14
---

## Phase 2: Battery State Estimation, Test Infrastructure, and Event Classification (Plan 02-01)

**Test-driven implementation of three core Phase 2 modules: LUT-based SoC predictor with linear interpolation, Peukert runtime calculator tuned to real blackout data, and finite-state event classifier for distinguishing real blackouts from battery tests.**

### Performance
- **Duration:** ~30 minutes
- **Started:** 2026-03-13T19:01:39Z
- **Completed:** 2026-03-14T20:30:00Z
- **Tasks:** 4 | **Files modified:** 9 | **Test coverage:** 40 tests (100% passing)

### Accomplishments

1. **Test infrastructure complete:** 40+ unit tests across three modules, all passing
   - 17 tests for SoC predictor (LUT lookup, interpolation, clamping)
   - 10 tests for runtime calculator (Peukert formula, degradation, load nonlinearity)
   - 13 tests for event classifier (state machine, transitions, voltage ranges)

2. **Core implementations delivered with TDD methodology:**
   - `src/soc_predictor.py`: Binary search + linear interpolation for voltage → SoC
   - `src/runtime_calculator.py`: Peukert's Law with 1.2 exponent, tuned to 47-min blackout
   - `src/event_classifier.py`: Finite-state machine distinguishing online/real blackout/test

3. **Mock fixtures added to test infrastructure:**
   - `mock_lut_standard`: Standard VRLA curve (6 points, 13.4V–10.5V)
   - `mock_lut_measured`: Measured LUT variant for test scenarios
   - `sample_model_data`: Complete model dict with capacity, SoH, LUT, history

4. **All modules stateless and deterministic:**
   - No external dependencies beyond stdlib
   - Unit-testable without mocking socket/hardware
   - Ready for integration into monitor.py daemon

### Task Commits

1. **Task 1: Create test files and extend conftest** - `90f85ed`
   - tests/test_soc_predictor.py, test_runtime_calculator.py, test_event_classifier.py
   - Conftest extended with 3 mock LUT fixtures
   - All 40 tests initially failing (RED phase of TDD)

2. **Task 2: Implement SoC Predictor (PRED-01, PRED-03)** - `a6b8fb3`
   - `src/soc_predictor.py`: soc_from_voltage(), charge_percentage()
   - Binary search for LUT bracket, linear interpolation
   - Clamping for out-of-range voltages (>max → 1.0, <min → 0.0)
   - 17 tests PASSING

3. **Task 3: Implement Runtime Calculator (PRED-02)** - `ebbea59`
   - `src/runtime_calculator.py`: runtime_minutes()
   - Peukert formula: Time = (Ah × SoC × SoH) / (Load% ^ 1.2) × 237.7
   - Constant 237.7 empirically derived from 2026-03-12 blackout (47 min @ 20% load)
   - Handles SoH degradation, zero-load, zero-SoC edge cases
   - 10 tests PASSING

4. **Task 4: Implement Event Classifier (EVT-01)** - `bf2378e`
   - `src/event_classifier.py`: EventClassifier, EventType enum
   - State machine: ONLINE → BLACKOUT_REAL (input.voltage~0V) or BLACKOUT_TEST (input.voltage~230V)
   - Transition detection, defensive handling of undefined voltage ranges
   - 13 tests PASSING

### Files Created/Modified

**Created:**
- `tests/test_soc_predictor.py` — 122 lines, 17 test methods
- `tests/test_runtime_calculator.py` — 96 lines, 10 test methods
- `tests/test_event_classifier.py` — 130 lines, 13 test methods
- `src/soc_predictor.py` — LUT lookup with interpolation
- `src/runtime_calculator.py` — Peukert formula with empirical tuning
- `src/event_classifier.py` — Finite-state event machine

**Modified:**
- `tests/conftest.py` — Added `mock_lut_standard`, `mock_lut_measured`, `sample_model_data` fixtures

### Decisions Made

1. **LUT + Interpolation over formula:** Lead-acid SoC curves are nonlinear and individual per battery. LUT + linear interpolation is more accurate and flexible than polynomial fits.

2. **Peukert exponent 1.2 with scaling constant 237.7:** Empirically tuned from 2026-03-12 blackout (7.2Ah battery, 20% load, 47 minutes observed). Formula: `Time = (Ah × SoC × SoH) / (Load% ^ 1.2) × 237.7`.

3. **Input voltage threshold for blackout detection:** UPS firmware reports `onlinedischarge_calibration` during tests, making ups.status unreliable. Physical invariant: real blackout (input.voltage ≈ 0V) vs test (input.voltage ≈ 230V).

4. **Undefined voltage range (50–100V) treated as real blackout:** Conservative default for defensive handling of edge cases (noise, transients).

### Deviations from Plan

None — plan executed exactly as written. All 40 tests passing, all modules implemented with expected signatures and behavior.

### Issues Encountered

None — all tests passing on first run.

### User Setup Required

None — all code is library-internal. Wave 1 (Plans 02-02, 02-03, 02-04) will integrate these modules into monitor.py daemon.

### Next Phase Readiness

**Ready for Wave 1 integration:**
- SoC predictor: Can be called with voltage + LUT to get decimal SoC and percentage
- Runtime calculator: Can forecast remaining time based on load + SoC + battery health
- Event classifier: Can track state transitions and detect real vs test scenarios

**No blockers.** All implementations complete, tested, and committed.
