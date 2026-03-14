# Phase 7: Safety-Critical Metrics — Research

**Researched:** 2026-03-15
**Domain:** Real-time metric publishing during blackout events; low-latency LB flag decision
**Confidence:** HIGH

## Summary

Phase 7 addresses a **safety-critical latency gap**: during blackout events, virtual UPS metrics and the LB (LOW_BATTERY) flag are written only every 60 seconds, creating up to 50+ second delays in shutdown signaling. This is dangerous during tight margins — if battery drains quickly (final 5 minutes), a 60-second flag delay can mean shutdown happens after data loss.

**The fix:** During OB (on-battery) state, write virtual UPS metrics and evaluate LB decision **every 10-second poll**, not batched to every 6th poll. During OL (online), revert to 60-second batching for log noise reduction.

**Real impact:** 2026-03-12 blackout showed 47-minute actual vs 22-minute firmware estimate. Fast LB flag enables reliable shutdown with every minute counted; slow flag wastes precious discharge time waiting for signal.

**Primary recommendation:** Implement conditional polling frequency: OB state → per-poll writes/decisions, OL state → batched writes. Verify with mock blackout test.

---

## Standard Stack

### Core Dependencies (Already in Project)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.10+ (f-strings, modern stdlib) | Project baseline | Debian 13 default, type hints support |
| pytest | (existing) | Unit/integration testing | Already in tests/ |
| unittest.mock | stdlib | Fixture mocking | Patching socket, filesystem, systemd |
| systemd.journal | (exists) | Journal logging | Structured logs for blackout traceability |
| pathlib | stdlib | File path operations | Safer than os.path, type hints ready |

### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| time | stdlib | Polling loop timestamps | Every poll: `time.time()` for discharge tracking |
| signal | stdlib | Process control | SIGTERM handler for clean shutdown |
| tempfile | stdlib | Atomic writes | Tmpfs atomic rename pattern (already used in virtual_ups.py) |

**Note:** Phase 7 requires **no new external dependencies**. All infrastructure (pytest, systemd integration, tmpfs writes) already exists.

---

## Architecture Patterns

### Current Polling Loop Structure (Lines 651-679 in monitor.py)

```python
# CURRENT (batched every 60s):
while self.running:
    timestamp = time.time()
    ups_data = self.nut_client.get_ups_vars()          # Every poll (10s)

    # Per-poll tasks:
    self._update_ema(ups_data)                          # Every poll
    self._classify_event(ups_data)                      # Every poll
    self._track_voltage_sag(voltage)                    # Every poll
    self._track_discharge(voltage, timestamp)           # Every poll (accumulates buffer)

    # Batched every 6 polls (~60s):
    if self.poll_count % REPORTING_INTERVAL_POLLS == 0:
        battery_charge, time_rem = self._compute_metrics()    # Only every 60s
        self._handle_event_transition()                       # Only every 60s  <-- LB FLAG DECISION HERE
        self._write_virtual_ups(ups_data, battery_charge, time_rem)  # Only every 60s
```

**Problem:** If blackout occurs at poll N, LB decision not evaluated until poll N+5. During fast discharge, battery voltage can drop significantly, but flag remains stale for 50 seconds.

### Recommended Pattern for Phase 7

**Conditional batching:** Change decision gate from `poll_count % 6 == 0` to check if OB state active.

```python
# PROPOSED (per-poll during OB):
event_type = self.current_metrics.get("event_type")
is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

if is_discharging or self.poll_count % REPORTING_INTERVAL_POLLS == 0:
    battery_charge, time_rem = self._compute_metrics()
    self._handle_event_transition()  # LB decision executes every poll while OB
    self._write_virtual_ups(...)
```

**Benefit:** LB flag decision executes every 10s during blackout, <10s from actual low-battery condition to shutdown signal. During ONLINE, still batched 60s to reduce log spam.

### File Write Pattern (Already Correct in virtual_ups.py)

`write_virtual_ups_dev()` already uses atomic tmpfs pattern:
1. Tempfile created in `/dev/shm/` (same mount)
2. Content written
3. `os.fsync()` ensures sync to kernel
4. Atomic `rename()` on POSIX guarantees atomicity

**No changes needed to write mechanism.** Only frequency needs adjustment.

### State Machine for Event-Driven Metrics

Event classifier (3-state machine) is already stable:
- **ONLINE** → "OL" (normal operations, batched writes OK)
- **BLACKOUT_TEST** → "OB DISCHRG" (intentional test, track data)
- **BLACKOUT_REAL** → "OB DISCHRG" or "OB DISCHRG LB" (real power loss, flag when minutes < threshold)

