---
date: 2026-03-15
phase: 07-safety-critical-metrics
revision_type: checker-feedback-fix
issues_addressed: 2
---

# Phase 7 Revision Summary

## Issues Addressed

### BLOCKER #1: Nyquist Compliance — Test Stub Dependencies

**Problem:** Task 2 in 07-01-PLAN.md has `<verify>` command that references 4 test functions:
- `test_per_poll_writes_during_blackout`
- `test_handle_event_transition_per_poll_during_ob`
- `test_no_writes_during_online_state`
- `test_lb_flag_signal_latency`

These tests don't exist on disk, violating Nyquist Rule: all verify commands must reference tests that exist before task execution begins.

**Solution:** Created Wave 0 plan (07-00-PLAN.md) with single task:
- Creates 4 test function stubs in tests/test_monitor.py
- Stubs have proper signatures, docstrings, and `pass` statements (no logic)
- Updated 07-01-PLAN.md to depend on 07-00-PLAN.md via `depends_on: ["00"]`

**Status:** ✅ RESOLVED

---

### BLOCKER #2: Roadmap Plan Count Mismatch

**Problem:** ROADMAP.md says Phase 7 has "0/2" plans, but only 1 main plan exists. Wave 0 helper plan doesn't count toward the official plan count (it's infrastructure, not a main work plan).

**Solution:** Updated line 43 in ROADMAP.md:
```
Before:  | 7. Safety-Critical Metrics | v1.1 | 0/2 | Not started | — |
After:   | 7. Safety-Critical Metrics | v1.1 | 0/1 | Not started | — |
```

**Status:** ✅ RESOLVED

---

## Changes Made

| File | Change | Reason |
|------|--------|--------|
| .planning/phases/07-safety-critical-metrics/07-00-PLAN.md | Created | Wave 0 test stub creation (Nyquist compliance) |
| .planning/phases/07-safety-critical-metrics/07-01-PLAN.md | Updated frontmatter | Added `depends_on: ["00"]` to enforce Wave 0 execution |
| .planning/ROADMAP.md | Updated line 43 | Phase 7 plan count from 0/2 → 0/1 |

---

## Wave Structure After Revision

```
Wave 0:  07-00-PLAN.md      [Test stub creation - prerequisite]
         └─ Task: Create 4 test function stubs
         └─ Output: tests/test_monitor.py with stub definitions

Wave 1:  07-01-PLAN.md      [Safety-critical metrics refactor]
         └─ Depends on: 07-00
         └─ Task 1: Implement is_discharging gate in monitor.py
         └─ Task 2: Populate 4 test stubs with assertions
         └─ Output: State-dependent polling + 164 passing tests
```

---

## Verification

### Test Stub References
All 4 test functions mentioned 10 times in 07-00-PLAN.md:
- In task action section (function signatures with docstrings)
- In behavior section (test names and requirements)
- In success criteria (acceptance conditions)

### Dependency Chain
```bash
$ grep "depends_on:" .planning/phases/07-safety-critical-metrics/*.md
07-00-PLAN.md:depends_on: []           # Root, no dependencies
07-01-PLAN.md:depends_on: ["00"]       # Depends on Wave 0
```

### Roadmap Consistency
```bash
$ grep "7\. Safety" .planning/ROADMAP.md
| 7. Safety-Critical Metrics | v1.1 | 0/1 | Not started | — |
```

---

## Next Steps for Execution

1. **Execute Wave 0 (07-00-PLAN.md):**
   - Claude creates test stubs in tests/test_monitor.py
   - Verify stubs exist: `grep -c "def test_" tests/test_monitor.py`
   - Creates SUMMARY documenting stub locations

2. **Execute Wave 1 (07-01-PLAN.md):**
   - Task 1: Modify monitor.py run() method (gate logic)
   - Task 2: Populate stubs with full test implementations
   - All 164 tests pass (160 v1.0 + 4 Phase 7)

---

## Checker Compliance

- ✅ Nyquist Rule: All test functions exist on disk before Task 2 runs
- ✅ Roadmap: Phase 7 plan count reflects actual main plans (Wave 0 excluded)
- ✅ Dependencies: Wave 0 → Wave 1 execution order enforced via `depends_on`
- ✅ No new issues: Changes are surgical, preserve all other plan content

**Revision status:** COMPLETE — Ready for execution
