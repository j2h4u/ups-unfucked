---
phase: 6
slug: calibration-mode
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 6 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 + conftest.py fixtures |
| **Config file** | pytest.ini (existing) |
| **Quick run command** | `pytest tests/test_monitor.py tests/test_model.py -v -x` |
| **Full suite command** | `pytest tests/ -v --tb=short` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_monitor.py tests/test_model.py -v -x`
- **After every plan wave:** Run `pytest tests/ -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 06-01-01 | 01 | 0 | CAL-01 | unit | `pytest tests/test_monitor.py::test_calibration_flag_parsing -xvs` | ❌ W0 | ⬜ pending |
| 06-01-02 | 01 | 0 | CAL-01 | unit | `pytest tests/test_virtual_ups.py::test_calibration_threshold_override -xvs` | ✅ Partial | ⬜ pending |
| 06-01-03 | 01 | 0 | CAL-02 | unit | `pytest tests/test_model.py::test_calibration_write_fsync -xvs` | ❌ W0 | ⬜ pending |
| 06-01-04 | 01 | 0 | CAL-02 | unit | `pytest tests/test_monitor.py::test_discharge_buffer_calibration_write -xvs` | ❌ W0 | ⬜ pending |
| 06-01-05 | 01 | 1 | CAL-03 | unit | `pytest tests/test_soh_calculator.py::test_interpolate_cliff_region -xvs` | ❌ W0 | ⬜ pending |
| 06-01-06 | 01 | 1 | CAL-03 | unit | `pytest tests/test_model.py::test_lut_source_field_preservation -xvs` | ❌ W0 | ⬜ pending |
| 06-01-07 | 01 | 1 | CAL-03 | integration | `pytest tests/test_monitor.py::test_calibration_lut_update -xvs` | ❌ W1 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_monitor.py::test_calibration_flag_parsing` — argparse integration
- [ ] `tests/test_model.py::test_calibration_write_fsync` — atomic write with fsync
- [ ] `tests/test_monitor.py::test_discharge_buffer_calibration_write` — buffer flushing
- [ ] `tests/test_soh_calculator.py::test_interpolate_cliff_region` — cliff region math
- [ ] `tests/test_model.py::test_lut_source_field_preservation` — source field handling

*Existing infrastructure covers framework/fixtures (Phase 5 installed pytest + conftest.py).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Full calibration discharge cycle | CAL-01+02+03 | Requires real UPS power loss (30-45 min) | 1. Start daemon with --calibration-mode 2. Disconnect UPS power 3. Monitor logs for datapoint writes 4. Verify model.json updated after OB→OL |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
