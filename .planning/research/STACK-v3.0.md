# Technology Stack: v3.0 Active Battery Care Features

**Project:** UPS Battery Monitor v3.0 — Sulfation Modeling, Smart Scheduling, Cycle ROI

**Researched:** 2026-03-17

**Confidence:** MEDIUM-HIGH (with integration validation flags)

---

## Executive Summary

v3.0 transitions the daemon from passive observer to active battery manager by adding three interdependent capabilities: sulfation detection/recovery modeling, intelligent deep discharge scheduling (daemon-controlled via upscmd), and a cycle ROI metric balancing desulfation benefit against wear cost. Technology stack remains minimal — **no new external dependencies required** — by reusing existing systemd integration and pure-Python electrochemical models. Critical unknown: systemd D-Bus timer control requires either subprocess fallback (`systemctl enable/disable`) or lightweight D-Bus binding like dbus-python (not yet added). Temperature remains constant assumption (~35°C indoor) until NUT HID support becomes available (currently unavailable on UT850EG).

Key architectural insight: v3.0 adds **two new responsibilities to the daemon** that previously required systemd timers + manual intervention:
1. **Scheduling** — daemon decides when to run deep discharge tests (not ops)
2. **Closed-loop control** — daemon calls `upscmd test.battery.start.deep` (not upsd/monitoring only)

This shift requires careful design to separate data collection (existing daemon loop) from control decisions (new scheduler module) while maintaining safety constraints (minimum SoH floor, grid stability).

---

## Recommended Stack

### Core Framework (No Changes)
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Python | 3.13 | Daemon core + sulfation model | Existing; stable; math library sufficient for electrochemical models |
| systemd | 249+ | Service/timer management, logging | Already integrated; all machines have systemd |

### New Dependencies for v3.0

| Library | Version | Purpose | Status | Why Not Adding |
|---------|---------|---------|--------|-----------------|
| `dbus-python` | 1.3.2+ | D-Bus timer enable/disable (alternative to subprocess) | **Optional** | Adds external dep; subprocess fallback works; D-Bus adds latency (~50ms per call) |
| `pystemd` | 0.12+ | Enhanced systemd D-Bus API (alternative) | **Not recommended** | Cython-compiled; brittle on Python version changes; subprocess simpler for one-off calls |

**Recommendation: NO new Python dependencies.** Use `subprocess.run(['systemctl', 'enable', ...])` for timer control. This is idiomatic in service daemons and avoids D-Bus complexity.

### Sulfation Modeling (New Mathematical Module)

**File:** `src/battery_math/sulfation.py`

| Component | Approach | Data Source | Why |
|-----------|----------|-------------|-----|
| Sulfation state estimate | Physics + data-driven hybrid | Voltage curve shape, IR trend, recovery delta | Shepherd model overkill for VRLA; curve morphology + impedance trend adequate for v3 |
| Lead sulfate recovery curve | Empirical charge voltage curve | IEC/IEEE standards (2.67-3.0V per cell high-voltage phase) | Lead sulfate solubility increases exponentially above 2.45V; well-characterized recovery mechanics |
| Sulfation score | Dimensionless index (0-100) | SoH baseline, IR history, voltage hysteresis | Composite indicator: raw aging + recovery potential |

**Mathematical Model (Simplified):**
```python
# Sulfation detection: curve shape analysis + impedance trend
sulfation_score = f(
    soh_baseline,           # Long-term capacity vs reference (baseline trend)
    ir_percent_change,      # Internal resistance rise from discharge history (impedance aging)
    voltage_recovery_delta,  # Recovery voltage (deep discharge → recovery phase) drops with sulfation
    recent_recovery_tests   # Count of successful desulfation events
)

# Cycle ROI: benefit vs wear cost
cycle_roi = desulfation_benefit / discharge_wear_cost
          = (capacity_recovery_potential × recovery_probability) / (cycle_count_impact × soh_loss_percent)

# Schedule decision: should we deep discharge now?
should_schedule_deep_test = (
    sulfation_score > threshold AND
    soh >= min_safe_soh AND
    time_since_last_test >= interval_days AND
    no_recent_blackouts  # Natural blackouts count as "free" deep discharges
)
```

