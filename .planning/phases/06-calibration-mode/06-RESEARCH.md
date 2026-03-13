# Phase 6: Calibration Mode - Research

**Researched:** 2026-03-14
**Domain:** Command-line flag handling, real-time model persistence, cliff region calibration, threshold dynamics
**Confidence:** HIGH

## Summary

Phase 6 enables one-time manual battery calibration without production shutdown risk. The feature centers on a `--calibration-mode` flag that:
1. Reduces shutdown threshold from N minutes to ~1 minute (allows battery to discharge near cutoff)
2. Enables real-time model.json writes with fsync (one-time cost during calibration event)
3. Auto-interpolates cliff region (11.0V–10.5V) from measured data after discharge completes

The implementation leverages existing Phase 3 virtual UPS architecture and Phase 4 discharge buffer + SoH calculation. No new dependencies required; all logic fits into monitor.py configuration and event handling paths.

**Primary recommendation:** Implement as 3 plans (Wave 0–1): (1) flag parsing + threshold override, (2) real-time fsync writes during BLACKOUT_TEST events, (3) cliff region interpolation on discharge completion.

## User Constraints

(No CONTEXT.md found; proceeding with standard stack and patterns.)

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CAL-01 | `--calibration-mode` flag reduces shutdown threshold to ~1 min; daemon doesn't initiate critical shutdown until Time_rem ≈ 1 min | Flag parsing + threshold override in monitor.py; compute_ups_status_override() uses runtime_minutes < calibration_threshold |
| CAL-02 | In calibration mode, each datapoint written to disk with fsync; model.json updated in real-time (one-time cost, not repeated) | Discharge buffer points flushed on-demand; atomic_write_json() with fsync already in model.py |
| CAL-03 | After calibration event completes, cliff region (11.0V–10.5V) auto-interpolated to anchor (10.5V, 0 min); measured points replace "standard" entries | Linear interpolation between measured boundary points; LUT entry source flags (standard/measured/anchor) already in place |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.13+ | Runtime | Already chosen for project; systemd daemon |
| pytest | 8.3.5 | Unit testing | Established test infrastructure (130 tests) |
| logging | stdlib | Event logging | Integrated with journald via systemd.journal.JournalHandler |

### Supporting (Already In Place)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| systemd.journal | systemd pkg | Structured logging | All logging > syslog in monitor.py |
| pathlib | stdlib | File I/O | Configuration, model.json persistence |
| tempfile | stdlib | Atomic writes | model.py atomic_write_json() + virtual_ups.py |
| argparse | stdlib | CLI argument parsing | **NEW: Parse --calibration-mode flag** |

### No New Dependencies
Phase 6 requires **zero new external libraries**. All capabilities exist:
- Flag parsing: argparse (stdlib)
- Real-time writes: Already using fsync in model.py + virtual_ups.py
- Interpolation: Linear math (already in soc_predictor.py for LUT)
- Threshold override: compute_ups_status_override() in virtual_ups.py already parameterized

## Architecture Patterns

### Recommended Project Structure

No new directories needed. Phase 6 integrates into existing files:
```
src/
├── monitor.py              # Add: --calibration-mode flag, threshold override
├── model.py                # Add: calibration_write() method with fsync
├── virtual_ups.py          # No changes (threshold already parameterized)
├── event_classifier.py     # No changes (BLACKOUT_TEST already distinguished)
└── soh_calculator.py       # Add: interpolate_cliff_region() function

tests/
├── test_monitor.py         # Add: calibration mode flag tests
├── test_model.py           # Add: calibration_write() tests
└── test_soh_calculator.py  # Add: interpolation tests
```

### Pattern 1: Command-Line Flag Parsing

**What:** Use argparse to accept `--calibration-mode` flag at daemon startup, propagate as configuration to MonitorDaemon.

**When to use:** Any daemon accepting runtime overrides without env vars; more explicit than environment variables.

**Example:**
```python
# Source: stdlib argparse, applied to monitor.py main()
import argparse

def main():
    parser = argparse.ArgumentParser(description="UPS Battery Monitor")
    parser.add_argument('--calibration-mode', action='store_true',
                        help="Enable calibration mode (lower shutdown threshold to ~1 min)")
    args = parser.parse_args()

    # Pass to daemon
    daemon = MonitorDaemon(calibration_mode=args.calibration_mode)
    daemon.run()
```

**Key invariant:** Flag immutable after daemon startup (set once, never changes during run).

