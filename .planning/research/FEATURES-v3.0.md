# Feature Research: v3.0 Active Battery Care

**Domain:** Enterprise VRLA UPS battery management system (daemon-driven desulfation & lifecycle optimization)
**Researched:** 2026-03-17
**Confidence:** MEDIUM (enterprise algorithms proprietary; research covers physics baseline + published standards + UPS industry practices)

## Feature Landscape

### Table Stakes (Users Expect These)

Features required for a credible active battery management system. Missing these = unreliable scheduling.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Sulfation detection** | VRLA batteries naturally sulfate; BMS must identify it or false scheduling occurs | MEDIUM | Rising internal resistance (IR) trend + discharge curve shape deviation are primary indicators. IR estimation via voltage sag during quick test (ΔV/ΔI). |
| **Safe deep discharge triggering** | Automated desulfation requires gate-keeping: minimum SoH floor, grid stability check, rate limiting | MEDIUM | Prevent unsafe conditions (discharge when SoH<50%, no recovery margin) or excessive wear (more than 1 test/week). |
| **Temperature compensation for sulfation rate** | Sulfation accelerates exponentially with temperature (rate doubles per 10°C). Missing this = wrong scheduling. | LOW-MEDIUM | Lead-acid sulfation rate follows Arrhenius model; temperature coefficient ~0.003V/°C per cell. Fallback to configurable constant (~35°C typical). |
| **Cycle count accumulation** | Track full charge-discharge cycles to correlate with capacity loss. Enterprise requirement for lifecycle cost. | LOW | Count OL→OB transitions from systemd journal or NUT events. Simple counter, persisted in model.json. |
| **Recovery delta tracking** | SoH bounce after deep discharge = desulfation evidence. Enables data-driven detection of sulfation reversal. | MEDIUM | Measure voltage recovery 30s post-discharge: lower recovery = higher sulfation. Trend over 3+ tests enables confidence. |
| **Test scheduling constraints** | Must skip scheduled tests when recent natural blackouts already desulfated (avoid excessive stress). | LOW-MEDIUM | Simple credit logic: if blackout occurred <N days ago with sufficient depth, defer scheduled deep test. |

### Differentiators (Competitive Advantage)

Features that separate this daemon from static systemd timers and basic UPS firmware.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Physics-based sulfation model (hybrid)** | Combine Peukert/Shepherd discharge curve model with data-driven IR trending. Replaces proprietary firmware guessing. | HIGH | Baseline: Shepherd model voltage prediction vs. actual → curve shape deviation indicates sulfation. Enhancement: IR trend (exponential fit) + recovery delta confirm. Confidence score from CoV-based convergence (≥3 tests, CoV<10%). |
| **Intelligent maintenance scheduling** | Daemon calls `upscmd` directly (replaces static systemd timers). Adapts test frequency based on sulfation score, temperature, cycle age, and recent blackouts. | HIGH | Decision tree: IF sulfation_score>threshold AND SoH>min_floor AND days_since_last_test>min_interval AND no_recent_blackout_credit THEN schedule_test(). Avoids wake-from-float tests when already desulfated. |
| **Cycle ROI metric** | Single number balancing desulfation benefit (SoH recovery %) vs. wear cost (estimated capacity loss per test). Enables data-driven threshold tuning. | MEDIUM | ROI = (measured_soh_delta_after_test) / (estimated_capacity_loss_from_discharge). Export to health.json for Grafana trending. |
| **Quick test as IR measurement tool** | 10-second discharge test already measures voltage drop under load; repurpose as non-destructive IR proxy. | LOW | Standard UPS quick test: measure voltage pre/post 10s load. ΔV/I = R_internal. Compare to baseline to detect rising resistance (sulfation). |
| **Natural blackout credit in scheduling** | Skip scheduled deep tests when recent blackouts (full discharge) provide equivalent or superior desulfation. Reduces wear, improves reliability during high-outage periods. | LOW-MEDIUM | Credit logic: blackout ≥90% depth counts as maintenance test. Defer scheduled test for N days if recent blackout occurred. |
| **Reporting: sulfation score + scheduling decisions** | Export sulfation confidence, next test ETA, ROI metrics to health.json. Grafana dashboard shows SoH trend + sulfation trajectory + test schedule. Journald structured events track every scheduling decision. | MEDIUM | Fields in health.json: `sulfation_score` (0-100), `ir_trend_status` (rising/stable/recovering), `recovery_delta_ma` (mV improvement post-test), `next_test_reason`, `next_test_eta_timestamp`, `cycle_roi_percent`. Journald: `@fields.reason`, `@fields.soh_delta`, `@fields.ir_delta`. |

