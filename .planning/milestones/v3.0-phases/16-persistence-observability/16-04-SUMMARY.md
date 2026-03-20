---
phase: 16-persistence-observability
plan: 04
subsystem: monitoring-observability
tags: [health-endpoint, metrics-export, phase-16-observability]
type: summary
completed_date: 2026-03-17
duration_minutes: 35
status: complete

dependency_graph:
  requires:
    - "16-03-PLAN: discharge handler integration and sulfation scoring"
  provides:
    - "health.json schema extended with 11 Phase 16 observability fields"
    - "Integration tests for health.json export (8 tests)"
    - "Foundation for Grafana metrics pipeline (16-RESEARCH Example 3)"
  affects:
    - "Phase 17 scheduler decision logging"
    - "MOTD module for sulfation display (16-05 planned)"

tech_stack:
  added: []
  patterns:
    - "Atomic JSON writes with tempfile + fdatasync + rename (v2.0 inherited)"
    - "Optional parameters pattern for backward-compatible schema extension"
    - "Integration testing with temporary file fixtures and mocking"

key_files:
  created: []
  modified:
    - "src/monitor_config.py (+36 lines, -6 lines)"
    - "tests/test_health_endpoint_v16.py (+177 lines, -19 lines)"

decisions:
  - "health.json remains single schema (no versioning) - all Phase 16 fields optional for backward compatibility"
  - "Numeric precision standardized: sulfation_score 3 decimals, ir_trend_rate 6 decimals, recovery_delta 3"
  - "Timestamp fields use ISO8601 strings in persistent storage, unix timestamps in health.json for Grafana"
  - "All Phase 16 fields default to None; daemon provides values only when available (sulfation scoring from discharge_handler)"

test_metrics:
  total_tests_written: 8
  total_tests_passing: 8
  integration_suite_status: "22 tests pass (health endpoint + sulfation persistence + discharge events)"
  regression_suite_status: "389 tests pass (full suite, no regressions)"
  coverage_areas:
    - "File creation and JSON validity (1 test)"
    - "Phase 16 sulfation fields presence and precision (1 test)"
    - "Phase 16 ROI fields presence and values (1 test)"
    - "Phase 16 discharge fields presence and format (1 test)"
    - "Null handling for optional fields (1 test)"
    - "Backward compatibility with v2.0 fields (1 test)"
    - "ISO8601 timestamp format validation (1 test)"
    - "Unix timestamp format and sanity checks (1 test)"

requirement_traceability:
  RPT-01: "✅ Sulfation score exported to health.json (sulfation_score field, 3-decimal precision)"
  ROI-02: "✅ ROI factors exported to health.json (cycle_roi, days_since_deep, ir_trend_rate, sulfation_score, cycle_budget_remaining)"
  ROI-03: "✅ Scheduling state exported to health.json (scheduling_reason='observing', next_test_timestamp=None for Phase 16)"

---

# Phase 16 Plan 04: Health.json Schema Extension Summary

**Completed:** 2026-03-17
**Duration:** 35 minutes
**Status:** All 4 tasks complete ✅

## Overview

Extended health.json export to include 11 Phase 16 observability metrics (sulfation, ROI, scheduling state). All new fields are optional and fully backward-compatible with v2.0 consumers. Schema now provides complete visibility into daemon's internal sulfation model and cycle ROI scoring for external monitoring tools (Grafana, check_mk, custom dashboards).

**One-liner:** Sulfation score, ROI metrics, and scheduling state exported to health.json (Phase 16 observability foundation for Grafana).

## Tasks Completed

| Task | Name | Status | Commit |
|------|------|--------|--------|
| 1 | Extend write_health_endpoint() function signature | ✅ | 1f24b78 |
| 2 | Implement health.json schema with Phase 16 fields | ✅ | 1f24b78 |
| 3 | Implement integration tests (8 tests) | ✅ | 5fdc49c |
| 4 | Verify full integration (22 integration + 389 regression) | ✅ | N/A |

## Changes Made

### Task 1: Function Signature Extension
- Extended `write_health_endpoint()` with 11 new optional parameters
- New parameters with defaults:
  - `sulfation_score: Optional[float] = None`
  - `sulfation_confidence: str = 'high'`
  - `days_since_deep: Optional[float] = None`
  - `ir_trend_rate: Optional[float] = None`
  - `recovery_delta: Optional[float] = None`
  - `cycle_roi: Optional[float] = None`
  - `cycle_budget_remaining: Optional[int] = None`
  - `scheduling_reason: str = 'observing'`
  - `next_test_timestamp: Optional[int] = None`
  - `last_discharge_timestamp: Optional[str] = None`
  - `natural_blackout_credit: Optional[float] = None`
- All new parameters default to None for backward compatibility
- No changes to existing v2.0 parameters or behavior

