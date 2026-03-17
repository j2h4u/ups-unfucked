# Glossary

Terms used in the codebase and documentation, with domain context.

---

## Battery & Electrochemistry

### SoC — State of Charge
How much energy remains in the battery right now, from 0% to 100%.

Analogy: fuel gauge. Full tank = SoC 100%, empty = 0%.

Problem: CyberPower UPS can't measure SoC accurately. It reports `battery.charge`, but the value jumps around and doesn't account for load. So we **compute SoC ourselves** from battery voltage via a LUT.

### SoH — State of Health
How much the battery has degraded compared to new. New battery = SoH 1.0 (100%), worn = 0.8 (80%).

Analogy: engine wear. A new engine delivers full power; after 100k km — only 80%. Same tank, but fuel economy is worse.

SoH affects runtime: at SoH=0.8, the battery delivers only 80% of its nominal runtime.

Calculated after every qualifying discharge (≥300s, ΔSoC ≥5%) using **capacity-based method**: coulomb counting measures Ah delivered during discharge, LUT-based ΔSoC extrapolates to full-discharge capacity, and `SoH = measured_capacity / rated_capacity`. The update uses **Bayesian prior-posterior blending** weighted by ΔSoC depth — shallow events (small ΔSoC) barely change SoH, while deep discharges carry full weight.

### VRLA — Valve-Regulated Lead-Acid
Sealed, maintenance-free lead-acid battery type. Found in most consumer UPS units, including CyberPower UT850EG.

