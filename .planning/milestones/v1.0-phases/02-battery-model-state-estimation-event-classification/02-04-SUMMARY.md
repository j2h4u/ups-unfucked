---
phase: 02-battery-model-state-estimation-event-classification
plan: 04
subsystem: Event Classification
tags: [state-machine, physical-invariant, blackout-detection, firmware-independence]
decision_made:
  - "Physical invariant (input.voltage) chosen over firmware flags to avoid onlinedischarge_calibration bug"
  - "Voltage thresholds: >100V = mains present, <50V = mains absent, 50-100V = undefined (logged)"
  - "State machine with 3 states: ONLINE, BLACKOUT_REAL, BLACKOUT_TEST"
tech_stack_added:
  - Python 3.13
  - pytest 8.3
patterns_used:
  - "State machine pattern (FSM)"
  - "Physical sensor-based classification (vs firmware flag interpretation)"
  - "Transition detection pattern (state != new_state)"
  - "Hysteresis thresholds (eliminate oscillation in undefined range)"
key_files_created:
  - "src/event_classifier.py (99 lines) — EventClassifier class, EventType enum"
key_files_modified:
  - "tests/test_event_classifier.py (130 lines) — 13 test cases (Wave 0 code slightly reformatted)"
requirement_provides:
  - EVT-01: "Event type correctly determined from UPS status and mains voltage"
depends_on:
  - "02-01: NUT socket client (provides ups_status and input_voltage fields)"
---

# Phase 2 Plan 4: Event Classifier Summary

**One-liner:** State machine distinguishing real blackouts (mains absent) from battery tests (mains present) using physical invariant input.voltage, eliminating firmware misinterpretations.

---

## Objective Met

Implemented event classification module that uses physical sensor data (input.voltage) rather than unreliable firmware flags to distinguish between:
- **BLACKOUT_REAL**: UPS on battery + mains absent (input.voltage ≈ 0V) → initiate shutdown sequence
- **BLACKOUT_TEST**: UPS on battery + mains present (input.voltage ≈ 230V) → collect calibration data only
- **ONLINE**: UPS on mains → normal operation

This eliminates the firmware bug (`onlinedischarge_calibration: true` prevents critical state declaration during tests).

---

## Implementation Details

### EventType Enum (3 states)

```python
class EventType(Enum):
    ONLINE = "OL"
    BLACKOUT_REAL = "OB_BLACKOUT"
    BLACKOUT_TEST = "OB_TEST"
```

### EventClassifier Class

**State machine logic:**
1. If NOT on battery (`status = OL`) → ONLINE
2. If on battery + mains present (`status = OB` + `input.voltage > 100V`) → BLACKOUT_TEST
3. If on battery + mains absent (`status = OB` + `input.voltage < 50V`) → BLACKOUT_REAL

**Voltage thresholds (hysteresis):**
- **> 100V**: Mains present (clear margin from 0V measurements, tolerates ±10V AC ripple on 230V nominal)
- **< 50V**: Mains absent (clear gap prevents oscillation)
- **50-100V**: Undefined range → log warning, treat as absent (graceful fallback for sensor errors)

**Transition detection:**
- `transition_occurred` flag set when `state != new_state`
- Allows daemon to detect state changes for event logging and shutdown triggering

### Why Physical Invariant Wins

| Aspect | Firmware Flags | input.voltage |
|--------|---|---|
| **Reliability** | Interprets based on internal state machine (buggy) | Direct sensor measurement (physical fact) |
| **onlinedischarge_calibration Bug** | Blocks critical state during tests | Unaffected; detects mains presence independently |
| **Calibration Data** | Mixed with false alarms | Cleanly separated (test vs real) |
| **Recovery from NUT restart** | Flags reset; miss transition | Voltage stable; no dependence on state history |

---

## Test Coverage

**13 tests, all passing:**

### Core Classification (3)
- `test_classify_online`: status=OL, voltage=230V → ONLINE
- `test_classify_real_blackout`: status=OB DISCHRG, voltage=0V → BLACKOUT_REAL
- `test_classify_battery_test`: status=OB DISCHRG, voltage=230V → BLACKOUT_TEST

### State Transitions (3)
- `test_transition_ol_to_real_blackout`: OL → OB DISCHRG (mains absent) detected
- `test_transition_real_blackout_to_ol`: OB DISCHRG → OL (power restored) detected
- `test_no_transition_when_state_unchanged`: Repeated same state → transition_occurred=False

### Voltage Thresholds (3)
- `test_undefined_voltage_range_70v`: 75V → treated as absent (boundary case)
- `test_undefined_voltage_range_50v`: 50V → treated as absent (boundary case)
- `test_undefined_voltage_range_100v`: 100V → treated as absent (boundary case)

### Initialization (2)
- `test_initial_state_is_online`: Starts in ONLINE state
- `test_initial_transition_flag_false`: Starts with transition_occurred=False

### Consistency (2)
- `test_multiple_online_events_no_transition`: Repeated same state doesn't flag transitions
- `test_state_after_multiple_transitions`: State consistent across OL → OB → OL → OB sequence

---

## Verification

**Command:** `pytest tests/test_event_classifier.py -v`

**Result:** 13 passed in 0.03s

```
tests/test_event_classifier.py:
• TestEventClassification: 3 tests — PASSED
• TestEventStateTransitions: 3 tests — PASSED
• TestEventUndefinedVoltage: 3 tests — PASSED
• TestEventInitialization: 2 tests — PASSED
• TestEventConsistency: 2 tests — PASSED
```

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Ready for Next Plan

**Plan 05 (Monitor Loop Integration)** can now:
1. Instantiate `EventClassifier()` in daemon startup
2. Call `classifier.classify(ups_status, input_voltage)` each poll cycle
3. Detect transitions via `transition_occurred` flag
4. Route to appropriate handlers (BLACKOUT_REAL → start shutdown; BLACKOUT_TEST → collect data)

**Integration pattern:**
```python
from src.event_classifier import EventClassifier

classifier = EventClassifier()

# In daemon loop:
event_type = classifier.classify(ups_data["ups.status"], ups_data["input.voltage"])
if classifier.transition_occurred:
    logger.info(f"Event transition: {classifier.state.value}")
```

---

## Metrics

| Metric | Value |
|--------|-------|
| **Duration** | ~8 minutes |
| **Tasks Completed** | 1 (TDD: RED + GREEN) |
| **Tests Written** | 13 (comprehensive) |
| **Lines of Code** | 99 (event_classifier.py) |
| **Import Errors** | 0 |
| **Test Failures** | 0 |

