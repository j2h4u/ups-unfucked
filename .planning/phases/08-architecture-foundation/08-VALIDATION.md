---
phase: 8
slug: architecture-foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-15
---

# Phase 8 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7+ |
| **Config file** | pytest.ini |
| **Quick run command** | `pytest tests/test_monitor.py -x` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_monitor.py -x`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-00-01 | 00 | 0 | ARCH-01 | unit | `pytest tests/test_monitor.py -k "metrics" -x` | ❌ W0 | ⬜ pending |
| 08-00-02 | 00 | 0 | ARCH-02 | unit | `pytest tests/test_monitor.py -k "config" -x` | ❌ W0 | ⬜ pending |
| 08-00-03 | 00 | 0 | ARCH-02 | unit | `pytest tests/test_monitor.py -k "immutability" -x` | ❌ W0 | ⬜ pending |
| 08-01-01 | 01 | 1 | ARCH-01 | unit | `pytest tests/test_monitor.py -k "metrics" -x` | ❌ W0 | ⬜ pending |
| 08-02-01 | 02 | 1 | ARCH-02 | unit | `pytest tests/test_monitor.py -k "config" -x` | ❌ W0 | ⬜ pending |
| 08-03-01 | 03 | 1 | ARCH-03 | integration | `python -c "from src.monitor import Monitor"` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_monitor.py::test_current_metrics_dataclass` — verify CurrentMetrics instantiation with all fields
- [ ] `tests/test_monitor.py::test_config_dataclass` — verify Config frozen semantics and __init__ injection
- [ ] `tests/test_monitor.py::test_config_immutability` — verify FrozenInstanceError on config field mutation
- [ ] `tests/conftest.py` update — create `config_fixture()` and `current_metrics_fixture()` for test reuse

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
