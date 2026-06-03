---
phase: 25-desulfation-retraction-diagnostic-only-capacity-verification
reviewed: 2026-06-04T00:00:00Z
depth: standard
files_reviewed: 21
files_reviewed_list:
  - src/battery_math/scheduler.py
  - src/scheduler_manager.py
  - src/model.py
  - src/discharge_handler.py
  - src/monitor.py
  - src/monitor_config.py
  - src/motd_status.py
  - src/virtual_ups_exporter.py
  - src/battery_math/__init__.py
  - scripts/battery-health.py
  - scripts/install.sh
  - docs/adr/0001-desulfation-premise-reversal.md
  - docs/internal/CONTEXT.md
  - tests/test_scheduler.py
  - tests/test_scheduler_manager.py
  - tests/test_model.py
  - tests/test_monitor.py
  - tests/test_dispatch.py
  - tests/test_discharge_event_logging.py
  - tests/test_health_endpoint_v16.py
  - tests/test_motd_status.py
  - tests/test_year_simulation.py
findings:
  critical: 1
  warning: 5
  info: 4
  total: 10
status: issues_found
---

# Phase 25: Code Review Report

**Reviewed:** 2026-06-04
**Depth:** standard
**Files Reviewed:** 21
**Status:** issues_found

## Summary

Phase 25 reframes the test scheduler from a sulfation/cycle-ROI decision engine into a
diagnostic time-cadence engine. The deletion of `sulfation.py` and `cycle_roi.py` is clean:
a repo-wide grep for `sulfation|cycle_roi|desulf` in `src/` and `scripts/` returns **no code
references** — only narrative mentions in `README.md`, the ADR, and a stale `config.toml`
comment. The new two-input timing split in `scheduler.py` (`days_since_last_attempt` →
rate-limit gate; `days_since_last_test_success` → cadence gate) is correct, well-tested, and
genuinely resolves the bootstrap deadlock the ADR documents. Gate ordering matches the
docstring and tests.

However, the review surfaced one BLOCKER that defeats the entire reframed cadence: the
`test_running` flag set on a successful dispatch is **never reset**, so the second annual
diagnostic test (and every test thereafter) is permanently blocked by the precondition
validator. This was not introduced by Phase 25 but lives squarely in the dispatch path that
this phase re-purposed and re-tested, and it silently breaks the feature the phase exists to
ship. Several quality issues around dead read paths and stale documentation references also
remain.

## Critical Issues

### CR-01: `test_running` flag is never reset → all tests after the first are permanently blocked

**File:** `src/scheduler_manager.py:117` (set), `src/scheduler_manager.py:81` (gate)
**Issue:**
On a successful dispatch, `dispatch_test_with_audit` sets
`battery_model.state["test_running"] = True` (line 117) and persists it via `safe_save`
(line 126) and `battery_model.save()` (line 437). Nothing in the codebase ever sets
`test_running` back to `False` — a grep across `src/` finds exactly two references: the
read at line 81 and the write at line 117. The OB→OL discharge-complete path
(`discharge_handler.update_battery_health`) classifies the discharge as `test_initiated`
but does **not** clear the flag, and `_reset_battery_baseline` in `monitor.py` does not
touch it either.

Consequence: once the first annual diagnostic test dispatches successfully,
`validate_preconditions_before_upscmd` (line 81 → line 53-54) reads `test_running=True` from
persisted model.json on every future evaluation and returns
`(False, "test_already_running")`. The scheduler will keep proposing on the ~365-day cadence,
but dispatch is blocked forever. This defeats the core feature the phase ships (annual
capacity verification) and does so silently — the daemon logs `test_precondition_blocked`
rather than an error.

Because model.json persists the flag, even a daemon restart does not clear it.

**Fix:** Clear `test_running` when the test completes (OB→OL transition after a
`test_initiated` discharge), and as a safety net at daemon startup. Minimal version — reset
on discharge classification in `discharge_handler.update_battery_health`:

```python
# in update_battery_health, after _classify_discharge_trigger:
discharge_trigger = self._classify_discharge_trigger(discharge_buffer)
if discharge_trigger == "test_initiated":
    # Test discharge completed — clear the in-flight flag so the next
    # cadence-driven test can dispatch.
    self.battery_model.state["test_running"] = False
```

Also consider clearing it on daemon startup in `_init_battery_model_and_estimators`
(a test can never still be "running" across a process restart):

```python
self.battery_model.state["test_running"] = False
```

Add a regression test asserting that after a dispatched test's discharge completes,
`validate_preconditions_before_upscmd` no longer returns `test_already_running`.

