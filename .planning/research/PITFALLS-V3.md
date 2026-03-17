# Domain Pitfalls: Active Battery Care (v3.0 — Sulfation & Smart Testing)

**Domain:** UPS/Lead-Acid Battery Active Discharge Management

**Researched:** 2026-03-17

**Milestone Context:** Adding daemon-controlled deep discharge scheduling, sulfation modeling, and cycle ROI metric to v2.0's passive monitoring system.

---

## Executive Summary

Moving from passive monitoring (v2.0) to active battery management (v3.0) introduces **safety-critical pitfalls** that didn't exist before:

1. **Daemon initiates dangerous hardware state change** (deep discharge) → collision with natural blackouts is catastrophic
2. **upscmd reliability unknown** — silent failures, incomplete command execution, state ambiguity
3. **Sulfation model highly sensitive to temperature** — estimation error can be ±20% without measurement
4. **Recovery delta noise** — distinguishing genuine desulfation from measurement noise requires statistical rigor
5. **Race conditions between daemon scheduling and systemd timers** — double-testing, test cancellation mid-way
6. **Deep testing on already-degraded battery** — may not recover, can accelerate failure
7. **Cycle ROI metric is unstable** — sulfation benefit varies 10x based on temperature, SoH, and discharge profile

**Operating context risk:** Headless server, no physical access, frequent blackouts (several/week), battery already under stress. A wrong decision = server doesn't shut down cleanly or battery dies prematurely mid-life.

---

## Critical Pitfalls

### Pitfall 1: Daemon-Initiated Deep Discharge During Natural Blackout

**What goes wrong:**

- Daemon schedules deep discharge test for 3:00 AM (grid is usually stable then)
- At 2:55 AM: daemon sends `upscmd cyberpower test.battery.start.deep`
- Test begins: UPS disconnected from AC for up to 10 minutes of actual discharge
- **At 3:02 AM: actual grid blackout occurs** while daemon-initiated test is ongoing
- UPS is now partially discharged (estimated 30-40% remaining from test prep)
- Real blackout + partial discharge = **critical safety window**: runtime drops from ~47 min to ~20 min
- If blackout lasts 25 minutes, server shuts down uncleanly (corrupts data)

**Why it happens:**

- Designed assuming "scheduled test doesn't overlap with real blackout"
- But blackout timing is unpredictable. Test window may be broad (say, 2 hrs to accumulate statistics)
- Even with narrow window, probability of collision increases with blackout frequency (here: several/week)

**Consequences:**

- Data loss (filesystem corruption due to unclean shutdown)
- User panic ("why did the battery die when it had 50 minutes?")
- Distrust of the system ("daemon made things worse")
- Potential for daemon crash if handler doesn't account for unexpected OB during test

**Prevention:**

1. **Block test scheduling when recent blackout detected:**
   - If `last_blackout_timestamp < now - 2 hours` → skip scheduled test
   - Recent blackout already provided desulfation benefit naturally
   - Log: "Skipped test; recent natural blackout on YYYY-MM-DD HH:MM provides equivalent credit"

2. **Require minimum runtime remaining before test:**
   - Before `upscmd test.battery.start.deep`: check current SoC
   - Refuse test if SoC < 80% (not enough margin for blackout collision)
   - Guard against "daemon scheduled test but capacity estimator running simultaneously"
   - Log: "Blocked deep test; SoC only 72%, risk too high"

3. **Implement test abortion protocol:**
   - If blackout detected **during** test (input.voltage returns to normal for >30 sec): call `upscmd cyberpower test.battery.stop`
   - Some hardware doesn't support stop; in that case, log ERROR and monitor closely
   - Set alert: "Deep test collision detected; monitor SoC closely"

4. **Limit test window to low-risk hours:**
   - Schedule deep tests during known stable hours only (e.g., 6:00–7:00 AM)
   - Track grid stability pattern in model.json: rolling window of "blackouts per hour" per time-of-day
   - Never schedule test within 2 hours of historical peak blackout time
   - Log scheduling decision: "Next deep test scheduled 2026-03-20 06:30 (3/7 stable hours)"

5. **Add pre-test blackout probability check:**
   - Before initiating test, compute: P(blackout in next 10 min) from historical rate
   - If P > 5%, defer test to next stable window
   - Makes scheduling adaptive to grid conditions

**Detection:**

- Monitor for OB events that start during test (NUT status shows DISCHRG + test.result not yet updated)
- Alert if SoC drops >5% during test (indicates actual blackout, not just test)
- Log discrepancy: "Test duration 8 min but SoC fell 45% (normal is 35%); collision likely"

---

### Pitfall 2: upscmd Silent Failures and State Ambiguity

**What goes wrong:**

- Daemon calls: `upscmd -u admin cyberpower test.battery.start.deep`
- Command returns exit code 0 (success)
- Daemon logs: "Deep test initiated"
- But UPS actually rejected test silently because battery wasn't charged to 100%
- Daemon waits 10 minutes for test to complete; nothing happens
- Next poll: `test.result` not updated
- Did test complete? Did it fail? Is it still running? **Unknown state**

**Real-world scenarios:**

1. **Battery charge requirement:** CyberPower refuses deep test if battery < 95% charged
   - May take 12-24 hours to fully charge after previous test
   - `upscmd` doesn't report this as error; just silently refuses

2. **Quick test succeeds but deep test fails:**
   - `test.battery.start.quick` returns OK (5-min test)
   - Immediately after: `test.battery.start.deep` returns OK but does nothing
   - UPS is confused about test state; subsequent `test.battery.start` gets ignored

