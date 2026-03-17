# Expert Panel: Phase 12.1 — Metrology, Adversarial QA, VRLA Lifecycle

**Date:** 2026-03-15
**Trigger:** Blind spot analysis — previous panels focused on math correctness and architecture but missed measurement quality, pathological inputs, and battery physics realism
**Focus:** (1) ADC resolution and error propagation, (2) Pathological input sequences, (3) Nonlinear battery degradation
**Panel:** 3 experts — Metrologist, Adversarial QA, VRLA Battery Lifecycle

---

## Scope

**What:** Identify blind spots in Phase 12/12.1 — measurement quality, adversarial inputs, and battery lifecycle realism.

**Blast radius:** `contained` — affects model accuracy, not system availability.

**Decision type:** Stress-testing an existing plan from angles previous panels missed.

**Hardware reality:**
- CyberPower UT850EG, USB HID driver via NUT
- battery.voltage resolution: 0.1V ADC (confirmed from real data)
- ups.load resolution: integer percent (1% steps)
- Poll interval: 10 seconds
- Blackout frequency: several times per week (unstable grid, Kazakhstan)
- Server load: ~17% average, ~35% peak

---

## Expert Analysis

### 📏 Dr. Sato — Metrologist / Measurement Theory

**Assessment:** You're building a precision instrument out of a consumer UPS's bargain-bin ADC. This isn't fatal — but you need to know your error floor, because right now you're optimizing math accuracy past the sensor's resolution limit.

**Finding 1: CyberPower UT850EG voltage resolution is 0.1V — confirmed by your own data.**

The README shows the 2026-03-12 discharge went from 13.2V → 10.5V. That's 27 discrete steps of 0.1V over 47 minutes. For coulomb counting, you're integrating current (derived from load%) over time. Load is integer percent — so at 17% load: `I = 0.17 × 425 / 12 = 6.02A`. But the UPS reports 17%, not 17.3% — that's already a **±0.5% quantization error** on current, which is **±0.03A**.

Over 47 minutes with trapezoidal integration at 10s intervals (282 steps):
```
Quantization error per step: ±0.03A × 10s / 3600 = ±0.000083 Ah
Over 282 steps (random walk): ±0.000083 × √282 = ±0.0014 Ah
Relative to 7.2Ah: ±0.02%
```

**Good news: quantization error is negligible for coulomb counting.** The √N random walk property saves you — errors don't accumulate linearly because quantization noise is uncorrelated between polls.

**Finding 2: EMA filter introduces systematic bias on discharge curves — and this DOES matter for SoH.**