## Warnings

### WR-01: `battery-health.py` reads a top-level key that is never written → measured-capacity line is dead

**File:** `scripts/battery-health.py:93`, `scripts/battery-health.py:97-98`
**Issue:**
`measured_cap = model_data.get("measured_capacity_ah")` reads a **top-level** model.json key.
A grep confirms `measured_capacity_ah` is only ever written *inside* `discharge_events[]`
entries (`discharge_handler.py:132,143`) and as a `soh_calculator` metadata string
(`soh_calculator.py:110`). It is never set at the top level of model.json. The locked
baseline lives under the different key `capacity_ah_measured`
(`discharge_handler.py:518`, `model.py:744`). So `measured_cap` is always `None`, and the
`if measured_cap:` branch at line 97 is dead — the operator never sees the measured-capacity
figure in the maintenance view this phase added.

**Fix:** Read the correct key (and note the name swap — `capacity_ah_measured`, not
`measured_capacity_ah`):

```python
measured_cap = model_data.get("capacity_ah_measured")
```

### WR-02: `next_test_timestamp` type contract is inconsistent (str vs int) across producer/consumers

**File:** `src/monitor_config.py:306`, `tests/test_health_endpoint_v16.py:95,114-115`
**Issue:**
The producer chain types `next_test_timestamp` as an ISO8601 **string**:
`SchedulerDecision.next_eligible_timestamp` is built via `.isoformat()`
(`scheduler.py:141`), flows to `HealthSnapshot.next_test_timestamp: Optional[str]`
(`monitor_config.py:306`), and consumers parse it as a string —
`battery-health.py:41` does `datetime.fromisoformat(next_ts)` and `motd_status.py:82`
treats it as a string. But `test_health_endpoint_v16.py:95` writes
`next_test_timestamp=1710845400` (a unix **int**) and the docstring at line 84-85 documents
it as "int or null". A genuine int in health.json would make
`battery-health.py:41 datetime.fromisoformat(1710845400)` raise `TypeError` — caught at
line 46, but it degrades to printing a raw epoch number instead of a date.

This is a latent contract mismatch: the test encodes a type the producer never emits. If
anything ever does write an int there, the maintenance view silently degrades.

**Fix:** Make the test reflect the real producer contract (ISO string), and align the
docstring:

```python
next_test_timestamp="2026-03-19T10:30:00+00:00",
...
assert data["next_test_timestamp"] == "2026-03-19T10:30:00+00:00"
```

### WR-03: `_get_last_natural_blackout` assumes append-order equals chronological order

**File:** `src/scheduler_manager.py:342-354`
**Issue:**
`_get_last_natural_blackout` iterates `reversed(events)` and returns the first
`event_reason == "natural"` it finds, treating list position as recency. That holds only if
`discharge_events` is always appended in time order. `model.save()` prunes via
`_cap_history_entries("discharge_events")` which keeps the last 30 by **list position**, not
by timestamp (`model.py:510-514`) — so the invariant is "append order == time order", which
is true today but undocumented and fragile. The grid-stability gate that consumes this
timestamp (`scheduler.py:175-186`) silently assumes the returned blackout is the most recent;
if an out-of-order event ever lands (e.g., a backfilled or replayed event), the gate could
key off a stale or future blackout.

**Fix:** Either select by parsed timestamp instead of position, or document the invariant
explicitly. Robust version:

```python
natural = [e for e in events if e.get("event_reason") == "natural" and e.get("timestamp")]
if not natural:
    return None
latest = max(natural, key=lambda e: e["timestamp"])  # ISO8601 sorts lexicographically
return {"timestamp": latest["timestamp"], "depth": latest.get("depth_of_discharge", 0.0)}
```

### WR-04: Successful dispatch is rate-limited by its own attempt timestamp, masking the missing `test_running` reset

**File:** `src/scheduler_manager.py:286-305`, `src/battery_math/scheduler.py:165-172`
**Issue:**
After a successful dispatch, `update_upscmd_result` writes `last_upscmd_timestamp=now`
(status `OK`). On subsequent daily runs, `_calculate_days_since_last_attempt` returns ~0–7
days, so the rate-limit gate (gate 2) defers with `rate_limit` for the first 7 days —
*before* the precondition validator ever runs. This means the `test_running` BLOCKER
(CR-01) is invisible for the first week after a test and only manifests at the **next annual
cadence**, ~365 days later. Worth calling out because it makes CR-01 hard to catch in
testing: a same-week re-dispatch is blocked by rate-limit, not by `test_running`, so a short
test run would not reveal the permanent block. The two failure modes must be tested
independently (advance the clock past 365d AND past the 7d rate limit).