### Anti-Features (Commonly Requested, Often Problematic)

Features that seem appealing but create problems in practice.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Automatic shallow discharge for SOC equalization** | Some BMS systems use 20-50% discharges to balance cell voltage. Looks smart in documentation. | VRLA cells are not lithium; shallow discharges don't equalize voltage and add wear without benefit. Quick test already validates cell balance via voltage measurement. | Skip shallow discharges entirely. Use quick test voltage readings (cell-by-cell) to detect imbalance; flag for manual servicing if individual cell <10.5V during test. |
| **Predictive RUL via ML (LSTM/XGBoost)** | Literature shows 98% accuracy with neural networks for remaining useful life. Tempting for "smart" marketing. | Requires large training dataset (hundreds of discharge curves) from identical battery units. CyberPower UT850 provides no public discharge history. Overfitting to synthetic data produces confident-but-false predictions. | Stick to physics-based SoH (from Peukert + voltage curve) + empirical replacement threshold (SoH<30% = 6 months warning). Simple, interpretable, works with sparse data. |
| **Test on user-defined time schedule (configuration UI)** | Admins want to "tune" when tests run. Seems flexible. | Creates configuration drift and hidden assumptions. If test timing is wrong, no one knows why. Scheduling should be algorithmic and logged. | Expose tuning parameters (min_interval, sulfation_threshold, soh_floor) in config, but run scheduling logic in daemon code, not static systemd timers. Log every decision to journald with reason. |
| **Discharge to 0% for "full capacity" measurement** | Every cycle could be a capacity check. Tempting for continuous calibration. | VRLA cells suffer permanent damage below 10.5V per cell (~50% SOC). Full discharge creates risk of stalling server at critical moment. Previous cycles trapped at mid-SOC during blackout are more reliable data. | Collect capacity from natural blackouts (full depth measured mid-failure). Confidence increases with CoV convergence, not test frequency. |
| **Temperature-based test rescheduling (heat = more tests)** | Higher temperature accelerates sulfation. Logic seems sound. | Temperature is almost constant (35°C in this deployment due to inverter heat). Scheduling changes based on noise (±2°C = ±1% sulfation rate change) create false positives. Cost of test >> benefit of marginal optimization. | Accept temperature as fixed for this hardware. Use temperature as confounder adjustment when analyzing multi-site deployments. For single CyberPower UT850, temperature compensation negligible. |

## Feature Dependencies

```
[Sulfation Model (hybrid)]
    ├──requires──> [Quick Test IR Measurement]
    │                   └──requires──> [Recovery Delta Tracking]
    ├──requires──> [Temperature Compensation] (fallback to constant OK)
    └──requires──> [Cycle Count Accumulation]

[Intelligent Scheduling]
    ├──requires──> [Sulfation Model]
    ├──requires──> [Safe Deep Discharge Constraints]
    │                   ├──requires──> [SoH Minimum Floor Check]
    │                   └──requires──> [Grid Stability Check]
    ├──requires──> [Natural Blackout Credit]
    └──requires──> [upscmd Integration] (daemon calls UPS commands directly)

[Cycle ROI Metric]
    ├──requires──> [Sulfation Model] (benefit = SoH delta)
    └──requires──> [Cycle Count] + [Wear Cost Estimate] (capacity loss per test)

[Reporting Pipeline]
    ├──requires──> [Sulfation Model] (sulfation_score)
    ├──requires──> [Intelligent Scheduling] (next_test_reason, eta)
    └──requires──> [Cycle ROI Metric] (roi_percent)
```

### Dependency Notes

- **Sulfation Model requires Quick Test IR Measurement:** Rising internal resistance is the only non-destructive indicator of sulfation in VRLA. Quick test voltage sag (ΔV under 10s load ÷ load current) gives IR estimate without extended discharge. Recovery delta (voltage recovery 30s post-load) refines confidence.