3. **Timeout during test:**
   - Test is running but taking longer than expected
   - Daemon assumes it's done; calls `test.battery.stop`
   - UPS actually in middle of test; stop command ignored
   - Test continues unexpectedly; SoC drops further than planned

**Why it happens:**

- NUT/CyberPower driver doesn't expose battery charge pre-condition as NUT variable
- `upscmd` protocol returns OK/FAIL on **request acceptance**, not on **command completion**
- No heartbeat or progress variable; daemon must infer state from `test.result`
- `test.result` may be stale or delayed by NUT caching

**Consequences:**

- Daemon thinks it scheduled test; test never actually runs
- Capacity estimator waits for discharge event that never comes
- Replacement predictor doesn't update; system appears "stuck"
- Or test runs when daemon thinks it's done; unexpected SoC drop

**Prevention:**

1. **Verify preconditions before upscmd:**
   ```python
   # Before calling upscmd test.battery.start.deep:
   if battery.charge_percent < 95:
       log.warning(f"Battery only {battery.charge_percent}%, skipping deep test")
       return False

   # Check that no test is currently running
   if last_test_result_time() < now - timedelta(minutes=15):
       log.warning("Previous test still running; skipping")
       return False
   ```

2. **Implement test state machine with polling:**
   - Before test: snapshot current `test.result` and `battery.charge`
   - After `upscmd test.battery.start.deep`: poll every 30 sec for 15 minutes
   - Detect completion by: `test.result` changes, `battery.charge` drops > 5%, `ups.status` shows DISCHRG
   - If no change after 15 min: log ERROR "Test may have failed silently"
   - Do not assume test is complete without confirmation

3. **Implement timeout safeguard:**
   - Deep test should complete in 8-12 minutes (typical for 7.2Ah battery)
   - Set hard timeout: if running > 15 min, try `upscmd test.battery.stop`
   - If stop fails or test continues: escalate to manual intervention
   - Log: "Deep test timeout at 18 min; possible UPS firmware hang"

4. **Add test result validation:**
   ```python
   # After test, verify it actually happened
   estimated_soc_drop = (test_end_charge - test_start_charge) / 100
   expected_soc_drop = 0.35  # 35% typical for 12-min deep discharge

   if abs(estimated_soc_drop - expected_soc_drop) > 0.10:
       log.error(f"Test result suspicious: charge dropped {estimated_soc_drop:.1%}, "
                f"expected ~35%. Test may have failed silently.")
       # Don't mark as valid test for sulfation credit
   ```

5. **Require explicit battery charge confirmation:**
   - Add NUT variable read: wait for battery.charge ≥ 95% with 3× confirmation polls (30 sec apart)
   - If charge doesn't reach 95% after 24 hours, log WARNING and skip test that cycle
   - Don't attempt test on degraded battery that won't fully charge

**Detection:**

- Monitor test result vs charge drop mismatch
- Alert if scheduled test never starts (upscmd OK but charge/status unchanged)
- Flag if test duration abnormal (>20 min for deep, >5 min for quick)
- Correlate with `test.result` changes; if no correlation → likely silent failure

---

### Pitfall 3: Sulfation Model Temperature Sensitivity Without Measurement

**What goes wrong:**

- Daemon implements Shepherd/Bode sulfation model: `sulfation_rate = k × exp(E_a / RT) × (1 - SoC)^n`
- Model parameter `k` (pre-exponential factor) varies **10x** between manufacturers and temperature assumptions
- Daemon hard-codes T = 35°C (estimated from inverter heat)
- Winter: actual temperature 15°C (battery inside room)
- Summer: actual temperature 35°C (measured during blackout)
- Model predicts: **winter SoH drops 2× slower than summer**
- In reality: temperature effect is ±5%, sulfation rate follows actual temperature
- After 6 months, daemon's "sulfation credit" from deep tests is wrong by 30%

**Specific error case:**

```
Winter test (T=15°C, daemon assumes 35°C):
  - Model predicts: sulfation_reversal = 20% capacity recovered
  - Actual at 15°C: reversal ≈ 10% (reaction slower)
  - Daemon over-credits test; skips next test for 2 months
  - Result: battery sulfates unexpectedly; SoH drops 5% in month 3

Summer (T=35°C):
  - Model predicts: sulfation_reversal = 20%
  - Actual at 35°C: reversal ≈ 20% ✓
  - But daemon already over-credited winter test
  - Skips test again; sulfation accumulates
```

**Why it happens:**

- Temperature sensor not available on CyberPower UT850
- No NUT variable exposed: `battery.temperature` or `ups.temperature`
- Hard-coding avoids NaN/missing data, but trades accuracy for simplicity
- Shepherd/Bode parameters are **highly sensitive to T**: doubling T cuts lifetime by 2x

**Consequences:**

- Sulfation credit wrong by 30-50% in cold climates
- Replacement prediction off by months
- User may replace battery prematurely (winter) or experience failure (summer)
- Deep test scheduling becomes anti-optimal: too frequent in winter, too rare in summer

**Prevention:**

1. **Accept temperature uncertainty as fundamental limitation:**
   - Document in model.json: `"temperature_estimated": 35, "temperature_uncertainty": ±10`
   - Never tune sulfation scheduling tighter than ±20% error margin
   - Conservative default: assume worst-case (slowest recovery) temperature
   - Log: "Sulfation model assumes T=35°C; actual ±10°C difference ≈ ±30% error in reversal estimate"

