# Phase 22: Naming + Docs Sweep - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Rename misleading/opaque identifiers and add docstrings that capture non-obvious behaviors. Pure mechanical refactoring — no behavior changes, no new features. Requirements: NAME-01, NAME-02, NAME-03, DOC-01, DOC-02, DOC-03, DOC-04.

</domain>

<decisions>
## Implementation Decisions

### BatteryModel.data → state (NAME-01)
- Rename Python attribute `self.data` to `self.state` across entire codebase
- Python-only rename — model.json schema is unchanged (`data` IS the raw dict, there's no JSON key called "data")
- Scope: ~101 occurrences in src/ (model.py:80, discharge_handler.py:10, scheduler_manager.py:3, monitor.py:8) + ~129 in tests/ (8 test files)
- `_sync_physics_from_data` and `_sync_physics_to_data` method names should also be updated to `_sync_physics_from_state` / `_sync_physics_to_state`

### category → power_source (NAME-02)
- Rename return value, variable names, dict keys, and test assertions
- Scope: EventClassifier.classify() in event_classifier.py (7 occurrences) + scheduler.py (1) + test_event_classifier.py (2)
- Full rename at all call sites — no partial rename

### rls/d variables in _sync_physics_from_data (NAME-03)
- `rls` (local dict, line 211) → `rls_state` (matches PhysicsParams.rls_state field name it populates)
- `d` (loop variable, line 213) → `stored_params` (descriptive: JSON-loaded filter parameters)
- `rls_data` (line 209) is fine — already descriptive
- Scope: model.py lines 205-230 only (local variables)

### Docstring additions (DOC-01 through DOC-04)
- **DOC-01** `_handle_capacity_convergence` (discharge_handler.py:586): Add write-once guard behavior — `has_logged_baseline_lock` flag prevents duplicate baseline_lock log entries across daemon lifecycle; method is idempotent after first log
- **DOC-02** `_opt_round` (monitor_config.py:272): Already has docstring "Round v to n decimal places, or return None if v is None." — verify this satisfies requirement, amend only if rounding intent unclear
- **DOC-03** `_prune_lut` (model.py:438): Already has comprehensive docstring including dedup logic and voltage band bucketing — verify satisfaction, amend only if inline comments remain that should be in docstring
- **DOC-04** `_classify_discharge_trigger` (discharge_handler.py:696): Already has docstring covering buffer start time comparison and 60s window — verify satisfaction, amend only if buffer start semantics need clarification

### Claude's Discretion
- Exact wording of docstring additions (DOC-01 through DOC-04)
- Whether `_sync_physics_from_data`/`_sync_physics_to_data` rename to `_from_state`/`_to_state` or keep original method names
- Order of operations (rename data→state first vs category→power_source first)
- Whether to update any inline comments that reference old names

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — NAME-01, NAME-02, NAME-03, DOC-01, DOC-02, DOC-03, DOC-04 definitions

### Key source files (rename targets)
- `src/model.py` — BatteryModel.data (80 occurrences), _sync_physics_from_data, _prune_lut, rls/d variables
- `src/discharge_handler.py` — BatteryModel.data refs (10), _handle_capacity_convergence, _classify_discharge_trigger
- `src/event_classifier.py` — category variable (7 occurrences)
- `src/monitor_config.py` — _opt_round docstring
- `src/scheduler_manager.py` — BatteryModel.data refs (3)
- `src/monitor.py` — BatteryModel.data refs (8)

### Key test files (rename targets)
- `tests/test_model.py` — 37 .data occurrences
- `tests/test_monitor.py` — 23 .data occurrences
- `tests/test_sulfation_persistence.py` — 17 .data occurrences
- `tests/test_discharge_event_logging.py` — 16 .data occurrences
- `tests/test_monitor_integration.py` — 13 .data occurrences
- `tests/test_scheduler_manager.py` — 10 .data occurrences
- `tests/test_dispatch.py` — 6 .data occurrences
- `tests/test_discharge_handler.py` — 7 .data occurrences
- `tests/test_event_classifier.py` — 2 category occurrences

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- No new modules or utilities needed — this is pure rename + docstring work

### Established Patterns
- `BatteryModel.data` is a plain dict loaded from model.json via `json.load()` — rename to `state` is a find-replace on the attribute name
- `category` is a string return value from `EventClassifier.classify()` — returned in a dict, consumed by callers reading dict key

### Integration Points
- All `.data` references must be updated atomically to avoid partial rename breakage
- `model.json` file format is NOT affected (no serialization changes)
- Test assertions reference `.data` extensively — must be included in sweep

</code_context>

<specifics>
## Specific Ideas

No specific requirements — standard mechanical rename with grep-verify approach.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 22-naming-docs-sweep*
*Context gathered: 2026-03-20*
