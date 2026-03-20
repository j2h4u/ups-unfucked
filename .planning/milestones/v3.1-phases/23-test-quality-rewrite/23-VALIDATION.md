---
phase: 23
slug: test-quality-rewrite
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 23 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `python3 -m pytest tests/ -x -q` |
| **Full suite command** | `python3 -m pytest tests/ -v` |
| **Estimated runtime** | ~3 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/ -x -q`
- **After every plan wave:** Run `python3 -m pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | TEST-01 | meta-test | `grep -c assert_called tests/test_monitor.py` returns 0 | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-02 | meta-test | `python3 -m pytest tests/test_monitor.py -v` all pass | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-03 | unit | `grep -c "patch.*Path" tests/test_virtual_ups.py` returns 0 | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-04 | meta-test | `grep -cP "assert.*_\w+.*called" tests/test_monitor.py` returns 0 | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-05 | integration | `python3 -m pytest tests/test_monitor_integration.py -v` all pass | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-06 | marker | `grep "pytest.mark.slow" tests/test_capacity_estimator.py` finds match | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-07 | marker | `grep "pytest.mark.integration" tests/test_motd.py` finds match | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-08 | meta-test | No tautological assertions remain | ✅ | ⬜ pending |
| TBD | TBD | TBD | TEST-09 | meta-test | Multi-assertion tests have descriptive messages | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- Existing infrastructure covers all phase requirements. No new test files or frameworks needed.
- `@pytest.mark.slow` marker registration may need adding to pyproject.toml.

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
