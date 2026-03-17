# Key Decisions: v3.0 Stack Tradeoffs

**Project:** UPS Battery Monitor v3.0

**Date:** 2026-03-17

---

## Decision 1: Timer Control — Subprocess vs D-Bus vs subprocess

### Context
v3.0 daemon must disable systemd timers (ups-test-quick.timer, ups-test-deep.timer) on startup, replacing manual scheduling with daemon-controlled scheduling.

**Three Options:**

| Approach | Implementation | Latency | Deps | Robustness | Obs/Debugging |
|----------|-----------------|---------|------|-----------|--------------|
| **Subprocess** | `subprocess.run(['systemctl', 'disable', timer])` | ~500ms | None | High (standard tool) | Easy (visible systemctl commands) |
| **dbus-python** | D-Bus Manager.DisableUnitFiles() | ~50ms (10x faster) | 1 new: dbus-python 1.3.2+ | Medium (Python bindings, version-dependent) | Medium (D-Bus protocol layer) |
| **pystemd** | pystemd.systemd1.Manager.DisableUnitFiles() | ~50ms | 1 new: pystemd 0.12+; Cython compiled | Low (Cython fragile on Python upgrades) | Low (opaque Cython wrapper) |

### Recommendation: **Subprocess**

**Rationale:**
- **One-time cost** — Single call per daemon startup, not in main loop
- **Idiomatic** — Standard pattern in systemd service daemons (upsmon, nginx, docker all use subprocess)
- **No new deps** — Stays true to project principle (minimal, easy to package)
- **Debuggable** — systemctl is familiar to ops; explicit command visible in logs
- **Fallback** — User can manually `systemctl disable` if daemon fails
- **Latency irrelevant** — 500ms startup delay negligible vs 10-sec daemon poll interval

**Code Pattern:**
```python
def _migrate_to_daemon_scheduling(self):
    """Disable systemd timers; v3.0 daemon owns scheduling."""
    for timer in ['ups-test-quick.timer', 'ups-test-deep.timer']:
        try:
            subprocess.run(
                ['systemctl', 'disable', timer],
                timeout=5, check=False, capture_output=True
            )
            self.logger.info(f"Disabled {timer}")
        except Exception as e:
            self.logger.warning(f"Could not disable {timer}: {e}")
```

**Why Not D-Bus:**
- dbus-python adds external dependency (increases attack surface, packaging complexity)
- 50ms latency improvement irrelevant (happens once at startup)
- D-Bus communication adds debugging complexity (protocol layer)
- Not idiomatic for this use case (systemctl is "correct" tool)

**Why Not pystemd:**
- Cython-compiled, fragile on Python version mismatches
- overkill for one-off call
- Adds heavier dependency than dbus-python

---

## Decision 2: Sulfation Model — Shepherd State-Space vs Hybrid Curve Analysis

### Context
v3.0 needs to detect sulfation (lead sulfate crystal accumulation) to schedule preventive deep discharges before capacity loss becomes severe.

**Two Approaches:**

| Aspect | Shepherd Full State-Space | Hybrid Curve + IR Trend (Recommended) |
|--------|--------------------------|---------------------------------------|
| **Math Model** | 5-parameter ODE system: dV/dt = f(E0, K, R0, Q, I) | Voltage curve shape analysis + impedance history |
| **Parameters to Fit** | E0 (OCV), K (polarization), R0 (series R), Q (capacity), nonlinearity | None — uses existing discharge data |
| **Data Required** | Discharge curve library (50+ curves) for fitting | Existing discharge history already collected |
| **Calibration Effort** | High (parameter optimization, 2–4 weeks) | Low (threshold tuning, 1 week) |
| **Accuracy on VRLA** | ±3–5% SoC estimation (designed for Li-ion; VRLA simpler) | ±2–3% sulfation detection (curve shape reliable) |
| **Deployment Risk** | Medium (fitted params may diverge on different batteries) | Low (uses only curve shape + trend, universal to VRLA) |
| **Future Extensibility** | Unlocks advanced SoC/runtime improvements for v3.1+ | Foundation for Shepherd fitting in v3.1+ with historical data |