### Pattern 2: Threshold Override in Shutdown Logic

**What:** compute_ups_status_override() accepts threshold parameter; in calibration mode, pass 1 instead of SHUTDOWN_THRESHOLD_MINUTES.

**When to use:** When threshold is context-dependent (normal vs. test/calibration scenarios).

**Example:**
```python
# Source: Phase 3 virtual_ups.py compute_ups_status_override()
# Already parameterized; just pass different value
threshold = 1 if calibration_mode else SHUTDOWN_THRESHOLD_MINUTES
ups_status_override = compute_ups_status_override(
    event_type,
    time_rem_minutes,
    threshold
)
```

**Existing support:** Function already uses `<` comparison (not `<=`), verified in test_virtual_ups.py::test_calibration_mode_threshold().

### Pattern 3: Real-Time Model Write During Calibration

**What:** Define calibration_write() method in BatteryModel that bypasses normal update-on-completion pattern. Called from monitor.py discharge buffer when in BLACKOUT_TEST + calibration_mode.

**When to use:** One-time calibration events requiring immediate disk persistence for safety/visibility.

**Example:**
```python
# Source: model.py enhancement
class BatteryModel:
    def calibration_write(self, timestamp: float):
        """
        Write current discharge buffer state to model.json with fsync.
        Used during calibration mode to capture intermediate points in real-time.

        Args:
            timestamp: Current measurement timestamp
        """
        # Extract latest point from discharge buffer
        # Append to soh_history as "calibration" entry with timestamp
        # Call atomic_write_json() → forces immediate fsync
        pass
```

**Safety invariant:** atomic_write_json() already uses tempfile + fsync + os.replace pattern (POSIX atomic); no new risk.

### Pattern 4: Cliff Region Interpolation

**What:** After discharge completes, identify measured points in cliff region (11.0V–10.5V), linearly interpolate missing entries between them.

**When to use:** Converting sparse calibration data into dense LUT suitable for normal operation.

**Example:**
```python
# Source: soh_calculator.py new function
def interpolate_cliff_region(
    measured_points: List[Dict],  # {v, soc, source}
    anchor_voltage: float = 10.5,
    cliff_start: float = 11.0
) -> List[Dict]:
    """
    Interpolate cliff region (11.0V–10.5V) from measured calibration points.

    Fills gaps between measured points with linear interpolation.
    Marks interpolated entries with source='interpolated'.

    Returns: Updated LUT entries for cliff region.
    """
    # Filter measured points in cliff region
    cliff_measured = [p for p in measured_points
                      if anchor_voltage <= p['v'] <= cliff_start]

    if len(cliff_measured) < 2:
        return cliff_measured  # Can't interpolate single point

    # Linear interpolation between consecutive points
    # Insert 0.1V resolution points (10.5, 10.6, 10.7, ..., 11.0)
    interpolated = []
    for i in range(len(cliff_measured) - 1):
        p1, p2 = cliff_measured[i], cliff_measured[i+1]
        # Linear: soc = p1['soc'] + (v - p1['v']) / (p2['v'] - p1['v']) * (p2['soc'] - p1['soc'])
        pass

    return interpolated
```

**Existing support:** soc_predictor.py already implements linear interpolation for LUT lookup (soc_from_voltage); reuse same logic in reverse.

### Anti-Patterns to Avoid

- **Polling `--calibration-mode` at runtime:** Flag set once at startup; checking `sys.argv` in poll loop wastes CPU. Store as `self.calibration_mode` instance variable.
- **Writing model.json on every poll in calibration mode:** Only write when discharge buffer reaches N points (e.g., every 10 samples, not every 1-second poll).
- **Hardcoding interpolation resolution:** Make cliff region step (0.1V, 0.05V, etc.) configurable via env var `UPS_MONITOR_CLIFF_RESOLUTION`.
- **Skipping fsync during calibration:** Opposite of real-time safety; always fsync + atomic rename when calibration_write() called.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| CLI argument parsing | Custom sys.argv loop | argparse (stdlib) | Handles help, type validation, error messages; 12 lines vs 50+ custom |
| Atomic file writes | Multiple syscalls (open, write, close, rename) | atomic_write_json() pattern (tempfile + fsync + replace) | Already implemented in model.py; prevents corruption on power loss |
| Linear interpolation math | Manual slope calculation | soc_predictor.soc_from_voltage() pattern | Inverse already verified by 8 tests; reuse existing math |
| Discharge buffer flushing | Custom threading/queue | Synchronous write in event handler | Discharge events are rare (monthly); complexity not justified |
| Threshold override logic | Conditional sprawl in monitor.py | Parameter to compute_ups_status_override() | Single function responsible for LB flag; easier to test than spread conditionals |