**Fix:** No code change beyond CR-01; add a regression test that simulates a successful
dispatch, advances `last_upscmd_timestamp` and `last_upscmd_status=OK` age past 365 days, and
asserts the next proposal actually dispatches (i.e., `test_running` was cleared).

### WR-05: Scheduler grid gate and `_get_last_natural_blackout` use different ISO parsers

**File:** `src/scheduler_manager.py:299,334` vs `src/discharge_handler.py:26-28`
**Issue:**
`SchedulerManager` parses timestamps with bare `datetime.fromisoformat(last_ts)`
(lines 299, 334), while `discharge_handler._parse_iso_utc` (line 26-28) normalizes a
trailing `Z` to `+00:00` before parsing. On Python 3.13 (this environment) bare
`fromisoformat` accepts `Z`, but the project targets a range where this differs: on
Python ≤3.10 `datetime.fromisoformat("...Z")` raises `ValueError`. Discharge-event
timestamps are written with a `Z` suffix in several fixtures and the daemon's
`now_iso = datetime.now(timezone.utc).isoformat()` produces `+00:00` (no Z), but
externally-seeded or test-fixture timestamps using `Z` (e.g.,
`test_scheduler_manager.py:239` `"2026-03-18T10:00:00Z"`) would only parse on 3.11+. The
inconsistency means the same string parses in one module and (on older Pythons) raises in
another.

**Fix:** Route all ISO parsing through one helper that normalizes `Z`, or document the hard
Python ≥3.11 floor. Reuse the existing `_parse_iso_utc` pattern in `scheduler_manager.py`:

```python
def _parse_iso_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
```

## Info

### IN-01: Stale `sulfation` reference in config.toml comment

**File:** `config.toml:29`
**Issue:** The phase brief calls for honest docs after retracting the sulfation premise, but
`config.toml:29` still reads `# Deep discharge threshold: >70% DoD (for sulfation days_since_deep)`.
`days_since_deep` is now a pure diagnostic metric, not a sulfation input.
**Fix:** Update the comment to drop "sulfation", e.g.
`# Deep discharge threshold: >70% DoD (diagnostic days_since_deep metric)`.

### IN-02: `SchedulerDecision.test_type` retains unreachable `"deep"` literal

**File:** `src/battery_math/scheduler.py:64,131`; tests `test_dispatch.py:120,210,244`
**Issue:** The engine only ever emits `test_type="quick"` (`scheduler.py:206`,
verified by `test_scheduler.py::test_never_proposes_deep`). The `"deep"` arm of the
`Literal["deep", "quick"]` is reserved for completeness but is never produced autonomously.
Several dispatch tests still pass `test_type="deep"` manually
(`test_dispatch.py:120,210,244`), exercising a path the production scheduler can never
generate. Not a bug — but the `"deep"` literal and its tests are now testing dead production
behavior. Consider documenting why `deep` is retained or dropping it if no manual/CLI dispatch
path uses it.
**Fix:** Either keep with a one-line comment noting `deep` is reserved for a future
manual-dispatch CLI, or narrow the Literal to `"quick"` and update the manual-`deep` tests.

### IN-03: `_estimate_dod_from_buffer` magic voltages duplicated from LUT anchors

**File:** `src/discharge_handler.py:652-656`
**Issue:** `v_nominal = 12.0` / `v_floor = 10.5` are hardcoded inline, duplicating the LUT
anchor (`model.py:424` `{"v": 10.5, ... "anchor"}`) and nominal voltage
(`physics.nominal_voltage`, default 12.0). If the anchor or nominal voltage is ever
reconfigured, this heuristic silently diverges. The docstring already flags it as an
approximation, so this is low severity.
**Fix:** Source `v_floor` from `battery_model.get_anchor_voltage()` and `v_nominal` from
`battery_model.get_nominal_voltage()` with fallbacks, or hoist the constants with a comment.

### IN-04: `test_health_endpoint_v16.py` docstrings still say "v2.0 backward compatibility"

**File:** `tests/test_health_endpoint_v16.py:166-180`
**Issue:** The health endpoint no longer carries any sulfation/cycle-ROI fields, and the
phase removed those from model.json/health. The test docstrings reference "v2.0 fields" and
"backward compatibility" framing that predates the retraction. Cosmetic, but the
no-backward-compat policy (`MEMORY.md` feedback) makes "backward compatibility" language
misleading in this repo.
**Fix:** Reword docstrings to "current health schema fields" rather than "v2.0 backward
compatibility".

---

_Reviewed: 2026-06-04_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
