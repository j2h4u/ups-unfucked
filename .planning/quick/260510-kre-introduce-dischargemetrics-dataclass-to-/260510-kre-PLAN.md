---
phase: 260510-kre
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/discharge_handler.py
  - tests/test_discharge_handler.py
autonomous: true
requirements:
  - REFACTOR-DM-01
must_haves:
  truths:
    - "DischargeMetrics is a frozen dataclass exposing all 16 fields by name"
    - "_compute_sulfation_metrics returns a DischargeMetrics instance, not a dict"
    - "_persist_sulfation_and_discharge and _log_discharge_complete accept DischargeMetrics with attribute access"
    - "All existing test behaviors still pass (no regressions)"
  artifacts:
    - path: "src/discharge_handler.py"
      provides: "DischargeMetrics dataclass + typed handoff between compute/persist/log"
      contains: "class DischargeMetrics"
    - path: "tests/test_discharge_handler.py"
      provides: "Tests updated to construct DischargeMetrics in arrange phase"
      contains: "DischargeMetrics("
  key_links:
    - from: "DischargeHandler._compute_sulfation_metrics"
      to: "DischargeHandler._persist_sulfation_and_discharge / _log_discharge_complete"
      via: "DischargeMetrics dataclass instance"
      pattern: "DischargeMetrics\\("
---

<objective>
Replace the 16-key opaque dict that flows between
`_compute_sulfation_metrics` -> `_persist_sulfation_and_discharge` -> `_log_discharge_complete`
in `src/discharge_handler.py` with a `@dataclass(frozen=True)` named `DischargeMetrics`.

Purpose: Eliminate stringly-typed key access at three call sites (and any test sites
constructing the dict by hand). Named attributes provide type checkability, IDE
autocomplete, and a single declared schema instead of a docstring promise.

Output: New `DischargeMetrics` dataclass; three methods updated to produce/consume it;
test file updated to construct `DischargeMetrics` in arrange phase; full test suite
passes.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@src/discharge_handler.py
@tests/test_discharge_handler.py

<interfaces>
<!-- Field shapes extracted from src/discharge_handler.py lines 261-352 (return dict at 335-352). -->
<!-- The dataclass mirrors these field names and types exactly. -->

```python
from dataclasses import dataclass
from typing import Optional
from src.battery_math.sulfation import SulfationState  # already imported in discharge_handler.py

@dataclass(frozen=True)
class DischargeMetrics:
    """Per-discharge metrics computed once, consumed by persist + log steps.

    Produced by DischargeHandler._compute_sulfation_metrics(); consumed by
    _persist_sulfation_and_discharge() and _log_discharge_complete().
    Frozen to enforce single-write semantics across the discharge pipeline.
    """
    now_iso: str
    sulfation_state: Optional[SulfationState]
    roi: Optional[float]
    sulfation_score_r: Optional[float]
    days_since_deep_r: Optional[float]
    ir_trend_r: float
    recovery_delta_r: float
    discharge_duration: float
    dod_r: float
    depth_of_discharge: float
    roi_r: Optional[float]
    soh_new: float
    soh_delta: float
    discharge_trigger: str
    capacity_ah_ref: Optional[float]
    confidence_level: str
```