Key VRLA properties that affect the code:
- **Non-linear discharge**: capacity drops disproportionately at high loads (→ Peukert's law)
- **Cliff region**: voltage drops sharply in the 11.0–10.5V range (→ cliff region interpolation)
- **Cutoff voltage**: discharging below 10.5V causes irreversible damage (→ anchor voltage)

### Anchor Voltage
10.5V — the physical discharge limit of a 12V VRLA battery. Below this threshold, plate sulfation (irreversible damage) begins.

In code: a LUT point with `soc=0.0, source='anchor'`. All calculations stop at this threshold.

### Cliff Region
The 11.0–10.5V range where VRLA battery voltage drops **sharply and non-linearly**. The last 15–20% of charge drains in a couple of minutes.

Analogy: a mountain slope. Most of the discharge is a gentle descent (13.4V → 11.0V). Then — a cliff. Without knowing the cliff shape, runtime prediction will be wildly wrong.

The daemon measures the cliff shape automatically during deep discharges (long blackouts or `test.battery.start.deep`).

---

## Electrical Quantities

### Battery Voltage (battery.voltage)
Instantaneous voltage at the battery terminals in volts. For a 12V VRLA: from ~13.5V (fully charged on float) to ~10.5V (fully discharged).

**Important**: voltage depends on two things simultaneously:
1. **How much charge remains** (SoC) — what we want to know
2. **Current load** — interferes with the measurement

This is exactly why IR compensation is needed.

### Load (ups.load)
What percentage of the UPS's maximum power the equipment is consuming. Our server typically draws 15–18% of 425W ≈ 64–77W.

### I_rated (Rated Current)
The current at which the battery's nominal capacity is rated. For VRLA, the standard is the **C/20 rate**: capacity divided by 20 hours.

Example: 7.2Ah battery → I_rated = 7.2/20 = 0.36A. At this current, it delivers exactly 7.2Ah over 20 hours. At higher currents, it delivers **less** (Peukert effect).

### I_actual (Actual Current)
Real discharge current, computed from load: `I = load% / 100 × nominal_power / nominal_voltage`.

Example: 17% × 425W / 12V = 6.0A — 17× higher than I_rated, so the battery delivers far less than its nominal 7.2Ah.

---

## Peukert's Law

### Overview
A law describing how **lead-acid battery capacity decreases as discharge current increases**.

Formula: `T = T_rated × (I_rated / I_actual)^n`

Where:
- `T_rated` = 20 hours (at C/20 rate)
- `I_rated` = 0.36A (for a 7.2Ah battery)
- `I_actual` = actual current (depends on load)
- `n` = Peukert exponent

### Peukert Exponent
A number from 1.0 to ~1.4 characterizing battery "quality":
- **1.0** = ideal battery, capacity independent of current
- **1.2** = typical VRLA (our code default)
- **1.4** = old/cheap battery, significant capacity loss under load

Auto-calibration: after every blackout, the code compares predicted vs actual runtime. If the error exceeds 10%, it computes a new exponent and saves it to `model.json`.

---

## Smoothing & Compensation

### EMA — Exponential Moving Average
A method for smoothing noisy data. Each new value is "blended" into the previous one with weight α.

Analogy: a thermometer with inertia. If temperature jumps from 20° to 25°, the EMA won't show 25° immediately — first 20.4°, then 20.7°, then 21.0°...

Why: battery voltage jitters ±0.1V from sample to sample (ADC noise, momentary load spikes). Without smoothing, SoC would bounce 95%→98%→93%→97%.

### EMA Buffer
A ring buffer that:
1. Stores the last N voltage and load samples
2. Computes EMA for each (separately for voltage and load)
3. Tracks **stabilization** — whether enough data has accumulated for reliable predictions

### EMA Stabilization
The EMA needs time to "warm up" — initial values are unreliable because the average hasn't settled.

Threshold: **12 samples** (≈2 minutes at 10-second poll interval). Until stabilized, IR compensation and SoC calculation are disabled (logs show `stabilized=False`, `V_norm=N/A`).

Why 12, not 3: at α≈0.08 (window=120s, poll=10s), at least 12 samples are needed for the EMA to reach 63% of the true value (one time constant τ).

### Alpha (α) — Smoothing Coefficient
Weight of the new sample in the EMA. Formula: `α = 1 - exp(-Δt / τ)`, where Δt = poll interval, τ = smoothing window.

With our settings (Δt=10s, τ=120s): **α ≈ 0.08**. This means each new sample contributes 8% to the result, while 92% is inertia from previous values.

### IR Compensation (Internal Resistance Compensation)
Corrects battery voltage for load effects.

**Problem**: under high load, terminal voltage drops due to internal resistance — even if charge hasn't changed. Without correction, the code "thinks" the battery is discharged when only the load increased.

**Formula**: `V_norm = V_ema + k × (L_ema - L_base)`

- `V_ema` — smoothed voltage (V)
- `L_ema` — smoothed load (%)
- `L_base` — reference load (20% default)
- `k` — IR coefficient (0.015 V/% default)

Example: at 40% load (20% above baseline), compensation adds 0.015×20 = 0.3V to measured voltage. This normalizes voltage to what it would be at 20% load.

### V_norm (Normalized Voltage)
Battery voltage after IR compensation. Used for SoC lookup in the LUT. In logs: `V_norm=13.42V`.

---

## Battery Model (model.json)

### LUT — Lookup Table
A voltage→SoC mapping table. Given normalized voltage, find how much charge remains.

Example entry: `{v: 12.4, soc: 0.64, source: "standard"}` — at 12.4V the battery is 64% charged.

Entry types by `source` field:
- `standard` — standard VRLA curve from datasheets
- `measured` — real measurement during discharge
- `interpolated` — gap-filling between measured points
- `anchor` — physical limit (10.5V = 0%)

### Physics Section
Battery and UPS physical parameters stored in `model.json`:
- `peukert_exponent` — Peukert exponent (auto-calibrated)
- `nominal_voltage` — battery nominal voltage (12V)
- `nominal_power_watts` — UPS nominal power (425W)
- `ir_compensation.k_volts_per_percent` — IR coefficient
- `ir_compensation.reference_load_percent` — baseline load

### SoH History
Array of `{date, soh}` entries — battery degradation history. Each blackout adds a point. Linear regression on these points predicts the replacement date.

---

## Daemon Architecture

### NUT — Network UPS Tools
Standard Linux software for UPS management. Consists of:
- `nut-server` (upsd) — daemon communicating with the UPS via USB
- `nut-monitor` (upsmon) — monitors status, can initiate shutdown
- `upsc` — CLI for reading metrics: `upsc cyberpower@localhost`

Our daemon **does not replace** NUT — it reads data from it and computes its own metrics (SoC, SoH, runtime) that are more accurate than the built-in ones.

### Virtual UPS
A file on tmpfs (`/dev/shm/ups-virtual.dev`) where the daemon writes computed metrics in NUT format. A second NUT instance (`dummy-ups`) reads this file and serves the data as if it were a real UPS.

Why: `upsmon` can trigger shutdown when `battery.runtime < threshold`. But CyberPower's native metrics lie. The virtual UPS substitutes them with computed values.

### Event Classifier
Determines what's currently happening with the UPS:
- **ONLINE** — on mains power, normal operation
- **BLACKOUT_REAL** — actual power outage
- **BLACKOUT_TEST** — battery test discharge (`test.battery.start.deep`)

Distinguishes real from test by `input.voltage`: during a test, the UPS disconnects its inverter, but input voltage remains >0.

### Discharge Buffer
Accumulates (voltage, time) pairs while running on battery (OB). On power restoration (OB→OL), the data is used for:
1. SoH calculation (area under curve)
2. Peukert exponent auto-calibration
3. In calibration mode — writing points to the LUT

---

## Alerts & Prediction

### Replacement Predictor
Linear regression on SoH history → date when SoH will drop below threshold (80% default).

### Alert Thresholds
- **SoH < 80%** → alert "battery degraded, plan replacement"
- **Runtime at 100% < 20 min** → alert "battery can't sustain the load"

Alerts are written via structured logging to journald, where Grafana Alloy scrapes them.

---

## NUT-Specific Terms

| Term | Meaning |
|------|---------|
| `OL` | Online — on mains power |
| `OB` | On Battery — running on battery |
| `LB` | Low Battery — low charge, shutdown imminent |
| `DISCHRG` | Discharging — battery is discharging |
| `ups.status` | Status string, combination of flags: `OL`, `OB DISCHRG`, `OB DISCHRG LB` |
| `battery.voltage` | Battery voltage (V) |
| `battery.charge` | Charge according to UPS (%, often wrong) |
| `battery.runtime` | Runtime according to UPS (sec, often wrong) |
| `ups.load` | UPS load (% of nominal) |
| `input.voltage` | UPS input voltage (V, 220–230 normal) |
| `upsc` | NUT CLI utility for reading variables |
| `upscmd` | NUT CLI utility for sending commands (`test.battery.start.deep`) |
