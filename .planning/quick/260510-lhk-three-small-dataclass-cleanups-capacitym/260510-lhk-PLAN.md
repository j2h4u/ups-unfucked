---
phase: 260510-lhk-quick
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/capacity_estimator.py
  - src/model.py
  - src/monitor_config.py
autonomous: true
requirements:
  - QUICK-01-CapacityMeasurement-frozen-dataclass
  - QUICK-02-RLSParams-asdict
  - QUICK-03-HealthSnapshot-frozen
must_haves:
  truths:
    - "CapacityMeasurement is a frozen dataclass; no positional/tuple-style access remains"
    - "RLSParams.to_dict() is removed; all callers use dataclasses.asdict(rls)"
    - "HealthSnapshot is frozen; no code mutates instances after construction"
    - "Full test suite passes after all three changes"
  artifacts:
    - path: "src/capacity_estimator.py"
      provides: "CapacityMeasurement as @dataclass(frozen=True)"
      contains: "@dataclass(frozen=True)"
    - path: "src/model.py"
      provides: "RLSParams without to_dict, callers use dataclasses.asdict"
      contains: "import dataclasses"
    - path: "src/monitor_config.py"
      provides: "HealthSnapshot frozen=True"
      contains: "@dataclass(frozen=True)"
  key_links:
    - from: "src/capacity_estimator.py:has_converged"
      to: "CapacityMeasurement.ah"
      via: "named-field attribute access"
      pattern: "m\\.ah for m in self\\.capacity_measurements"
    - from: "src/capacity_estimator.py:get_weighted_estimate"
      to: "CapacityMeasurement fields"
      via: "named-field iteration (no tuple unpacking)"
      pattern: "for m in self\\.capacity_measurements"
    - from: "src/model.py"
      to: "dataclasses.asdict"
      via: "module-level import"
      pattern: "dataclasses\\.asdict\\(rls\\)"
---

<objective>
Three small, independent dataclass cleanups in one quick task:

1. **CapacityMeasurement** — convert NamedTuple → @dataclass(frozen=True); fix fragile positional access (`m[1]` → `m.ah`) and tuple-unpacking iteration.
2. **RLSParams** — drop the manual `to_dict()` method in `src/model.py`; replace callers with `dataclasses.asdict(rls)`.
3. **HealthSnapshot** — add `frozen=True`; verified no mutation sites exist (grep clean).

Purpose: Replace fragile positional access and hand-rolled serialization with idiomatic dataclass patterns. Make accidental mutation a TypeError instead of a silent bug.

Output: Three modified source files, all tests still green.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
<!-- Pre-extracted to avoid scavenger hunts. Treat as authoritative for scope. -->

Current CapacityMeasurement (src/capacity_estimator.py:11):
```python
class CapacityMeasurement(NamedTuple):
    timestamp: str
    ah: float
    confidence: float
    metadata: Dict
```

Current fragile sites (src/capacity_estimator.py):
- Line 293 (has_converged):  `compute_cov([m[1] for m in self.capacity_measurements]) < 0.10`
- Line 321 (get_weighted_estimate):  `for timestamp, ah, confidence, metadata in self.capacity_measurements:`

Current RLSParams (src/model.py:49-64):
```python
@dataclass
class RLSParams:
    theta: float = 0.0
    P: float = 1.0
    sample_count: int = 0
    forgetting_factor: float = 0.97

    def to_dict(self) -> dict:
        return {
            "theta": self.theta, "P": self.P,
            "sample_count": self.sample_count,
            "forgetting_factor": self.forgetting_factor,
        }
```

RLSParams.to_dict() callers (3 sites in src/model.py):
- Line 290: `{name: rls.to_dict() for name, rls in self.physics.rls_state.items()}`
- Line 512: `return RLSParams().to_dict()`
- Line 513: `return rls.to_dict()`

NOTE: `src/battery_math/rls.py` has a SEPARATE `ScalarRLS.to_dict()` (line 50) used by tests/test_rls.py.
This is a DIFFERENT class — DO NOT touch it. Only `RLSParams.to_dict()` in src/model.py is in scope.