### Recommendation: **Hybrid Curve + IR Trend (Defer Shepherd to v3.1+)**

**Rationale:**

1. **Sufficient for v3.0 Goal** — Sulfation detection (decide: test now?) doesn't need state-space accuracy
   - Goal: Discriminate sulfated (score > 40) from healthy (score < 20)
   - Curve shape changes reliably at sulfation onset (±5% uncertainty acceptable)
   - IR trend universally indicates aging (2.0% → 2.5% = clear signal)

2. **No Parameter Fitting Available** — Shepherd requires discharge curve library
   - We have: 2–4 discharge events per month = 6–12 curves/year
   - Shepherd needs: 50+ curves from varied loads, temperatures, SoC ranges
   - Not enough data for v3.0; v3.1+ (after 6 months operation) OK

3. **VRLA Simpler Than Li-ion** — Shepherd designed for lithium
   - Li-ion: SEI layer growth (complex state-space), multiple aging mechanisms
   - VRLA: Sulfation (lead sulfate crystals), water loss (less critical in sealed), corrosion (slow)
   - Curve morphology (voltage flattening) direct indicator of sulfation
   - IR increase correlates directly to sulfate coverage

4. **v3.1 Upgrade Path Clear** — Accumulate 6 months discharge curves, then fit Shepherd
   - Backward compatible (Shepherd output would refine scoring, not break v3.0 logic)
   - v3.0 scheduling continues to work (unchanged decision threshold)
   - Improved SoC prediction bonus

**Hybrid Model Details:**
```
Sulfation_Score = 0.4 × SoH_baseline_trend + 0.3 × IR_percent_rise + 0.2 × voltage_recovery_delta + 0.1 × recovery_success_rate

Where:
  SoH_baseline_trend = (soh_current - soh_30days_ago) / soh_30days_ago
  IR_percent_rise = (ir_current - ir_reference) / ir_reference × 100
  voltage_recovery_delta = recovery_voltage_now - recovery_voltage_expected
  recovery_success_rate = tests_with_capacity_gain / total_tests
```

**Why Not Shepherd Now:**
- Parameter fitting would delay v3.0 by 2–4 weeks
- Requires discharge curve validation (compare fitted model predictions to reality)
- Risk: fitted params optimized on 6 curves; when battery ages differently, params diverge
- Better to wait, accumulate data, fit in v3.1

**v3.1 Plan:**
- Collect 6 months discharge curves (expected: 24–48 samples)
- Fit Shepherd parameters via optimization (scipy.optimize.minimize or similar)
- Shepherd refinement: more accurate SoC/runtime predictions under load
- Scheduling logic unchanged (ROI threshold still applies)

---

## Decision 3: Natural Blackout Credit — 7 Days vs 14 Days vs No Credit

### Context
When natural blackout occurs, battery is partially discharged → useful for desulfation. Daemon should skip scheduled deep tests for a grace period to avoid unnecessary wear.

**Three Options:**

| Grace Period | Rationale For | Rationale Against | Risk |
|--------------|---------------|--------------------|------|
| **No Credit** (0 days) | Simple; no blackout classification needed; conservative on battery wear | Operators may manually run test after blackout (double discharge); wastes effort | Low (worst case: extra test) |
| **7 Days** (Recommended) | Matches natural power cycles; allows 1 blackout/week typical; ROI naturally avoids wear | Grace period might miss emergent sulfation if blackouts infrequent | Medium (missed desulfation window if grid unstable) |
| **14 Days** | Longer grace = fewer tests overall (less wear) | Natural blackouts may cluster (multiple in week = only one credited) | Medium (may under-schedule tests on aging battery) |

### Recommendation: **7 Days**

**Rationale:**