Existing call chain (do not change shape):
- `_score_and_persist_sulfation` (lines 241-259) — only production caller of all three.
- `_compute_soh` (line 120) — returns `(soh_after, capacity_ah_ref)` tuple, NOT this dict. Out of scope.
- `handle_discharge_complete` (line 552) — uses an unrelated `discharge_data` dict (voltage_series, time_series, ...). Out of scope.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Introduce DischargeMetrics dataclass and migrate producer + consumers</name>
  <files>
    src/discharge_handler.py,
    tests/test_discharge_handler.py
  </files>
  <behavior>
    Production code:
    - `DischargeMetrics` dataclass exists in `src/discharge_handler.py`, frozen, with the 16 fields above in the same order.
    - `_compute_sulfation_metrics` returns `DischargeMetrics` (return annotation updated from `dict` to `DischargeMetrics`).
    - In the success path it constructs `DischargeMetrics(...)` with the same values it previously placed under each dict key.
    - In the `except (ValueError, TypeError)` path it still returns a `DischargeMetrics` with `sulfation_state=None` and `roi=None` (and `roi_r=None`, since `roi is None`); all other fields populated as before. Behavior parity required.
    - `_persist_sulfation_and_discharge(self, data: DischargeMetrics) -> None`: change every `data["x"]` to `data.x`. Type annotation updated. Docstring updated to say "DischargeMetrics returned by _compute_sulfation_metrics".
    - `_log_discharge_complete(self, data: DischargeMetrics) -> None`: same treatment.
    - `_score_and_persist_sulfation` does not need changes beyond the type flowing through (it already passes `data` opaquely).

    Test code (`tests/test_discharge_handler.py`):
    - Add `from src.discharge_handler import DischargeHandler, DischargeMetrics` (extend existing import).
    - Replace every hand-built `data = {...}` dict at lines ~418, ~457, ~496, ~530, ~556 with `data = DischargeMetrics(...)`. Field values stay identical.
    - The `_log_discharge_complete` test dicts (lines 530, 556) currently omit several fields (e.g. `sulfation_state`, `ir_trend_r`, `days_since_deep_r`, `confidence_level`, `depth_of_discharge`, `roi`). These MUST now be supplied because the dataclass requires all 16 fields. Use sensible test values matching the surrounding scenario (e.g., `sulfation_state=None` for the None-handling test; `confidence_level="medium"` / `"low"` to match the assigned `last_sulfation_confidence`).
    - The `_compute_sulfation_metrics` tests (lines 286-396) currently assert `self._REQUIRED_DATA_KEYS.issubset(data.keys())`. Replace with attribute-existence checks against the dataclass — preferred form: assert `set(f.name for f in dataclasses.fields(data)) == _REQUIRED_DATA_KEYS` (import `dataclasses` at module top), OR delete that assertion and rely on the dataclass type itself enforcing field presence (the `isinstance(data, DischargeMetrics)` check is stronger than the dict-key check). Pick whichever produces the clearer test; do not silently drop coverage.
    - Other assertions like `assert data["roi"] is None` become `assert data.roi is None`.

    Out of scope:
    - Do NOT touch `_compute_soh`, `handle_discharge_complete`, `_handle_capacity_convergence`, or any other method — they use different dicts/tuples.
    - Do NOT add backward-compat shims (no `__getitem__`, no `to_dict()`). Per project policy: single user, fail fast (feedback_no_backward_compat).
    - Do NOT change field names, ordering of operations, or rounding behavior.
  </behavior>
  <action>
    1. Edit `src/discharge_handler.py`:
       - Add `from dataclasses import dataclass` to the top imports.
       - Add the `SulfationState` import: `from src.battery_math.sulfation import SulfationState` already exists transitively via `compute_sulfation_score`; verify and add an explicit import if needed for the type annotation.
       - Define `DischargeMetrics` frozen dataclass below the existing module-level constants (after `RATED_CYCLE_LIFE = 300` at line 35), before `class DischargeHandler`.
       - Update `_compute_sulfation_metrics` return annotation to `-> DischargeMetrics` and replace the final `return {...}` (lines 335-352) with `return DischargeMetrics(now_iso=..., sulfation_state=..., ...)`. Keep the same field values.
       - In the `except (ValueError, TypeError)` branch (lines 303-310): the existing code sets `sulfation_state = None; roi = None` then falls through to the rounding block. The rounding block already handles `None` correctly (`sulfation_score_r = round(... ,3) if sulfation_state else None`, `roi_r = round(roi, 3) if roi is not None else None`). So a single `return DischargeMetrics(...)` at the end of the method covers both paths — no separate construction needed inside the except.
       - Update `_persist_sulfation_and_discharge` signature to `data: DischargeMetrics` and change all `data["k"]` accesses to `data.k` (lines 363-388).
       - Update `_log_discharge_complete` signature to `data: DischargeMetrics` and change all `data["k"]` accesses to `data.k` (lines 398-416). Note line 409: `round(data["capacity_ah_ref"], 2) if data["capacity_ah_ref"] is not None else None` becomes `round(data.capacity_ah_ref, 2) if data.capacity_ah_ref is not None else None`.
       - Update docstrings on both consumer methods to say "DischargeMetrics returned by _compute_sulfation_metrics" instead of "dict returned by ...".

    2. Edit `tests/test_discharge_handler.py`:
       - Extend the existing `from src.discharge_handler import ...` line to include `DischargeMetrics`.
       - Add `import dataclasses` if you adopt the `fields()` approach for the `_REQUIRED_DATA_KEYS` assertion replacement.
       - Replace every test-local `data = {...}` literal that flows into `_persist_sulfation_and_discharge` or `_log_discharge_complete` with a `DischargeMetrics(...)` construction.
       - For the two `_log_discharge_complete` test dicts that omit fields, supply all 16 fields. Recommended fillers:
         * `sulfation_state=None` (the log path doesn't use it directly; only `sulfation_score_r` is logged)
         * `ir_trend_r=0.001`, `days_since_deep_r=2.0`, `depth_of_discharge=<same as dod_r>`, `roi=<same as roi_r when not None, else None>`, `confidence_level="medium"` (or `"low"` for the None case to match `last_sulfation_confidence = None`)
       - Update `_REQUIRED_DATA_KEYS` assertions in `_compute_sulfation_metrics` tests: replace `self._REQUIRED_DATA_KEYS.issubset(data.keys())` with `set(f.name for f in dataclasses.fields(data)) >= self._REQUIRED_DATA_KEYS`. Optionally add `assert isinstance(data, DischargeMetrics)`.
       - Replace `data["k"]` reads (e.g. `assert data["roi"] is None`) with `data.k`.

    3. Run the test suite:
       `cd /home/j2h4u/repos/j2h4u/ups-battery-monitor && python -m pytest tests/ -x -q`

    4. Restart the daemon per project policy (feedback_always_restart_daemon):
       Do NOT auto-restart from this task — verification command is the test suite. The user runs `sudo systemctl restart ups-battery-monitor` after merging if desired. Mention this in the SUMMARY.

    Implementation order (TDD-friendly): edit the dataclass definition first, run the test suite (should fail with `NameError` or `TypeError` if tests still build dicts), then update each consumer, then update tests, then re-run suite.
  </action>
  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/ups-battery-monitor && python -m pytest tests/ -x -q</automated>
  </verify>
  <done>
    - `grep -n 'class DischargeMetrics' src/discharge_handler.py` returns one line.
    - `grep -nE 'data\["' src/discharge_handler.py` returns 0 matches inside `_persist_sulfation_and_discharge` and `_log_discharge_complete` (verify by inspecting the matched line ranges; module-wide grep may have unrelated hits — there should be none in this file outside the `handle_discharge_complete` discharge_data dict).
    - `grep -n 'DischargeMetrics(' tests/test_discharge_handler.py` returns >= 5 matches (one per former dict literal).
    - `python -m pytest tests/ -x -q` exits 0 with all 476+ tests passing.
    - No new files created. No backward-compat shim methods on `DischargeMetrics`.
  </done>
</task>

</tasks>

<verification>
- Full pytest suite passes: `python -m pytest tests/ -x -q`
- Static read of `src/discharge_handler.py` confirms:
  - `DischargeMetrics` dataclass present with 16 fields, `frozen=True`
  - Three method signatures updated
  - All `data["..."]` -> `data.<attr>` migrations done
- Test file constructs `DischargeMetrics(...)` instead of dict literals at every former site.
</verification>

<success_criteria>
- All tests pass (no regressions vs. v3.1 baseline of 476 tests).
- `_compute_sulfation_metrics` return type is `DischargeMetrics` (not `dict`).
- `_persist_sulfation_and_discharge` and `_log_discharge_complete` accept `DischargeMetrics` and use attribute access exclusively.
- Test file no longer constructs the metrics payload as a dict for these three methods.
- No backward-compat shims (no `__getitem__`, no `to_dict()`, no helper that converts dict<->dataclass).
- Daemon-runtime behavior identical: same model writes, same journald event_type/extras keys/values.
</success_criteria>

<output>
After completion, create `.planning/quick/260510-kre-introduce-dischargemetrics-dataclass-to-/260510-kre-01-SUMMARY.md` documenting:
- Number of `data["k"]` -> `data.k` migrations performed
- Whether `_REQUIRED_DATA_KEYS` assertion was kept (via `dataclasses.fields`) or replaced by `isinstance(..., DischargeMetrics)`
- Reminder to operator: `sudo systemctl restart ups-battery-monitor` to pick up the change in production
</output>