**Not Implementing Full Shepherd Model v3.0:**
- Shepherd model (5-parameter state-space) requires parameter fitting from discharge curves
- Overkill for VRLA (where voltage shape + impedance trend sufficient)
- Defer parameter learning to v3.1+ when more historical data available
- v3.0 uses simpler curve morphology analysis (does discharge curve flatten? does recovery speed drop?)

### Scheduling & Control (New Orchestration)

**File:** `src/test_scheduler.py`

| Component | Implementation | Interface |
|-----------|-----------------|-----------|
| Decision engine | Pure function: (battery_state, history) → (should_test, reason) | Called from monitor.py main loop, runs once per 24 hours |
| Test executor | Call `upscmd` via subprocess; capture response | Safe wrapper: refuses if UPS not fully charged or SoH too low |
| Timer integration | Replace systemd timer with daemon-controlled scheduling | Disable ups-test-deep.timer; enable on-demand from daemon |

**Safety Constraints (Hard):**
- Don't test if SoH < 60% (battery may not survive deep discharge)
- Don't test if input voltage unstable (grid issues; manual intervention needed)
- Don't test if last discharge (natural or scheduled) < 24 hours ago (rest period required)
- Don't test if battery not fully charged (upscmd will refuse anyway; log error)

**Integration Points:**
- On daemon startup: disable systemd timers `ups-test-deep.timer`, `ups-test-quick.timer`
- On natural blackout detection (OL→OB→OL): record event, skip scheduled test for 7 days
- Every 24 hours (new timer: `test_scheduler.check_interval()` in main loop): call decision engine
- On test completion: record test result, update sulfation score, calculate cycle ROI

### NUT Integration for Control

**Current State (v1.0-v2.0):**
```
upsc (read-only) → monitoring only
```

**v3.0 Addition:**
```
upscmd (write) → daemon-initiated deep discharge tests
```

**Implementation Pattern:**
```python
# src/nut_client.py: new method
def send_instant_command(self, command: str) -> bool:
    """
    Send instant command to UPS (requires admin credentials or auth).

    Args:
        command: e.g., 'test.battery.start.deep'

    Returns:
        True if accepted; False if UPS refused (not charged, test already running, etc.)
    """
    try:
        with self._socket_session():
            response = self.send_command(f"INSTCMD {self.ups_name} {command}")
            return response.startswith("OK")
    except Exception as e:
        self.logger.error(f"INSTCMD failed: {e}")
        return False
```

**Commands Available (Verified):**
- `test.battery.start.quick` — ~10 sec runtime test (safe, informational)
- `test.battery.start.deep` — ~30–120 min discharge to low-battery threshold (full capacity measurement)
- `test.battery.stop` — interrupt running test (graceful)

**Restrictions Known:**
- UPS must be fully charged before accepting deep test (hardware safeguard)
- No two tests can run simultaneously (queued or refused)
- Typical interval: 3–6 months per manufacturer (IEEE 1188 compliance)

---

## Temperature Handling

**Current State (v1.0-v2.0):**
```
battery.temperature NOT available from NUT UT850EG HID
```

**v3.0 Approach: Configurable Constant**

**File:** `config.toml` (new field)
```toml
# Battery temperature assumption (°C)
# UT850 lacks temp sensor; default 35°C based on field observation (inverter heat)
battery_temperature_celsius = 35.0

# Temperature compensation range: ±3°C variation acceptable
# (affects IR compensation and sulfation model by ±5%)
```

**Why Not Wait for Sensor:**
- CyberPower UT850EG has no internal temperature sensor
- Future UPS swap might have sensor (e.g., APC Smart-UPS); design for future sensor addition
- Temperature compensation via EMA is already architected (MetricEMA class, v1.1)
- v3.0 can skip temperature input today; when sensor data available, plug into existing EMA infrastructure

