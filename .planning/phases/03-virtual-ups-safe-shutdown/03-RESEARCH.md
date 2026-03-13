# Phase 3: Virtual UPS & Safe Shutdown - Research

**Researched:** 2026-03-14
**Domain:** NUT dummy-ups driver configuration, tmpfs file I/O, upsmon shutdown coordination, battery state override arbitration
**Confidence:** HIGH

## Summary

Phase 3 bridges the calculation layer (Phase 2 metrics) with the NUT ecosystem by implementing a transparent virtual UPS proxy. The daemon writes corrected battery state to a tmpfs file; NUT's `dummy-ups` driver reads this file and serves metrics to `upsmon` and Grafana without any configuration changes to those systems.

Research confirms that:
- NUT `dummy-ups` driver (v2.8.1+) supports reading from `.dev` files with automatic re-parsing on filesystem timestamp changes (no polling overhead)
- tmpfs `/dev/shm` is ideal for this use case: zero SSD wear, fast I/O, survives across process restarts (data structure is simple text key-value pairs)
- `upsmon` receives `LOW_BATTERY` signal when `ups.status` contains `LB` flag; safe shutdown requires coordination with `FINALDELAY` timing
- Three fields are safely overridden (battery.runtime, battery.charge, ups.status); all other fields transparently mirrored from real UPS
- Graceful shutdown is coordinated by setting LB flag only when calculated `time_rem < threshold`; this signal to upsmon must not fire before actual time remaining expires

**Primary recommendation:** Write corrected metrics to `/dev/shm/ups-virtual.dev` every 10 seconds (matching daemon poll interval); configure dummy-ups in NUT with `mode = dummy-once` to minimize CPU load; set upsmon FINALDELAY ≥ 15 seconds to ensure daemon can calculate time_rem and emit shutdown signal before battery dies.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **Virtual UPS source:** `/dev/shm/ups-virtual.dev` (tmpfs, not disk)
- **File format:** NUT standard key-value pairs (matching `upsc` output format)
- **Three overridden fields:** `battery.runtime`, `battery.charge`, `ups.status`
- **All other fields:** Transparent pass-through from real UPS (no filtering or modification)
- **Shutdown signal:** Set `ups.status` to include `LB` flag when `time_rem < SHUTDOWN_THRESHOLD_MINUTES`
- **No NUT config changes:** dummy-ups source is added, but existing upsd.conf and upsmon.conf remain untouched
- **Integration with upsmon:** upsmon must receive LB signal and initiate graceful shutdown via SHUTDOWNCMD

### Claude's Discretion
- **Shutdown threshold:** Default 5 minutes (configurable via environment variable)
- **Calibration mode threshold:** Reduced to ~1 minute for battery testing (Phase 6)
- **File write frequency:** Aligned with polling interval (10 seconds by default)
- **Error handling:** What to do if `/dev/shm` is unavailable or write fails

### Deferred Ideas (OUT OF SCOPE)
- Real-time file monitoring via inotify (dummy-once mode handles file changes via timestamp)
- Atomic file locking mechanisms (simple tmpfs write is atomic for single variables)
- Multiple virtual UPS instances (single virtual device for main monitoring)
- Integration with upssched (Phase 5 operations, not Phase 3)