- **Intelligent Scheduling requires Safe Deep Discharge Constraints:** Automated test triggering must prevent unsafe conditions (SoH<50% = recovery margin at risk; grid spike from unscheduled discharge) and excessive wear (more than 1 test/week = accelerated aging).

- **Natural Blackout Credit requires Cycle Count:** Must distinguish scheduled tests from unplanned blackouts. Both desulfate, but blackouts provide "free" maintenance at risk of server shutdown. Blackout frequency determines whether scheduled tests are even needed.

- **Cycle ROI requires Wear Cost Estimate:** Benefit (SoH recovery %) is measurable; cost (estimated capacity loss per discharge cycle) requires model. Use Peukert-based cycle aging formula: each full discharge ~0.1-0.2% capacity loss (tuned from historical SoH curve).

- **Reporting requires all models:** health.json export aggregates sulfation_score, next_test_reason, cycle_roi into single structured output for Grafana + alerting.

## MVP Definition

### Launch With (v3.0.0)

Minimum viable active battery management system — what's needed to replace static systemd timers with intelligent scheduling.

- [x] **Sulfation detection via IR trend + recovery delta** — Collect quick test data (voltage pre/post load); compute IR estimate; trend over 3+ tests to detect rising resistance (sulfation) vs. stable/recovering (healthy). Bootstrap from existing quick-test infrastructure.

- [x] **Safe deep discharge constraints (SoH floor + rate limit)** — Gate scheduled tests: require SoH>50% and interval>7 days minimum. Prevents unsafe discharge and excessive wear. Simple thresholds, tuned from VRLA lifecycle data.

- [x] **Temperature fallback constant (~35°C)** — Accept temperature as fixed for single-UPS deployment. If NUT HID temperature available, use it for future compensation; otherwise use configurable constant. No scheduling change based on ±2°C noise.

- [x] **Natural blackout credit logic** — If OL→OB event with ≥90% depth occurred <7 days ago, defer scheduled deep test. Count as maintenance credit. Avoids redundant testing during high-outage periods.

- [x] **daemon upscmd integration** — Daemon calls `upscmd UT850 test.battery.start` directly (replaces `ups-test-deep.timer` systemd call). Log scheduling decision to journald with reason code.

- [x] **Cycle ROI metric (health.json export)** — Calculate ROI = (SoH_delta_after_test_%) / (estimated_capacity_loss_from_discharge_%). Expose in health.json for Grafana trending. Needed to validate scheduling effectiveness over time.

- [x] **Journald structured events for scheduling decisions** — Log every test trigger/skip decision with fields: `sulfation_score`, `ir_trend_status`, `recovery_delta_ma`, `soh_delta_post_test`, `test_reason` (scheduled|blackout_credit|new_battery), `next_test_eta`. Enable root-cause analysis of scheduling logic.

### Add After Validation (v3.1+)

Features to add once core scheduling is running and collecting data.

- [ ] **Temperature compensation from NUT HID** — If `battery.temperature` available via NUT, apply Arrhenius adjustment to sulfation rate (doubles per 10°C). Enables multi-site deployments with varying ambient temps. Fallback to configurable constant if HID unavailable.

- [ ] **Peukert exponent calibration** — Current implementation fixed at 1.2 for v2.0 (avoids circular dependency: capacity ↔ exponent). Refine exponent from deep discharge curve shape once baseline capacity converged (CoV<10%). Improves runtime prediction accuracy.

- [ ] **Shallow test as leading indicator** — Run quick test before scheduling deep test to forecast whether desulfation is needed (IR spike = yes, stable = defer). Reduces unnecessary deep discharges while maintaining schedule responsiveness.

- [ ] **Cliff-edge degradation detector** — Detect abrupt SoH drops (>5% in single test) = sudden failure risk. Trigger immediate replacement alert and stop all testing. Flag from discharge curve irregularity (voltage tail flattens unexpectedly).

- [ ] **Multi-battery capacity normalization** — If replacement battery installed mid-way through deployment, recalibrate SoH baseline against new measured capacity. Avoid false "replacement needed" alerts from old baseline. Triggered by new-battery detection logic already in v2.0.