2. **Add optional temperature sensor (future):**
   - DS18B20 thermistor (~$2) mounted on battery case
   - Parse as optional NUT variable or read directly from GPIO
   - If available: re-calibrate sulfation model dynamically
   - If not available: fall back to constant 35°C with documented uncertainty

3. **Implement temperature band fallback:**
   - Maintain separate sulfation schedules for "cold" (<20°C), "nominal" (20-30°C), "hot" (>30°C)
   - Estimate band from time-of-day and season heuristic
   - Adjust deep test interval: cold ×1.5 (slower sulfation), hot ×0.8 (faster)
   - Log: "Seasonal adjustment applied: detected winter pattern, increasing test interval by 50%"

4. **Use empirical recovery measurement instead of formula:**
   - After each deep test: measure actual SoH recovery
   - Compare measured recovery vs model prediction
   - Adjust per-test calibration factor: `calibration_alpha = measured_recovery / predicted_recovery`
   - Over time, empirical model converges to actual device (ignores parameter uncertainty)
   - Log: "Test #5 recovery calibration: model predicted +18%, measured +15%, alpha→0.83"

5. **Never use sulfation model alone for scheduling:**
   - Combine with empirical indicators:
     - IR trend (if rising rapidly, skip test — battery may not recover)
     - SoC recovery speed (if slow, battery is unhealthy; test may damage it)
     - SoH trajectory (if already declining rapidly, deep test risk too high)
   - Sulfation model as tie-breaker, not sole decision

**Detection:**

- Monitor model predictions vs actual SoH changes
- Alert if recovery per test varies >30% across similar conditions
- Flag if winter tests show no recovery; summer tests show excessive recovery
- Compare model coefficient of variation over seasons; if >0.5 → temperature likely cause

---

### Pitfall 4: Recovery Delta Noise — Distinguishing Signal from Measurement Error

**What goes wrong:**

- Deep test completed; daemon measures SoH before and after
- Before test: SoH = 92.1% (from fitted curve to discharge data)
- After test: SoH = 92.4% (+0.3%)
- Is this genuine sulfation reversal (+0.3% capacity recovered)?
- Or is it measurement noise (±0.5% typical from discharge measurement error)?
- **Can't tell.** Statistical confidence is uncertain.

**Practical cascade:**

1. Test 1: ΔSoH = +0.2% (noise, not signal)
2. Test 2: ΔSoH = +0.4% (noise)
3. Daemon accumulates credit: "sulfation_recovered = +0.6%"
4. Over 12 tests at 1.2% recovery each: claimed +14.4% recovery
5. Measured degradation over same period: -8%
6. **Model predicts replacement in 18 months**
7. **Battery actually fails at 10 months** (sulfation credit was illusion)

**Why it happens:**

- Discharge measurement has ±2-3% inherent uncertainty (coulomb counting drift, LUT lookup error, load estimation)
- SoH = measured_capacity / rated_capacity = (5.8 ±0.2 Ah) / 7.2 Ah = 81 ±2.8%
- Single recovery measurement: ΔSoH = (5.8 ± 0.2 - 5.7 ± 0.2) / 7.2 = (0.1 ± 0.28) / 7.2 = **±39% relative error**
- Noise floor is high compared to expected signal (+0.5% per deep test)
- Daemon can't distinguish +0.3% recovery from ±1% noise

**Consequences:**

- Sulfation credit accumulation becomes meaningless
- Replacement prediction unreliable
- May over-test (wasting cycles) or under-test (allowing sulfation)
- System appears to work but is actually flying blind

**Prevention:**

1. **Require minimum sample size before crediting recovery:**
   - Single test: ΔSoH may be noise; don't apply sulfation credit
   - Pool 3+ recent tests (within 2 weeks): if mean recovery > 2× noise floor → credit
   - Set noise floor = 0.5% (±2-3× measurement error)
   - If mean recovery < 0.5%, credit = 0 (treat as noise)
   ```python
   recent_tests = [t for t in history if now - t['timestamp'] < timedelta(days=14)]
   if len(recent_tests) < 3:
       recovery_credit = 0  # Not enough data
       log.debug("Pooling tests for recovery signal; need 3, have {}".format(len(recent_tests)))
   else:
       mean_recovery = mean([t['soh_delta'] for t in recent_tests])
       noise_floor = 0.005  # ±0.5%
       if abs(mean_recovery) > noise_floor:
           recovery_credit = mean_recovery * len(recent_tests)
           log.info(f"Pooled recovery signal: {recovery_credit:.1%} from {len(recent_tests)} tests")
       else:
           recovery_credit = 0
           log.info("Recovery below noise floor; not credited")
   ```