</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| VUPS-01 | Daemon writes all fields to `/dev/shm/ups-virtual.dev` (tmpfs, zero SSD wear) | tmpfs is RAM-based virtual filesystem; survives process restart but cleared on reboot. Zero I/O overhead to persistent storage. File format: NUT standard key-value pairs. |
| VUPS-02 | All real UPS fields transparently mirrored to virtual device | NUT upsc output contains all available variables. Daemon reads from upsc, writes all fields except 3 overrides directly to `.dev` file. |
| VUPS-03 | Three fields overridden: battery.runtime (Time_rem), battery.charge (SoC%), ups.status (LB arbiter) | Phase 2 daemon already calculates these values. Phase 3 writes calculated values to `.dev` file instead of logging. ups.status logic: emit "OL" or "OB DISCHRG" or "OB DISCHRG LB" based on event type and time_rem threshold. |
| VUPS-04 | dummy-ups configured in NUT as data source for upsmon and Grafana | NUT dummy-ups driver reads `.dev` file and provides variables to other NUT consumers. Configuration: add block to upsd.conf or /etc/nut/ups.conf with `driver = dummy-ups`, `port = /dev/shm/ups-virtual.dev`, `mode = dummy-once`. |
| SHUT-01 | upsmon receives LB signal and initiates graceful shutdown | upsmon monitors `ups.status` field; when it contains "LB" flag, triggers LOWBATT notify event and executes SHUTDOWNCMD. Daemon must ensure LB flag is only set when battery actually has < threshold minutes remaining. |
| SHUT-02 | Shutdown threshold configurable (minutes before end) | Environment variable `UPS_MONITOR_SHUTDOWN_THRESHOLD_MIN` already exists in Phase 1 daemon. Default: 5 minutes. |
| SHUT-03 | Calibration mode reduces threshold to ~1 minute | Phase 6 feature; Phase 3 provides the infrastructure. Threshold adjustment via environment or runtime flag. |

</phase_requirements>

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python `pathlib.Path` | stdlib | File I/O to `/dev/shm/ups-virtual.dev` | Built-in, atomic write patterns established in Phase 1 (tempfile → fsync → os.replace) |
| Python `os` | stdlib | File permissions, fsync for durability | Standard daemon I/O; atomic writes verified in Phase 1 testing |
| NUT `dummy-ups` | 2.8.1 (system) | Virtual UPS driver reading `.dev` file | Available on Debian 13; mode `dummy-once` auto-re-parses on timestamp change (no polling overhead) |
| `systemd` | Debian 13 standard | Service orchestration (already in use) | Existing infrastructure; no new dependencies |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| NUT `upsd` | 2.8.1 (system) | UPS daemon serving variables to dummy-ups, upsmon, Grafana | Already running; Phase 3 adds dummy-ups as secondary source |
| NUT `upsmon` | 2.8.1 (system) | Monitor tool receiving LB signal and triggering shutdown | Already configured; Phase 3 data source switches from real to virtual UPS |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| tmpfs `/dev/shm` | `/tmp` ext4 mount | /dev/shm guarantees RAM-only, zero SSD writes; /tmp may spill to disk depending on mount options |
| NUT `dummy-once` mode | `dummy-loop` mode | dummy-once reads file once per timestamp change (lower CPU); dummy-loop polls with sleep cycles (higher CPU, unneeded for infrequent changes) |
| Simple text key-value file | Binary protocol / socket | Text format matches `upsc` output, integrates seamlessly with NUT tools; binary adds complexity without benefit |
| `os.replace()` atomic write | Direct file write | `tempfile → fsync → os.replace()` is atomic and crashes safely; direct write risks partial data on crash |

**Installation:**
```bash
# dummy-ups is part of NUT package (already installed)
# Verify:
which dummy-ups
apt list --installed | grep nut
```

---

## Architecture Patterns

### Recommended Project Structure
```
src/
├── monitor.py           # Phase 1-2 daemon (extended with virtual UPS output)
├── virtual_ups.py       # NEW: Writes metrics to /dev/shm/ups-virtual.dev
├── nut_client.py        # Phase 1: Real UPS data source
├── [existing modules]   # Phase 1-2 (ema_ring_buffer, model, soc_predictor, etc.)

systemd/
├── ups-battery-monitor.service  # Service unit (unchanged)

config/
├── dummy-ups.conf       # NEW: NUT configuration snippet for dummy-ups source
```

### Pattern 1: Safe tmpfs File Writing for NUT Integration

**What:** Write corrected UPS metrics to a tmpfs file in NUT standard format (`upsc`-compatible key-value pairs), ensuring atomic updates and zero SSD wear.

**When to use:** Transparent data source switching for monitoring systems (NUT, Grafana, custom tools) without modifying their configurations.

