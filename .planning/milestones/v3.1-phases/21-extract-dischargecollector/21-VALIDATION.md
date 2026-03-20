---
phase: 21
slug: extract-dischargecollector
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 21 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | none — pytest discovers via `tests/` |
| **Quick run command** | `pytest tests/test_discharge_collector.py tests/test_discharge_handler.py tests/test_monitor.py -x` |
| **Full suite command** | `pytest tests/ -x` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_discharge_collector.py tests/test_discharge_handler.py tests/test_monitor.py -x`
- **After every plan wave:** Run `pytest tests/ -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 21-01-01 | 01 | 1 | ARCH-05 | unit | `pytest tests/test_discharge_collector.py::test_track_accumulates_samples -x` | ❌ W0 | ⬜ pending |
| 21-01-02 | 01 | 1 | ARCH-05 | unit | `pytest tests/test_discharge_collector.py::test_track_starts_collection_on_ob -x` | ❌ W0 | ⬜ pending |
| 21-01-03 | 01 | 1 | ARCH-05 | unit | `pytest tests/test_discharge_collector.py::test_cooldown_continuation -x` | ❌ W0 | ⬜ pending |
| 21-01-04 | 01 | 1 | ARCH-05 | unit | `pytest tests/test_discharge_collector.py::test_calibration_write_batch -x` | ❌ W0 | ⬜ pending |
| 21-01-05 | 01 | 1 | ARCH-05 | unit | `pytest tests/test_discharge_collector.py::test_finalize_records_on_battery_time -x` | ❌ W0 | ⬜ pending |
| 21-02-01 | 02 | 1 | ARCH-06 | unit | `pytest tests/test_discharge_handler.py::test_compute_sulfation_metrics_returns_dict -x` | ❌ W0 | ⬜ pending |
| 21-02-02 | 02 | 1 | ARCH-06 | unit | `pytest tests/test_discharge_handler.py::test_compute_sulfation_metrics_handles_error -x` | ❌ W0 | ⬜ pending |
| 21-02-03 | 02 | 1 | ARCH-06 | unit | `pytest tests/test_discharge_handler.py::test_persist_sulfation_appends_history -x` | ❌ W0 | ⬜ pending |
| 21-02-04 | 02 | 1 | ARCH-06 | unit | `pytest tests/test_discharge_handler.py::test_log_discharge_complete_emits_event -x` | ❌ W0 | ⬜ pending |
| 21-03-01 | 03 | 2 | ARCH-05 | regression | `pytest tests/test_monitor.py -x -k discharge` | ✅ | ⬜ pending |
| 21-03-02 | 03 | 2 | ARCH-05/06 | regression | `pytest tests/ -x` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_discharge_collector.py` — stubs for ARCH-05 (accumulation, cooldown, calibration, finalize, is_collecting)
- [ ] New test methods in `tests/test_discharge_handler.py` — stubs for ARCH-06 (_compute_sulfation_metrics, _persist_sulfation_and_discharge, _log_discharge_complete)

*Existing infrastructure (pytest, conftest.py) covers framework requirements.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
