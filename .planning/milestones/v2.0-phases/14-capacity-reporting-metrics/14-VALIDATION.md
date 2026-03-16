---
phase: 14
slug: capacity-reporting-metrics
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-16
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.5 |
| **Config file** | pytest.ini (testpaths=tests, addopts=-v --tb=short) |
| **Quick run command** | `pytest tests/test_motd.py tests/test_monitor.py -v --tb=short` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~10 seconds (full suite, 295+ tests) |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_motd.py tests/test_monitor.py -v --tb=short`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 14-01-01 | 01 | 1 | RPT-01 | integration | `pytest tests/test_motd.py::test_motd_capacity_displays -v` | ❌ W0 | ⬜ pending |
| 14-01-02 | 01 | 1 | RPT-01 | unit | `pytest tests/test_motd.py::test_motd_handles_empty_estimates -v` | ❌ W0 | ⬜ pending |
| 14-01-03 | 01 | 1 | RPT-01 | unit | `pytest tests/test_motd.py::test_motd_convergence_status_badge -v` | ❌ W0 | ⬜ pending |
| 14-02-01 | 02 | 1 | RPT-02 | unit | `pytest tests/test_monitor.py::test_journald_capacity_event_logged -v` | ❌ W0 | ⬜ pending |
| 14-02-02 | 02 | 1 | RPT-02 | unit | `pytest tests/test_monitor.py::test_journald_baseline_lock_event -v` | ❌ W0 | ⬜ pending |
| 14-02-03 | 02 | 1 | RPT-02 | integration | `pytest tests/test_monitor_integration.py::test_journald_event_filtering -v` | ❌ W0 | ⬜ pending |
| 14-03-01 | 03 | 1 | RPT-03 | unit | `pytest tests/test_monitor.py::test_health_endpoint_capacity_fields -v` | ❌ W0 | ⬜ pending |
| 14-03-02 | 03 | 1 | RPT-03 | unit | `pytest tests/test_monitor.py::test_health_endpoint_convergence_flag -v` | ❌ W0 | ⬜ pending |
| 14-03-03 | 03 | 1 | RPT-03 | integration | `pytest tests/test_monitor_integration.py::test_health_endpoint_capacity_persistence -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_motd.py::test_motd_capacity_displays` — MOTD shows correct Ah, confidence%, sample count format
- [ ] `tests/test_motd.py::test_motd_handles_empty_estimates` — MOTD exits cleanly if capacity_estimates missing
- [ ] `tests/test_motd.py::test_motd_convergence_status_badge` — MOTD shows LOCKED or MEASURING badge
- [ ] `tests/test_monitor.py::test_journald_capacity_event_logged` — Verify EVENT_TYPE=capacity_measurement in journald
- [ ] `tests/test_monitor.py::test_journald_baseline_lock_event` — Verify EVENT_TYPE=baseline_lock when CoV < 10%
- [ ] `tests/test_monitor_integration.py::test_journald_event_filtering` — Query journalctl by EVENT_TYPE
- [ ] `tests/test_monitor.py::test_health_endpoint_capacity_fields` — /health endpoint contains new capacity fields
- [ ] `tests/test_monitor.py::test_health_endpoint_convergence_flag` — capacity_converged flag matches get_convergence_status()
- [ ] `tests/test_monitor_integration.py::test_health_endpoint_capacity_persistence` — Verify /health persists across discharge cycles

*Existing infrastructure covers framework and fixture requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MOTD renders correctly in SSH terminal | RPT-01 | Terminal rendering (colors, badges) not testable in pytest | SSH to server, verify `Capacity:` line in MOTD output |
| Grafana can scrape /health capacity fields | RPT-03 | Requires live Grafana instance | Add /health scrape target, verify capacity_ah_measured appears in Explore |
| Grafana query shows convergence scatter plot | RPT-03 | Requires Grafana UI with data over time | Import provided query, verify scatter plot renders with sample data |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
