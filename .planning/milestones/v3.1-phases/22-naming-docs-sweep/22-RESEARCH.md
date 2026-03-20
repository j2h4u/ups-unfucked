# Phase 22: Naming + Docs Sweep - Research

**Researched:** 2026-03-20
**Domain:** Python mechanical refactoring — identifier rename + docstring additions
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**BatteryModel.data → state (NAME-01)**
- Rename Python attribute `self.data` to `self.state` across entire codebase
- Python-only rename — model.json schema is unchanged (`data` IS the raw dict, there's no JSON key called "data")
- Scope: ~101 occurrences in src/ (model.py:80, discharge_handler.py:10, scheduler_manager.py:3, monitor.py:8) + ~129 in tests/ (8 test files)
- `_sync_physics_from_data` and `_sync_physics_to_data` method names should also be updated to `_sync_physics_from_state` / `_sync_physics_to_state`

**category → power_source (NAME-02)**
- Rename return value, variable names, dict keys, and test assertions
- Scope: EventClassifier.classify() in event_classifier.py (7 occurrences) + scheduler.py (1) + test_event_classifier.py (2)
- Full rename at all call sites — no partial rename

**rls/d variables in _sync_physics_from_data (NAME-03)**
- `rls` (local dict, line 211) → `rls_state` (matches PhysicsParams.rls_state field name it populates)
- `d` (loop variable, line 213) → `stored_params` (descriptive: JSON-loaded filter parameters)
- `rls_data` (line 209) is fine — already descriptive
- Scope: model.py lines 205-230 only (local variables)

**Docstring additions (DOC-01 through DOC-04)**
- DOC-01 `_handle_capacity_convergence` (discharge_handler.py:586): Add write-once guard behavior — `has_logged_baseline_lock` flag prevents duplicate baseline_lock log entries across daemon lifecycle; method is idempotent after first log
- DOC-02 `_opt_round` (monitor_config.py:272): Already has docstring "Round v to n decimal places, or return None if v is None." — verify this satisfies requirement, amend only if rounding intent unclear
- DOC-03 `_prune_lut` (model.py:438): Already has comprehensive docstring including dedup logic and voltage band bucketing — verify satisfaction, amend only if inline comments remain that should be in docstring
- DOC-04 `_classify_discharge_trigger` (discharge_handler.py:696): Already has docstring covering buffer start time comparison and 60s window — verify satisfaction, amend only if buffer start semantics need clarification

### Claude's Discretion
- Exact wording of docstring additions (DOC-01 through DOC-04)
- Whether `_sync_physics_from_data`/`_sync_physics_to_data` rename to `_from_state`/`_to_state` or keep original method names
- Order of operations (rename data→state first vs category→power_source first)
- Whether to update any inline comments that reference old names

### Deferred Ideas (OUT OF SCOPE)
None — discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| NAME-01 | `BatteryModel.data` renamed to `state` across entire codebase | 101 src + 129 test occurrences confirmed via grep; method renames `_sync_physics_from_data`/`_to_data` in scope |
| NAME-02 | `category` renamed to `power_source` in EventClassifier.classify() | `category` is a local variable in classify(); 7 occurrences in event_classifier.py; scheduler.py ref is unrelated (docstring word only); test_event_classifier.py has 2 inline comment occurrences only (no assertions) |
| NAME-03 | `rls` / `d` variables cleaned up in `_sync_physics_from_data` | Confirmed at lines 211 and 213 of model.py; local scope only |
| DOC-01 | `_handle_capacity_convergence` write-once behavior documented | Method has only one-liner docstring; `has_logged_baseline_lock` guard logic is undocumented |
| DOC-02 | `_opt_round` docstring added (monitor_config.py) | Docstring exists and is accurate; requirement is already satisfied |
| DOC-03 | Dedup inline comment moved to `_prune_lut` docstring | Docstring already contains dedup explanation; inline comment `# Dedup — when multiple entries...` at line 455 still present (redundant) |
| DOC-04 | Buffer start time comment moved to `_classify_discharge_trigger` docstring | Docstring covers comparison logic; inline comment `# Use buffer start time (Unix float) instead of wall clock` at line 709 still present |
</phase_requirements>

---

## Summary

Phase 22 is a pure mechanical refactoring phase: rename three identifiers and verify/patch four docstrings. There are no behavior changes, no new modules, and no architectural decisions to make.

The dominant work item is NAME-01: `BatteryModel.data` → `state`. With 101 src occurrences and 129 test occurrences across 12 files, this is a large but straightforward find-replace. The risk is partial rename — leaving a stale `.data` reference that passes tests silently because Python dicts don't enforce attribute names. The mitigation is a post-rename grep to verify zero remaining occurrences before running the suite.

NAME-02 has a critical finding: `category` in `event_classifier.py` is a **local variable only** — it is never returned, never appears in a dict key, and is never read by callers. The CONTEXT.md describes it as "return value, variable names, dict keys" but the actual code returns `EventType` directly. The rename is still correct and worthwhile (the local variable name is confusing), but the scope is narrower than described. The `scheduler.py` reference is a docstring word, not a variable. The `test_event_classifier.py` references are comment text only.

DOC-02, DOC-03, and DOC-04 are partially satisfied already. The planner should structure tasks to verify current docstrings, then make targeted amendments rather than writing from scratch.

**Primary recommendation:** Execute NAME-01 first (largest blast radius, most test coverage), verify with grep + test run, then NAME-02, NAME-03, and docstring work in any order.

---

## Standard Stack

No new dependencies. This phase uses only:

| Tool | Purpose |
|------|---------|
| `grep` / `rg` | Pre- and post-rename verification of occurrence counts |
| `pytest` | Suite runs after each rename to confirm no broken references |
| Editor find-replace | Mechanical rename execution |

**Baseline:** 555 tests pass before this phase begins (confirmed 2026-03-20).

---

## Architecture Patterns

### Rename Execution Pattern

For large-scope renames in this codebase:

1. Grep to establish pre-rename baseline count
2. Apply rename across all files (src/ + tests/ in one operation)
3. Grep to verify zero remaining occurrences of old name
4. Run `python3 -m pytest -x -q` to confirm no breakage
5. Commit

This pattern prevents partial-rename states from persisting across tasks.

### Attribute Rename in Python

`BatteryModel.data` is a plain dict assigned in `__init__`. The rename to `self.state` is a direct string substitution. No `property`, `__getattr__`, or descriptor machinery is involved — the rename is purely textual.

**Gotcha:** `self.data` is also a common pattern in Python test fixtures, mock objects, and other classes. Grep patterns must be scoped to avoid false positives. The safe pattern is `self\.data\b` (word boundary) on src/ and tests/ only.

### Method Rename Scope

`_sync_physics_from_data` and `_sync_physics_to_data` are private methods called only within `model.py`. No external callers exist (confirmed by grep). The method rename is self-contained within one file.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead |
|---------|-------------|-------------|
| Rename verification | Custom script | `rg '\.data\b' src/ tests/` — zero output = clean |
| Test integrity check | Manual review | `python3 -m pytest -x -q` after each rename |

---

## Common Pitfalls

### Pitfall 1: Partial Rename Breaks at Runtime, Not Import Time

**What goes wrong:** Renaming `self.data` to `self.state` in most places but missing one leaves a `AttributeError` at runtime (when that code path executes), not at module import. If the missed occurrence is in an infrequently-exercised branch, tests may still pass.

**How to avoid:** After rename, run `rg '\.data\b' src/ tests/` and confirm zero hits before running the test suite. The grep is the safety net, not the tests.

**Warning signs:** Any remaining hit from `rg '\.data\b' src/model.py` or `src/discharge_handler.py` after the rename is a defect.

### Pitfall 2: NAME-02 Scope Is Narrower Than CONTEXT.md Describes

**What goes wrong:** CONTEXT.md says "rename return value, variable names, dict keys" for `category` → `power_source`. The actual code shows `category` is a local variable in `classify()` — it is never returned (the method returns `EventType` directly), never put in a dict, and never read outside the function body.

**How to avoid:** The rename is still correct — apply it to the 5 local variable occurrences inside `classify()` (lines 64, 66, 68, 70, 72). Also update the class docstring at line 28 which uses the word "category" and the inline comment at line 60. Skip: `scheduler.py` (the word appears in an unrelated docstring for `reason_code`), `test_event_classifier.py` (the 2 hits are comment text describing test intent, not variable names).

**Warning signs:** If any task plan calls for updating `scheduler.py` variable names for this requirement, that is out of scope.

### Pitfall 3: DOC-02/03/04 Already Partially Satisfied

**What goes wrong:** Creating a task to "add docstring" for `_opt_round`, `_prune_lut`, or `_classify_discharge_trigger` from scratch when docstrings already exist.

**Current state (verified):**
- `_opt_round` (monitor_config.py:272): Has docstring `"Round v to n decimal places, or return None if v is None."` — complete, no changes needed.
- `_prune_lut` (model.py:438): Has comprehensive docstring covering strategy, dedup logic, and voltage bucketing. One inline comment at line 455 (`# Dedup — when multiple entries share voltage...`) duplicates docstring content — could be removed.
- `_classify_discharge_trigger` (discharge_handler.py:696): Has docstring covering buffer start time comparison and 60s window. One inline comment at line 709 (`# Use buffer start time (Unix float) instead of wall clock`) is the "comment that should be in docstring" — already in docstring, comment is now redundant.

**How to avoid:** Tasks for DOC-02/03/04 should be "verify + remove redundant inline comment" not "write docstring."

### Pitfall 4: `_sync_physics_from_data` Method Rename Conflicts with NAME-03

**What goes wrong:** NAME-03 renames local variables inside `_sync_physics_from_data`. The discretionary method rename (`_sync_physics_from_data` → `_sync_physics_from_state`) touches the same lines. If done in separate tasks without coordination, one task may edit a method that another task renames, causing merge confusion.

**How to avoid:** Do NAME-03 variable renames and the method rename in the same task or at minimum in the same file edit pass.

---

## Code Examples

### Pre-rename verification (NAME-01)
```bash
# Establish baseline — should show 101 src hits, 129 test hits
rg '\.data\b' /home/j2h4u/repos/j2h4u/ups-battery-monitor/src/ --count
rg '\.data\b' /home/j2h4u/repos/j2h4u/ups-battery-monitor/tests/ --count

# Post-rename verification — should return nothing
rg '\.data\b' /home/j2h4u/repos/j2h4u/ups-battery-monitor/src/
rg '\.data\b' /home/j2h4u/repos/j2h4u/ups-battery-monitor/tests/
```

Note: `rg '\.data\b'` will also match `.data` on other objects (e.g. `response.data`, `config.data`). Review hits by file — the ones in `src/model.py`, `src/discharge_handler.py`, `src/scheduler_manager.py`, `src/monitor.py` are all `BatteryModel` instances. A tighter pattern is `self\.data\b` for `self` assignments, but some callers use `battery_model.data` or `model.data` — use `\.data\b` and review by context.

### NAME-01 scope by file (pre-rename counts, verified)
```
src/model.py                  80 occurrences
src/discharge_handler.py      10 occurrences
src/scheduler_manager.py       3 occurrences
src/monitor.py                 8 occurrences
tests/test_model.py           37 occurrences
tests/test_monitor.py         23 occurrences
tests/test_sulfation_persistence.py  17 occurrences
tests/test_discharge_event_logging.py 16 occurrences
tests/test_monitor_integration.py    13 occurrences
tests/test_scheduler_manager.py      10 occurrences
tests/test_dispatch.py         6 occurrences
tests/test_discharge_handler.py  7 occurrences
```

### NAME-02 actual scope in event_classifier.py
```python
# Lines to rename (local variable only):
category = "battery"   # line 64
category = "online"    # line 66
category = None        # line 68
if category == "online":   # line 70
elif category == "battery":  # line 72

# Class docstring line 28 — update "Battery category" → "Power source category"
# Inline comment line 60 — update "category=None" → "power_source=None"
```

### NAME-03 target lines in model.py
```python
# Before (lines 211, 213):
rls = {}
for name, default_theta in [...]:
    d = rls_data.get(name, {})
    rls[name] = RLSParams(...)

# After:
rls_state = {}
for name, default_theta in [...]:
    stored_params = rls_data.get(name, {})
    rls_state[name] = RLSParams(
        theta=stored_params.get('theta', default_theta),
        P=stored_params.get('P', 1.0),
        sample_count=stored_params.get('sample_count', 0),
        forgetting_factor=stored_params.get('forgetting_factor', 0.97),
    )
# Also update: rls_state=rls → rls_state=rls_state at line 229
```

### DOC-01 target — _handle_capacity_convergence
```python
# Current (discharge_handler.py:586):
def _handle_capacity_convergence(self, convergence_status: dict) -> None:
    """Check convergence state: lock baseline, detect new battery, persist."""

# Needs to add: write-once guard via has_logged_baseline_lock
# Suggested replacement:
def _handle_capacity_convergence(self, convergence_status: dict) -> None:
    """Lock baseline on first convergence, detect new battery, persist flags.

    Write-once guard: baseline_lock is logged exactly once per daemon lifecycle
    via self.has_logged_baseline_lock. Subsequent calls skip the log entry but
    still check for new-battery detection and update capacity_converged flag.
    Idempotent after first call.
    """
```

### Test run command
```bash
python3 -m pytest -x -q
# 555 tests, 1.62s — run after each rename step
```

---

## State of the Art

| Old | Current | Note |
|-----|---------|------|
| `self.data` (BatteryModel) | `self.state` (post phase 22) | Aligns with `EventClassifier.state` naming already in codebase |
| `category` local var | `power_source` | Removes false "category" concept; var was always power source classification |

---

## Open Questions

1. **`_sync_physics_from_data` method rename is discretionary**
   - What we know: method is private, called only within model.py, no external callers
   - What's unclear: whether the planner should make this rename or leave it for a later sweep
   - Recommendation: include in NAME-01 task since it touches the same file and is low-risk; rename to `_sync_physics_from_state` / `_sync_physics_to_state`

2. **Inline comment removal for DOC-03 / DOC-04**
   - What we know: comments at model.py:455 and discharge_handler.py:709 are now redundant with docstrings
   - What's unclear: whether removing them counts as "DOC-03/04 complete" or requires docstring expansion
   - Recommendation: remove redundant inline comments (they duplicate docstring content); requirement is satisfied as written

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (version from pyproject.toml) |
| Config file | pytest.ini or pyproject.toml [tool.pytest.ini_options] |
| Quick run command | `python3 -m pytest -x -q` |
| Full suite command | `python3 -m pytest -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| NAME-01 | No `.data` on BatteryModel after rename | grep verification + full suite | `rg '\.data\b' src/ tests/` (expect 0) + `python3 -m pytest -q` | N/A — grep check |
| NAME-02 | No `category` local var in classify() | grep + full suite | `rg '\bcategory\b' src/event_classifier.py` (expect 0 in code) + `python3 -m pytest -q` | N/A |
| NAME-03 | No `rls` / `d` local vars in _sync_physics_from_data | grep | `rg '\brls\b|\bd\b' src/model.py` (verify only renamed lines) | N/A |
| DOC-01 | _handle_capacity_convergence docstring captures write-once guard | manual review | — | N/A |
| DOC-02 | _opt_round docstring satisfies requirement | manual review | — | ✅ already exists |
| DOC-03 | _prune_lut docstring covers dedup; inline comment removed | manual review | — | ✅ already exists |
| DOC-04 | _classify_discharge_trigger docstring covers buffer start; inline comment removed | manual review | — | ✅ already exists |

### Sampling Rate
- **Per task commit:** `python3 -m pytest -x -q` (fast, 1.62s)
- **Per wave merge:** `python3 -m pytest -q` (full, same speed)
- **Phase gate:** All 555 tests green before `/gsd:verify-work`

### Wave 0 Gaps
None — existing test infrastructure covers all phase requirements. No new test files needed; this phase does not add behavior.

---

## Sources

### Primary (HIGH confidence)
- Direct code inspection: src/model.py, src/discharge_handler.py, src/event_classifier.py, src/monitor_config.py
- `rg '\.data\b' src/ --count` — 101 occurrences confirmed
- `rg '\.data\b' tests/ --count` — 129 occurrences confirmed
- `python3 -m pytest -x -q` — 555 passed, 1 warning (baseline confirmed 2026-03-20)

### Secondary (MEDIUM confidence)
- CONTEXT.md decisions — locked scope and approach

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new dependencies; pure rename
- Architecture: HIGH — all rename targets directly inspected
- Pitfalls: HIGH — verified against actual code, not assumed from description

**Research date:** 2026-03-20
**Valid until:** Indefinite (static codebase, no external dependencies)
