---
phase: 10
slug: code-quality-efficiency
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-14
---

# Phase 10 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (existing from Phase 9) |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `pytest tests/ -x --tb=short` |
| **Full suite command** | `pytest tests/ -x` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/ -x --tb=short`
- **After every plan wave:** Run `pytest tests/ -x`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 10-01-01 | 01 | 1 | QUAL-01 | unit | `pytest tests/test_monitor.py -k safe_save -xvs` | ✅ Existing; add new test | ⬜ pending |
| 10-01-02 | 01 | 1 | QUAL-02 | unit | `pytest tests/test_model.py::test_default_vrla_lut_uses_current_date -xvs` | ❌ W0 | ⬜ pending |
| 10-01-03 | 01 | 1 | QUAL-03 | docstring | `grep "linear scan" src/soc_predictor.py` | N/A | ⬜ pending |
| 10-01-04 | 01 | 1 | QUAL-04 | unit | `pytest tests/test_model.py::test_calibration_batch_flush -xvs` | ❌ W0 | ⬜ pending |
| 10-01-05 | 01 | 1 | QUAL-05 | unit | `pytest tests/test_virtual_ups.py::test_write_failure_single_log -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_model.py::test_default_vrla_lut_uses_current_date` — verify date is today, not hardcoded
- [ ] `tests/test_model.py::test_calibration_batch_flush` — verify N calibration_write() calls → 1 model.save()
- [ ] `tests/test_virtual_ups.py::test_write_failure_single_log` — verify single error log on write failure

*Existing Phase 9 test suite (160+ tests) acts as regression oracle for all refactors.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docstring says "linear scan" | QUAL-03 | Source text verification | `grep "linear scan" src/soc_predictor.py` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