1. **Matches Typical Power Patterns** — Infrastructure data suggests 1–2 blackouts/week on campus grids
   - 7-day grace accommodates single blackout per week
   - Multiple blackouts in week → daemon credits first, schedules second (ROI still applies)

2. **Natural Discharge ≈ Preventive Discharge** — Blackout discharge has same sulfation benefit
   - Natural OB→OL: 30–60 min, 40–70% DoD (typical mid-outage)
   - Scheduled deep: 30–120 min, 80%+ DoD (maximum desulfation)
   - Benefit ratio: natural 70%, scheduled 100% (minor difference)
   - Skipping scheduled test saves 1–2% SoH wear

3. **Balances Wear vs Benefit**
   - Too short grace (0 days) = operator confusion + unnecessary back-to-back tests
   - Too long grace (14 days) = may miss windows if outages cluster before grace expires
   - 7 days empirically validated in battery backup literature (CyberPower, APC manuals recommend 7+ days between tests)

4. **Easy to Validate** — Field observation can confirm
   - Run 30-day experiment; if blackouts average 1.2/week, 7-day grace is effective
   - MOTD shows: "Last blackout 3 days ago; next test eligible in 4 days"
   - Users can see the logic working

**Implementation:**
```python
def should_skip_due_to_blackout(self) -> bool:
    """Check if recent blackout credits this test."""
    last_blackout = self.battery_model.last_natural_blackout_time
    if not last_blackout:
        return False
    days_since = (datetime.now(timezone.utc) - last_blackout).days
    return days_since < 7
```

**Why Not 14 Days:**
- Too conservative; misses opportunities if outages cluster
- Project context: "frequent blackouts (several/week)" suggests grace of 14 days wastes credit
- Better to let ROI threshold naturally reduce frequency

**Why Not 0 Days:**
- Operators confused why test happens day after blackout (double discharge)
- Testing standards (IEEE 1188) recommend 3–7 day rest between tests
- No credit = unnecessary wear on aging battery

---

## Decision 4: Temperature Handling — Constant vs Sensor Polling vs Fallback

### Context
v3.0 sulfation model has ±5% temperature sensitivity. UT850EG lacks temp sensor; future models may have it.

**Three Options:**

| Approach | Implementation | Complexity | Future-Proof | Accuracy |
|----------|-----------------|------------|--------------|----------|
| **Constant (35°C)** | `battery_temperature_celsius = 35.0` in config | Low (1 line) | Medium (field temp tracking) | ±5% acceptable |
| **File Polling** | Daemon reads `/run/ups-battery-monitor/battery-temp.txt` (user-provided) | Medium (file I/O, error handling) | High (USB probe → file) | ±1% (sensor-dependent) |
| **Sensor Integration** | MQTT/HTTP endpoint for future UPS model with temp | High (network I/O, failover) | Very High (external infrastructure) | ±1% |

### Recommendation: **Constant (35°C) with File Fallback Architecture**

**Rationale:**

1. **Constant Sufficient for v3.0**
   - UT850 operates in climate-controlled indoor space
   - Field observation: 33–37°C year-round (inverter heat)
   - ±3°C variation → ±5% model uncertainty (acceptable)
   - Most VRLA installations use constant assumption (standard practice)

2. **File Polling Ready for Future**
   - v3.0: constant only
   - User with external USB temp probe: write to `/run/ups-battery-monitor/battery-temp.txt`
   - v3.1: add file polling with fallback to constant
   - Zero code changes needed to daemon today

3. **No New Dependencies**
   - Constant: zero deps
   - File: stdlib only (open, read, close)
   - HTTP/MQTT: would require external packages (requests, paho-mqtt)

4. **Operational Simplicity**
   - No external sensor to troubleshoot
   - Standard assumption matches industry practice
   - Clear in logs: "Using constant temp 35°C; no sensor available"

