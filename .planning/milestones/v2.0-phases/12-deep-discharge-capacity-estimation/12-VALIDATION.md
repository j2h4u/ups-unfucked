---
phase: 12
slug: deep-discharge-capacity-estimation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-15
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x + hypothesis for property-based testing |
| **Config file** | `pytest.ini` (existing from v1.1) |
| **Quick run command** | `pytest tests/test_capacity_estimator.py -v` |
| **Full suite command** | `pytest tests/ -x -v --tb=short` |
| **Estimated runtime** | ~60 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_capacity_estimator.py -v`
- **After every plan wave:** Run `pytest tests/ -x -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 12-01-01 | 01 | 0 | CAP-01 | unit | `pytest tests/test_capacity_estimator.py::test_coulomb_integration -xvs` | ❌ W0 | ⬜ pending |
| 12-01-02 | 01 | 0 | CAP-01 | integration | `pytest tests/test_capacity_estimator.py::test_real_discharge_validation -xvs` | ❌ W0 | ⬜ pending |
| 12-01-03 | 01 | 0 | CAP-02 | unit | `pytest tests/test_capacity_estimator.py::test_weighted_averaging -xvs` | ❌ W0 | ⬜ pending |
| 12-01-04 | 01 | 0 | CAP-03 | unit | `pytest tests/test_capacity_estimator.py::test_confidence_convergence -xvs` | ❌ W0 | ⬜ pending |
| 12-01-05 | 01 | 0 | CAP-04 | integration | `pytest tests/test_model.py::test_capacity_estimate_persistence -xvs` | ❌ W0 | ⬜ pending |
| 12-01-06 | 01 | 0 | CAP-05 | integration | `pytest tests/test_monitor.py::test_new_battery_flag -xvs` | ❌ W0 | ⬜ pending |
| 12-01-07 | 01 | 0 | VAL-01 | unit | `pytest tests/test_capacity_estimator.py::test_quality_filter -xvs` | ❌ W0 | ⬜ pending |
| 12-01-08 | 01 | 0 | VAL-02 | unit | `pytest tests/test_capacity_estimator.py::test_peukert_parameter -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_capacity_estimator.py` — stubs for CAP-01, CAP-02, CAP-03, VAL-01, VAL-02
- [ ] `tests/conftest.py` — fixtures for discharge data (synthetic + 2026-03-12 replay)
- [ ] `tests/test_model.py::test_capacity_estimate_persistence` — atomic JSON write tests for CAP-04
- [ ] `tests/test_monitor.py::test_new_battery_flag` — integration test for CAP-05

*Framework already installed (pytest 7.x from Phase 1 v1.1).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MOTD shows capacity info | CAP-01 | Shell output formatting | Run `motd/51-ups.sh` after model.json has capacity data; verify format matches spec |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