Phase 7 doesn't change state classification — only **write frequency becomes state-dependent**.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Socket timeouts during slow NUT polls | Custom timeout wrapper | `socket.timeout` + `socket.settimeout()` already used in NUTClient | Works, tested, stdlib |
| Atomic tmpfs writes | Naive file.write() + manual sync | `tempfile.NamedTemporaryFile` + `os.fsync()` + `rename()` | Already implemented in virtual_ups.py, battle-tested |
| Conditional polling cadence | Multiple run() copies or flags | Single conditional gate `if is_discharging or poll % 6 == 0` | Clear, maintainable, minimal diff |
| Testing mock blackout events | Manual process sleeping | Pytest mock with controlled `poll_count` and `event_type` | Deterministic, repeatable, CI-safe |

**Key insight:** The only new logic is a boolean condition. Don't overthink it — add the gate, test it, move on.

---

## Common Pitfalls

### Pitfall 1: Off-by-One Errors in Poll Counter

**What goes wrong:** `if poll_count % 6 == 0` triggers on poll 0, 6, 12, ... But if you add OR logic, might trigger twice:
```python
if is_discharging or poll_count % 6 == 0:  # Could execute every poll + every 6th
```

**Why it happens:** Misunderstanding of when condition gates are evaluated.

**How to avoid:**
- During OB: execute every single poll regardless of modulo
- During OL: execute only on modulo 6
- This is OR logic, not AND — should be fine
- Test with `assert` on execution count after 12 mock polls

**Warning signs:**
- Virtual UPS file mtime changes at irregular intervals
- Debug logs show `_handle_event_transition` firing 7-12 times per 60s instead of 6

### Pitfall 2: Forgetting to Check Event Type BEFORE Conditional Write

**What goes wrong:**
```python
# WRONG: event_type not set yet
if is_discharging or poll_count % 6 == 0:
    event_type = self.current_metrics.get("event_type")  # Set INSIDE
    # Too late — is_discharging was evaluated against old value
```

**Why it happens:** Event type is set in `_classify_event()` which runs every poll, but metrics not computed until every 6th poll. Order of operations matters.

**How to avoid:**
- `_classify_event()` already runs every poll (line 664) — sets `event_type` in `current_metrics`
- Read `event_type` AFTER `_classify_event()` and BEFORE conditional gate
- Trace execution order: classify → evaluate gate → compute → handle transition

**Warning signs:**
- LB flag doesn't trigger immediately after blackout
- Debug log shows `event_type=None` during blackout

### Pitfall 3: Virtual UPS File Becomes Too Fresh (Monitoring False Alarms)

**What goes wrong:** If file mtime updates every 10s during blackout, external monitoring tools might interpret every write as "file is being actively modified" and trigger alerts on normal operation.

**Why it happens:** Misunderstanding of downstream tool expectations.

**How to avoid:**
- This is **not actually a problem** — tools expect metric updates
- File mtime updating every 10s during blackout = correct behavior
- Tools that expect 60s interval updates need reconfiguration (out of scope)

**Warning signs:**
- Monitoring alerts flood during long blackouts
- Timestamp drift in external logs

### Pitfall 4: Stale Metrics at Tail End of Poll Cycle

**What goes wrong:**
```python
if self.poll_count % REPORTING_INTERVAL_POLLS == 0:
    # Poll 0, 6, 12, ...
    # But what if OB at poll 7? Next write at poll 12 = 50s delay
```

**Why it happens:** Modulo arithmetic aligns to specific polls, not to when event happened.

**How to avoid:**
- OR logic fixes this: `if is_discharging or ...` means write immediately on OB, not waiting for next modulo boundary
- First write after OL→OB may happen at arbitrary poll, that's OK — metrics accurate

**Warning signs:**
- LB flag written at inconsistent intervals
- First discharge write sometimes 5s, sometimes 50s after blackout

---

## Code Examples

All examples verified against current codebase (monitor.py as of 2026-03-15).

### Pattern 1: State-Dependent Polling Gate

**Source:** Current code lines 669-676, proposed refactor