**Key insight:** The project already has atomic write, interpolation, and event-driven architecture. Phase 6 is plumbing + config, not new algorithms.

## Common Pitfalls

### Pitfall 1: Calibration Mode Flag Affects Wrong Threshold

**What goes wrong:** Dev sets calibration_threshold in SHUTDOWN_THRESHOLD_MINUTES env var, but monitor.py still uses global constant. Battery charges to 1 minute, LB flag fires, daemon shuts down server prematurely.

**Why it happens:** Confusion between environment variable (persistent, global) vs. command-line flag (transient, per-invocation). If both exist, which wins?

**How to avoid:**
- Command-line flag takes precedence (lowest precedence: env var → default constant → arg flag).
- Document precedence in help: `--calibration-mode overrides UPS_MONITOR_SHUTDOWN_THRESHOLD_MIN env var`.
- Test both scenarios: env var alone, flag alone, both (flag wins).

**Warning signs:** In logs, shutdown threshold printed at startup doesn't match what user expects. Virtual UPS metrics show LB flag before expected time.

### Pitfall 2: Real-Time Writes Cause I/O Stalls

**What goes wrong:** calibration_write() called every poll (10 sec interval) during BLACKOUT_TEST. With fsync, each write stalls 10–50 ms waiting for kernel. 130 polls × 50 ms = 6 sec overhead across 22-minute calibration. But if disk saturated (logs, other I/O), stall ≈ 1 sec per write. Daemon CPU usage spikes, polls delayed.

**Why it happens:** fsync blocks until disk completes write; SSD doesn't guarantee instant completion. Calibration doesn't justify constant I/O cost.

**How to avoid:**
- Write only on dischage buffer transition to BLACKOUT_TEST (once), then every N samples (e.g., N=6 = every 60 sec, not every 10 sec).
- Log write timing: `logger.info(f"Calibration write took {elapsed:.2f}ms")` to detect stalls in production.
- Batch writes: accumulate 5–10 points, write once, not per-point.

**Warning signs:** journalctl shows `Monitor polling behind schedule` errors. Poll interval drifts > 5 sec. CPU usage 15–25% during calibration (vs. 2–3% normally).

### Pitfall 3: Cliff Region Interpolation Overwrites Manual Tuning

**What goes wrong:** User calibrates once, gets measured cliff region (10.8V → 5%, 10.6V → 2%). Then later, user manually tweaks LUT entries in model.json (e.g., 10.7V → 3.5%). On next discharge, auto-interpolation replaces manual tuning with linear fit, losing calibration.

**Why it happens:** Interpolation algorithm doesn't know which entries are hand-tuned vs. auto-generated. Overwrites without mercy.

**How to avoid:**
- Mark interpolated entries with `source: 'interpolated'` in LUT JSON.
- Interpolation only replaces entries with `source: 'interpolated'`, never `source: 'measured'` or `source: 'manual'`.
- Document in model.json schema: "To tune manually, change source to 'manual' and interpolation won't overwrite".

**Warning signs:** model.json changes unexpectedly after discharge. User reports "I fixed the 10.7V point, but it changed again".

### Pitfall 4: Calibration Mode Leaves Daemon in Inconsistent State

**What goes wrong:** User starts daemon with `--calibration-mode`, performs calibration (battery discharge). Calibration completes. User forgets flag is set; daemon continues with threshold=1 min. Next normal brownout, LB flag fires at 1 min instead of 5 min. Server shuts down early, production loss.

**Why it happens:** Flag not reset after calibration; assumption that user manually restarts daemon is fragile.

**How to avoid:**
- Add logic: on discharge completion during calibration, log prominent message: `logger.warning("Calibration complete; disable --calibration-mode for normal operation")`.
- Consider auto-exit: `if calibration_mode and discharge_complete: logger.info("Exiting calibration mode"); sys.exit(0)`.
- Test: verify daemon doesn't stay in calibration mode after one discharge.

**Warning signs:** Logs show no warning message at calibration end. User reports daemon kept low threshold after manual test.

