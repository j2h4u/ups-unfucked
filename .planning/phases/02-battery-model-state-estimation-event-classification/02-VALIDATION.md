---
phase: 2
slug: battery-model-state-estimation-event-classification
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-13
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.0+ |
| **Config file** | `tests/conftest.py` (reuse Phase 1 fixtures) |
| **Quick run command** | `pytest tests/test_soc_predictor.py tests/test_runtime_calculator.py tests/test_event_classifier.py -v --tb=short` |
| **Full suite command** | `pytest tests/ -v --cov=src --tb=short` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_soc_predictor.py tests/test_runtime_calculator.py tests/test_event_classifier.py -x`
- **After every plan wave:** Run `pytest tests/ -v --cov=src --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 2-01-01 | 01 | 0 | PRED-01 | unit | `pytest tests/test_soc_predictor.py -x` | ❌ W0 | ⬜ pending |
| 2-01-02 | 01 | 0 | PRED-02 | unit | `pytest tests/test_runtime_calculator.py -x` | ❌ W0 | ⬜ pending |
| 2-01-03 | 01 | 0 | EVT-01 | unit | `pytest tests/test_event_classifier.py -x` | ❌ W0 | ⬜ pending |
| 2-02-01 | 02 | 1 | PRED-01 | unit | `pytest tests/test_soc_predictor.py::test_soc_interpolation -xvs` | ❌ W0 | ⬜ pending |
| 2-02-02 | 02 | 1 | PRED-01 | unit | `pytest tests/test_soc_predictor.py::test_soc_exact_point -xvs` | ❌ W0 | ⬜ pending |
| 2-02-03 | 02 | 1 | PRED-01 | unit | `pytest tests/test_soc_predictor.py::test_soc_clamp_high -xvs` | ❌ W0 | ⬜ pending |
| 2-02-04 | 02 | 1 | PRED-01 | unit | `pytest tests/test_soc_predictor.py::test_soc_clamp_low -xvs` | ❌ W0 | ⬜ pending |
| 2-02-05 | 02 | 1 | PRED-03 | unit | `pytest tests/test_soc_predictor.py::test_charge_percentage -xvs` | ❌ W0 | ⬜ pending |
| 2-03-01 | 03 | 1 | PRED-02 | integration | `pytest tests/test_runtime_calculator.py::test_peukert_blackout_match -xvs` | ❌ W0 | ⬜ pending |
| 2-03-02 | 03 | 1 | PRED-02 | unit | `pytest tests/test_runtime_calculator.py::test_peukert_zero_soc -xvs` | ❌ W0 | ⬜ pending |
| 2-03-03 | 03 | 1 | PRED-02 | unit | `pytest tests/test_runtime_calculator.py::test_peukert_zero_load -xvs` | ❌ W0 | ⬜ pending |
| 2-04-01 | 04 | 1 | EVT-01 | unit | `pytest tests/test_event_classifier.py::test_classify_real_blackout -xvs` | ❌ W0 | ⬜ pending |
| 2-04-02 | 04 | 1 | EVT-01 | unit | `pytest tests/test_event_classifier.py::test_classify_battery_test -xvs` | ❌ W0 | ⬜ pending |
| 2-04-03 | 04 | 1 | EVT-02 | integration | Manual | ✅ manual | ⬜ pending |
| 2-04-04 | 04 | 1 | EVT-03 | integration | Manual | ✅ manual | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_soc_predictor.py` — stubs for PRED-01, PRED-03 (LUT, clamping, interpolation)
- [ ] `tests/test_runtime_calculator.py` — stubs for PRED-02 (Peukert formula, edge cases)
- [ ] `tests/test_event_classifier.py` — stubs for EVT-01 (state machine transitions)
- [ ] `tests/conftest.py` — extend with mock LUT fixtures (Phase 1 fixtures already exist)

*pytest available from Phase 1 — no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| BLACKOUT_REAL triggers daemon shutdown call | EVT-02 | Requires live NUT integration or daemon mock | Simulate blackout in integration test; confirm runtime_minutes() called |
| BLACKOUT_TEST suppresses shutdown signal | EVT-03 | Requires live NUT integration or daemon mock | Simulate battery test (input.voltage=230V, OB DISCHRG); confirm no shutdown |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