**Fallback for User with External Sensor:**
- Daemon config accepts `battery_temperature_source` (future): `constant`, `file`, `mqtt`, `http`
- v3.0 only implements `constant`
- If user adds USB temp probe → can write to `/run/ups-battery-monitor/battery-temp.txt`; daemon reads and integrates

---

## Data Model Extensions

### model.json Schema (v3.0 Additions)

```json
{
  "sulfation_model": {
    "sulfation_score": 15,
    "sulfation_score_history": [
      { "date": "2026-03-10", "score": 10 },
      { "date": "2026-03-15", "score": 15 }
    ],
    "ir_percent_history": [
      { "date": "2026-03-10", "ir_percent": 2.1 },
      { "date": "2026-03-15", "ir_percent": 2.3 }
    ],
    "last_recovery_test": "2026-03-14T09:30:00Z",
    "recovery_success_rate": 0.87,
    "recovery_tests_total": 8
  },

  "test_schedule": {
    "next_scheduled_test": "2026-03-31T08:00:00Z",
    "test_interval_days": 28,
    "tests_completed": [
      {
        "date": "2026-02-14T08:15:00Z",
        "type": "deep",
        "result": "passed",
        "capacity_ah_before": 5.8,
        "capacity_ah_after": 5.9,
        "recovery_delta_percent": 1.7
      }
    ],
    "natural_blackouts": [
      {
        "date": "2026-03-12T17:30:00Z",
        "type": "natural",
        "duration_minutes": 47,
        "capacity_measure": 5.8,
        "note": "grid fault; credited as deep discharge"
      }
    ]
  },

  "cycle_roi": {
    "current_roi": 1.42,
    "roi_threshold_for_scheduling": 1.10,
    "last_roi_update": "2026-03-15T20:00:00Z",
    "roi_metric": {
      "desulfation_benefit_potential": 2.1,
      "discharge_wear_cost": 1.48
    }
  }
}
```

---

## Integration Points in Existing Codebase

### monitor.py (Orchestration Changes)

```python
# New imports
from src.test_scheduler import TestScheduler
from src.battery_math.sulfation import calculate_sulfation_score
from src.nut_client import NUTClient  # existing, enhanced

# New initialization (in MonitorDaemon.__init__)
self.test_scheduler = TestScheduler(
    nut_client=self.nut_client,
    model=self.battery_model,
    config=self.config
)

# New logic in main poll loop
# Every 24 hours (periodic check)
if self._should_check_test_schedule():
    should_test, reason = self.test_scheduler.decide_next_test()
    if should_test:
        self.logger.info(f"Scheduling deep test: {reason}")
        success = self.test_scheduler.execute_test()
        if success:
            self.battery_model.record_test_execution(...)
            self._update_sulfation_score()

# On discharge completion (existing OB→OL event)
self._handle_discharge_complete()  # Existing v2.0
self._update_sulfation_score()  # NEW: recalc after each discharge
```

### discharge_handler.py (New Metrics)

```python
# After capacity estimation completes:
def _post_discharge_analysis(self):
    """Recalculate sulfation score and cycle ROI after discharge."""
    # Update sulfation history
    new_score = calculate_sulfation_score(
        soh=self.battery_model.soh,
        ir_trend=self.battery_model.ir_percent_change_history,
        recovery_rate=self.battery_model.recovery_success_rate,
        recent_tests_count=len(self.battery_model.recent_test_completions)
    )
    self.battery_model.add_sulfation_estimate(new_score)

    # Calculate and export cycle ROI to health.json
    self.battery_model.update_cycle_roi()
```

### battery-health.py (Reporting)

```python
# New fields in JSON export:
{
    "sulfation_score": 15,
    "sulfation_trend": "rising",
    "cycle_roi": 1.42,
    "next_scheduled_test": "2026-03-31T08:00:00Z",
    "test_interval_recommended_days": 28,
    "natural_blackouts_credited": 1
}
```