### Deferred (v4.0+, Domain Maturity)

Features to skip unless demand emerges.

- [ ] **Active charge current limiting during test** — Reduce load during test to extend battery life (50% load test vs. 100% test). Current quick test is fixed at ~15% load; limited benefit in extending test intervals. Revisit if test wear becomes measurable problem (SoH cliff detected).

- [ ] **Seasonal thermal correction** — Adjust sulfation thresholds based on seasonal temperature variation (summer = faster aging, schedule more tests). Adds complexity for single-UPS with constant 35°C. Valuable only if deploying across multiple datacenters with ±15°C variation.

- [ ] **Grid stability check (external)** — Before scheduling discharge test, query grid frequency or power company API for "stable" window. Avoid test during peak demand or grid stress. Requires external integration; current implementation (manual blackout credit) sufficient for single-site UPS.

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority | Phase |
|---------|------------|---------------------|----------|-------|
| Sulfation detection (IR + recovery delta) | HIGH | MEDIUM | P1 | v3.0.0 |
| Safe discharge constraints (SoH floor) | HIGH | LOW | P1 | v3.0.0 |
| Natural blackout credit | HIGH | LOW | P1 | v3.0.0 |
| daemon upscmd integration | HIGH | MEDIUM | P1 | v3.0.0 |
| Journald structured events (scheduling) | HIGH | MEDIUM | P1 | v3.0.0 |
| Cycle ROI metric (health.json) | MEDIUM | MEDIUM | P2 | v3.0.0 |
| Temperature fallback constant | MEDIUM | LOW | P2 | v3.0.0 |
| NUT HID temperature compensation | MEDIUM | MEDIUM | P2 | v3.1 |
| Peukert exponent calibration | MEDIUM | HIGH | P2 | v3.1 |
| Shallow test as leading indicator | MEDIUM | MEDIUM | P2 | v3.1 |
| Cliff-edge degradation detector | MEDIUM | HIGH | P2 | v3.1 |
| Seasonal thermal correction | LOW | HIGH | P3 | v4.0+ |
| Grid stability check (external) | LOW | HIGH | P3 | v4.0+ |
| Active charge current limiting | LOW | HIGH | P3 | v4.0+ |

**Priority key:**
- **P1 (Must have for launch):** Core algorithm + safe operation + observability. MVP features for v3.0.0. Tested with 1+ month real blackout data.
- **P2 (Should have, add when possible):** Refinement + edge cases. v3.1+ once core is stable and producing multi-cycle data.
- **P3 (Nice to have, future):** Multi-site scaling + domain maturity. Only if deployment varies significantly (temp, grid conditions, battery types).

## Enterprise Practices Reference

### How Enterprise BMS Systems Model Sulfation

**Industry Standard Approach (Eaton, Schneider, Vertiv):**

1. **Internal Resistance Trending**
   - Measure via DC load test (quick 10s discharge): ΔV/I = R_internal
   - Compare to baseline (new battery): rising trend = sulfation indicator
   - Empirical rule: 20-30% IR rise above baseline = action needed

2. **Discharge Curve Shape Analysis**
   - Healthy VRLA: exponential decay (Shepherd model) — flat plateau then cliff drop
   - Sulfated battery: rounded knee (reduced knee voltage) + earlier cliff
   - Deviation from Peukert fit indicates chemistry change (sulfation crystals reduce reversible capacity)

3. **Recovery Delta Monitoring**
   - Measure voltage recovery 30-60s after discharge stops
   - Healthy: fast recovery (within 2% of pre-discharge voltage)
   - Sulfated: slow recovery (stagnates at lower voltage) due to reduced mass transport
   - Recovery delta = (pre_discharge_voltage - post_discharge_voltage_at_60s)

4. **Temperature Compensation**
   - Sulfation rate follows Arrhenius equation: rate doubles per 10°C above 25°C
   - Adjustment: reduce sulfation threshold by 10% per °C above baseline
   - Published coefficient: -5 to -6 mV/°C per cell (temperature sensitivity)

### Enterprise Scheduling Algorithms

**Decision Tree (typical pattern from Eaton ABM, Schneider):**