### Task 2: health.json Schema Extension
Updated `health_data` dict to include all Phase 16 fields with proper rounding:
```json
{
  // Existing v2.0 fields (unchanged):
  "last_poll": "2026-03-17T13:16:51+00:00",
  "last_poll_unix": 1710758400,
  "current_soc_percent": 75.5,
  "online": true,
  "daemon_version": "1.1.0",
  "poll_latency_ms": 0.3,
  "capacity_ah_measured": 6.8,
  "capacity_ah_rated": 7.2,
  "capacity_confidence": 0.95,
  "capacity_samples_count": 5,
  "capacity_converged": true,

  // NEW Phase 16 fields:
  "sulfation_score": 0.45,                           // 3 decimals
  "sulfation_score_confidence": "high",              // string
  "days_since_deep": 7.2,                            // 1 decimal
  "ir_trend_rate": 0.000008,                         // 6 decimals
  "recovery_delta": 0.12,                            // 3 decimals
  "cycle_roi": 0.52,                                 // 3 decimals
  "cycle_budget_remaining": 150,                     // integer
  "scheduling_reason": "observing",                  // Phase 16: always "observing"
  "next_test_timestamp": null,                       // Phase 16: always null
  "last_discharge_timestamp": "2026-03-17T10:00:00Z", // ISO8601 or null
  "natural_blackout_credit": 0.15                    // 3 decimals or null
}
```

**Precision standards:**
- `sulfation_score`: 3 decimals (0.450)
- `ir_trend_rate`: 6 decimals (0.000008)
- `recovery_delta`: 3 decimals (0.123)
- `cycle_roi`: 3 decimals (0.520)
- `days_since_deep`: 1 decimal (7.2)
- `natural_blackout_credit`: 3 decimals (0.150)
- `cycle_budget_remaining`: integer (no rounding)

**Atomic write pattern preserved:** No changes to I/O logic. Continues using tempfile + fdatasync + atomic rename.

### Task 3: Integration Tests (8 tests)
Implemented comprehensive test suite in `tests/test_health_endpoint_v16.py`:

1. **test_write_health_endpoint_creates_file** — Verifies file creation and valid JSON structure
2. **test_health_endpoint_includes_v16_sulfation_fields** — Verifies sulfation_score, confidence, days_since_deep, ir_trend_rate, recovery_delta with correct precision
3. **test_health_endpoint_includes_v16_roi_fields** — Verifies cycle_roi, cycle_budget_remaining, scheduling_reason, next_test_timestamp
4. **test_health_endpoint_includes_v16_discharge_fields** — Verifies last_discharge_timestamp, natural_blackout_credit
5. **test_health_endpoint_nulls_when_sulfation_not_provided** — Verifies all optional fields are null when not provided (no errors)
6. **test_health_endpoint_preserves_v20_fields** — Verifies all v2.0 fields unchanged (backward compatibility)
7. **test_health_endpoint_iso8601_timestamps** — Verifies timestamp format (YYYY-MM-DDTHH:MM:SS[.µs]Z or ±HH:MM)
8. **test_health_endpoint_unix_timestamp** — Verifies last_poll_unix is integer and near current time

All tests use temporary file fixtures and mocking for isolation.

### Task 4: Full Integration Verification
- ✅ `test_health_endpoint_v16.py`: 8 tests pass
- ✅ `test_sulfation_persistence.py`: 7 tests pass (model.json persistence)
- ✅ `test_discharge_event_logging.py`: 7 tests pass (discharge event schema)
- ✅ Combined integration suite: 22 tests pass
- ✅ Full regression suite: 389 tests pass (no regressions from v2.0)

## Schema Backward Compatibility

**v2.0 consumer behavior:**
- Missing `sulfation_score`, `cycle_roi`, etc. → gracefully ignored (optional fields)
- All existing v2.0 fields present and unchanged
- No breaking changes

**v2.0 → v3.0 upgrade:**
- Phase 16 daemon writes all 11 new fields to health.json every poll
- Old v2.0 monitoring tools continue reading v2.0 fields without issues
- New Phase 16 aware tools can read sulfation and ROI metrics

## Integration with Monitor.py

Previous plan (16-03) already extended `monitor.py` to call `write_health_endpoint()` with Phase 16 parameters. Phase 04 finalizes the health.json export layer:
- In-memory values flow from discharge_handler (sulfation scoring) → monitor.py → write_health_endpoint()
- Every poll (10s) health.json is updated with current daemon state
- Grafana can query health.json via /run/ups-battery-monitor/ups-health.json endpoint

## Metrics and Coverage

**Tests written:** 8 comprehensive integration tests
**Tests passing:** 8/8 (100%)
**Code coverage:** Function signature (11 params) + schema dict (11 fields) + atomic write pattern (unchanged)
**Regression tests:** 389 pass (full suite green)

## Deviations from Plan

None - plan executed exactly as written.

All Phase 16 observability fields match RESEARCH.md Example 3 specification. Numeric precision aligned with requirement tables. Tests cover all acceptance criteria.

## Next Steps

- Phase 16 Plan 05: MOTD module for sulfation display (will read health.json and show next_test_timestamp)
- Phase 17: Scheduler decision logic will extend health.json with scheduling decisions (scheduling_reason, next_test_timestamp populated)

## Files Modified

| File | Changes |
|------|---------|
| `src/monitor_config.py` | +36 lines (function signature, health_data dict) |
| `tests/test_health_endpoint_v16.py` | +177 lines (8 comprehensive integration tests) |

## Commits

| Commit | Message |
|--------|---------|
| 1f24b78 | feat(16-04): extend write_health_endpoint() function signature with Phase 16 parameters |
| 5fdc49c | test(16-04): implement integration tests for health.json Phase 16 export |

---

**Wave 3 completion:** Phase 16 observability metrics infrastructure now complete. Health.json provides full visibility into sulfation model and cycle ROI scoring. Ready for Phase 16 Plan 05 (MOTD) and Phase 17 (scheduler decision logic).