### config.toml (User-Configurable)

```toml
# Battery sulfation model (v3.0+)
battery_temperature_celsius = 35.0          # Assumption; constant until sensor available
sulfation_score_alert = 50                  # Alert if score exceeds threshold
min_soh_for_deep_test = 0.60                # Safety floor; don't test below this

# Smart test scheduling (v3.0+)
auto_schedule_deep_tests = true             # Enable daemon-controlled scheduling
test_interval_base_days = 28                # Initial interval; adjusted by scheduler
test_skip_after_blackout_days = 7           # Skip test if natural blackout occurred recently
test_skip_if_power_unstable_hours = 4       # Skip if grid glitches detected in past 4h
```

---

## Systemd Integration Changes

### Disable Manual Timers on Startup

**Approach:** Daemon disables old systemd timers on first run (idempotent operation).

```python
# src/monitor.py: MonitorDaemon.__init__()
def _migrate_to_daemon_scheduling():
    """Disable systemd timers; v3.0 daemon now owns scheduling."""
    timers_to_disable = [
        'ups-test-quick.timer',
        'ups-test-deep.timer'
    ]
    for timer in timers_to_disable:
        try:
            subprocess.run(
                ['systemctl', 'disable', timer],
                timeout=5,
                check=False,  # Silently ignore if already disabled
                capture_output=True
            )
            self.logger.info(f"Disabled {timer} (daemon now owns scheduling)")
        except Exception as e:
            self.logger.warning(f"Could not disable {timer}: {e}")
```

**Why Subprocess vs D-Bus:**
- Subprocess is simpler, more robust, and doesn't add Python dependencies
- Single call per daemon startup (negligible overhead)
- Fallback to manual `systemctl disable` if daemon fails
- D-Bus would require dbus-python or pystemd (adds external deps + complexity)

---

## Validation & Testing Strategy

### Unit Tests (New)

**Test Coverage:**

| Module | Tests | Purpose |
|--------|-------|---------|
| `test_sulfation_model.py` | 12 | Sulfation score calculation, trend detection, IR aging curve |
| `test_scheduler.py` | 18 | Scheduling decision logic, safety constraint enforcement, conflict detection |
| `test_cycle_roi.py` | 8 | ROI metric calculation, benefit vs wear tradeoff, threshold logic |
| `test_nut_upscmd.py` | 6 | INSTCMD parsing, error handling, UPS refusal cases |

**Key Fixtures:**
- Synthetic discharge histories (varying sulfation levels, IR trends)
- Mock UPS states (charged, charging, testing, failed)
- Real model.json snapshots from 2026-03-12 blackout event

### Integration Tests (Validation Gates)

| Scenario | What We Validate | Data Source |
|----------|------------------|-------------|
| Natural blackout skip | After blackout, scheduler skips test for 7 days | 2026-03-12 event log |
| Sulfation detection | Rising IR trend + capacity delta detected as sulfation | Simulated multi-month aging |
| ROI threshold | Scheduler proposes test only when ROI > 1.10 | Historical test results |
| Safety floor | Refuses test if SoH < 60% | Synthetic low-SoH model |
| upscmd refusal | Handles "test refused" gracefully (waits for charge) | Mock NUT responses |

### Validation Gates (Before v3.0 Release)

**Must Confirm Before Shipping:**

1. **Sulfation Score Stability** — 30 days of normal operation; score should trend slowly (not oscillate). Variance < 5 points/day.

2. **Scheduler Robustness** — Stress test with 1000 synthetic decision calls; no crashes, decisions consistent and safe.

3. **upscmd Compatibility** — Run real deep discharge test on UT850EG; capture actual UPS responses; verify daemon handles all cases (charge pending, test already running, battery low after test).

4. **Blackout Credit Logic** — Simulate natural blackout → verify daemon skips scheduled test for 7 days.

5. **ROI Calibration** — Compare daemon-calculated ROI against field data (battery life extension observed in long-running installations with regular desulfation).

