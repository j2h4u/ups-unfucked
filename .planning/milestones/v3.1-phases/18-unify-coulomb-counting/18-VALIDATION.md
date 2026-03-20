---
phase: 18
slug: unify-coulomb-counting
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-20
---

# Phase 18 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | `pytest.ini` |
| **Quick run command** | `cd /opt/ups-battery-monitor && python -m pytest tests/ -x -q` |
| **Full suite command** | `cd /opt/ups-battery-monitor && python -m pytest tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run quick suite
- **After every plan wave:** Run full suite
- **Before `/gsd:verify-work`:** Full suite must be green (476+ tests)
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 18-01-01 | 01 | 1 | ARCH-01 | unit + accuracy | `python -m pytest tests/test_integration_math.py -v` | ❌ W0 | ⬜ pending |
| 18-01-02 | 01 | 1 | ARCH-02 | regression | `python -m pytest tests/ -x -q` | ✅ | ⬜ pending |
| 18-01-03 | 01 | 1 | ARCH-01, ARCH-02 | regression gate | `python -m pytest tests/ -v` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_integration_math.py` — stub for integrate_current() unit + accuracy comparison test (created by Task 1)

---

## Grep Verification Checks

| Check | Command | Expected |
|-------|---------|----------|
| integrate_current exists in battery_math | `grep -r "def integrate_current(" src/battery_math/` | 1 match in integration.py |
| integrate_current exported | `grep "integrate_current" src/battery_math/__init__.py` | 1 match |
| Old _integrate_current removed | `grep -r "def _integrate_current(" src/` | 0 matches |
| _check_alerts has avg_load param | `grep "def _check_alerts.*avg_load" src/discharge_handler.py` | 1 match |
| _check_alerts no self._avg_load | `grep "_avg_load" src/discharge_handler.py` | Present but NOT inside _check_alerts |