HealthSnapshot (src/monitor_config.py:288): currently `@dataclass`, becomes `@dataclass(frozen=True)`.
Mutation grep across src/ and tests/ returns ZERO snapshot.<field> = ... assignments — safe to freeze.
Construction sites: src/virtual_ups_exporter.py:44, tests/test_monitor.py (~12 sites), tests/test_monitor_integration.py (3 sites), tests/test_health_endpoint_v16.py (~8 sites). All use kwargs construction; none mutate.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Apply all three dataclass cleanups</name>
  <files>src/capacity_estimator.py, src/model.py, src/monitor_config.py</files>

  <behavior>
    Behavior preserved (no functional changes — refactor only):
    - CapacityMeasurement still constructed positionally at src/capacity_estimator.py:287:
      `CapacityMeasurement(timestamp, ah, confidence, metadata)` must still work
      → @dataclass(frozen=True) supports this (positional construction is fine; mutation is what's blocked).
    - has_converged() returns same bool (cov of ah values across measurements).
    - get_weighted_estimate() returns same float (depth-weighted or simple mean).
    - rls.to_dict() and dataclasses.asdict(rls) produce identical dicts for RLSParams
      (all 4 fields are simple scalars; no nested dataclasses).
    - HealthSnapshot construction via kwargs unchanged; field access in write_health_endpoint() unchanged.

    Negative tests (must still pass / now stronger):
    - Attempting `measurement.ah = 5.0` now raises FrozenInstanceError (was silently allowed before for NamedTuple? actually NamedTuple was already immutable — this preserves immutability while gaining field-name safety).
    - Attempting `snapshot.soc_percent = 99.0` now raises FrozenInstanceError (was silently allowed before — this is the actual hardening win).
  </behavior>

  <action>
    Apply all three changes in one pass, then run the test suite once at the end.

    **Change 1 — CapacityMeasurement (src/capacity_estimator.py):**
    a. Update import on line 4: remove `NamedTuple`, add `dataclass`:
       ```python
       from dataclasses import dataclass
       from typing import Dict, List, Optional, Tuple
       ```
    b. Replace class definition (lines 11-17):
       ```python
       @dataclass(frozen=True)
       class CapacityMeasurement:
           """Single capacity measurement from a discharge event."""
           timestamp: str
           ah: float
           confidence: float
           metadata: Dict
       ```
    c. Fix line 293 (has_converged): `m[1]` → `m.ah`
    d. Fix line 321 (get_weighted_estimate): replace
       `for timestamp, ah, confidence, metadata in self.capacity_measurements:`
       with `for m in self.capacity_measurements:`
       Then update the loop body to use `m.timestamp`, `m.ah`, `m.confidence`, `m.metadata`.
       Read lines 320-335 first to capture all field references in the loop body.
    e. Verify constructor call at line 287 still works (positional args — frozen dataclass handles this fine).

    **Change 2 — RLSParams (src/model.py):**
    a. Add `import dataclasses` to the top-of-file imports (alongside `from dataclasses import dataclass, field` on line 8 — keep both; `dataclasses.asdict` needs the module reference).
    b. Delete the `to_dict()` method on RLSParams (lines 58-64 inclusive — the method body and its blank line).
    c. Replace 3 caller sites:
       - Line 290: `{name: rls.to_dict() for ...}` → `{name: dataclasses.asdict(rls) for ...}`
       - Line 512: `return RLSParams().to_dict()` → `return dataclasses.asdict(RLSParams())`
       - Line 513: `return rls.to_dict()` → `return dataclasses.asdict(rls)`
    d. Verify ScalarRLS.to_dict() in src/battery_math/rls.py is UNTOUCHED (different class, used by tests/test_rls.py).
    e. Final grep check: `grep -n "\.to_dict()" src/model.py` must return zero matches.

    **Change 3 — HealthSnapshot (src/monitor_config.py):**
    a. Line ~287-288: change `@dataclass` decorator on the HealthSnapshot class to `@dataclass(frozen=True)`.
    b. No other changes — grep already confirmed zero mutation sites in src/ and tests/.

    **Final verification:** run the full pytest suite (per <verify>).
  </action>

  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/ups-battery-monitor && python -m pytest tests/ -x -q</automated>

    Additional spot-checks (run if pytest passes, surface any unexpected output):
    - `grep -n "NamedTuple\|m\[1\]" src/capacity_estimator.py` → no matches
    - `grep -n "\.to_dict()" src/model.py` → no matches
    - `grep -n "@dataclass(frozen=True)" src/monitor_config.py` → matches the HealthSnapshot line
    - `grep -n "ScalarRLS.*to_dict\|to_dict.*ScalarRLS" src/battery_math/rls.py tests/test_rls.py` → still present (untouched)
  </verify>

  <done>
    - All three classes converted as specified
    - `python -m pytest tests/ -x -q` exits 0
    - No remaining `m[1]` positional access or tuple-unpacking iteration over CapacityMeasurement
    - No remaining `RLSParams.to_dict()` references in src/model.py
    - HealthSnapshot decorator includes `frozen=True`
    - ScalarRLS.to_dict() in src/battery_math/rls.py untouched (verified by grep)
  </done>
</task>

</tasks>

<verification>
Run the full test suite once after all three changes:

```
cd /home/j2h4u/repos/j2h4u/ups-battery-monitor && python -m pytest tests/ -x -q
```

Expected: all tests pass. If any test fails, the most likely culprits are:
- A test asserting `isinstance(m, tuple)` for CapacityMeasurement (NamedTuple was a tuple subclass; frozen dataclass is not). Grep `tests/` for `isinstance.*CapacityMeasurement.*tuple` if tests fail.
- A test calling `RLSParams.to_dict()` directly. Grep `tests/` for `RLSParams.*to_dict` if model tests fail.
- An accidental HealthSnapshot mutation in test setup that wasn't caught by the initial grep (would manifest as FrozenInstanceError).

Per project convention (feedback_always_restart_daemon.md): after committing, restart the daemon:
`sudo systemctl restart ups-battery-monitor`
</verification>

<success_criteria>
- pytest exits 0
- All four grep spot-checks in <verify> match expected outcomes
- Three commits OR one focused commit (developer choice — these are atomic refactors with no behavior change)
- ScalarRLS in src/battery_math/rls.py is verifiably untouched
</success_criteria>

<output>
After completion, create `.planning/quick/260510-lhk-three-small-dataclass-cleanups-capacitym/260510-lhk-SUMMARY.md` recording:
- Which files changed (line counts)
- Test result (pass count)
- Any test that needed updating (e.g., isinstance checks)
- Confirmation ScalarRLS was not touched
</output>
