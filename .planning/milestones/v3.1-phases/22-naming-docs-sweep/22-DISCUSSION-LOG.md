# Phase 22: Naming + Docs Sweep - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 22-naming-docs-sweep
**Areas discussed:** Rename scope, Variable naming, Docstring verification, Category rename scope
**Mode:** --auto (all decisions auto-selected)

---

## Rename Scope (BatteryModel.data → state)

| Option | Description | Selected |
|--------|-------------|----------|
| Python attribute only | Rename self.data → self.state; model.json unchanged | ✓ |
| Python + JSON schema | Also rename keys inside model.json | |

**User's choice:** [auto] Python attribute only (recommended default)
**Notes:** `data` IS the raw dict — no JSON key called "data" exists. JSON schema unchanged.

---

## Variable Naming (rls/d in _sync_physics_from_data)

| Option | Description | Selected |
|--------|-------------|----------|
| rls→rls_state, d→stored_params | Matches PhysicsParams field name; descriptive for JSON params | ✓ |
| rls→rls_params_map, d→raw_params | More explicit but verbose | |
| rls→filter_states, d→filter_config | Domain-oriented naming | |

**User's choice:** [auto] rls→rls_state, d→stored_params (recommended default)
**Notes:** `rls_state` matches the PhysicsParams.rls_state field it populates. `stored_params` describes JSON-loaded filter parameters.

---

## Docstring Verification

| Option | Description | Selected |
|--------|-------------|----------|
| Verify each, update only gaps | Check existing docstrings, add only what's missing | ✓ |
| Rewrite all four docstrings | Full rewrite regardless of existing state | |

**User's choice:** [auto] Verify each, update only gaps (recommended default)
**Notes:** DOC-02 (_opt_round), DOC-03 (_prune_lut), DOC-04 (_classify_discharge_trigger) appear already complete. DOC-01 (_handle_capacity_convergence) needs write-once guard behavior documented.

---

## Category → power_source Rename

| Option | Description | Selected |
|--------|-------------|----------|
| Full rename at all sites | Return value, variables, dict keys, test assertions | ✓ |
| Rename only in EventClassifier | Keep `category` at call sites for now | |

**User's choice:** [auto] Full rename at all sites (recommended default)
**Notes:** 8 src + 2 test occurrences. Small scope, clean sweep preferred.

---

## Claude's Discretion

- Exact docstring wording
- Method rename (_sync_physics_from_data → _sync_physics_from_state) at Claude's discretion
- Operation order (which rename first)
- Inline comment updates for stale name references

## Deferred Ideas

None — all decisions within phase scope.
