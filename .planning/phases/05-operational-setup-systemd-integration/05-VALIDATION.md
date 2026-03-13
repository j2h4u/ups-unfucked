---
phase: 5
slug: operational-setup-systemd-integration
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 5 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 + systemd-python 234 |
| **Config file** | pytest.ini (existing, covers Phase 1-4) |
| **Quick run command** | `python3 -m pytest tests/ -k "test_" --tb=short` |
| **Full suite command** | `python3 -m pytest tests/ -v --cov=src --tb=short` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/ --tb=short -q`
- **After every plan wave:** Run `python3 -m pytest tests/ -v --cov=src --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 5-01-01 | 01 | 1 | OPS-01 | system | `systemctl show -p Restart ups-battery-monitor` | ✅ | ⬜ pending |
| 5-01-02 | 01 | 1 | OPS-01 | system | `systemctl is-enabled ups-battery-monitor` | ✅ | ⬜ pending |
| 5-01-03 | 01 | 1 | OPS-02 | integration | `bash install.sh --dry-run` | ❌ W0 | ⬜ pending |
| 5-01-04 | 01 | 1 | OPS-02 | integration | `grep -c "cyberpower-virtual" /etc/nut/ups.conf` | ❌ W0 | ⬜ pending |
| 5-01-05 | 01 | 1 | OPS-03 | unit | `id -u $(systemctl show -p User ups-battery-monitor --value)` | ✅ | ⬜ pending |
| 5-01-06 | 01 | 1 | OPS-04 | unit | `journalctl -u ups-battery-monitor --no-pager -n 1` | ✅ | ⬜ pending |
| 5-01-07 | 01 | 1 | OPS-04 | unit | pytest test_journald_logging.py | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `install.sh` — installation script with prerequisite validation and dry-run mode
- [ ] `tests/test_logging.py` — JournalHandler fallback test (mock /dev/log, verify stderr)
- [ ] Verify `systemd-python` installed (`import systemd.journal`)

*Existing infrastructure (pytest, conftest.py) covers remaining requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Service restarts after crash | OPS-01 | Requires killing running process | `systemctl start ups-battery-monitor && kill -9 $(systemctl show -p MainPID --value ups-battery-monitor) && sleep 3 && systemctl is-active ups-battery-monitor` |
| NUT config merge idempotent | OPS-02 | Modifies /etc/nut/ups.conf | Run `install.sh` twice, verify single dummy-ups block |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