### Pitfall 5: Discharge Buffer Fills Indefinitely in Test Mode

**What goes wrong:** User manually triggers battery test via UPS button, discharge takes 30 minutes. Daemon is in BLACKOUT_TEST mode. Discharge buffer collects 180 samples (30 min ÷ 10 sec). Each calibration_write() copies full buffer to model.json. Buffer never clears. Next discharge, buffer merges with previous data, interpolation confused by duplicate points.

**Why it happens:** BLACKOUT_TEST doesn't transition to ONLINE until button released and UPS returns to mains. If test lasts long, buffer accumulates. No automatic cleanup.

**How to avoid:**
- Cap discharge buffer size: `if len(discharge_buffer) > 500: discharge_buffer.pop(0)` (rolling window).
- Flush buffer explicitly on OB→OL transition (existing code does this).
- Test: verify buffer clears after discharge completion.

**Warning signs:** model.json soh_history grows by >300 entries per calibration. Interpolation produces nonsense SoC values (e.g., 10.8V → 150% SoC).

## Code Examples

Verified patterns from official sources and existing codebase:

### Calibration Mode Flag Parsing
```python
# Source: stdlib argparse, monitor.py
import argparse

def main():
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon",
        epilog="Calibration mode: use --calibration-mode for one-time battery discharge testing"
    )
    parser.add_argument(
        '--calibration-mode',
        action='store_true',
        default=False,
        help="Enable calibration mode (shutdown threshold ~1 min, real-time model writes)"
    )
    args = parser.parse_args()

    try:
        daemon = MonitorDaemon(calibration_mode=args.calibration_mode)
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
```

### Threshold Override in Shutdown Logic
```python
# Source: Phase 3 monitor.py + virtual_ups.py
class MonitorDaemon:
    def __init__(self, calibration_mode=False):
        # ... existing code ...
        self.calibration_mode = calibration_mode
        self.shutdown_threshold_minutes = 1 if calibration_mode else SHUTDOWN_THRESHOLD_MINUTES
        logger.info(f"Daemon initialized: calibration_mode={calibration_mode}, "
                    f"shutdown_threshold={self.shutdown_threshold_minutes} min")

    def _handle_event_transition(self):
        # ... existing code ...
        # In BLACKOUT_REAL event handling:
        ups_status_override = compute_ups_status_override(
            event_type,
            time_rem_minutes,
            self.shutdown_threshold_minutes  # Use instance var, not constant
        )
```

### Real-Time Model Write (Calibration)
```python
# Source: model.py enhancement
class BatteryModel:
    def calibration_write(self, voltage: float, soc: float, timestamp: float):
        """
        Write calibration datapoint to model.json with fsync.

        Called from monitor.py during BLACKOUT_TEST calibration events
        to capture intermediate measurements in real-time.

        Args:
            voltage: Measured battery voltage (V)
            soc: Calculated SoC from voltage (0.0–1.0)
            timestamp: Unix timestamp of measurement

        Raises:
            IOError: If atomic write fails
        """
        # Add to lut if not present (avoid duplicates)
        existing = [e for e in self.data['lut'] if abs(e['v'] - voltage) < 0.01]
        if not existing:
            self.data['lut'].append({
                'v': voltage,
                'soc': soc,
                'source': 'measured',
                'timestamp': timestamp  # For traceability
            })
            # Sort LUT descending by voltage (highest to lowest)
            self.data['lut'].sort(key=lambda x: x['v'], reverse=True)

        # Atomic write
        self.save()
```