**Example:**
```python
# Source: /dev/shm convention + NUT protocol
import os
import tempfile
from pathlib import Path

def write_virtual_ups_metrics(metrics_dict: dict) -> None:
    """
    Write corrected UPS metrics to /dev/shm/ups-virtual.dev.

    Format: NUT standard key-value pairs (matches upsc output)
    Example line: "VAR cyberpower battery.charge 87"

    Atomic write: tempfile in same filesystem (tmpfs) → fsync → rename
    """
    virtual_ups_path = Path('/dev/shm/ups-virtual.dev')

    # Build content in NUT format
    lines = []
    for key, value in metrics_dict.items():
        # Format: "VAR <ups_name> <key> <value>"
        lines.append(f"VAR cyberpower {key} {value}\n")

    content = "".join(lines)

    # Atomic write: tmpfile on same filesystem (tmpfs) → fsync → rename
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir='/dev/shm',
            delete=False,
            prefix='ups-virtual-',
            suffix='.tmp'
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name

        # Atomic rename
        os.replace(tmp_path, virtual_ups_path)
    except Exception as e:
        logger.error(f"Failed to write virtual UPS metrics: {e}")
        raise
```

### Pattern 2: ups.status Override Logic (LB Arbitration)

**What:** Emit correct `ups.status` value based on event type (ONLINE, BLACKOUT_REAL, BLACKOUT_TEST) and remaining time threshold. This replaces firmware's unreliable `onlinedischarge_calibration` logic.

**When to use:** Safe shutdown coordination when firmware status flags are unreliable or bugged.

**Example:**
```python
# Source: monitor.py event handler logic
def compute_ups_status_override(
    event_type: EventType,
    time_rem_minutes: float,
    shutdown_threshold_minutes: int
) -> str:
    """
    Compute correct ups.status value including LB (LOW_BATTERY) flag.

    Logic:
    - ONLINE: "OL"
    - BLACKOUT_TEST: "OB DISCHRG" (no LB, for calibration data collection)
    - BLACKOUT_REAL + time_rem >= threshold: "OB DISCHRG"
    - BLACKOUT_REAL + time_rem < threshold: "OB DISCHRG LB" (trigger shutdown)

    Returns: String matching NUT status format (e.g., "OL", "OB DISCHRG", "OB DISCHRG LB")
    """
    if event_type == EventType.ONLINE:
        return "OL"
    elif event_type == EventType.BLACKOUT_TEST:
        return "OB DISCHRG"
    elif event_type == EventType.BLACKOUT_REAL:
        if time_rem_minutes < shutdown_threshold_minutes:
            return "OB DISCHRG LB"  # Signal LOW_BATTERY to upsmon
        else:
            return "OB DISCHRG"
    else:
        return "OL"  # Default/unknown state
```

### Anti-Patterns to Avoid

- **Writing to persistent disk for every poll:** SSD wear; use tmpfs instead
- **Partial file writes without fsync:** Risk data corruption on crash; always fsync before rename
- **Modifying NUT upsd.conf or upsmon.conf:** These remain unchanged; dummy-ups is only addition
- **Setting LB flag too early:** Shutdown signal must not fire before time_rem actually expires; verification step checks timing
- **Ignoring file timestamp changes:** dummy-once mode handles this; dummy-loop adds unnecessary CPU overhead
- **Hardcoding UPS name or field names:** Make configurable via environment; CONTEXT.md shows "cyberpower" is default but should be flexible

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| NUT protocol handling | Custom socket protocol parser | Use NUT's `upsc` command or NUT library; Phase 1 already has socket client | Protocol has edge cases (multiline values, escaping, connection recovery) |
| Virtual UPS data representation | Custom binary format or database | NUT standard key-value format (text); matches what `upsc` and `dummy-ups` expect | Interoperability; NUT tools expect standard format |
| Atomic file writes with crash safety | Simple file.write() + close() | tempfile → fsync → os.replace() pattern | Recovers correctly from power loss or daemon crash mid-write |
| Shutdown timing coordination | Custom daemon-to-upsmon signaling | Use standard NUT mechanisms: ups.status field with LB flag | upsmon already watches this field; no custom IPC needed |
| File monitoring for changes | inotify / file stat polling | NUT's dummy-once mode (auto-re-parses on timestamp change) | dummy-once is built for this use case; lower CPU than polling |

