---
phase: 16
slug: persistence-observability
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-17
---

# Phase 16 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.0+ (existing) |
| **Config file** | `pytest.ini` |
| **Quick run command** | `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -v` |
| **Full suite command** | `python3 -m pytest tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -x`
- **After every plan wave:** Run `python3 -m pytest tests/ -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 16-01-01 | 01 | 0 | SULF-05 | integration | `pytest tests/test_sulfation_persistence.py -v` | ❌ W0 | ⬜ pending |
| 16-01-02 | 01 | 0 | RPT-01 | integration | `pytest tests/test_health_endpoint_v16.py -v` | ❌ W0 | ⬜ pending |
| 16-01-03 | 01 | 0 | RPT-02 | integration | `pytest tests/test_journald_sulfation_events.py -v` | ❌ W0 | ⬜ pending |
| 16-01-04 | 01 | 0 | RPT-03 | integration | `pytest tests/test_discharge_event_logging.py -v` | ❌ W0 | ⬜ pending |
| 16-XX-XX | XX | 1 | SULF-01 | unit | `pytest tests/test_sulfation.py::TestComputeSulfationScore -v` | ✅ | ⬜ pending |
| 16-XX-XX | XX | 1 | SULF-02 | unit | `pytest tests/test_sulfation.py -k "physics or baseline" -v` | ✅ | ⬜ pending |
| 16-XX-XX | XX | 1 | SULF-03 | unit | `pytest tests/test_sulfation.py -k "ir_trend" -v` | ✅ | ⬜ pending |
| 16-XX-XX | XX | 1 | SULF-04 | unit | `pytest tests/test_sulfation.py -k "recovery" -v` | ✅ | ⬜ pending |
| 16-XX-XX | XX | 1 | ROI-01 | unit | `pytest tests/test_cycle_roi.py -v` | ✅ | ⬜ pending |
| 16-XX-XX | XX | 1 | ROI-02 | unit | `pytest tests/test_cycle_roi.py::TestROIFactors -v` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_sulfation_persistence.py` — stubs for SULF-05 (model.json schema, append methods, pruning)
- [ ] `tests/test_health_endpoint_v16.py` — stubs for RPT-01 (health.json includes sulfation_score, confidence, days_since_deep)
- [ ] `tests/test_journald_sulfation_events.py` — stubs for RPT-02 (structured event logging with event_type, event_reason fields)
- [ ] `tests/test_discharge_event_logging.py` — stubs for RPT-03 (discharge_events array in model.json persisted)

*Framework: pytest already installed. No additional test dependencies needed. Systemd journal testing uses mocked JournalHandler (existing pattern in test_logging.py)*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MOTD displays sulfation_score, next_test_eta, blackout_credit_countdown | RPT-01 | Shell output formatting requires visual check | SSH to server, trigger discharge event, verify MOTD updates on next login |
| Event reason distinguishes natural vs test-initiated | SULF-05 | Requires real UPS discharge event | Review journald after blackout: `journalctl -t ups-battery-monitor --since "1h ago"` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
