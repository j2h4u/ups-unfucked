---
phase: 7
slug: safety-critical-metrics
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-15
---

# Phase 7 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `pytest tests/test_monitor.py -v` |
| **Full suite command** | `pytest tests/ -v --tb=short` |
| **Estimated runtime** | ~10-15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_monitor.py -v`
- **After every plan wave:** Run `pytest tests/ -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 7-01-01 | 01 | 1 | SAFE-01 | integration | `pytest tests/test_monitor.py::test_per_poll_writes_during_blackout -v` | ❌ W0 | ⬜ pending |
| 7-01-02 | 01 | 1 | SAFE-02 | integration | `pytest tests/test_monitor.py::test_handle_event_transition_per_poll_during_ob -v` | ❌ W0 | ⬜ pending |
| 7-01-03 | 01 | 1 | SAFE-01 | unit | `pytest tests/test_monitor.py::test_no_writes_during_online_state -v` | ❌ W0 | ⬜ pending |
| 7-01-04 | 01 | 1 | SAFE-02 | integration | `pytest tests/test_monitor.py::test_lb_flag_signal_latency -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_monitor.py::test_per_poll_writes_during_blackout` — stub for SAFE-01 per-poll writes
- [ ] `tests/test_monitor.py::test_handle_event_transition_per_poll_during_ob` — stub for SAFE-02 per-poll LB decision
- [ ] `tests/test_monitor.py::test_no_writes_during_online_state` — stub for SAFE-01 OL-state silence
- [ ] `tests/test_monitor.py::test_lb_flag_signal_latency` — stub for SAFE-02 latency check
- [ ] `tests/conftest.py` — fixture `mock_event_type_during_poll_sequence` if needed

*Existing infrastructure covers framework and base fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| upsmon receives LB within 10s | SAFE-02 | Requires real upsmon + systemd journal | 1. Start daemon with dummy-ups 2. Simulate OB transition 3. Check journal timing |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
