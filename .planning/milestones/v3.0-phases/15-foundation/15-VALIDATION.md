---
phase: 15
slug: foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-17
---

# Phase 15 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.0+, unittest.mock |
| **Config file** | pyproject.toml (existing) |
| **Quick run command** | `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -v` |
| **Full suite command** | `python3 -m pytest tests/ -v` |
| **Estimated runtime** | ~30 seconds (full suite) |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -x` (~5 sec)
- **After every plan wave:** Run `python3 -m pytest tests/ -v --tb=short` (~30 sec)
- **Before `/gsd:verify-work`:** Full suite must be green + live INSTCMD test successful
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 15-01-01 | 01 | 1 | SULF-06 | unit | `pytest tests/test_sulfation.py::test_compute_sulfation_score_* -v` | ❌ W0 | ⬜ pending |
| 15-01-02 | 01 | 1 | SULF-06 | integration | `pytest tests/test_sulfation.py::test_sulfation_with_year_simulation -v` | ❌ W0 | ⬜ pending |
| 15-02-01 | 02 | 1 | SCHED-02 | unit | `pytest tests/test_nut_client.py::test_send_instcmd_* -v` | ❌ W0 | ⬜ pending |
| 15-02-02 | 02 | 1 | SCHED-02 | manual | `bash scripts/test_instcmd_live.sh --ups cyberpower --quick` | ❌ W0 | ⬜ pending |
| 15-03-01 | 03 | 2 | Zero regression | integration | `pytest tests/test_monitor.py tests/test_monitor_integration.py -v` | ✅ existing | ⬜ pending |
| 15-03-02 | 03 | 2 | Zero regression | integration | `pytest tests/test_year_simulation.py -v` | ✅ existing | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_sulfation.py` — Unit tests for compute_sulfation_score(), estimate_recovery_delta()
- [ ] `tests/test_cycle_roi.py` — Unit tests for compute_cycle_roi()
- [ ] `tests/test_nut_client.py` (extend) — Tests for send_instcmd() method
- [ ] `tests/test_sulfation_offline_harness.py` — Year-simulation integration with synthetic discharge curves
- [ ] `scripts/test_instcmd_live.sh` — Live UT850EG validation script

*Existing NUT client and daemon tests are sufficient for regression detection.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live UT850EG INSTCMD dispatch | SCHED-02 | Requires real UPS hardware | Run `scripts/test_instcmd_live.sh --ups cyberpower --quick`, confirm test.result updates in `upsc cyberpower` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