```
IF (sulfation_score > threshold):
  AND (soh_percent > safety_floor=50%):
  AND (days_since_last_test > min_interval=7):
  AND (load_demand < peak_threshold):
  THEN schedule_test()
ELSE:
  defer until next check (typically daily)
```

**Thresholds (empirical from VRLA lifecycle studies):**
- `sulfation_score >= 65` (on 0-100 scale): initiate desulfation test
- `soh_percent < 50%`: stop all testing (recovery margin at risk)
- `min_interval = 7 days`: prevent excessive cycling
- `max_frequency = 1 test/week`: cap wear rate

**Blackout Credit Logic:**
- Full discharge during blackout (OL→OB depth ≥ 90%) counts as maintenance credit
- Defer scheduled test for N days (typically 7-14 days) if recent blackout occurred
- Rationale: full discharge provides superior desulfation (100% depth) compared to controlled test (typically 30-50% depth)

### Cycle ROI Calculation

**Enterprise Formula (simplified from lifecycle studies):**

```
ROI = (SoH_improvement_percent) / (Estimated_capacity_loss_percent)

SoH_improvement = SoH_post_test - SoH_pre_test

Estimated_capacity_loss = baseline_loss_per_cycle * depth_of_discharge_percent
                        = 0.15% * (90% depth) ≈ 0.135% per test

Example:
  SoH before test: 92%
  SoH after test:  93.5% (recovered 1.5% from desulfation)
  Estimated capacity loss: 0.135%
  ROI = 1.5 / 0.135 = 11.1x benefit (excellent)
```

**Threshold Decision:**
- ROI > 5x: strong case for test (benefit outweighs wear)
- ROI 1-5x: marginal (sulfation score tie-breaker needed)
- ROI < 1x: skip test (wear exceeds benefit — battery too degraded)

### Real-World Operating Context (This Deployment)

**Hardware:** CyberPower UT850EG, frequent blackouts (2-5/week), battery at ~35°C constant.

**Enterprise Equivalent:**
- APC Smart-UPS: Eaton ABM (automatic battery management) with scheduled self-tests
- Eaton 9PX: tracks cumulative on-battery time (proxy for cycle count), auto-triggers test after 24h float
- Schneider Symmetra: SNMP-exposed `batteryHealthStatus` bitmap (bits for cycle aging, replacement flag)

**Standard Maintenance (published specs):**
- Sealed VRLA: 1 test per month minimum
- Every 6 months: full capacity test (deeper discharge)
- Annual: internal resistance measurement via load bank
- Replacement: at SoH < 30% or after 3-5 years (whichever first)

## Feature Complexity Breakdown

| Feature | Lines of Code (Est.) | Test Coverage | Risk Level |
|---------|---------------------|----------------|------------|
| Sulfation detection (IR + recovery delta) | 150-200 | HIGH (E2E with discharge sim) | MEDIUM — IR estimation sensitive to load accuracy; recovery delta needs stable 30s wait |
| Safe constraints (SoH floor) | 30-50 | HIGH (unit tests) | LOW — simple threshold checks |
| Natural blackout credit | 50-80 | MEDIUM (journal parsing) | LOW — event-driven logic, deterministic |
| daemon upscmd integration | 100-150 | MEDIUM (mock upscmd) | MEDIUM — shell interaction error handling required |
| Journald structured events | 80-120 | MEDIUM (log parsing tests) | LOW — structured fields already working in v2.0 |
| Cycle ROI metric | 60-100 | HIGH (unit + integration) | MEDIUM — wear cost estimate model unvalidated; needs 6+ months data |
| Temperature compensation | 40-60 | MEDIUM (with NUT data) | LOW — fallback to constant acceptable |

## Implementation Rationale (Why These Features)

1. **Sulfation detection is non-negotiable:** VRLA sulfation is irreversible and undetectable by firmware. Rising IR is the only physics-based leading indicator. Without it, scheduling is just static timers (already v2.0 limitation).

2. **Safe constraints prevent catastrophic failure:** Discharging below 50% SoH removes recovery margin; excessive discharge adds wear faster than desulfation benefit (lifecycle studies show 1 test/week ceiling). Boundaries prevent daemon from causing harm.

3. **Natural blackout credit increases reliability:** Frequent blackouts (2-5/week) mean maintenance "free" via production discharge. Scheduling should adapt to actual operational pattern, not ignore it.

