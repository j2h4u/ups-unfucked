---
phase: 11
slug: polish-future-prep
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-15
---

# Phase 11 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 + pytest-cov 5.0.0 |
| **Config file** | ./pytest.ini |
| **Quick run command** | `pytest tests/test_model.py tests/test_ema_filter.py tests/test_alerter.py -v` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_model.py tests/test_ema_filter.py tests/test_alerter.py -v`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 11-01-01 | 01 | 1 | LOW-01 | unit | `pytest tests/test_model.py::test_prune_soh_history -xvs` | ❌ W0 | ⬜ pending |
| 11-01-02 | 01 | 1 | LOW-01 | unit | `pytest tests/test_model.py::test_prune_r_internal_history -xvs` | ❌ W0 | ⬜ pending |
| 11-01-03 | 01 | 1 | LOW-02 | unit | `pytest tests/test_model.py::test_atomic_write_uses_fdatasync -xvs` | ❌ W0 | ⬜ pending |
| 11-01-04 | 01 | 1 | LOW-03 | unit | `pytest tests/test_ema_filter.py::test_metric_ema_generic -xvs` | ❌ W0 | ⬜ pending |
| 11-01-05 | 01 | 1 | LOW-03 | unit | `pytest tests/test_ema_filter.py::test_metric_ema_multiple_metrics -xvs` | ❌ W0 | ⬜ pending |
| 11-01-06 | 01 | 1 | LOW-04 | unit | `pytest tests/test_alerter.py::test_no_setup_ups_logger_wrapper -xvs` | ❌ W0 | ⬜ pending |
| 11-01-07 | 01 | 1 | LOW-04 | unit | `pytest tests/test_alerter.py::test_alerter_uses_standard_logging -xvs` | ❌ W0 | ⬜ pending |
| 11-01-08 | 01 | 1 | LOW-05 | integration | `pytest tests/test_monitor.py::test_health_endpoint_updates_on_poll -xvs` | ❌ W0 | ⬜ pending |
| 11-01-09 | 01 | 1 | LOW-05 | unit | `pytest tests/test_monitor.py::test_health_endpoint_structure -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_model.py` — stubs for test_prune_soh_history, test_prune_r_internal_history, test_atomic_write_uses_fdatasync
- [ ] `tests/test_ema_filter.py` — stubs for test_metric_ema_generic, test_metric_ema_multiple_metrics
- [ ] `tests/test_alerter.py` — stubs for test_no_setup_ups_logger_wrapper, test_alerter_uses_standard_logging
- [ ] `tests/test_monitor.py` — stubs for test_health_endpoint_updates_on_poll, test_health_endpoint_structure

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| fdatasync syscall used (not fsync) | LOW-02 | Requires strace on live daemon | `strace -e fdatasync,fsync python -m ups_battery_monitor` during save |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
