# Phase 21: Extract DischargeCollector - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 21-extract-dischargecollector
**Areas discussed:** Module boundary, Sulfation split strategy, Public interface, State ownership
**Mode:** --auto (all decisions auto-selected from recommended defaults)

---

## Module Boundary

| Option | Description | Selected |
|--------|-------------|----------|
| Sample accumulation + calibration writes move | DischargeCollector owns hot-path during active discharge; DischargeHandler keeps post-discharge analysis pipeline | ✓ |
| Full discharge lifecycle moves | Both collection and post-discharge processing move to DischargeCollector | |
| Minimal extraction | Only buffer + cooldown moves, calibration writes stay in MonitorDaemon | |

**User's choice:** [auto] Sample accumulation + calibration writes move (recommended default)
**Notes:** Follows the same boundary principle as SagTracker (owns the real-time tracking) and SchedulerManager (owns the decision loop). DischargeCollector owns the active discharge data path; DischargeHandler owns the analytical pipeline that runs after discharge completes.

---

## Sulfation Split Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Three methods: compute, persist, log | _compute_sulfation_metrics() → data dict, _persist_sulfation_and_discharge() → model writes, _log_discharge_complete() → journald event | ✓ |
| Two methods: compute+persist, log | Combine compute and persist since they share the same data flow | |
| Keep monolith, add unit test seams | Add parameters for testability without splitting the method | |

**User's choice:** [auto] Three methods: compute, persist, log (recommended default)
**Notes:** Three-way split gives maximum testability — compute can be tested without model writes or log side effects. Each method has a single responsibility matching ARCH-06 success criteria.

---

## Public Interface

| Option | Description | Selected |
|--------|-------------|----------|
| Single track() + properties | track(voltage, timestamp, event_type, current_metrics) entry point, is_collecting/buffer properties | ✓ |
| Multiple entry points | Separate start(), collect(), finalize() methods called from MonitorDaemon | |
| Event-driven callbacks | DischargeCollector registers callbacks, MonitorDaemon fires events | |

**User's choice:** [auto] Single track() + properties (recommended default)
**Notes:** Matches SagTracker pattern exactly — single entry point called from _poll_once(), properties for state reads. Simplest integration, minimal changes to MonitorDaemon call sites.

---

## State Ownership

| Option | Description | Selected |
|--------|-------------|----------|
| All four fields move | discharge_buffer, _discharge_start_time, discharge_buffer_clear_countdown, calibration_last_written_index | ✓ |
| Buffer stays in MonitorDaemon | Only move cooldown and calibration index, buffer shared | |
| New buffer type | Create a new internal buffer type, adapter for DischargeBuffer compatibility | |

**User's choice:** [auto] All four fields move (recommended default)
**Notes:** Clean ownership — DischargeCollector is the sole owner of all discharge collection state. MonitorDaemon reads buffer via property for health snapshot and _update_battery_health(). Consistent with SagTracker owning all sag state and SchedulerManager owning all scheduler state.

---

## Claude's Discretion

- Exact constructor signature (full Config vs individual fields)
- Whether track() returns status or mutates state
- Internal method naming
- Unit test structure and fixture design
- _grant_blackout_credit placement within persist method

## Deferred Ideas

- Anti-sulfation todo reviewed but not folded (already implemented in v3.0)