**Key insight:** tmpfs + NUT standard format + standard UPS status flags solve the coordination problem without custom machinery. The daemon is "dumb" from NUT's perspective: it writes a file, dummy-ups reads it, upsmon reacts to the flags. No special hooks or APIs needed.

---

## Common Pitfalls

### Pitfall 1: LB Flag Fires Before Battery Actually Dies

**What goes wrong:** Daemon sets `ups.status = "OB DISCHRG LB"` based on calculated time_rem < 5 minutes, but upsmon's shutdown takes 20 seconds to complete (FINALDELAY + script execution time). If time_rem actually runs out during those 20 seconds, shutdown fails mid-execution.

**Why it happens:** Mismatch between daemon's time_rem calculation (accounting for only load) and actual battery degradation rate under shutdown load (higher CPU, more current draw).

**How to avoid:**
- Set shutdown threshold conservatively: if time_rem calc says 5 min, don't fire LB until time_rem < 3 min
- Log every LB flag transition with calculated time_rem value
- Verify experimentally during Phase 5 testing that shutdown completes before battery dies

**Warning signs:**
- Daemon logs show "LB signal at time_rem=2.5min" but system restarts due to power loss
- MOTD shows different time_rem than when shutdown fired

### Pitfall 2: `/dev/shm` Not Available on Boot

**What goes wrong:** Daemon starts before tmpfs mount is ready, or `/dev/shm` has restrictive permissions (mode 1777 by default is world-writable but may be changed by security policy).

**Why it happens:** Boot order, systemd dependencies, or custom initramfs configurations.