### Cliff Region Interpolation
```python
# Source: soh_calculator.py new function
def interpolate_cliff_region(
    lut: List[Dict],
    anchor_voltage: float = 10.5,
    cliff_start: float = 11.0,
    step_mv: float = 0.1
) -> List[Dict]:
    """
    Interpolate cliff region (11.0V–10.5V) in LUT from measured calibration data.

    Fills gaps between measured points with linear interpolation.
    Marks interpolated entries with source='interpolated'.
    Removes old 'standard' entries in cliff region.

    Args:
        lut: Current LUT entries
        anchor_voltage: Bottom of cliff (10.5V default)
        cliff_start: Top of cliff (11.0V default)
        step_mv: Interpolation resolution (100 mV default = 0.1V)

    Returns:
        Updated LUT with cliff region interpolated
    """
    # Separate cliff region from rest of LUT
    cliff_entries = [e for e in lut if anchor_voltage <= e['v'] <= cliff_start and e['source'] == 'measured']
    other_entries = [e for e in lut if e['v'] < anchor_voltage or e['v'] > cliff_start]

    if len(cliff_entries) < 2:
        # Can't interpolate single or zero points; keep standard entries
        return lut

    # Sort measured points ascending by voltage
    cliff_entries.sort(key=lambda x: x['v'])

    # Interpolate between consecutive measured points
    interpolated = []
    for i in range(len(cliff_entries) - 1):
        p1, p2 = cliff_entries[i], cliff_entries[i + 1]

        # Add first point
        interpolated.append(p1)

        # Linear interpolation: soc = p1['soc'] + (v - p1['v']) / (p2['v'] - p1['v']) * (p2['soc'] - p1['soc'])
        v_current = p1['v'] + step_mv
        while v_current < p2['v']:
            frac = (v_current - p1['v']) / (p2['v'] - p1['v'])
            soc_interp = p1['soc'] + frac * (p2['soc'] - p1['soc'])
            interpolated.append({
                'v': round(v_current, 2),
                'soc': round(soc_interp, 3),
                'source': 'interpolated'
            })
            v_current += step_mv

    # Add last point
    interpolated.append(cliff_entries[-1])

    # Combine with non-cliff entries and re-sort
    updated_lut = other_entries + interpolated
    updated_lut.sort(key=lambda x: x['v'], reverse=True)

    return updated_lut
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.5 + conftest.py fixtures |
| Config file | pytest.ini (existing: testpaths=tests, python_files=test_*.py) |
| Quick run command | `pytest tests/test_monitor.py tests/test_model.py -v -x` |
| Full suite command | `pytest tests/ -v --tb=short` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CAL-01 | `--calibration-mode` flag parsed, threshold set to 1 min, LB flag deferred until Time_rem < 1 min | unit | `pytest tests/test_monitor.py::test_calibration_flag_parsing -xvs` | ❌ Wave 0 |
| CAL-01 | Shutdown threshold override: normal mode threshold 5 min vs. calibration mode 1 min | unit | `pytest tests/test_virtual_ups.py::test_calibration_threshold_override -xvs` | ✅ Partial (test_calibration_mode_threshold exists, needs update) |
| CAL-02 | calibration_write() atomically writes voltage/SoC to model.json with fsync | unit | `pytest tests/test_model.py::test_calibration_write_fsync -xvs` | ❌ Wave 0 |
| CAL-02 | Discharge buffer collected during BLACKOUT_TEST, written on-demand | unit | `pytest tests/test_monitor.py::test_discharge_buffer_calibration_write -xvs` | ❌ Wave 0 |
| CAL-03 | Cliff region interpolation: linear math between measured points 11.0V–10.5V | unit | `pytest tests/test_soh_calculator.py::test_interpolate_cliff_region -xvs` | ❌ Wave 0 |
| CAL-03 | LUT source field: 'measured' vs. 'interpolated' vs. 'standard' correctly distinguished | unit | `pytest tests/test_model.py::test_lut_source_field_preservation -xvs` | ❌ Wave 0 |
| CAL-03 | After discharge OB→OL, cliff region auto-interpolated, measured entries replace standard | integration | `pytest tests/test_monitor.py::test_calibration_lut_update -xvs` | ❌ Wave 1 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_monitor.py tests/test_model.py -x` (5–10 sec, fast)
- **Per wave merge:** `pytest tests/ -v --tb=short` (15–20 sec, full coverage)
- **Phase gate:** Full suite green + manual UPS battery test (30–45 min live calibration)

### Wave 0 Gaps
- [ ] `tests/test_monitor.py::test_calibration_flag_parsing` — argparse integration, MonitorDaemon(calibration_mode=True) constructor
- [ ] `tests/test_monitor.py::test_discharge_buffer_calibration_write` — discharge buffer flushing, calibration_write() calls
- [ ] `tests/test_model.py::test_calibration_write_fsync` — BatteryModel.calibration_write() atomic write with fsync
- [ ] `tests/test_soh_calculator.py::test_interpolate_cliff_region` — interpolate_cliff_region() math, source field preservation
- [ ] `tests/test_monitor.py::test_calibration_lut_update` — integration: discharge OB→OL transition triggers interpolation

