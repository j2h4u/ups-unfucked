---
phase: 3
slug: virtual-ups-safe-shutdown
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.4.0+ (existing from Phase 1) |
| **Config file** | `pytest.ini` (existing) |
| **Quick run command** | `pytest tests/test_virtual_ups.py -v` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_virtual_ups.py -v`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 3-01-01 | 01 | 0 | VUPS-01 | unit | `pytest tests/test_virtual_ups.py::test_write_to_tmpfs -v` | ❌ Wave 0 | ⬜ pending |
| 3-01-02 | 01 | 0 | VUPS-02 | unit | `pytest tests/test_virtual_ups.py::test_passthrough_fields -v` | ❌ Wave 0 | ⬜ pending |
| 3-01-03 | 01 | 1 | VUPS-03 | unit | `pytest tests/test_virtual_ups.py::test_field_overrides -v` | ❌ Wave 0 | ⬜ pending |
| 3-01-04 | 01 | 1 | VUPS-04 | unit | `pytest tests/test_virtual_ups.py::test_nut_format_compliance -v` | ❌ Wave 0 | ⬜ pending |
| 3-02-01 | 02 | 1 | SHUT-01 | unit | `pytest tests/test_virtual_ups.py::test_lb_flag_threshold -v` | ❌ Wave 0 | ⬜ pending |
| 3-02-02 | 02 | 1 | SHUT-02 | unit | `pytest tests/test_virtual_ups.py::test_configurable_threshold -v` | ❌ Wave 0 | ⬜ pending |
| 3-02-03 | 02 | 1 | SHUT-03 | unit | `pytest tests/test_virtual_ups.py::test_calibration_mode_threshold -v` | ❌ Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_virtual_ups.py` — stubs for VUPS-01 through SHUT-03
- [ ] `src/virtual_ups.py` — new module: write_virtual_ups_dev() and ups_status_override_logic()
- [ ] `src/monitor.py` — integration: call write_virtual_ups_dev() in polling loop, compute ups.status override
- [ ] `systemd/ups-battery-monitor.service` — Add `After=sysinit.target` (ensure /dev/shm is ready)

*Existing infrastructure from Phase 1–2 covers conftest.py fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| dummy-ups reads /dev/shm/ups-virtual.dev correctly | VUPS-04 | Requires live NUT daemon | `upsc cyberpower-virtual@localhost` shows overridden fields |
| upsmon receives LB and initiates graceful shutdown | SHUT-01 | Requires real/simulated power event | Trigger via `upsmon -c fsd` with time_rem < threshold; confirm clean shutdown |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