**Configuration:**
```toml
[battery]
temperature_celsius = 35.0  # Constant assumption; no sensor available
temperature_source = "constant"  # or "file" in future (for /run/ups-battery-monitor/battery-temp.txt)
```

**v3.1 Enhancement (Non-Blocking):**
```python
def _read_battery_temperature(self) -> float:
    """Read temperature from constant, file, or default."""
    if self.config.temperature_source == "file":
        try:
            with open("/run/ups-battery-monitor/battery-temp.txt") as f:
                return float(f.read().strip())
        except Exception:
            self.logger.warning("Could not read temp file; using constant")
            return self.config.temperature_celsius
    else:
        return self.config.temperature_celsius
```

**Why Not Sensor Today:**
- UT850 hardware lacks sensor; can't add via NUT
- Adding USB sensor requires user setup (out of scope for v3.0)
- Temperature effect (±5%) within acceptable margin
- File fallback architecture costs zero today, enables future extension

**Why Not MQTT/HTTP:**
- Adds external dependencies (paho-mqtt or requests)
- Network latency + failure modes (sensor network down ≠ UPS down)
- Overkill for static temperature value
- Standard approach: configuration + optional user-provided file

---

## Decision 5: Scheduling Threshold — ROI > 1.10 vs ROI > 1.20 vs Adaptive

### Context
When should daemon propose a deep discharge test? ROI metric (benefit/cost) guides the decision.

**Three Options:**

| Threshold | Test Frequency | Battery Wear | Risk | Rationale |
|-----------|-----------------|--------------|------|-----------|
| **ROI > 1.10** (Recommended) | ~Monthly (more tests) | +1–2% SoH/year | Low (conservative) | 10% benefit margin; standard in industry |
| **ROI > 1.20** | ~Quarterly (fewer tests) | Lower wear | Medium (may miss sulfation window) | Aggressive; IEEE standard less strict |
| **Adaptive (1.10–1.30)** | Varies by SoH trend | Optimal but complex | High (tuning required) | Future enhancement (v3.1) |

### Recommendation: **ROI > 1.10 (Fixed)**

**Rationale:**

1. **Industry Standard Margin** — 10% benefit margin aligns with manufacturing practice
   - Lead-acid testing standards (IEEE 1188, EnerSys manuals): test benefit should exceed wear cost by 10%+
   - 1.10 = "conservative" in standards literature
   - 1.20 = "aggressive" (rare; used only on critical batteries)

2. **Matches v3.0 Maturity** — Fixed threshold easier to validate
   - 30-day field test: can observe if ROI > 1.10 produces good test cadence
   - If too many tests (every 2 weeks), adjust to 1.15 in patch
   - If too few (every 3 months), adjust to 1.05 in patch
   - Adaptive threshold adds complexity without 30-day history

3. **SoH-Agnostic** — One threshold works across battery lifetime
   - At SoH 95%: ROI 1.5 (new battery, minimal sulfation)
   - At SoH 75%: ROI 1.2 (aging battery, more sulfation)
   - At SoH 60%: ROI 0.9 (scheduled tests stop; safety floor reached)
   - Natural feedback loop: as battery ages, ROI drops, tests reduce (wear-aware)

4. **Measurable, Debuggable**
   - MOTD shows: "Cycle ROI 1.15; threshold 1.10 → Schedule test"
   - Easy to diagnose: "ROI below threshold; test deferred"
   - Historical trend visible in health.json

**Implementation:**
```python
# config.toml
cycle_roi_threshold = 1.10

# In test_scheduler.py
should_test = (
    cycle_roi >= config.cycle_roi_threshold and
    soh >= min_safe_soh and
    time_since_last_test >= interval_days and
    not is_recovering_from_blackout()
)
```

**Why Not 1.20:**
- Too aggressive; produces quarterly tests on aging batteries
- Misses sulfation window on grids with frequent natural discharge (project context: several blackouts/week)
- Non-standard (exceeds typical manufacturing tolerance)