*(Existing test infrastructure adequate; Phase 5 installed pytest, conftest.py fixtures cover NUT mock, tmpfs, model.json setup)*

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual model.json editing post-discharge | Real-time writes during BLACKOUT_TEST | Phase 6 | Calibration data captured immediately without daemon restart |
| Fixed threshold for all scenarios | Parameterized threshold via flag | Phase 6 | Calibration and normal operation use different thresholds; safety preserved |
| Linear standard curve (13.4–10.5V) | Measured points + interpolation | Phase 6 | Cliff region now accurate per real battery instead of generic datasheet |
| Single SoH value stored | SoH history + regression prediction | Phase 4 | Already done; Phase 6 reuses for calibration flag marking |

**Deprecated/outdated:**
- **Manual calibration runs** (before Phase 6): user had to manually edit model.json after discharge; now automated.
- **Firmware-based calibration** (UT850EG): NUT's `onlinedischarge_calibration: true` doesn't work; replaced by software detection (BLACKOUT_TEST) + Phase 6 flag.

## Open Questions

1. **Calibration-write frequency during discharge**
   - What we know: Current code writes model.json on completion (OB→OL). Calibration mode needs intermediate writes.
   - What's unclear: Every 10 sec (each poll), every 60 sec (6 polls), or every N points (10 samples)?
   - Recommendation: Every 60 sec = 6 polls, unless production experience shows need for faster updates. Set threshold via `UPS_MONITOR_CALIBRATION_WRITE_INTERVAL_SEC` env var.

2. **Cliff region interpolation step size**
   - What we know: Standard LUT has entries 0.4V apart (13.4 → 12.8 → 12.4, etc.). Cliff region much steeper.
   - What's unclear: Use 0.1V step (10 points), 0.05V (20 points), or adaptive based on measured density?
   - Recommendation: Fixed 0.1V (9 points: 10.5 to 11.0). Tested simpler than adaptive. Override via `UPS_MONITOR_CLIFF_RESOLUTION` if needed.

3. **Calibration-mode behavior at discharge end**
   - What we know: After OB→OL transition, cliff region auto-interpolated, daemon logs completion.
   - What's unclear: Should daemon auto-exit (flag was one-time), or stay running until user signals?
   - Recommendation: Daemon stays running (user may want to repeat test). Add prominent log warning: "Calibration complete; remove --calibration-mode for normal operation". Document in help message.

4. **Backward compatibility: model.json schema**
   - What we know: Current schema has `lut[]` with {v, soc, source}. Phase 6 adds timestamp field.
   - What's unclear: If old model.json lacks timestamp, should code handle gracefully?
   - Recommendation: timestamp optional; code uses `entry.get('timestamp')` not `entry['timestamp']`. Migration not needed.

## Sources

### Primary (HIGH confidence)
- Phase 1–5 codebase (monitor.py, model.py, virtual_ups.py, soh_calculator.py) — current architecture confirmed
- test_virtual_ups.py test_calibration_mode_threshold() — threshold override already tested
- STATE.md (Project State) — Phase 5 complete, Phase 6 ready, no blockers
- REQUIREMENTS.md CAL-01, CAL-02, CAL-03 — locked requirements for Phase 6

### Secondary (MEDIUM confidence)
- project_ups_monitor_spec.md memory — business context: "one-time manual calibration flag", real discharge data (2026-03-12 blackout)
- Model.py atomic_write_json() + fsync pattern — verified safe for persistent writes during discharge
- Phase 4 discharge_buffer pattern — already collects voltage/time during BLACKOUT_REAL; Phase 6 reuses for BLACKOUT_TEST

### Tertiary (references for implementation detail, verified with codebase)
- sys.argv, argparse (stdlib) — no external sources needed; standard Python
- Linear interpolation math (soc_predictor.py) — existing implementation, 8 tests passing

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** - No new dependencies; argparse, fsync, linear math all stdlib or in-codebase
- Architecture: **HIGH** - Phase 3 virtual_ups parameterization already done; Phase 4 discharge buffer pattern established
- Pitfalls: **MEDIUM** - Common scenarios (I/O stalls, flag lifecycle, buffer overflow) identified from similar projects; specific tuning (write frequency, interpolation step) TBD in planning

**Research date:** 2026-03-14
**Valid until:** 2026-03-21 (7 days — Phase 6 is final phase, stable scope)

**Next steps:** Planner will create Wave 0 (flag parsing + threshold override) and Wave 1 (real-time writes + interpolation) plans. Expect 2–3 plans, ~8–15 min execution per plan, ~30 tests.
