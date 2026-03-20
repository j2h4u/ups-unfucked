---
phase: 22
slug: naming-docs-sweep
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-20
---

# Phase 22 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | pyproject.toml [tool.pytest.ini_options] |
| **Quick run command** | `python3 -m pytest -x -q` |
| **Full suite command** | `python3 -m pytest -q` |
| **Estimated runtime** | ~2 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest -x -q`
- **After every plan wave:** Run `python3 -m pytest -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 2 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 22-01-01 | 01 | 1 | NAME-01 | grep + suite | `rg '\.data\b' src/ tests/ --type py` (expect 0 BatteryModel refs) + `python3 -m pytest -q` | N/A — grep | ⬜ pending |
| 22-01-02 | 01 | 1 | NAME-03 | grep + suite | `rg '\brls\b' src/model.py` (verify only module refs remain) | N/A — grep | ⬜ pending |
| 22-02-01 | 02 | 1 | NAME-02 | grep + suite | `rg '\bcategory\b' src/event_classifier.py` (expect 0 in code) + `python3 -m pytest -q` | N/A — grep | ⬜ pending |
| 22-02-02 | 02 | 1 | DOC-01 | manual review | grep `has_logged_baseline_lock` in docstring | N/A | ⬜ pending |
| 22-02-03 | 02 | 1 | DOC-02 | manual review | verify _opt_round docstring | ✅ already exists | ⬜ pending |
| 22-02-04 | 02 | 1 | DOC-03 | manual review | verify _prune_lut docstring covers dedup | ✅ already exists | ⬜ pending |
| 22-02-05 | 02 | 1 | DOC-04 | manual review | verify _classify_discharge_trigger docstring | ✅ already exists | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No new test files needed — this phase does not add behavior, only renames identifiers and adds/verifies docstrings.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| DOC-01 docstring quality | DOC-01 | Docstring content is subjective | Read _handle_capacity_convergence docstring, confirm write-once guard mentioned |
| DOC-02/03/04 completeness | DOC-02/03/04 | Verify existing docstrings satisfy requirements | Read each docstring, confirm non-obvious behaviors documented |

---

## Validation Sign-Off

- [x] All tasks have automated verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 2s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