2. **Implement per-test confidence scoring:**
   - High confidence (apply recovery credit):
     - ΔSoC from discharge > 50% (deep discharge)
     - Discharge duration > 10 min
     - Voltage stability σ < 0.2V
     - Multiple recent tests agree on trend
   - Low confidence (pool with others, don't credit alone):
     - Any single test
     - Shallow discharge (ΔSoC < 35%)
     - High voltage noise
   - Very low confidence (ignore):
     - Tests during unusual load patterns
     - Tests with measurement anomalies
   ```python
   confidence = 0.3 * (1 - min(abs(recovery) / 0.01, 1)) + \
                0.4 * min(discharge_duration / 600, 1) + \
                0.3 * min(discharge_depth / 0.5, 1)

   if confidence < 0.5:
       log.warning(f"Recovery {recovery:.2%} marked low confidence; pooling with others")
   ```

3. **Trend over absolute values:**
   - Ignore individual recovery magnitude
   - Track recovery **trend**: is it increasing, stable, or decreasing over past 6 tests?
   - If trend upward → sulfation reversible, continue testing
   - If trend downward → battery not recovering, skip tests (may cause damage)
   - Log: "Recovery trend: +0.1%, +0.3%, -0.1%, +0.2%, -0.3%, -0.1% (flat, not improving)"

4. **Cross-validate with independent health indicator:**
   - Don't rely on SoH alone for recovery credit
   - Also check: IR trend, voltage curve shape, charge acceptance rate
   - If IR still rising even though SoH claims recovery → credit is false
   - Log: "SoH shows +0.4% recovery but IR up 15%; recovery doubtful"

5. **Conservative credit application:**
   - Even if recovery is real, apply **only 50% as scheduling credit**
   - Remaining 50% counts toward model confidence but not test deferral
   - Prevents false deferral of tests due to noise
   - Log: "Credited 50% of measured recovery (0.2% of 0.4%); conservative approach"

**Detection:**

- Monitor recovery trend across tests; alert if oscillating or noisy
- Compare recovery trend to IR trend (should correlate)
- Flag if claimed recovery exceeds degradation rate (recovery > -SoH_trend/12)
- Check if deep tests actually correlate with SoH improvement in real data

---

### Pitfall 5: Race Condition Between Daemon Test Scheduling and Systemd Timers

**What goes wrong:**

- v2.0 has two systemd timers:
  - `ups-test-quick.timer` (daily at 08:00)
  - `ups-test-deep.timer` (monthly, 1st of month at 09:00)
- v3.0 adds daemon-controlled scheduling:
  - Daemon maintains `next_deep_test_time` in model.json
  - Daemon schedules test via upscmd 30 min before calculated time
- **At 08:55 on month 1st:**
  - Systemd timer fires: `systemctl start ups-test-deep.service`
  - Service calls: `upscmd cyberpower test.battery.start.deep`
  - Daemon receives request: "Test initiated, wait 10 min for completion"
- **At 08:56:**
  - Daemon wakes up (10-sec poll interval)
  - Daemon also decided: "Time for deep test per my logic"
  - Daemon calls: `upscmd cyberpower test.battery.start.deep` again
  - UPS receives two test requests **simultaneously**

**Consequences:**

- UPS state undefined: which test is running? Can it handle two requests?
- CyberPower firmware may:
  - Queue requests → tests run back-to-back (20 min SoC drop instead of 10)
  - Ignore second request silently
  - Hang/crash (unknown behavior)
- Daemon loses observability: doesn't know which test is actually running
- Capacity estimation corrupted (if two tests overlap, discharge curve is nonsense)

**Why it happens:**

- No locking mechanism between systemd and daemon
- Daemon and systemd timer are independent schedulers
- No "request permission" handshake (daemon asks: "can I test?" → systemd: "yes, I won't")

**Prevention:**

1. **Migrate from systemd timers to daemon-only scheduling:**
   - Remove `ups-test-quick.timer` and `ups-test-deep.timer`
   - Daemon owns all test scheduling
   - Rationale: daemon has full state (SoH, last test, blackout history); systemd timer is dumb
   - Action: during v3.0 migration, document: "Disable old timers: `systemctl disable ups-test-quick.timer ups-test-deep.timer`"
   - Log: "Transitioned to daemon-controlled testing; systemd timers disabled"

2. **If migration not feasible, implement mutual exclusion:**
   - Create `/var/run/ups-battery-monitor/test.lock` (fcntl-based)
   - Before daemon calls upscmd: acquire lock, wait up to 60 sec
   - Systemd service also tries to acquire lock before calling upscmd
   - Lock held for duration of test (10+ min)
   - Loser of race backs off; logs: "Test already running (lock held by systemd), skipping my test"
   ```python
   # daemon_scheduler.py
   with open("/var/run/ups-battery-monitor/test.lock", "w") as lock_file:
       fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
       try:
           # We have exclusive lock; safe to call upscmd
           upscmd_test_battery_start_deep()
           # Hold lock until test completes
           wait_for_test_completion()
       finally:
           fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
   ```

3. **Add test state variable in model.json:**
   - `current_test_state`: None, "quick_running", "deep_running", "quick_complete", "deep_complete"
   - Before calling upscmd: check state
   - If test already running: don't call upscmd again
   - Log: "Test state is 'deep_running'; skipping my scheduled test"
   ```python
   if model.current_test_state in ["quick_running", "deep_running"]:
       log.info("Test already running; deferring my test")
       return

   # Safe to initiate
   model.current_test_state = "deep_running"
   upscmd_test_battery_start_deep()
   ```

4. **Implement test request debouncing:**
   - Before calling upscmd: check if identical request in last 30 sec
   - If yes: skip (assume previous caller already initiated)
   - Add `last_test_request_time` and `last_test_request_type` to model.json
   ```python
   if (model.last_test_request_type == "deep" and
       now - model.last_test_request_time < timedelta(seconds=30)):
       log.debug("Deep test requested in last 30s; skipping duplicate")
       return
   ```

**Detection:**

- Monitor for multiple upscmd calls in close succession (< 1 min apart)
- Alert if UPS returns unexpected status (two tests running, or test hangs)
- Log all upscmd invocations with source (daemon vs systemd)

---

### Pitfall 6: Deep Test on Degraded Battery Accelerates Failure

**What goes wrong:**

- Battery SoH = 65% (already in red zone, replacement due within 6 months)
- Daemon algorithm: "SoH < 80%, schedule deep test to recover sulfation and extend life"
- Daemon initiates deep test
- Battery is partially sulfated + weak → can't fully recover from discharge
- Post-test SoH = 64% (recovered -1% instead of expected +1%)
- Next test: SoH = 62% (degradation accelerated)
- User sees: "Why did deep test make battery worse?"

**Mechanism:**

- Lead-acid at end-of-life has both sulfation **and** active material loss (irreversible)
- Deep discharge can recover sulfation (reversible)
- But deep discharge also stresses active material loss (irreversible)
- Below SoH 60%, irreversible damage dominates → deep discharge **harmful**
- VRLA manufacturers recommend: "No deep discharge testing below 50% SoH"

**Why it happens:**

- Daemon doesn't distinguish recoverable sulfation from irreversible degradation
- Model predicts sulfation based on time since last deep test, not on SoH trend
- Scheduling algorithm: "hasn't tested in 4 weeks, schedule test" (ignores SoH)
- Failing to incorporate SoH floor into decision

**Consequences:**

- Battery fails weeks earlier than it would under normal ops
- User blames system ("daemon killed my battery")
- Replacement prediction becomes meaningless (system intervened with harmful test)

**Prevention:**

1. **Implement SoH floor check before each test:**
   ```python
   MIN_SOH_FOR_DEEP_TEST = 0.65  # Below 65%, don't test

   def should_initiate_deep_test():
       if model.soh < MIN_SOH_FOR_DEEP_TEST:
           log.warning(f"SoH {model.soh:.1%} below threshold {MIN_SOH_FOR_DEEP_TEST:.1%}; "
                      f"skipping deep test to avoid accelerated failure")
           return False
       # ... other checks ...
       return True
   ```

2. **Use SoH trend to detect irreversible degradation:**
   - If SoH declining > 1% per month → battery approaching end-of-life
   - Don't test if decline rate > 0.8% per month (indicates active material loss)
   - Only test if SoH change is flat or slow (< 0.3% per month)
   - Log: "SoH declining 1.2%/month (active material loss); skipping test to avoid acceleration"
   ```python
   recent_soh = [s for s in model.soh_history if now - s['timestamp'] < timedelta(days=30)]
   if len(recent_soh) >= 3:
       soh_decline_rate = (recent_soh[0]['soh'] - recent_soh[-1]['soh']) / 30
       if soh_decline_rate > 0.008:  # > 0.8% per month
           log.warning(f"SoH decline {soh_decline_rate:.1%}/month (irreversible); skipping test")
           return False
   ```

3. **Distinguish sulfation from active material loss via IR trend:**
   - Sulfation: IR rises slowly, reversible with deep discharge
   - Active material loss: IR rises rapidly, NOT reversible
   - Before testing: check if IR trend matches sulfation signature
   - Log: "IR stable; sulfation risk low, test deferred (wait for higher confidence)"
   ```python
   ir_trend = slope(model.ir_history[-10:])
   if ir_trend > 0.02:  # Rapid IR rise (mΩ/week)
       log.warning("IR rising rapidly; likely active material loss, not sulfation; skip test")
       return False
   ```

4. **Require explicit user approval for tests below 70% SoH:**
   - System doesn't auto-schedule below 70%
   - Alerts user: "Battery at 65% SoH. Deep test may help (if sulfation) or harm (if material loss)."
   - User runs test manually if they choose (takes responsibility)
   - Log: "Manual test approval by user; SoH 65%, inherent risk acknowledged"

5. **Monitor post-test SoH change:**
   - After test, measure actual ΔSoH
   - If ΔSoH < -0.3% (battery got worse): DON'T schedule more tests
   - Alert: "Test on degraded battery (SoH 65%) caused -0.5% SoH loss. Recommend battery replacement instead of further testing."
   - Log: "Disabling automatic tests due to demonstrated harm at low SoH"

**Detection:**

- Alert if SoH drops after deep test (expected: ≥0%, observed: <-0.3%)
- Monitor for correlation: deep test at low SoH → higher SoH decline rate afterward
- Flag if battery reaches SoH 50% within 6 months of v3.0 enabled (suggests testing caused acceleration)

---

## Moderate Pitfalls

### Pitfall 7: Cycle ROI Metric Instability

**What goes wrong:**

- Daemon computes: `cycle_roi = (sulfation_recovered_ah - cycle_wear_ah) / capacity_ah`
- Sulfation recovered = +0.5 Ah (estimated from model)
- Cycle wear = -0.3 Ah (assumed from literature: ~0.05 Ah per 100 cycles)
- ROI = (+0.5 - 0.3) / 7.2 = 2.8% per cycle (seems good, test often)
- But sulfation model uncertainty = ±30%, cycle wear estimate = ±50% error
- **True ROI could be anywhere from -0.5% to +5.0% per cycle** (range is 10x!)
- User sees number swinging ±30% between tests
- Doesn't know if testing is helpful or harmful
- Makes replacement prediction useless

**Why it happens:**

- Both numerator and denominator are highly uncertain
- Sulfation model: ±30% error from temperature, discharge profile, time
- Cycle wear: ±50% error from unknown degradation rate (varies by voltage, discharge depth, temperature)
- Small difference of two large uncertain numbers = very uncertain result

**Consequences:**

- Reported ROI meaningless; user ignores metric
- Scheduling algorithm driven by unreliable signal → tests too often or too rarely
- Export to Grafana shows noisy graph (user suspects bug)

**Prevention:**

1. **Report confidence interval, not point estimate:**
   ```python
   roi_mean = 0.028  # 2.8%
   roi_ci_lower = -0.005  # -0.5%
   roi_ci_upper = 0.050  # +5.0%

   # Output:
   health.json["cycle_roi"] = {
       "value": roi_mean,
       "confidence_lower": roi_ci_lower,
       "confidence_upper": roi_ci_upper,
       "interpretation": "ROI uncertain; confidence range includes zero"
   }
   ```

2. **Require high confidence before using ROI for decisions:**
   - Only schedule tests if ROI > 1% AND confidence_lower > 0% (clearly positive)
   - If confidence_upper < 0% (clearly negative), skip tests
   - If confidence spans zero → can't decide; defer to conservative default (test monthly)
   ```python
   def should_test_based_on_roi(roi_mean, roi_ci_lower, roi_ci_upper):
       if roi_ci_lower > 0.01:  # Confidence range clearly positive
           return True
       elif roi_ci_upper < 0:  # Confidence range clearly negative
           return False
       else:  # Uncertain
           return default_policy()  # Fall back to monthly schedule
   ```

3. **Use empirical ROI instead of model:**
   - After each test, measure actual ΔSoH
   - Compare to SoH without test (baseline degradation)
   - Empirical ROI = (ΔSoH_with_test - ΔSoH_without_test) / discharge_energy
   - Over 6-12 months, empirical ROI converges to true value
   - Log: "Empirical ROI: month 1 = +3%, month 2 = +1.5%, month 3 = +1.8% (converging)"
   ```python
   # Compare SoH trend before vs after introducing deep tests
   soh_before_tests = [s for s in history if s['timestamp'] < test_start_date]
   soh_after_tests = [s for s in history if s['timestamp'] > test_start_date]

   slope_before = linear_regression(soh_before_tests)  # Typical degradation
   slope_after = linear_regression(soh_after_tests)    # With tests

   empirical_roi = (slope_before - slope_after) / num_tests
   ```

4. **Report range instead of number:**
   - "Cycle ROI: 2–4% (likely benefit) vs 0–1% (uncertain benefit)"
   - "Conservative estimate: 0% (test for redundancy, not ROI)"
   - Prevents user from over-interpreting single number
   - Log: "ROI range [0%, 4%]; insufficient signal to recommend testing frequency"

5. **Disable ROI-based scheduling; use heuristics instead:**
   - Don't let ROI drive test frequency
   - Use ROI as **validation metric** only (did tests help? yes/no)
   - Actual scheduling driven by: last test date, sulfation model (with bounds), SoH trend
   - Log: "Test scheduled per policy (every 4 weeks) regardless of ROI uncertainty"

**Detection:**

- Monitor ROI value variance: if σ > 50% of mean → too noisy for decisions
- Alert if confidence interval spans zero; flag as "inconclusive ROI"
- Compare model ROI vs empirical ROI; if diverge > 50%, model is unreliable

---

### Pitfall 8: Cycle Wear Estimation Wildly Wrong

**What goes wrong:**

- Daemon assumes: "Each full discharge cycle (100%→0%) = -0.05 Ah permanent capacity loss"
- Literature value from lead-acid: 0.1–0.5% capacity loss per cycle (varies widely)
- For 7.2 Ah battery: 0.1% per cycle = -0.007 Ah per cycle
- Daemon estimates -0.05 Ah per cycle = 7× higher than reality
- Over 100 tests: predicted wear = -5 Ah
- Actual wear = -0.7 Ah
- Daemon thinks cycling harmful; doesn't test
- Battery sulfates; SoH drops to 60% faster than with tests
- User replaces battery thinking daemon prevented damage; actually daemon caused harm

**Why it happens:**

- Literature values vary 5–50x depending on discharge depth, temperature, design
- Shallow discharges (20% DoD) = ~0.01% per cycle
- Deep discharges (100% DoD) = ~0.3% per cycle
- Hard to know which applies without knowing true discharge profile

**Consequences:**

- Cycle wear over-estimated → under-testing → sulfation accumulation
- Replacement prediction off by months

**Prevention:**

1. **Don't use generic literature values; measure empirically:**
   - Track battery degradation with and without tests
   - Estimate wear from actual SoH trend
   - After 6+ months of data: fit curve `SoH(t) = a - b×t - c×num_tests`
   - Coefficient `c` is empirical cycle wear
   ```python
   from scipy.optimize import curve_fit

   def soh_model(t_days, num_tests, a, b, c):
       return a - b*t_days - c*num_tests

   fit_params, _ = curve_fit(soh_model, history['days'], history['num_tests'], history['soh'])
   empirical_cycle_wear = fit_params[2]  # Capacity loss per test
   log.info(f"Empirical cycle wear: {empirical_cycle_wear:.4f} Ah/test")
   ```

2. **Use conservative estimate (worst-case) until empirical data available:**
   - Set cycle wear = literature max (0.3% per cycle) initially
   - Over-estimate wear, therefore under-test
   - Safe default: "when in doubt, don't test"
   - Once 6+ months data available: recalibrate to measured wear
   - Log: "Using conservative cycle wear estimate; will recalibrate at 6-month mark"

3. **Distinguish shallow vs deep cycle wear:**
   - Quick tests (5 min, 15% DoD) ≈ negligible wear
   - Deep tests (10 min, 40% DoD) ≈ 0.05% capacity loss
   - Don't lump together
   ```python
   if discharge_depth < 0.25:  # Shallow
       cycle_wear = 0.001 * capacity_ah  # 0.1% per cycle, conservative
   else:  # Deep
       cycle_wear = 0.005 * capacity_ah  # 0.5% per cycle
   ```

4. **Monitor for correlation: tests vs degradation rate:**
   - If adding tests causes SoH to decline *faster* (not slower) → cycle wear > sulfation benefit
   - Alert: "Tests correlate with faster degradation; disabling automatic testing"
   ```python
   soh_change_before_tests = soh_history[-30:].mean() - soh_history[-90:-60].mean()
   soh_change_after_tests = soh_history[-10:].mean() - soh_history[-40:-30].mean()

   if soh_change_after_tests > soh_change_before_tests:  # Degradation increased
       log.error("SoH decline accelerated after enabling tests; automatic testing disabled")
   ```

**Detection:**

- Monitor: SoH decline rate with vs without tests
- Alert if correlation negative (tests → faster degradation)
- Flag if cycle wear estimate > sulfation recovery (no benefit to testing)

---

### Pitfall 9: Temperature Estimation Error Propagates Through Decision-Making

**What goes wrong:**

- Daemon estimates temperature from time-of-day heuristic
- Assumes: 6 AM = 15°C, 12 PM = 22°C, 8 PM = 18°C
- Actual: 6 AM = 22°C (UPS indoors near inverter, generating heat overnight)
- Daemon uses 15°C → Model predicts slow sulfation rate
- Actual sulfation rate is 1.5× faster (at 22°C)
- After 8 weeks: daemon predicts "SoH drop 2%", actual is "SoH drop 3%"
- Replacement date off by 4 weeks

**Why it happens:**

- No temperature sensor
- Time-of-day heuristic assumes typical HVAC behavior
- But UPS environment different (steady ~35°C due to inverter)
- Error accumulates in exponential model (small T error → large rate error)

**Consequences:**

- Replacement prediction unreliable
- Scheduling decisions based on wrong sulfation rate
- User loses confidence in system

**Prevention:**

1. **Require explicit temperature override in config:**
   - `ups_location_temperature_celsius: 35`
   - Daemon uses constant value, not heuristic
   - Document: "Measure UPS case temperature with IR thermometer during initial setup. Update config."
   - Log: "Temperature constant: 35°C (configured); update if environment changes"

2. **Accept temperature as uncertainty parameter:**
   - Report scheduling decisions with confidence: "Next test in 4–6 weeks (±2 weeks temp uncertainty)"
   - Health.json: `"temperature_assumed": 35, "temperature_uncertainty": 10`
   - Grafana: plot sulfation schedule with error bands
   - Log: "Test scheduling confidence: 70% due to ±10°C temperature uncertainty"

3. **Make scheduling robust to temperature variation:**
   - Test more frequently rather than less: if uncertain about sulfation rate, test every 3 weeks not 4
   - Conservative: assumes worst-case (fastest) sulfation rate
   - Log: "Conservative scheduling: testing every 3 weeks to account for temperature uncertainty"

4. **Recalibrate on user feedback:**
   - Ask user during first week: "Is the UPS location warm or cool?"
   - Options: cool (<15°C), normal (15–25°C), warm (>25°C)
   - Adjust sulfation model factor: cool ×0.7, normal ×1.0, warm ×1.3
   - Log: "User feedback: warm location; temperature factor 1.3x, test interval reduced by 20%"

**Detection:**

- Monitor SoH degradation rate; if faster than model predicts → temperature likely higher
- Alert if replacement date changes >4 weeks between months (indicates parameter uncertainty)
- Compare SoH trend to sulfation model; if diverge, check temperature assumption

---

## Minor Pitfalls

### Pitfall 10: Deep Test Abort Incomplete

**What goes wrong:**

- Daemon initiates deep test: `upscmd cyberpower test.battery.start.deep`
- At minute 7 of 10, natural blackout occurs
- Daemon detects: `ups.status = OB DISCHRG`, calls `upscmd cyberpower test.battery.stop`
- Command returns OK
- But UPS firmware has bug: deep test continues anyway (already committed to discharge)
- Test runs for 12 more minutes instead of aborting at minute 7
- Total SoC drop = 65% instead of expected 40% from 7-min partial test
- **Daemon thinks test was aborted; next poll shows unexpected low SoC**

**Why it happens:**

- `test.battery.stop` command not guaranteed to work (firmware-dependent)
- CyberPower firmware may not support stop mid-test
- No heartbeat; daemon doesn't know if stop actually worked

**Prevention:**

1. **Check firmware capabilities before enabling stop:**
   - Query NUT: can CyberPower UT850 execute `test.battery.stop`?
   - Test on real hardware during install; document result
   - If not supported: log WARNING, don't rely on stop
   ```python
   SUPPORTED_COMMANDS = run_upscmd("list ups cyberpower")
   if "test.battery.stop" in SUPPORTED_COMMANDS:
       can_abort_test = True
   else:
       log.warning("CyberPower UT850 doesn't support test.battery.stop; can't abort mid-test")
       can_abort_test = False
   ```

2. **Verify stop actually worked:**
   - After calling stop: poll SoC every 5 sec for 30 sec
   - If SoC continues dropping: stop didn't work, test still running
   - Log: "Stop command sent but test continuing; aborting by hard shutdown not recommended"
   - Alert user

3. **Accept that test can't be aborted:**
   - If platform doesn't support stop, block test scheduling if risk window high
   - Log: "Can't abort tests on this UPS; only schedule when blackout risk < 5%"

**Detection:**

- Monitor for unexpected SoC drops during test
- Alert if test takes longer than expected (>15 min)
- Flag if SoC from "partial test abort" doesn't match expected ΔSoC

---

### Pitfall 11: Missing Capacity Convergence Before Starting Tests

**What goes wrong:**

- User installs v3.0 daemon
- Model.json initially empty (no historical discharges)
- Daemon initializes capacity = config value 7.2 Ah (rated)
- Daemon immediately starts scheduling deep tests to "prevent sulfation"
- But true capacity is 5.8 Ah (measured from later discharge)
- All test scheduling based on wrong capacity
- If capacity 20% lower, runtime predictions are 20% too optimistic
- Deep test scheduled thinking there's margin for safety; actually running close to edge

**Why it happens:**

- Daemon doesn't distinguish "known capacity" (from discharge data) vs "assumed capacity" (from config)
- Scheduling algorithm doesn't gate on convergence
- Feature flag missing: "Only enable sulfation scheduling after capacity converged"

**Prevention:**

1. **Require capacity convergence before smart scheduling:**
   ```python
   def enable_sulfation_scheduling():
       return model.is_capacity_converged() and model.capacity_confidence > 0.8

   if not enable_sulfation_scheduling():
       log.info("Capacity not yet converged; deferring sulfation scheduling")
       schedule_next_test(datetime.max)  # Disable for now
       return
   ```

2. **Log convergence status in MOTD:**
   - "Capacity estimation: 2/3 deep discharges (needs 1 more), confidence 75%"
   - "Sulfation scheduling: disabled until capacity converges (protect against bad estimates)"
   - Once converged: "Capacity converged! Sulfation scheduling enabled."

3. **Fast-track capacity convergence:**
   - Prioritize any available discharge for capacity measurement
   - Even shallow blackouts (ΔSoC > 25%) count; accept lower confidence early
   - Log: "Quick discharge detected (ΔSoC 32%); credited for capacity convergence (2/3 samples)"

**Detection:**

- Monitor enable/disable of sulfation scheduling
- Alert if scheduling enabled before convergence
- Log all convergence transitions

---

## Phase-Specific Warnings

| Phase | Topic | Likely Pitfall | Mitigation |
|-------|-------|---------------|-----------|
| **Design** | Sulfation model implementation | Over-trusting Shepherd/Bode parameters without temperature sensor | Accept ±30% uncertainty; use empirical calibration; document limitations |
| **Design** | Test scheduling algorithm | Daemon ignores natural blackouts; schedules test anyway | Gate scheduling on "last blackout > 2 hours" and "SoC > 80%" |
| **Design** | Recovery measurement | Single test = noise; can't distinguish signal | Require 3+ tests; pool over 2 weeks before crediting |
| **Implementation** | upscmd reliability | Silent failures; no heartbeat from UPS | Implement state machine; verify test actually running; timeout safeguard |
| **Implementation** | Daemon vs systemd timer | Race condition; both call upscmd simultaneously | Migrate to daemon-only scheduling; implement fcntl lock if not feasible |
| **Validation** | Testing on degraded battery | Deep test on SoH < 65% accelerates failure | SoH floor check; IR trend monitoring; empirical validation |
| **Validation** | Temperature effects | Hard-coded 35°C; actual ±10°C variance | Configure temp explicitly; accept ±30% model error; report confidence bands |
| **Release** | Cycle wear estimation | Over-estimate from literature; under-test | Measure empirically after 6 months; conservative default until then |
| **Field** | ROI metric noise | Confidence range spans zero; metric meaningless | Report confidence interval not point estimate; require ROI > 1% AND lower_CI > 0% |
| **Field** | Recovery delta confusion | User sees ±0.3% SoH bounce; thinks bug | Explain noise floor; show pooled recovery with confidence |

---

## Sources

- **NUT Issues:** GitHub networkupstools/nut #1970 (CyberPower UT850 onlinedischarge), #2983 (voltage scaling), #1689 (connection loop)
- **Sulfation Modeling:** MDPI Energies 15(21):8172 (Shepherd/Bode review); IEEE 2019 Tian et al (data-driven capacity fade)
- **Deep Discharge Safety:** VRLA manufacturer guides (East Penn, Mitsubishi); NERC battery testing standards
- **Temperature Effects:** Battery University BU-802; ResearchGate internal resistance studies
- **Data-Driven Health:** ScienceDirect "Data-Driven State of Health" (Dubarry 2021); MIT battery cycle life (Severson 2019)
- **Lead-Acid Standards:** IEEE-450-2010; IEC 61427-2 (VRLA testing procedures)
- **UPS Automation:** NUT advanced scheduling (upssched); apcupsd daemon patterns
- **Field Data:** Real blackout 2026-03-12 (CyberPower runtime prediction failure)

---

**Next Steps:**

1. **During v3.0 design:** Use this document to validate architecture decisions
2. **During implementation:** Add code comments referencing specific pitfalls (e.g., "Pitfall 1: Blackout collision → guard SoC > 80%")
3. **During testing:** Create test cases for each pitfall scenario (v2.0 lessons)
4. **During release:** Include migration guide: "Disable old timers → daemon now owns scheduling"
5. **During field validation:** Monitor metrics against detection signals (SoH recovery trend, ROI confidence, test timing collisions)