```python
def run(self):
    """Main polling loop with state-dependent metric write frequency."""
    while self.running:
        try:
            timestamp = time.time()
            ups_data = self.nut_client.get_ups_vars()

            # Every poll: EMA, classification, sag tracking, discharge tracking
            self._update_ema(ups_data)
            self._classify_event(ups_data)
            self._track_voltage_sag(voltage)
            self._track_discharge(voltage, timestamp)

            # Conditional write frequency:
            # - During OB (blackout): every poll (10s) for fast LB flag
            # - During OL (online): every 6 polls (60s) for log cleanliness
            event_type = self.current_metrics.get("event_type")
            is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

            if is_discharging or self.poll_count % REPORTING_INTERVAL_POLLS == 0:
                battery_charge, time_rem = self._compute_metrics()
                self._handle_event_transition()
                self._write_virtual_ups(ups_data, battery_charge, time_rem)

                # Only log during OL batching (reduce spam during blackout)
                if not is_discharging:
                    self._log_status(battery_charge, time_rem, poll_latency_ms)

            sd_notify('WATCHDOG=1')
            time.sleep(1 if self.sag_state == SagState.MEASURING else POLL_INTERVAL)

        except Exception as e:
            # Error handling unchanged
            logger.error(f"Error: {e}")
            time.sleep(POLL_INTERVAL)
```

**Rationale:**
- Evaluates `event_type` before gate (classifies first)
- OR logic: write if `is_discharging` OR every 6th poll
- Logging frequency optional (reduce noise during blackout)
- Preserves existing error handling

### Pattern 2: Verifying LB Flag Decision is Per-Poll

**Source:** monitor.py line 218-250, `_handle_event_transition()`

```python
def _handle_event_transition(self):
    """Execute per-poll during OB state to catch LB threshold immediately."""
    event_type = self.current_metrics["event_type"]

    # This block runs every poll now (not gated), so LB decision is immediate
    if event_type == EventType.BLACKOUT_REAL:
        time_rem = self.current_metrics.get("time_rem_minutes")
        if time_rem is not None and time_rem < self.shutdown_threshold_minutes:
            # Flag set immediately, written to virtual UPS this poll
            self.current_metrics["shutdown_imminent"] = True
            logger.warning(f"LB threshold crossed: {time_rem:.1f}min < {self.shutdown_threshold_minutes}min")
        else:
            self.current_metrics["shutdown_imminent"] = False

    # Status override calls compute_ups_status_override() which uses shutdown_imminent
    self.current_metrics["ups_status_override"] = compute_ups_status_override(
        event_type,
        self.current_metrics.get("time_rem_minutes", 0) or 0,
        self.shutdown_threshold_minutes
    )
    # Rest of method unchanged (OB→OL transition handling)
```

**Verification:** When `_handle_event_transition()` runs every poll (not every 6th), decision reflects actual battery state, not delayed state from 60s ago.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Write virtual UPS every 60s, always | Write every poll during OB, every 60s during OL | Phase 7 (2026-03) | LB flag latency drops from ~50s worst-case to <10s |
| Single polling cadence for all states | State-dependent cadence (OB vs OL) | Phase 7 | Safety during blackout without increasing ONLINE log spam |
| Batched metrics evaluation | Per-poll evaluation during discharge | Phase 7 | Metrics reflect live battery state, not 60s stale snapshot |

**Deprecation notes:** No deprecation — Phase 7 is backward-compatible refactor. Existing systemd service file, upsmon config, virtual UPS format all unchanged.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (existing) |
| Config file | pytest.ini (testpaths=tests, python_files=test_*.py) |
| Quick run command | `pytest tests/test_monitor.py -v --tb=short` |
| Full suite command | `pytest tests/ -v --tb=short` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SAFE-01 | Virtual UPS metrics file (dummy-ups state) updated every 10s while OB state active | integration | `pytest tests/test_monitor.py::test_per_poll_writes_during_blackout -v` | ❌ Wave 0 |
| SAFE-02 | LB flag decision (`_handle_event_transition()`) executes on every poll while OB state active, not batched | integration | `pytest tests/test_monitor.py::test_handle_event_transition_per_poll_during_ob -v` | ❌ Wave 0 |
| SAFE-01 | No metric writes occur during OL state — writes only during OB state transition | unit | `pytest tests/test_monitor.py::test_no_writes_during_online_state -v` | ❌ Wave 0 |
| SAFE-02 | upsmon receives LB signal within 10s of actual out-of-battery condition (via file mtime) | integration | `pytest tests/test_monitor.py::test_lb_flag_signal_latency -v` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_monitor.py -v` (monitor tests only, ~2-3 sec)
- **Per wave merge:** `pytest tests/ -v --tb=short` (full suite, ~10-15 sec with mocks)
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_monitor.py::test_per_poll_writes_during_blackout` — Fixture: mock poll loop with `poll_count`, event transitions (OL→OB→OL), verify `write_virtual_ups` call count per cycle
- [ ] `tests/test_monitor.py::test_handle_event_transition_per_poll_during_ob` — Fixture: mock `current_metrics` with BLACKOUT_REAL, time_rem below threshold, verify `_handle_event_transition()` called every iteration
- [ ] `tests/test_monitor.py::test_no_writes_during_online_state` — Fixture: mock ONLINE state for 7 polls, verify writes occur only on poll 0, 6 (not all 7)
- [ ] `tests/test_monitor.py::test_lb_flag_signal_latency` — Advanced integration: mock virtual UPS write, capture file mtime timestamps, verify <10s latency from OB transition to LB flag in file
- [ ] `tests/conftest.py` enhancements — May need fixture for `mock_event_type_during_poll_sequence` to simulate realistic blackout