Your adaptive EMA (ema_filter.py:42-52) uses `sensitivity=0.05` (5%). During a slow discharge (0.1V drop over 5 minutes), the deviation from EMA is small, so alpha stays at base value (~0.08). This means voltage **lags behind reality by ~4-6 samples (40-60 seconds)**. For SoH calculation via area-under-curve, this lag means:
- Discharge start voltage is read as **lower** than true (EMA still settling from float voltage)
- Discharge end voltage is read as **higher** than true (EMA hasn't caught up to the drop)

Effect: area-under-curve is **systematically underestimated** by ~2-5%, making SoH appear lower than reality. This is a **consistent bias**, not random noise — it doesn't cancel over multiple measurements.

**Finding 3: But for capacity estimation (Phase 12) it's fine — you're integrating current, not voltage.**

Coulomb counting uses load% (current), not voltage. Load changes are step-functions (server load jumps), not gradual curves — the adaptive alpha kicks in immediately (deviation >> 5%). So EMA tracks load accurately. Your ±10% accuracy target for capacity is comfortably achievable given the sensor.

**Recommendations:**
1. **Add an "instrument characterization" test to the simulation** — feed the same synthetic discharge through two paths: (a) raw values, (b) values quantized to 0.1V / 1% load, then EMA-filtered. Compare SoH and capacity outputs. The difference is your **systematic instrument bias**. Document it. If > 3%, it changes interpretation of SoH values.
2. **For SoH: consider using raw discharge_buffer voltages, not EMA-filtered values.** The discharge buffer collects `voltage` directly from NUT (monitor.py:673), not from EMA. Verify this — if it's already raw, the EMA bias doesn't affect SoH. *(I checked — line 673 appends `voltage` from the raw poll, not `v_ema`. You're OK.)*
3. **For capacity: use raw load% from discharge_buffer.loads, not EMA load.** Same logic. *(Checked — line 676 uses `self.ema_buffer.load`. This IS EMA-filtered. For steady-state loads this is fine, but if load changes during discharge, EMA introduces lag. Consider using raw `ups_data.get('ups.load')` for discharge buffer loads.)*

**Open question:** Does `battery.voltage` from CyberPower report the actual battery terminal voltage, or the rectified output voltage? If it's output (post-converter), it includes inverter regulation artifacts and is less useful for SoC estimation. NUT variable `battery.voltage` should be pre-inverter on UT850EG (line-interactive topology), but worth confirming with `upsc cyberpower` during a discharge.

---

### 🔥 Kowalski — Adversarial QA / Chaos Tester

**Assessment:** You have good nominal-path coverage. But this system runs on a server with "several blackouts per week" in Kazakhstan. The pathological case IS the nominal case. Let me break things.

**Attack 1: The flicker storm (20 micro-outages in an hour).**

This is your real operating environment. Here's what happens:

```
t=0:    OL → OB (blackout 1, 3 seconds)
t=3:    OB → OL (power back)
t=10:   OL → OB (blackout 2, 5 seconds)
t=15:   OB → OL
...repeat 20 times over 60 minutes...
```

Each OB→OL transition triggers `_update_battery_health()` (monitor.py:396-400). That calls `calculate_soh_from_discharge()` with a 3-5 second discharge buffer. The duration weight is:
```python
discharge_weight = min(3 / (0.30 * T_expected), 1.0)
# T_expected ≈ 2820s (47min), so:
discharge_weight = min(3 / 846, 1.0) = 0.0035
```

Weight 0.0035 means SoH barely changes per micro-outage — the Bayesian blending protects you. **BUT**: `cycle_count` increments 20 times (line 666), `cumulative_on_battery_sec` adds 20×3=60s, and 20 SoH history entries are added. After a year of weekly flicker storms: cycle_count = 1000+ (looks like the battery is dying), SoH history is polluted with hundreds of near-zero-signal entries that noise up the replacement predictor regression.

**Recommendation:** Add a **minimum discharge duration for model updates** — not just for capacity estimation (VAL-01 already has 300s), but for SoH updates too. Suggestion: if discharge < 30 seconds, increment cycle count and cumulative time, but **skip SoH calculation entirely**. The signal-to-noise ratio at 3 seconds is too low. This is a Phase 12.1 kernel decision — the pure function should return `None` for "insufficient data", and the orchestrator decides not to update SoH.

**Attack 2: Interrupted discharge (power returns mid-blackout, then fails again).**

```
t=0:    OL → OB  (start collecting discharge buffer)
t=300:  OB → OL  (power back — _update_battery_health fires, buffer cleared)
t=305:  OL → OB  (power fails AGAIN — new empty buffer starts)
t=600:  OB → OL  (battery health updated with only 295s of data)
```

The first discharge (300s) is processed and cleared. The second discharge (295s) is processed independently. But physically this was ONE continuous discharge with a 5-second break. The battery didn't recover in 5 seconds — the second discharge starts from the *same SoC* the first ended at, not from a recharged state. Two separate SoH calculations with two half-capacity discharge buffers give worse estimates than one combined buffer would.

**Recommendation:** Add a **discharge cooldown timer**. If OB→OL→OB occurs within 60 seconds, **append to the existing discharge buffer** instead of starting fresh. The event classifier already tracks transitions — just don't clear the buffer if the OL period is < 60s. This is an orchestrator-level change (monitor.py), not a kernel change.

**Attack 3: NUT returns garbage during USB reconnect.**

When the UPS USB disconnects briefly (cable wiggle, hub reset), NUT may return:
- `battery.voltage = 0.0` (or missing key)
- `ups.status = None` or unknown string
- `ups.load = 0`

The voltage bounds check (monitor.py:603-605) catches 0V (< 8.0V). But what about `battery.voltage = 8.1V` — below any real battery voltage (UPS cutoff is 10.5V) but passing the bounds check? This would insert a garbage LUT point at 8.1V / SoC ~0.0 via `calibration_write`, permanently corrupting the LUT.

**Recommendation:** Tighten the discharge-mode voltage floor. During OB state, if voltage < 10.0V (below cutoff anchor minus margin), **skip the sample** and log a warning. The bounds check at 8.0V is for all modes; during discharge, the physical floor is higher.

**Attack 4: Stale voltage (ADC freeze).**

If `battery.voltage` returns the same value for 5+ consecutive polls (50 seconds), the ADC may be frozen. The EMA won't notice because deviation = 0. The discharge buffer will accumulate identical voltage readings, making the discharge curve look like a flat line. SoH calculation via area-under-curve will produce a perfect rectangle instead of a declining curve — overestimating area and thus SoH.

**Recommendation:** Add a **stale value detector** to the simulation's pathological scenarios. If 5+ identical voltage readings during OB state, flag the discharge as suspect. This is a *simulation scenario*, not a production guard (in production, identical readings might be real during very low-load discharge). But the simulation should test that SoH doesn't blow up when fed flat curves.

**Summary — concrete tests to add to Phase 12.1 simulation:**

| Scenario | Input Pattern | Assert |
|----------|--------------|--------|
| Flicker storm | 20 × 3-second OB/OL in 1 hour | SoH unchanged (weight too low), cycle_count = +20 |
| Interrupted discharge | 300s OB → 5s OL → 295s OB | Combined capacity estimate within ±5% of single 600s discharge |
| Voltage spike | One poll at 8.5V during normal OB discharge | LUT not corrupted, sample skipped |
| Stale ADC | 50s of identical 12.4V readings during OB | SoH not inflated, discharge flagged suspect |
| NTP jump | Timestamp decreases by 2s mid-discharge | Integration handles gracefully (skip or abs(dt)) |

---

### 🔋 Dr. Petrov — VRLA Battery Lifecycle

**Assessment:** Your linear degradation model is fine for the first 18 months. After that, it will dangerously mask failure. Here's the real VRLA lifecycle.

**Finding 1: VRLA degradation is sigmoidal, not linear.**

A typical sealed lead-acid battery follows this pattern:
```
Year 0-1:   SoH 100% → 95%   (slow, break-in period, slight capacity increase possible)
Year 1-2:   SoH 95% → 85%    (linear-ish decline, your model works here)
Year 2-3:   SoH 85% → 70%    (accelerating decline)
Year 3-3.5: SoH 70% → 40%    (cliff edge, grid corrosion failure mode)
Year 3.5+:  SoH 40% → dead   (rapid, weeks not months)
```

Your Bayesian blending with `discharge_weight = min(duration / (0.30 * T_expected), 1.0)` caps weight at 1.0 for a 30%-of-expected discharge. At the cliff edge, discharges get shorter (battery dies faster), so weight gets *smaller*, and the prior (old SoH) dominates longer. **The worse the battery gets, the slower your model adapts.** This is exactly backwards.

Concrete example at cliff edge:
```python
# Battery at cliff edge: true SoH = 0.45
# Previous SoH estimate = 0.70 (model hasn't caught up)
# Deep discharge: actual = 12 minutes (vs expected 47min at SoH=1.0)
# duration = 720s, T_expected = 2820s
# discharge_weight = min(720 / (0.30 * 2820), 1.0) = min(0.85, 1.0) = 0.85

degradation_ratio = area_measured / area_reference  # ≈ 0.45/0.70 = 0.64
measured_soh = 0.70 * 0.64 = 0.448
new_soh = 0.70 * (1 - 0.85) + 0.448 * 0.85 = 0.105 + 0.381 = 0.486
```

OK, after one deep discharge it jumps from 0.70 to 0.486. Not great — true is 0.45 but model says 0.486. But with shorter discharges (more realistic at cliff):

```python
# Short discharge: actual = 4 minutes (battery nearly dead)
# duration = 240s
# discharge_weight = min(240 / 846, 1.0) = 0.28

degradation_ratio = 0.30  # Much worse
measured_soh = 0.70 * 0.30 = 0.21
new_soh = 0.70 * 0.72 + 0.21 * 0.28 = 0.504 + 0.059 = 0.563
```

SoH barely moves from 0.70 to 0.563 despite the battery being nearly dead (true ~0.20). **The model fails to react quickly enough at the cliff edge** because short discharges get low weight.

**Recommendation:** Add a **cliff-edge degradation scenario** to the year simulation:
```
Months 1-24: SoH degrades 1.5%/month (linear, gentle)
Months 25-30: SoH degrades 5%/month (accelerating)
Months 31-33: SoH degrades 15%/month (cliff)
```
Assert: model SoH tracks true SoH within ±10% at all times. **I predict this test will FAIL** with current Bayesian blending parameters. The fix is a separate concern (maybe adjust the 0.30 constant in `discharge_weight`, or add a "rapid change" detector) — but Phase 12.1 should **discover and document the failure mode**, not fix it.

**Finding 2: SoH can genuinely increase — and your model should handle it.**

After prolonged float charge (weeks without discharge) or after a deep discharge that breaks up sulfation crystals, a VRLA battery can recover 2-5% capacity. This is real physics (sulfation reversal), not a measurement error. Your model's SoH is capped at 1.0 (soh_calculator.py:117), which is correct — but what about `reference_soh = 0.85` and then a discharge measures `degradation_ratio = 1.04`?

```python
measured_soh = 0.85 * 1.04 = 0.884
```

This works — SoH increases from 0.85 to ~0.88. The model handles it naturally because the Bayesian update is symmetric. **No code change needed, but add a simulation scenario** that tests 3% recovery after deep discharge, verifying SoH increases smoothly and doesn't trigger any "impossible" guards.

**Finding 3: Temperature matters more than you think, even indoors.**

You dismissed temperature ("indoor ±3°C"). But the UT850EG sits near the server. Server exhaust heats the UPS. During heavy computation: UPS temperature rises 5-8°C above ambient. VRLA capacity coefficient is approximately **-0.5% per °C** above 25°C. At 33°C (ambient 25°C + 8°C server heat), capacity is ~4% lower than at 25°C. This is within your ±10% tolerance, but it's a *systematic* bias that affects every measurement taken during high-load periods.

**Recommendation:** Not a code change for Phase 12.1, but **document in STATE.md** that capacity measurements during high-load periods (>30% sustained) will systematically underestimate capacity by 2-5% due to thermal effects. The simulation should include a scenario where "summer months" have 3% lower true capacity than "winter months" — and verify the convergence_score still works (it should, because seasonal variation is within CoV < 10%).

---

## Panel Conflicts

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| EMA load in discharge buffer | Metrologist: use raw load, not EMA | Daemon Expert (previous panel): EMA protects against noise | **Store both.** Use raw `ups.load` in discharge buffer for coulomb counting accuracy. Keep EMA for real-time display/decisions. Discharge buffer already stores `loads` separately — just change the source from `self.ema_buffer.load` to raw `ups_data['ups.load']`. |
| Minimum discharge for SoH | QA: 30s minimum, skip SoH for flickers | Battery Expert: even 10s discharges have signal (Bayesian weight handles it) | **QA wins.** The Bayesian weight at 3 seconds is 0.0035 — mathematically negligible but pollutes history. Skip SoH for < 30s. The weight math is correct but 20 entries/hour of noise degrades the replacement predictor's regression. |
| Cliff-edge: fix or document? | Battery Expert: Phase 12.1 should discover + document, not fix | Previous Architect: don't expand scope | **Document.** Phase 12.1 is about stability testing. If cliff test fails, that's a *finding*, not a *bug to fix*. Log it in STATE.md as known limitation with severity assessment. Fix belongs in Phase 13 or later. |

## Recommended Plan — Additions to Phase 12.1

**Key decisions:**

1. **Discharge buffer stores raw load, not EMA** — change line 676 of monitor.py from `self.ema_buffer.load` to raw poll value. One-line change, improves coulomb counting accuracy for variable-load discharges. (Metrologist)

2. **Minimum 30s discharge for SoH update** — kernel function `calculate_soh` returns None for < 30s. Orchestrator skips SoH update but still increments cycle_count and cumulative_on_battery_sec. (QA)

3. **Discharge cooldown: 60s OL before clearing buffer** — if power returns for < 60s and fails again, treat as continuation of same discharge event. Orchestrator change only. (QA)

4. **5 adversarial scenarios added to simulation** — flicker storm, interrupted discharge, voltage spike, stale ADC, NTP jump. (QA)

5. **3 lifecycle scenarios added to simulation** — cliff-edge degradation (25→33 months), sulfation recovery (3% SoH increase), seasonal thermal variation (3% capacity swing). Cliff test is expected to fail with current blending params — that's a finding, not a bug. (Battery Expert)

6. **Instrument characterization test** — same discharge through raw vs quantized+EMA paths, compare SoH delta. Documents systematic instrument bias. (Metrologist)

**New items for STATE.md:**
- Thermal systematic bias during high-load periods: 2-5% capacity underestimate. Known, accepted for v2.0.
- Bayesian SoH blending has inertia at cliff edge — short discharges get low weight exactly when battery is failing fastest. Known limitation, needs research for v2.1+.

---

**Status:** All recommendations to be incorporated into Phase 12.1 ROADMAP.md and STATE.md on 2026-03-15.
