# Phase 19: Extract SagTracker - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 19-extract-sagtracker
**Areas discussed:** Module placement, Public interface, RLS ownership, State reset
**Mode:** auto (all decisions auto-selected)

---

## Module placement

| Option | Description | Selected |
|--------|-------------|----------|
| src/sag_tracker.py | Top-level src/, daemon collaborator not pure math | :heavy_check_mark: |
| src/battery_math/sag_tracker.py | Under battery_math package with other math modules | |

**User's choice:** [auto] src/sag_tracker.py (recommended default)
**Notes:** SagTracker is stateful and interacts with model/RLS — it's a daemon collaborator, not a pure math function. Consistent with DischargeHandler pattern and future SchedulerManager/DischargeCollector extractions.

---

## Public interface

| Option | Description | Selected |
|--------|-------------|----------|
| Single track() entry point | Drives state machine; is_measuring property for poll interval | :heavy_check_mark: |
| Separate track + record methods | Expose both _track_voltage_sag and _record_voltage_sag publicly | |
| Full state machine API | Separate start_measuring, add_sample, finalize methods | |

**User's choice:** [auto] Single track() entry point (recommended default)
**Notes:** Matches existing _track_voltage_sag pattern — single method called per poll. Internal state machine details stay private. is_measuring property replaces direct SagState checks in MonitorDaemon.

---

## RLS ownership

| Option | Description | Selected |
|--------|-------------|----------|
| SagTracker owns rls_ir_k | Clean encapsulation, only used in sag context | :heavy_check_mark: |
| MonitorDaemon keeps rls_ir_k | Injected per-call, SagTracker is stateless for RLS | |
| Shared ownership | Both hold reference, SagTracker mutates | |

**User's choice:** [auto] SagTracker owns rls_ir_k (recommended default)
**Notes:** rls_ir_k is exclusively used during voltage sag processing for ir_k auto-calibration. No other MonitorDaemon code touches it except _reset_battery_baseline, which will call SagTracker.reset_rls().

---

## State reset

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit reset_rls() method | MonitorDaemon calls sag_tracker.reset_rls() from _reset_battery_baseline | :heavy_check_mark: |
| Re-construct SagTracker | Throw away and re-create on battery replacement | |
| Reset via constructor params | Pass new ScalarRLS instance to setter | |

**User's choice:** [auto] Explicit reset_rls() method (recommended default)
**Notes:** Lightweight, explicit delegation. Re-construction would be wasteful and lose any non-RLS state. Consistent with how DischargeHandler interacts with MonitorDaemon.

---

## Claude's Discretion

- Exact constructor signature (full Config vs extracted fields)
- Whether load is passed as value or callable to track()
- Internal method decomposition within SagTracker
- Unit test structure and fixtures

## Deferred Ideas

None — all auto-selected decisions stayed within phase scope.
