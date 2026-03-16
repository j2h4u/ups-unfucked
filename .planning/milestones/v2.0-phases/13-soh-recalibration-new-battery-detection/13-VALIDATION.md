---
phase: 13
slug: soh-recalibration-new-battery-detection
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-16
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (matching Phase 12) |
| **Config file** | pyproject.toml (no test-specific config) |
| **Quick run command** | `pytest tests/test_model.py tests/test_replacement_predictor.py -xvs` |
| **Full suite command** | `pytest tests/ -x` |
| **Estimated runtime** | ~45 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_model.py tests/test_replacement_predictor.py -xvs`
- **After every plan wave:** Run `pytest tests/ -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 45 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 1 | SOH-01 | unit | `pytest tests/test_soh_calculator.py::test_soh_with_measured_capacity -xvs` | ❌ W0 | ⬜ pending |
| 13-01-02 | 01 | 1 | SOH-01 | unit | `pytest tests/test_soh_calculator.py::test_soh_with_rated_capacity_fallback -xvs` | ❌ W0 | ⬜ pending |
| 13-01-03 | 01 | 1 | SOH-02 | unit | `pytest tests/test_model.py::test_soh_history_entry_with_baseline -xvs` | ❌ W0 | ⬜ pending |
| 13-01-04 | 01 | 1 | SOH-02 | unit | `pytest tests/test_replacement_predictor.py::test_regression_backward_compat -xvs` | ❌ W0 | ⬜ pending |
| 13-01-05 | 01 | 1 | SOH-03 | unit | `pytest tests/test_replacement_predictor.py::test_regression_filters_by_baseline -xvs` | ❌ W0 | ⬜ pending |
| 13-01-06 | 01 | 1 | SOH-03 | unit | `pytest tests/test_replacement_predictor.py::test_regression_min_entries_per_baseline -xvs` | ❌ W0 | ⬜ pending |
| 13-02-01 | 02 | 2 | SOH-01,02,03 | integration | `pytest tests/test_monitor_integration.py::test_soh_recalibration_flow -xvs` | ❌ W0 | ⬜ pending |
| 13-02-02 | 02 | 2 | New battery | unit | `pytest tests/test_monitor.py::test_new_battery_detection_threshold -xvs` | ❌ W0 | ⬜ pending |
| 13-02-03 | 02 | 2 | New battery | unit | `pytest tests/test_monitor.py::test_new_battery_detection_requires_convergence -xvs` | ❌ W0 | ⬜ pending |
| 13-02-04 | 02 | 2 | Baseline reset | unit | `pytest tests/test_model.py::test_baseline_reset_clears_estimates -xvs` | ❌ W0 | ⬜ pending |
| 13-02-05 | 02 | 2 | Baseline reset | unit | `pytest tests/test_model.py::test_baseline_reset_creates_entry -xvs` | ❌ W0 | ⬜ pending |
| 13-02-06 | 02 | 2 | MOTD | integration | `pytest tests/test_motd.py::test_motd_shows_new_battery_alert -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_soh_calculator.py` — add tests for `calculate_soh_from_discharge()` with measured capacity parameter (SOH-01)
- [ ] `tests/test_model.py` — add tests for `add_soh_history_entry(capacity_ah_ref)` signature and backward compat (SOH-02)
- [ ] `tests/test_replacement_predictor.py` — add tests for `linear_regression_soh(capacity_ah_ref)` filtering (SOH-03)
- [ ] `tests/test_monitor_integration.py` — add end-to-end test: measured capacity → SoH update → regression filter
- [ ] `tests/test_monitor.py` — add tests for new battery detection logic (>10% threshold, convergence check)

*Framework install: already in place (pytest 8.x); no new dependencies needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MOTD displays new battery alert with correct formatting | New battery | Requires real shell environment and MOTD runner | Run `motd/51-ups.sh` with model.json containing `new_battery_detected: true`; verify alert text |
| Real discharge triggers new battery detection | New battery | Requires physical UPS discharge event | Wait for power outage or trigger UPS test; verify model.json flag and journald log |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 45s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