4. **Cycle ROI enables data-driven refinement:** Without measuring benefit (SoH delta), it's impossible to validate whether scheduling is working or just imposing wear. ROI trending over 6+ months informs v3.1 threshold tuning.

5. **upscmd integration replaces static timers:** Current v2.0 uses `ups-test-deep.timer` (systemd, monthly). Daemon should control scheduling so decisions are logged with reasoning. Enables future enhancement (shallow test as leading indicator, grid stability check).

6. **Journald structured events enable observability:** Scheduling logic is decision-critical. Every test trigger/defer needs "why" logged. Post-analysis of 6+ months data will reveal whether thresholds are correct.

## Known Unknowns (Research Gaps)

| Question | Impact | Resolution Path |
|----------|--------|-----------------|
| What is actual IR rise rate for CyberPower UT850 at 35°C? | Sulfation threshold tuning | Collect IR measurements from daily quick tests for 3 months; compute trend coefficients |
| How much capacity loss per discharge cycle (actual, not literature)? | ROI model validation | Compare SoH pre/post test over 10+ cycles; fit polynomial loss model |
| Does recovery delta (30s post-test) correlate with sulfation score? | Confidence in IR trend detection | Statistical analysis of recovery delta vs. IR rise; bootstrap confidence intervals |
| What is "safe" minimum SoH floor (currently assumed 50%)? | Risk threshold | Review discharge curves at SoH=50% vs. SoH=40% (voltage recovery risk); conservative default safe |
| Can NUT HID provide `battery.temperature` for CyberPower UT850? | Temperature compensation viability | Test with `upsc UT850@localhost` — check if `battery.temperature` field present; if yes, enable v3.1 feature |

## Sources

**Enterprise BMS & Sulfation Modeling:**
- MDPI (2024): "EIS & Internal Resistance Determination" — electrochemical impedance spectroscopy for non-destructive VRLA health monitoring
- ACTEC White Paper: "Internal Resistance of Lead-Acid Batteries" — empirical sulfation detection via IR trending
- Vertiv/EPRIS Evaluation: "Internal Ohmic Measurements and Capacity Relationship" — DC load test methodology (10s quick test standard)

**VRLA Physics & Discharge Curves:**
- PVEducation: "Characteristics of Lead-Acid Batteries" — Shepherd model voltage equation, temperature compensation coefficients
- ResearchGate: "Peukert's Law Simulation" — discharge curve modeling, exponent vs. depth behavior
- NREL Technical Report: "Shepherd Model Parameters" — battery model fits for sealed VRLA

**Cycle Life & Degradation:**
- ScienceDirect (2023): "Battery Cycle Life Optimization Under Uncertainty" — DOD impact on cycle count; 80% DOD = ~50% of 50% DOD cycle life
- Sandia Report SAND2004-3149: "Lead-Acid Battery Testing" — maintenance discharge frequency, capacity recovery evidence
- Battery University: "Elevated Self-Discharge" — temperature effects on aging (50% life reduction per 8.3°C above 25°C)

**Eaton/Schneider/Vertiv Published Practice:**
- Eaton ABM White Paper: "Advanced Battery Management" — automated test scheduling, cycle life extension strategies
- Schneider Monitoring Policies: SNMP battery health OID documentation (replacement threshold bits, lifecycle bits)
- Standard UPS Quick Test: 5-10s discharge at ~15% load, voltage sag measurement (ΔV = I × R_internal)

**Temperature & Sulfation:**
- Battery Chemistry: Sulfation rate ∝ exp(−E_a / (k·T)); doubles per 10°C above 25°C
- Coefficient: −0.003V/°C per cell (VRLA standard); −5 to −6 mV/°C for 2V cell

**This Deployment Context:**
- Real blackout 2026-03-12: 47 min actual vs. 22 min firmware prediction (validated daemon model)
- Operating pattern: 2-5 blackouts/week, 30-90% depth (full discharge common during grid events)
- Hardware: CyberPower UT850EG, USB via NUT usbhid-ups driver, battery at ~35°C constant

---

*Feature research for: UPS Battery Monitor v3.0 (Active Battery Care)*
*Researched: 2026-03-17*
*Status: Ready for phase planning*
