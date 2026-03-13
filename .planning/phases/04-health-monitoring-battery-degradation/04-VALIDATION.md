---
phase: 4
slug: health-monitoring-battery-degradation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (established in Phase 1) |
| **Config file** | tests/conftest.py (reused from Phase 1) |
| **Quick run command** | `pytest tests/test_soh_calculator.py tests/test_replacement_predictor.py tests/test_alerter.py -x` |
| **Full suite command** | `pytest tests/ -x` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_soh_calculator.py tests/test_replacement_predictor.py tests/test_alerter.py -x`
- **After every plan wave:** Run `pytest tests/ -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 04-01-01 | 01 | 0 | HLTH-01 | unit | `pytest tests/test_soh_calculator.py -x` | ❌ W0 | ⬜ pending |
| 04-01-02 | 01 | 0 | HLTH-02 | unit | `pytest tests/test_replacement_predictor.py -x` | ❌ W0 | ⬜ pending |
| 04-01-03 | 01 | 0 | HLTH-04, HLTH-05 | unit | `pytest tests/test_alerter.py -x` | ❌ W0 | ⬜ pending |
| 04-02-01 | 02 | 1 | HLTH-01 | unit | `pytest tests/test_soh_calculator.py -x` | ❌ W0 | ⬜ pending |
| 04-02-02 | 02 | 1 | HLTH-02 | unit | `pytest tests/test_replacement_predictor.py -x` | ❌ W0 | ⬜ pending |
| 04-03-01 | 03 | 1 | HLTH-04, HLTH-05 | unit | `pytest tests/test_alerter.py -x` | ❌ W0 | ⬜ pending |
| 04-04-01 | 04 | 2 | HLTH-03 | integration | Manual SSH login; grep MOTD output | N/A | ⬜ pending |
| 04-05-01 | 05 | 2 | ALL | integration | `pytest tests/ -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_soh_calculator.py` — stubs for HLTH-01 (area-under-curve, trapezoidal rule, edge cases)
- [ ] `tests/test_replacement_predictor.py` — stubs for HLTH-02 (linear regression, R², extrapolation, thresholds)
- [ ] `tests/test_alerter.py` — stubs for HLTH-04, HLTH-05 (journald integration, structured fields, threshold logic)

*Existing pytest infrastructure from Phase 1 covers framework setup.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MOTD displays health status on SSH login | HLTH-03 | Requires real SSH session + virtual UPS running | SSH into server; verify line shows charge%, runtime, load%, SoH, replacement date |
| journald alerts searchable via journalctl | HLTH-04, HLTH-05 | Requires running systemd service | `journalctl -u ups-battery-monitor -p warning --since "1 hour ago"` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
