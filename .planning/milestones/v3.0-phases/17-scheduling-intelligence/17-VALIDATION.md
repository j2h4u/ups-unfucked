---
phase: 17
slug: scheduling-intelligence
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-17
---

# Phase 17 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 |
| **Config file** | pytest.ini |
| **Quick run command** | `pytest tests/test_scheduler.py -v` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_scheduler.py -v`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 17-01-01 | 01 | 1 | SCHED-01 | unit + integration | `pytest tests/test_scheduler.py::test_propose_deep_test_high_sulfation -xvs` | ❌ W0 | ⬜ pending |
| 17-01-02 | 01 | 1 | SCHED-03 | unit | `pytest tests/test_scheduler.py::test_blackout_credit_active_blocks_test -xvs` | ❌ W0 | ⬜ pending |
| 17-01-03 | 01 | 1 | SCHED-04 | unit | `pytest tests/test_scheduler.py::test_precondition_blocks_test_during_ob -xvs` | ❌ W0 | ⬜ pending |
| 17-01-04 | 01 | 1 | SCHED-05 | unit | `pytest tests/test_scheduler.py::test_soh_floor_blocks_deep_test -xvs` | ❌ W0 | ⬜ pending |
| 17-01-05 | 01 | 1 | SCHED-06 | unit | `pytest tests/test_scheduler.py::test_grid_instability_defers_test -xvs` | ❌ W0 | ⬜ pending |
| 17-01-06 | 01 | 1 | SCHED-07 | integration | `pytest tests/test_systemd_integration.py::test_daemon_masks_legacy_timers -xvs` | ❌ W0 | ⬜ pending |
| 17-01-07 | 01 | 1 | SCHED-08 | unit | `pytest tests/test_discharge_handler.py::test_classify_test_initiated_discharge -xvs` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_scheduler.py` — stubs for SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06 (8 tests covering all gates + decision tree)
- [ ] `tests/test_dispatch.py` — stubs for upscmd dispatch with preconditions, error handling, model.json updates
- [ ] `tests/test_systemd_integration.py` — stub for SCHED-07 (daemon masks legacy timers)
- [ ] `src/battery_math/scheduler.py` — pure scheduler function skeleton (SchedulerDecision dataclass + evaluate_test_scheduling)

*Existing infrastructure covers framework install — pytest already installed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Journald decision logs readable | SCHED-01 | Requires live daemon + journald | Start daemon, wait for daily evaluation, `journalctl -t ups-battery-monitor --since "1 hour ago" \| grep -E "propose_test\|defer_test\|block_test"` |
| systemd timers masked on startup | SCHED-07 | Requires systemctl access | `systemctl status ups-test-deep.timer ups-test-quick.timer` — must show "masked" |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
