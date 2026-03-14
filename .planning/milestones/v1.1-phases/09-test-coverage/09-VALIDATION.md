---
phase: 9
slug: test-coverage
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 9 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 (Python 3.13.5) |
| **Config file** | `pytest.ini` |
| **Quick run command** | `python3 -m pytest tests/test_monitor.py -x -v` |
| **Full suite command** | `python3 -m pytest tests/ --cov=src --cov-report=term-missing` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/test_monitor.py -x -v`
- **After every plan wave:** Run `python3 -m pytest tests/ --cov=src --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 9-01-01 | 01 | 1 | TEST-04 | Unit | `pytest tests/conftest.py -xvs` | Partial | ⬜ pending |
| 9-01-02 | 01 | 1 | TEST-05 | Unit | `pytest tests/test_soc_predictor.py -xvs` | ❌ W0 | ⬜ pending |
| 9-02-01 | 02 | 2 | TEST-02 | Unit | `pytest tests/test_monitor.py::test_auto_calibrate_peukert -xvs` | ❌ W0 | ⬜ pending |
| 9-02-02 | 02 | 2 | TEST-03 | Unit | `pytest tests/test_monitor.py::test_signal_handler -xvs` | ❌ W0 | ⬜ pending |
| 9-03-01 | 03 | 3 | TEST-01 | Integration | `pytest tests/test_monitor.py::test_ol_ob_ol_lifecycle -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/conftest.py` — update mock_socket_ok or add mock_socket_list_var with proper LIST VAR format
- [ ] `tests/test_soc_predictor.py` — stub for floating-point tolerance test
- [ ] `tests/test_monitor.py` — stubs for Peukert, signal handler, OL→OB→OL lifecycle

*Existing infrastructure covers most needs; updates to conftest.py mock format are the primary gap.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