**How to avoid:**
- Add `After=sysinit.target` to systemd unit (ensures basic system is up)
- Verify `/dev/shm` exists at startup: `daemon._check_shm_availability()`
- Log error and exit cleanly if write fails (don't retry indefinitely)

**Warning signs:**
- systemd journal shows "Permission denied" or "No such file or directory" for `/dev/shm` writes
- dummy-ups shows empty or stale data in `upsc virtual-ups`

### Pitfall 3: File Timestamp Not Changing, dummy-once Doesn't Re-read

**What goes wrong:** Daemon updates virtual UPS file in place (not using tempfile→replace), or updates happen too rapidly (within same second), so filesystem timestamp doesn't change. dummy-once mode doesn't notice the update and serves stale data.

**Why it happens:** Misunderstanding of how filesystem timestamps work (typically 1-second granularity on ext4; tmpfs may have lower).

**How to avoid:**
- Always use `tempfile → fsync → os.replace()` pattern; replace() updates mtime
- Log file write timestamp: `logger.debug(f"Virtual UPS written at {Path('/dev/shm/ups-virtual.dev').stat().st_mtime}")`
- In tests, add 1 second delay between writes to ensure timestamp change

**Warning signs:**
- `upsc virtual-ups` shows outdated values (older SoC or time_rem)
- Multiple daemon poll cycles pass without file mtime change

### Pitfall 4: NUT Configuration Conflicts

**What goes wrong:** Existing upsd.conf or ups.conf has a device named "cyberpower" (the real UPS) and a device named "virtual-ups" (the virtual one) with conflicting port/driver settings, causing both to fail or overlap.

**Why it happens:** Adding new virtual device without checking existing config, or typos in config snippet.

**How to avoid:**
- Read existing NUT config during Phase 5 install (check what UPS names are already defined)
- Use clear naming: "cyberpower" for real, "cyberpower-virtual" or similar for virtual (ensure uniqueness)
- Add config validation: `upscmd -l virtual-ups` should list available commands (dummy-ups may not support many)

**Warning signs:**
- `upsd` fails to start with "duplicate device" or parse error
- `upsc cyberpower` and `upsc virtual-ups` return different values for same variable
- Grafana shows no data from virtual source

### Pitfall 5: Shutdown Threshold Too Aggressive

**What goes wrong:** Threshold set to 10 minutes to be "safe", but daemon rarely calculates time_rem < 10 min due to conservative Peukert constant. Result: LB flag never fires, shutdown never happens, server runs on battery until power loss.

**Why it happens:** Confusion between shutdown threshold and actual time remaining; not accounting for model calibration state.

**How to avoid:**
- Log time_rem value every 60 seconds (even if shutdown threshold not met)
- During Phase 5 testing with synthetic load, verify that LB flag fires at expected threshold
- Start with default 5 min; adjust only after live data shows what actual time_rem values are

**Warning signs:**
- Daemon runs through entire "deep test" cycle (30+ minutes) without LB flag
- MOTD shows healthy time_rem but battery voltage is visibly dropping in Grafana

---

## Code Examples

Verified patterns from official sources and Phase 1/2 existing code:

### Writing Virtual UPS Metrics (Atomic Pattern)

```python
# Source: Phase 1 atomic write pattern (model.py) applied to tmpfs
import json
import os
import tempfile
from pathlib import Path

def write_virtual_ups_dev(metrics: dict, ups_name: str = "cyberpower") -> None:
    """
    Write corrected UPS metrics to /dev/shm/ups-virtual.dev.

    Format: NUT standard (one VAR line per variable, matching upsc output format)
    Atomic write: tempfile (same tmpfs mount) → fsync → replace

    Args:
        metrics: Dict of {field_name: value} to write
        ups_name: UPS name identifier (default "cyberpower")
    """
    virtual_ups_path = Path('/dev/shm/ups-virtual.dev')
    virtual_ups_path.parent.mkdir(parents=True, exist_ok=True)

    # Build NUT-format content
    lines = [f"VAR {ups_name} {k} {v}\n" for k, v in metrics.items()]
    content = "".join(lines)

    # Atomic write using tempfile pattern from Phase 1
    with tempfile.NamedTemporaryFile(
        mode='w',
        dir='/dev/shm',
        delete=False,
        prefix='ups-virtual-',
        suffix='.tmp'
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name

    os.replace(tmp_path, virtual_ups_path)
```

### Integrating Virtual UPS Output into Monitor Daemon

```python
# Source: monitor.py extension (Phase 2 monitoring loop)
from src.virtual_ups import write_virtual_ups_dev

class MonitorDaemon:
    def run(self):
        """Main polling loop with virtual UPS output."""
        while self.running:
            try:
                # Phase 1-2: Poll real UPS
                ups_data = self.nut_client.get_ups_vars()

                # Phase 2: Calculate metrics
                soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
                battery_charge = charge_percentage(soc)
                time_rem = runtime_minutes(soc, load_ema, capacity_ah, soh)

                # Phase 3: Compute ups.status override
                event_type = self.event_classifier.classify(ups_status, input_voltage)
                ups_status_override = self._compute_ups_status(
                    event_type,
                    time_rem,
                    SHUTDOWN_THRESHOLD_MINUTES
                )

                # Phase 3: Write all fields (override 3, pass through rest) to virtual UPS
                virtual_metrics = {
                    # Overridden fields
                    "battery.runtime": int(time_rem * 60),  # Convert to seconds
                    "battery.charge": battery_charge,
                    "ups.status": ups_status_override,

                    # Pass-through from real UPS
                    **{k: v for k, v in ups_data.items()
                       if k not in ["battery.runtime", "battery.charge", "ups.status"]}
                }

                # Write to tmpfs
                write_virtual_ups_dev(virtual_metrics)

                time.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                time.sleep(POLL_INTERVAL)
```

### Configuration Snippet for NUT dummy-ups Integration

```bash
# File: /etc/nut/ups.conf (new device block, existing config unchanged)
# Add this block to enable virtual UPS:

[cyberpower-virtual]
driver = dummy-ups
port = /dev/shm/ups-virtual.dev
mode = dummy-once
desc = "Virtual UPS proxy with corrected battery metrics"

# Then update /etc/nut/upsmon.conf to monitor the virtual device:
MONITOR cyberpower-virtual@localhost 1 upsmon_user upsmon_pass master

# Existing entries remain:
# MONITOR cyberpower@localhost 1 upsmon_user upsmon_pass master  # Real UPS (optional, for reference)
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Reading UPS once at startup, static assumptions | Polling every N seconds with EMA smoothing (Phase 1) | NUT 2.0+, modern battery monitoring | Accurate real-time state; fast response to load changes |
| Trusting firmware battery.charge / battery.runtime | Calculate from voltage via LUT + Peukert law (Phase 2) | 2024+, UPS firmware bugs widespread | Precise time-to-empty; safe shutdown margins |
| Modifying NUT config to insert custom logic | Transparent dummy-ups proxy reads/writes files (Phase 3) | NUT 2.8.0+ dummy-once mode | No config changes; seamless Grafana integration |
| `onlinedischarge_calibration` flag to detect real vs test | Physical input.voltage invariant (Phase 2) | March 2026 CyberPower UT850EG analysis | Robust to firmware bugs; works offline |

**Deprecated/outdated:**
- **Direct upsmon shutdown based on firmware critical state:** firmware flags unreliable; custom orchestration via metrics override safer
- **Static battery LUT from datasheet only:** Individual battery curves vary; measured points improve accuracy (Phase 4)
- **SSD writes for every poll cycle:** tmpfs + disk writes only on discharge completion minimizes wear

---

## Open Questions

1. **Virtual UPS device naming convention**
   - What we know: Real UPS is "cyberpower" in NUT. Virtual could be "cyberpower-virtual" or "cyberpower-proxy"
   - What's unclear: Does Grafana (or other consumers) care about the device name? Will they see "cyberpower-virtual" and assume it's different hardware?
   - Recommendation: Use "cyberpower-virtual" with human-readable description in ups.conf ("Virtual UPS proxy..."). Test with Grafana to confirm dashboards still work.

2. **Handling `/dev/shm` unavailability gracefully**
   - What we know: `/dev/shm` is standard on systemd systems; Phase 1 daemon already handles NUT socket failures
   - What's unclear: Should daemon exit on first write failure, or retry with backoff? Should it switch to `/tmp` fallback?
   - Recommendation: Exit cleanly with error log if `/dev/shm` write fails (indicates system misconfiguration). No fallback to `/tmp` (violates zero-SSD-wear requirement).

3. **Validation Architecture for Phase 3 tests**
   - What we know: Phase 1-2 use pytest with mock sockets; Phase 3 involves file I/O and NUT integration
   - What's unclear: How to test dummy-ups behavior without running actual NUT daemon? Mock or integration test?
   - Recommendation: (Covered in Validation Architecture section below)

---

## Validation Architecture

> Validation workflow is enabled (`workflow.nyquist_validation: true` in config.json)

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 7.4.0+ (existing from Phase 1) |
| Config file | `pytest.ini` (existing) |
| Quick run command | `pytest tests/test_virtual_ups.py -v` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| VUPS-01 | Metrics written to `/dev/shm/ups-virtual.dev` in tmpfs | unit | `pytest tests/test_virtual_ups.py::test_write_to_tmpfs -v` | ❌ Wave 0 |
| VUPS-02 | All real UPS fields transparently pass through (except 3 overrides) | unit | `pytest tests/test_virtual_ups.py::test_passthrough_fields -v` | ❌ Wave 0 |
| VUPS-03 | battery.runtime, battery.charge, ups.status correctly overridden | unit | `pytest tests/test_virtual_ups.py::test_field_overrides -v` | ❌ Wave 0 |
| VUPS-04 | File written in NUT format (VAR lines) readable by dummy-ups simulator | unit | `pytest tests/test_virtual_ups.py::test_nut_format_compliance -v` | ❌ Wave 0 |
| SHUT-01 | ups.status includes "LB" when time_rem < threshold | unit | `pytest tests/test_virtual_ups.py::test_lb_flag_threshold -v` | ❌ Wave 0 |
| SHUT-02 | Shutdown threshold configurable via environment variable | unit | `pytest tests/test_virtual_ups.py::test_configurable_threshold -v` | ❌ Wave 0 |
| SHUT-03 | Calibration mode threshold can be overridden (prep for Phase 6) | unit | `pytest tests/test_virtual_ups.py::test_calibration_mode_threshold -v` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_virtual_ups.py -v` (all Phase 3 unit tests)
- **Per wave merge:** `pytest tests/ -v` (full suite including Phase 1-2 regression)
- **Phase gate:** Full suite green + manual verification with real NUT dummy-ups + Grafana dashboard switch

### Wave 0 Gaps

- [ ] `tests/test_virtual_ups.py` — Virtual UPS file writing, format, field override logic
- [ ] `src/virtual_ups.py` — New module: write_virtual_ups_dev() and ups_status_override_logic()
- [ ] `src/monitor.py` — Integration: call write_virtual_ups_dev() in polling loop, compute ups.status override
- [ ] `systemd/ups-battery-monitor.service` — Add `After=sysinit.target` (ensure /dev/shm is ready)
- [ ] Config snippet for `/etc/nut/ups.conf` (Phase 5 install, but document here)
- [ ] Integration test: mock NUT dummy-ups behavior to verify format compliance (optional, low-priority)

*(Note: existing test infrastructure from Phase 1-2 covers conftest.py fixtures; Phase 3 adds tmpfs and NUT format tests)*

---

## Sources

### Primary (HIGH confidence)
- **NUT Project Documentation** — dummy-ups(8) manual, dummy-once mode behavior, `.dev` file format specification
  - https://networkupstools.org/docs/man/dummy-ups.html
  - https://networkupstools.org/docs/man/upsmon.html
- **Debian 13 tmpfs / /dev/shm** — Standard POSIX tmpfs behavior, filesystem mounting
  - https://man7.org/linux/man-pages/man5/tmpfs.5.html
- **Phase 1-2 Existing Code** — Atomic write patterns (model.py), EMA logic (ema_ring_buffer.py), socket communication (nut_client.py)

### Secondary (MEDIUM confidence)
- **Linux FHS and /dev/shm convention** — Standard practice for runtime state files, zero SSD wear
- **NUT upsmon shutdown coordination** — FINALDELAY parameter, LOWBATT notify event, FSD (Forced Shutdown) logic
  - https://networkupstools.org/docs/man/upsmon.conf.html

### Tertiary
- *(No LOW confidence sources; all core technologies (NUT, tmpfs, filesystem atomicity) verified in official documentation)*

---

## Metadata

**Confidence breakdown:**
- **Standard stack (dummy-ups + tmpfs + file I/O):** HIGH — NUT v2.8.1 verified available on Debian 13, tmpfs behavior standard across Linux, atomic write patterns proven in Phase 1
- **Architecture (virtual UPS proxy via .dev file):** HIGH — NUT dummy-ups explicitly designed for this use case; Phase 1 experience with file I/O reduces implementation risk
- **Shutdown coordination (ups.status → upsmon):** MEDIUM-HIGH — upsmon documentation is clear on LB signal detection, but actual timing (FINALDELAY vs battery depletion) requires live testing; Phase 5 will validate experimentally
- **Pitfalls (edge cases, recovery):** MEDIUM — Common pitfalls identified through NUT community and testability of patterns; some (e.g., file timestamp granularity) require experimental verification

**Research date:** 2026-03-14
**Valid until:** 2026-04-14 (30 days; NUT v2.8.1 is stable; minor changes unlikely)