---

## Open Questions

1. **Should `_log_status()` also be gated by OB state (reduce blackout spam)?**
   - What we know: `_log_status()` currently called every 60s, logs raw metrics
   - What's unclear: Is per-poll logging acceptable, or too verbose in journal?
   - Recommendation: Make logging per-10s during OB but offer config flag to disable if verbose. Low priority for Phase 7.

2. **Does SAG measurement (1s sleep during MEASURING state) conflict with per-poll metrics?**
   - What we know: SAG state triggers 1s poll when measuring (line 679)
   - What's unclear: Should metrics still write during SAG measurement, or wait for sag completion?
   - Recommendation: Write metrics every poll regardless of SAG state. Sag capture is orthogonal to metrics. Add comment.

3. **Should virtual UPS file have a "heartbeat" during long OB (every 10s) to signal daemon liveness?**
   - What we know: File mtime will update every 10s, inherently
   - What's unclear: Should external monitors depend on mtime frequency for liveness?
   - Recommendation: Out of scope for Phase 7 (infrastructure concern, not safety-critical). Future phase.

---

## Sources

### Primary (HIGH confidence)
- **CONTEXT.md** — Project architecture, blackout scenario, firmware vs daemon metrics
- **STATE.md** — Phase 7 requirements, polling loop analysis, "LB flag decision gated by 6-poll interval"
- **EXPERT-PANEL-REVIEW-2026-03-15.md** — P0 findings: "stale metrics during blackout", SRE vs Kaizen trade-off resolution
- **monitor.py (lines 651-679)** — Current polling loop, REPORTING_INTERVAL_POLLS=6, modulo gate
- **monitor.py (lines 218-250)** — `_handle_event_transition()` method where LB decision occurs
- **virtual_ups.py (lines 22-95)** — `write_virtual_ups_dev()` atomic write implementation, already correct
- **event_classifier.py (lines 44-72)** — EventType enum and classify() method, state machine

### Secondary (MEDIUM confidence)
- **conftest.py (lines 1-100)** — pytest fixtures, mock patterns (mock_socket_ok, temporary_model_path)
- **Real blackout data (2026-03-12)** — 47 min actual vs 22 min firmware: validates need for accurate metrics

### Tertiary (LOW confidence — informational only)
- None — all critical design facts sourced from codebase and official project docs

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — Only stdlib + existing deps, no new packages
- Architecture: HIGH — Polling loop structure documented in code, expert panel agreed on fix direction
- Pitfalls: HIGH — Derived from code inspection and expert panel P0 findings
- Test strategy: MEDIUM — Existing test infrastructure solid, but Phase 7-specific tests need design

**Research date:** 2026-03-15
**Valid until:** 2026-03-22 (Phase 7 implementation window, low-churn domain)
**Invalidation triggers:** Changes to polling loop architecture, event classifier overhaul, new async polling mechanism

---

## Implementation Checklist for Planner

- [ ] Create conditional gate: `is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)`
- [ ] Refactor metrics write condition from `poll_count % 6 == 0` to `is_discharging or poll_count % 6 == 0`
- [ ] Verify `_classify_event()` executes **before** conditional gate (order: classify → gate → compute)
- [ ] Add debug log at gate execution: `logger.debug(f"Metrics gate: is_discharging={is_discharging}, poll_count={poll_count}")`
- [ ] Design 4 new integration tests (see Validation Architecture section)
- [ ] Verify existing 160 tests still pass (regression check)
- [ ] Manual test: Simulate OL→OB→OL with mock, check file mtime updates every 10s during OB
- [ ] Document state-dependent behavior in run() docstring