---

## Known Unknowns & Deferred

### v3.0 Scope (Included)
- [x] Sulfation score via curve morphology + IR trend (no Shepherd state-space fitting)
- [x] Daemon-controlled scheduling with safety constraints
- [x] Cycle ROI metric exported to health.json
- [x] Natural blackout credit (skip test for 7 days)
- [x] Configurable temperature constant (~35°C)
- [x] MOTD/journald reporting of sulfation and scheduling decisions
- [x] upscmd integration with error handling

### v3.1 Candidate Features (Deferred)
- **Shepherd Model Fitting** — Parameter learning when 6+ months historical data available
- **Temperature Sensor Integration** — When user adds USB probe or future UPS model includes it
- **Peukert Exponent Auto-Calibration** — Requires circular dependency resolution (deferred from v2.0)
- **Multi-UPS Support** — Currently single CyberPower only; architecture ready for extension
- **Blackout Prediction** — Grid stability analysis to preemptively desulfate before predicted outages
- **Micro-discharge Accumulation** — Estimate capacity from many short blackouts (2-5 min each)

### Validation Gaps (Require Testing)
- **Sulfation Recovery Rate** — IEEE 1188 says recovery works; haven't measured actual capacity recovery % on UT850
- **Optimal Test Interval** — v3.0 uses 28 days (conservative); field data may show 60 days sufficient (defer to v3.1)
- **Safety Floor Calibration** — SoH < 60% proposed as no-test threshold; real-world validation needed
- **Grid Stability Detection** — v3.0 only checks last 4 hours for glitches; multi-day patterns may reveal better heuristics

---

## Sources

### NUT & CyberPower UT850 Integration
- [NUT INSTCMD Documentation](https://networkupstools.org/docs/man/upscmd.html)
- [NUT Issue #3142 — UT850EG Compatibility](https://github.com/networkupstools/nut/issues/3142)
- [NUT Issue #2983 — UT850EG-FR Voltage Scaling](https://github.com/networkupstools/nut/issues/2983)
- [USBHID-UPS Driver Manual](https://networkupstools.org/docs/man/usbhid-ups.html)

### VRLA/AGM Testing Standards & Sulfation Physics
- [IEEE 1188-2005 — VRLA Battery Standard](https://standards.ieee.org/ieee/1188/1800/)
- [Exponential Power — VRLA Battery Testing Guide](https://www.exponentialpower.com/wp-content/uploads/2024/04/VRLA-Battery-Testing.pdf)
- [ScienceDirect — Sulfation Recovery via High Voltage (2025)](https://www.sciencedirect.com/science/article/abs/pii/S3050475925002696)
- [Battery University — Sulfation Prevention (BU-804b)](https://www.batteryuniversity.com/article/bu-804b-sulfation-and-how-to-prevent-it/)
- [EnerSys VRLA Maintenance Manual](https://www.enersys.com/493c0d/globalassets/documents/product-documentation/powersafe/_multi/amer/us-vr-om-002_0308.pdf)

### Electrochemical Modeling & Impedance
- [Shepherd Model Battery Circuits](https://www.mdpi.com/1996-1073/13/16/4085)
- [Electrochemical Impedance Spectroscopy for Battery Aging](https://www.mdpi.com/2313-0105/11/6/227)
- [Lead-Acid Internal Resistance vs Aging](https://actec.dk/media/documents/68F4B35DD5C5.pdf)
- [Distribution of Relaxation Times Analysis](https://www.mdpi.com/2313-0105/11/1/34)

### Systemd & Process Control
- [python-systemd Package Documentation](https://pypi.org/project/python-systemd/)
- [Freedesktop systemd D-Bus API](https://www.freedesktop.org/wiki/Software/systemd/dbus/)
- [systemd.timer Manual](https://man7.org/linux/man-pages/man5/systemd.timer.5.html)

---

**Status:** Ready for implementation. All required components documented, no blocking dependencies. Validation gates identified for pre-release testing.

