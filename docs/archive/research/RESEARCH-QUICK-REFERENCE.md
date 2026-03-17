# Quick Reference: What Each Manufacturer Exposes vs What's Estimable

## At a Glance

| What | Budget (CyberPower UT850) | Enterprise (APC/Eaton) | Estimable for CyberPower? |
|-----|--------------------------|----------------------|--------------------------|
| Charge/runtime % | ✗ Unreliable firmware | ✗ Still unreliable | ✓ YES (via voltage LUT) |
| Battery voltage | ✓ Direct measure | ✓ Direct measure | ✓ Already have it |
| Install date | ✗ Not exposed | ✓ SNMP battery.date | ✓ YES (store at startup) |
| Replacement flag | ✗ None | ✓ APC/Eaton SNMP | ✓ YES (track SoH) |
| Test date+result | ✗ Not exposed | ✓ CyberPower (PR only) | ✓ YES (self-test events) |
| Transfer count (OL→OB) | ✗ None | ✓ Eaton (some models) | ✓ YES (count systemd events) |
| Cycle count | ✗ None | ✓ APC (via API) | ✓ YES (count discharge cycles) |
| Internal impedance (mΩ) | ✗ None | ✓ APC (API only) | ✓ YES (estimate from dV/dI) |
| Per-cell voltage | ✗ None | ✓ APC Li-ion only | ✗ Can't (VRLA doesn't expose) |
| **Net:** | 2 metrics | 8+ metrics | **Can build 7 of 9!** |

---

## What Makes Enterprise UPS Different

### 1. **They Track More State**
- **Installation date** (firmware memory) → age-based replacement warnings
- **Test date + result** (self-diagnostic logs) → health verification
- **Transfer count** (event counter) → grid stability analysis
- **Cycle count** (some models only) → battery lifetime estimation

### 2. **They Expose Diagnostics**
- APC: SNMP MIB with 100+ OIDs including battery.replace indicator
- Eaton: Cellwatch service + predictive analytics (proprietary)
- Vertiv: LIEBERT-GP MIB with environmental thresholds
- **CyberPower:** Only basic metrics + newly added (test.date/result)

### 3. **They Estimate Smarter**
- **Battery replace flag** = age-based + impedance-based heuristic
- **Remaining runtime** = load-dependent Peukert formula (not simple LUT)
- Still unreliable → why enterprise customers use additional monitoring

### 4. **They Cost 5–10x More**
- Smart-UPS 3000: ~$3,000–5,000
- CyberPower UT850EG: ~$200–300
- Marginal value of extra metrics: mostly diagnostics you can estimate yourself

---

## Why Standard NUT Variables Are Unreliable

**The Core Problem:**
- `battery.charge` is a **firmware coulomb counter** that drifts over time (charge cycles, temperature changes, aging)
- `battery.runtime` is calculated from `charge%` × `nominal_runtime` ÷ `current_load` — cascades the coulomb-counter error
- Different manufacturers use different baseline assumptions → APC might say 50 min, CyberPower 22 min for same battery

**Example (Real Data from 2026-03-12 blackout):**
- CyberPower UT850EG actual runtime: **47 minutes** (16–18% load)
- NUT reported: `battery.charge` → 0% at 35 min, `battery.runtime` → 22 min initial
- Why: Firmware baseline assumes 100% load; at 16% load, actual capacity is 2.8x higher

**Enterprise UPS don't solve this perfectly either** — but they expose:
1. Actual voltage (you build your own LUT)
2. Temperature (adjust for chemistry-specific effects)
3. Test results (verify battery is healthy before relying on estimates)

---

## Metrics You MUST Estimate for CyberPower

### Tier 1: Essential (for accurate shutdown)
1. **SoC (State of Charge)** from voltage → LUT
2. **Runtime Remaining** → Peukert + SoC + SoH

### Tier 2: Useful (for maintenance alerts)
3. **SoH (State of Health)** → degradation tracking
4. **Battery Age** → store at startup
5. **Cycle Count** → count OB→OL events

### Tier 3: Nice-to-have (informational)
6. **Internal Impedance** → optional (from voltage slope)
7. **Predicted RUL** → age + SoH combined
8. **Transfer Reason** → log input failures

---

## Why Enterprise Metrics Still Don't Guarantee Accuracy

**Real Issue from Field:**
- Schneider Electric forum (2025-01-15): User reported Eaton 9PX **failed battery not detected** by SNMP `battery.replace` flag
- APC has similar complaints: replace flag triggers false positives (age-based) or misses real degradation
- Lesson: **Firmware heuristics are black-box guesses too**

**Your advantage with custom estimation:**
- You control the model (Peukert exponent, SoH calculation)
- You validate against real discharge data (your LUT from calibration)
- You can tune for CyberPower UT850's specific chemistry + circuit

---

## For Implementation: Pick Your Data Sources

| Source | What You Get | Update Frequency | Storage |
|--------|-------------|-----------------|---------|
| **NUT `upsc`** | V, load, status, charge% | Every 5–10 sec | RAM (EMA ring buffer) |
| **Calibration Run** | Voltage → SoC LUT | Once at startup (or yearly) | `ups_state.json` |
| **systemd Journal** | OB→OL transitions, test events | Real-time | `/var/log/journal` (rotated) |
| **Self-Tracking** | Age, cycle count, SoH history | Per discharge cycle | `ups_state.json` |
| **Optional: SNMP** | If RMCARD400 installed later | Every 60 sec | Monitor system (Grafana/Prometheus) |

---

## Final Answer: What Should You Monitor?

**For CyberPower UT850EG, you need:**

```python
# Direct from NUT (reliable)
battery_voltage_v = upsc("cyberpower", "battery.voltage")
ups_load_percent = upsc("cyberpower", "ups.load")
ups_status = upsc("cyberpower", "ups.status")
battery_temp_c = upsc("cyberpower", "battery.temperature")  # if available

# Estimate from LUT
soc_percent = voltage_to_soc(battery_voltage_v)  # via LUT built at calibration

# Estimate from discharge history
soh_percent = estimate_soh_from_discharge_curve()  # degradation tracking
runtime_minutes = estimate_runtime_peukert(soc_percent, soh_percent, ups_load_percent)
cycle_count = count_discharge_events_in_journal()
battery_age_days = (now - install_date).days

# Optional (if available)
test_date = upsc("cyberpower", "ups.test.date")  # NUT 2.8.1+, CyberPower PR series
test_result = upsc("cyberpower", "ups.test.result")  # same
transfer_reason = upsc("cyberpower", "input.transfer.reason")  # same
```

**This gives you 9 useful metrics, none of which require enterprise UPS hardware.**

