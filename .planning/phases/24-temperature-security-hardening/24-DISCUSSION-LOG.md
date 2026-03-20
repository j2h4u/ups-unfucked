# Phase 24: Temperature + Security Hardening - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-21
**Phase:** 24-temperature-security-hardening
**Areas discussed:** Temperature sensor, Validation depth, Security docs, atomic_write logging
**Mode:** --auto (all decisions auto-selected with recommended defaults)

---

## Temperature Sensor Resolution

| Option | Description | Selected |
|--------|-------------|----------|
| Log once + keep 35°C | Probe NUT vars at startup, log absence, keep hardcoded assumption | ✓ |
| Periodic re-check | Re-probe every N polls for hot-plugged sensors | |
| Remove temperature entirely | Strip all temperature params from sulfation model | |

**User's choice:** [auto] Log once at startup, keep 35°C documented assumption
**Notes:** Confirmed via live `upsc cyberpower` — UT850EG has no temperature variable. User also confirmed UPS has no temperature sensor.

---

| Option | Description | Selected |
|--------|-------------|----------|
| Single startup probe | Check NUT vars once at init | ✓ |
| Skip probe entirely | Don't check, just document absence | |

**User's choice:** [auto] Yes, check NUT vars once at startup for future-proofing
**Notes:** get_ups_vars() already returns all variables — zero-cost to check

---

## model.json Validation Depth

| Option | Description | Selected |
|--------|-------------|----------|
| Warn + reset to None | Log warning, clear invalid field, continue | ✓ |
| Raise error | Fail startup on invalid scheduling field | |
| Silent reset | Fix without logging | |

**User's choice:** [auto] Log warning + reset to None (consistent with existing clamp pattern)
**Notes:** Daemon must survive corrupt model.json — existing pattern is warn+reset

---

| Option | Description | Selected |
|--------|-------------|----------|
| Type-check lists, reset to [] | Validate sulfation_history etc. are lists | ✓ |
| Skip list validation | Only validate scalar fields | |

**User's choice:** [auto] Type-check as list, reset to [] if wrong type
**Notes:** Consistent with existing validation approach

---

## NUT Empty PASSWORD Documentation

| Option | Description | Selected |
|--------|-------------|----------|
| Code comment + README | Document at both connection site and project docs | ✓ |
| Code comment only | Inline explanation at PASSWORD line | |
| README only | Centralized security docs | |

**User's choice:** [auto] Code comment at connection site + README security section
**Notes:** Both locations serve different audiences — developers vs operators

---

## atomic_write Cleanup Logging

| Option | Description | Selected |
|--------|-------------|----------|
| logger.warning | Secondary issue, original error is primary | ✓ |
| logger.error | Treat cleanup failure as error-level | |

**User's choice:** [auto] logger.warning (original exception is the real error)
**Notes:** One-line fix, consistent with "don't mask the original exception" intent

---

## Claude's Discretion

- Exact NUT variable names to probe for temperature
- README security section wording
- Test structure for new validation code

## Deferred Ideas

None — all items within phase scope