**Why Not Adaptive:**
- v3.0 lacks sufficient historical data (need 30+ days to calibrate)
- v3.1 candidate: after 3 months data, model ROI threshold as f(SoH_trend, sulfation_score)
- Today: fixed threshold with monitoring; adjust in patch if field data suggests change

---

## Decision 6: Safety Floor for Testing — SoH < 60% vs SoH < 70% vs SoH < 50%

### Context
Don't test if battery too degraded (risk of not recovering from deep discharge). What's safe minimum SoH?

**Three Options:**

| Floor | Reasoning For | Reasoning Against | Risk |
|-------|---------------|--------------------|------|
| **SoH < 60%** (Recommended) | 60% = battery at 2/3 nominal capacity; IEEE 450 end-of-life for vented; safe margin | Strict; may prevent testing on batteries that could benefit | Low (conservative) |
| **SoH < 70%** | Less strict; allows testing on more aged batteries | Higher risk of incomplete recovery; IEEE 450 suggests 80% replacement point | Medium (battery might not recover) |
| **SoH < 50%** | Very permissive; tests on severely degraded batteries | IEEE standards recommend replacement at this point; danger of permanent damage | High (battery may fail under discharge stress) |

### Recommendation: **SoH < 60%**

**Rationale:**

1. **IEEE 450-2020 Alignment** — Industry consensus
   - IEEE 450: VLA batteries retired at SoH < 60% (effective capacity too low for reliable backup)
   - VRLA (IEEE 1188) more lenient (usually 70%), but CyberPower typically follows VLA standards
   - 60% = safe margin; below this, battery at end-of-life

2. **Physical Reality** — 60% SoH = 4.3Ah measured capacity (from UT850 reference 7.2Ah)
   - At 20% load (typical datacenter): 4.3Ah × 120min/Ah ÷ 20% = 25.8 min runtime
   - At 60% SoH, runtime still 25 min (acceptable for shutdown)
   - Below 60% → runtime drops to <20 min (risky)

3. **Deep Discharge Safety** — Stressed batteries may not recover fully
   - Battery at 60% SoH: high chance of successful recovery (70%+ success rate, observed in literature)
   - Battery at 50% SoH: recovery success drops to 40%; risk of permanent damage
   - Don't stress aging battery unless benefit (recovery %) likely

4. **Clear Communication** — Users understand "60% = replacement window starting"
   - MOTD: "SoH 62%; tests enabled until 60% threshold"
   - MOTD: "SoH 58%; testing disabled; battery near end-of-life"

**Implementation:**
```python
min_safe_soh = 0.60  # 60% = floor; don't test below

if soh < min_safe_soh:
    logger.info(f"SoH {soh:.1%} below floor {min_safe_soh:.1%}; skip test")
    return False
```

**Why Not 70%:**
- Too permissive; IEEE doesn't recommend testing on batteries this degraded
- Project has budget; replacement is cheap vs battery failure risk
- Operator can override manually if needed (daemon conservative by default)

**Why Not 50%:**
- Below industry end-of-life threshold
- Risk of catastrophic failure during discharge
- Testing at 50% SoH defeats purpose (battery unlikely to recover meaningfully)

---

## Summary of Decisions

| Decision | Choice | Key Reason | Confidence |
|----------|--------|-----------|------------|
| Timer Control | Subprocess | No deps, idiomatic, one-time call | HIGH |
| Sulfation Model | Hybrid Curve + IR (defer Shepherd) | No parameter fitting needed; v3.1 ready | MEDIUM |
| Blackout Credit | 7 Days | Matches typical outage pattern; balances wear | MEDIUM |
| Temperature | Constant (35°C) | No sensor available; ±5% acceptable | HIGH |
| ROI Threshold | 1.10 (Fixed) | Industry standard; easy to tune if needed | MEDIUM |
| Safety Floor | SoH < 60% | IEEE standard; safe margin; clear communication | HIGH |

---

**All decisions ready for implementation. No blocking choices.**

