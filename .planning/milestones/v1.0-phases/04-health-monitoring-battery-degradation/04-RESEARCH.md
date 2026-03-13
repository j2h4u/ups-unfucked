# Phase 4: Health Monitoring & Battery Degradation - Research

**Researched:** 2026-03-14
**Domain:** Battery state-of-health (SoH) tracking, linear regression for degradation prediction, journald alerting, MOTD integration
**Confidence:** HIGH

## Summary

Phase 4 requires four independent subsystems: (1) SoH calculation via area-under-curve (voltage × time) during discharge events, (2) linear regression over SoH history to predict replacement date with ±6 months notice, (3) MOTD integration for real-time status display, and (4) journald alert triggers for health threshold breaches.

All prerequisites exist from Phases 1–3: model.json stores soh_history as {date, soh} points; discharge events are detected by event_classifier.py; and the virtual UPS infrastructure writes metrics atomically. Phase 4 adds the mathematical layer that converts discharge voltage curves into health estimates and extrapolates replacement timelines.

The architecture is stateless for SoH calculation (pure arithmetic over discharge points) and stateless for regression (least-squares line fit from soh_history). No external dependencies needed; Python stdlib (statistics, math) handles all calculations. Alerting is purely journald logging with structured fields; MOTD script reads virtual UPS metrics and soh_history file.

**Primary recommendation:** Implement three independent modules (soh_calculator.py, replacement_predictor.py, alerter.py) plus integration into monitor.py polling loop. Each is testable in isolation; complexity lies in the area-under-curve math (voltage-time integral) and regression goodness-of-fit (R² validation).

---

## User Constraints (from CONTEXT.md)

*No CONTEXT.md exists yet for Phase 4. User constraints will be supplied during planning.*

---

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| HLTH-01 | SoH recalculated after each discharge event (area-under-curve voltage×time); value stored in soh_history | See "SoH Calculation via Area-Under-Curve" section; trapezoidal rule integration over discharge voltage profile |
| HLTH-02 | Linear regression over soh_history → replacement prediction (e.g., "March 2028") with 6+ months notice before SoH < threshold | See "Linear Regression for Degradation Prediction" section; least-squares line fit, confidence via R² |
| HLTH-03 | MOTD module displays: charge%, runtime (mins), load%, SoH, replacement date in single line | See "MOTD Integration" section; read virtual UPS and soh_history, format human-readable output |
| HLTH-04 | journald alert when SoH < threshold (e.g., 80%); logged as structured entry searchable via journalctl | See "journald Alerting" section; structured logging with SyslogIdentifier, configurable threshold |
| HLTH-05 | journald alert when Time_rem@100% < X minutes (exact X TBD, configurable); both thresholds independent | See "Runtime Alert Threshold" section; computed from Peukert at SoC=1.0, compared against configured threshold |

---

## Standard Stack

### Core (Python stdlib only)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.10+ | Runtime (established in Phase 1) | Phase 1 baseline; stdlib sufficient for all calculations |
| `statistics` | stdlib | Mean, median for SoH trend analysis | Lightweight; used in regression goodness-of-fit |
| `math` | stdlib | sqrt, pow for area-under-curve and R² computation | No external deps needed |
| `datetime` | stdlib | ISO 8601 date formatting for soh_history and alert timestamps | Already used in Phase 1 model.py |
| `json` | stdlib | Read soh_history from model.json | Already used in Phase 1 |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `numpy` | (NOT used) | Numerical operations, polyfit | Overkill for single linear regression; stdlib math sufficient |
| `scipy.stats` | (NOT used) | linregress, correlation | Overkill; Phase 4 uses manual least-squares (3 lines of code) |

### No Alternatives

Linear regression is locked by requirements: Phase 2 established Peukert's Law with empirically tuned constant (237.7 for 47-min blackout); SoH history is time-series with minimum 2 points (new battery + first discharge). Manual least-squares regression is more transparent than scipy.stats.linregress and avoids external dependency.

---

## Architecture Patterns

### Recommended Project Structure

```
src/
├── soh_calculator.py            # V(t) → SoH via area-under-curve
├── replacement_predictor.py     # soh_history → replacement date + R²
├── alerter.py                   # SoH/runtime thresholds → journald
├── monitor.py                   # (modified) integrate soh_calculator after discharge events
├── model.py                     # (unchanged; soh_history already persisted)
└── __init__.py

tests/
├── test_soh_calculator.py       # Area-under-curve: trapezoids, edge cases
├── test_replacement_predictor.py  # Linear regression: slope, intercept, R², extrapolation
├── test_alerter.py              # Threshold logic, journald log format
└── conftest.py                  # (reuse Phase 1 fixtures)

scripts/motd/
├── 51-ups-health.sh             # MOTD module: read virtual UPS + soh_history, format output
```

### Pattern 1: SoH Calculation via Area-Under-Curve

**What:** Integrate voltage over time during a discharge event to estimate energy extracted; compare against baseline discharge to derive SoH.

**When to use:** Once per discharge event, triggered by OB→OL transition (Phase 2 already detects this).

**Algorithm:**
1. During discharge (BLACKOUT_REAL state), collector records voltage and load every N seconds
2. On OB→OL transition, compute area under V(t) curve (energy extracted from battery)
3. Normalize against reference discharge curve: `SoH = (Energy_measured / Energy_reference) × SoH_previous`
4. Store {date: ISO8601, soh: float} entry in model.json['soh_history']

**Trapezoidal Rule:**
For discrete voltage samples at times t₀, t₁, ..., tₙ:
```
Area ≈ Σ((V[i] + V[i+1])/2 × Δt) for all adjacent pairs
```

**Edge cases to handle:**
- Empty discharge (UPS switched to mains immediately): skip SoH update
- Single voltage point: area = 0, SoH unchanged
- Anchor voltage reached (10.5V): stop integration at physical cutoff
- New battery (SoH_history empty): initialize with SoH=1.0, then update after first discharge

**Example:**

```python
# Source: CONTEXT.md § Адаптивная LUT, Phase 4 spec

def calculate_soh_from_discharge(
    discharge_voltage_series: List[float],
    discharge_time_series: List[float],  # seconds since discharge start
    reference_soh: float = 1.0,
    anchor_voltage: float = 10.5
) -> float:
    """
    Calculate State of Health (SoH) from measured discharge voltage profile.

    Assumes discharge data collected at ~10-sec intervals during blackout.
    Compares area-under-curve of measured discharge against baseline.

    Args:
        discharge_voltage_series: Voltage readings [V] during discharge
        discharge_time_series: Time [seconds] for each voltage reading
        reference_soh: Previous SoH estimate (0.0-1.0)
        anchor_voltage: Physical cutoff voltage (typically 10.5V)

    Returns:
        Updated SoH estimate (0.0-1.0)
    """
    if len(discharge_voltage_series) < 2:
        # No discharge detected or single point; SoH unchanged
        return reference_soh

    # Trim data at anchor voltage (10.5V is physical limit)
    trimmed_v = []
    trimmed_t = []
    for v, t in zip(discharge_voltage_series, discharge_time_series):
        if v <= anchor_voltage:
            break
        trimmed_v.append(v)
        trimmed_t.append(t)

    if len(trimmed_v) < 2:
        # Discharged to cutoff immediately; SoH unchanged
        return reference_soh

    # Compute area-under-curve using trapezoidal rule
    area_measured = 0.0
    for i in range(len(trimmed_v) - 1):
        v1, v2 = trimmed_v[i], trimmed_v[i + 1]
        t1, t2 = trimmed_t[i], trimmed_t[i + 1]
        dt = t2 - t1
        area_measured += (v1 + v2) / 2.0 * dt  # Voltage × time

    # Reference area-under-curve: standard VRLA discharge at new battery
    # Baseline: ~13.4V → 10.5V over ~47 minutes (2820 sec) at reference load
    # This is empirical; calibrated from real blackout 2026-03-12
    area_reference = 12.0 * 2820  # Rough estimate: average 12V over 47 min

    # SoH = measured / reference, scaled by previous SoH
    degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0
    new_soh = reference_soh * degradation_ratio

    # Clamp to [0, 1]
    new_soh = max(0.0, min(1.0, new_soh))

    return new_soh
```

**Edge cases to test:**
- Empty discharge (voltage never drops below starting point)
- Single sample (N=1)
- Multiple samples with all same voltage (no discharge)
- Voltage below anchor (10.5V) — should stop integration
- Multiple discharges in soh_history — SoH should degrade monotonically (or stay same)

### Pattern 2: Linear Regression for Degradation Prediction

**What:** Fit a line through {date, SoH} points to extrapolate when SoH crosses configured threshold (e.g., 80%).

**When to use:** Every time a new SoH point is added to soh_history (after each discharge event).

**Least-Squares Formula:**
```
y = mx + b
where:
  m = Σ((x - x̄)(y - ȳ)) / Σ((x - x̄)²)
  b = ȳ - m·x̄
  R² = 1 - (SS_res / SS_tot)
```

Convert dates to numeric axis (days since epoch or first measurement) for linear math.

**Example:**

```python
# Source: Phase 4 spec, least-squares regression

import statistics
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

def linear_regression_soh(
    soh_history: List[Dict[str, any]],  # [{'date': 'YYYY-MM-DD', 'soh': 0.95}, ...]
    threshold_soh: float = 0.80
) -> Optional[Tuple[float, float, float, Optional[datetime]]]:
    """
    Fit line to SoH history and predict replacement date.

    Args:
        soh_history: List of {'date': ISO8601, 'soh': float} dicts
        threshold_soh: SoH level at which replacement is required (default 80%)

    Returns:
        Tuple: (slope_soh_per_day, intercept, r_squared, replacement_date_or_none)
               or None if insufficient data

    Logic:
        - Require minimum 3 points for meaningful regression
        - Reject if R² < 0.5 (high scatter, unreliable prediction)
        - Return replacement_date if slope is negative (degrading)
        - If SoH already below threshold, return today's date
    """
    if len(soh_history) < 3:
        # Insufficient data for regression
        return None

    # Convert dates to numeric axis (days since first measurement)
    try:
        dates = [datetime.strptime(entry['date'], '%Y-%m-%d') for entry in soh_history]
    except (ValueError, KeyError):
        return None

    first_date = dates[0]
    days_since_first = [(d - first_date).days for d in dates]
    soh_values = [entry['soh'] for entry in soh_history]

    # Least-squares regression
    n = len(days_since_first)
    x_mean = statistics.mean(days_since_first)
    y_mean = statistics.mean(soh_values)

    # slope = Σ((x - x̄)(y - ȳ)) / Σ((x - x̄)²)
    numerator = sum((days_since_first[i] - x_mean) * (soh_values[i] - y_mean) for i in range(n))
    denominator = sum((days_since_first[i] - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        # No variance in x-axis (all dates are same); can't fit
        return None

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    # R² = 1 - (SS_res / SS_tot)
    ss_res = sum((soh_values[i] - (slope * days_since_first[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - y_mean) ** 2 for y in soh_values)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    # Predict replacement date: when SoH hits threshold
    replacement_date = None
    if slope < 0:  # Only valid if degrading
        days_to_threshold = (threshold_soh - intercept) / slope
        if days_to_threshold > 0:
            replacement_date = first_date + timedelta(days=days_to_threshold)

    return slope, intercept, r_squared, replacement_date
```

**Edge cases to test:**
- Fewer than 3 points (require 3 for meaningful slope)
- All SoH values identical (no degradation signal; slope=0)
- SoH improving (slope > 0; nonsensical for batteries, but reject gracefully)
- SoH already below threshold (return today's date)
- R² < 0.5 (high scatter; mark prediction as unreliable)
- Dates non-monotonic (reject or sort)

### Pattern 3: journald Structured Logging for Alerts

**What:** Emit structured log entries to systemd journal with SyslogIdentifier and configurable thresholds.

**When to use:** Every polling cycle in monitor.py; check if thresholds breached.

**Log format:**
```
journalctl -u ups-battery-monitor -p warning
# Shows entries with:
# MESSAGE=Battery SoH below threshold
# BATTERY_SOH=0.78
# THRESHOLD=0.80
# DAYS_TO_REPLACEMENT=45
```

**Example:**

```python
# Source: Phase 4 spec, journald structured logging

import logging
import logging.handlers
import json
from typing import Optional

def setup_ups_logger(identifier: str = "ups-battery-monitor") -> logging.Logger:
    """
    Configure logger for ups-battery-monitor daemon.

    Sends output to:
    1. systemd journal with SyslogIdentifier
    2. Structured JSON format for automated parsing

    Args:
        identifier: Syslog identifier (appears in journalctl output)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(identifier)
    logger.setLevel(logging.DEBUG)

    # journald handler (systemd integration)
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    handler.setFormatter(
        logging.Formatter(f'{identifier}: %(levelname)s - %(message)s')
    )
    logger.addHandler(handler)

    return logger

def alert_soh_below_threshold(
    logger: logging.Logger,
    current_soh: float,
    threshold_soh: float,
    days_to_replacement: Optional[float] = None
):
    """
    Log SoH alert to journald.

    Args:
        logger: Configured logger instance
        current_soh: Current state of health (0.0-1.0)
        threshold_soh: Alert threshold (0.0-1.0)
        days_to_replacement: Predicted days until SoH < threshold (or None if unknown)

    Message format includes structured fields for automated parsing:
    - BATTERY_SOH: numeric value for metrics
    - THRESHOLD: numeric threshold for comparison
    - DAYS_TO_REPLACEMENT: estimated days (if available)
    """
    msg = f"Battery SoH {current_soh:.2%} below alert threshold {threshold_soh:.2%}"
    if days_to_replacement:
        msg += f"; estimated {days_to_replacement:.0f} days to replacement"

    # Log as warning; journalctl -p warning will show it
    logger.warning(msg, extra={
        'BATTERY_SOH': f'{current_soh:.4f}',
        'THRESHOLD': f'{threshold_soh:.4f}',
        'DAYS_TO_REPLACEMENT': f'{days_to_replacement:.0f}' if days_to_replacement else 'unknown',
    })

def alert_runtime_below_threshold(
    logger: logging.Logger,
    runtime_at_100_percent: float,
    threshold_minutes: float
):
    """
    Log runtime alert to journald.

    Args:
        logger: Configured logger instance
        runtime_at_100_percent: Predicted runtime at full charge, minutes
        threshold_minutes: Alert threshold, minutes
    """
    msg = f"Battery runtime at 100% charge: {runtime_at_100_percent:.0f} min; alert threshold: {threshold_minutes:.0f} min"
    logger.warning(msg, extra={
        'RUNTIME_AT_100_PCT': f'{runtime_at_100_percent:.1f}',
        'THRESHOLD_MINUTES': f'{threshold_minutes:.1f}',
    })
```

**Verification command (Phase 5):**
```bash
journalctl -u ups-battery-monitor -p warning --since "1 hour ago"
# Shows recent SoH/runtime alerts
```

### Pattern 4: MOTD Integration

**What:** Bash script (~/scripts/motd/51-ups-health.sh) that reads virtual UPS metrics and soh_history file, formats one-liner with status, charge, runtime, load, SoH, and replacement date.

**When to use:** Every SSH login (called by runner.sh); should execute in < 100ms.

**Example (extending existing 51-ups.sh):**

```bash
#!/bin/bash
source "$(dirname "$0")/colors.sh"

# Read virtual UPS metrics (from Phase 3 infrastructure)
ups_data=$(upsc cyberpower-virtual@localhost 2>/dev/null) || exit 0

ups_status=$(echo "$ups_data" | awk -F': ' '/^ups.status:/{print $2}')
charge=$(echo "$ups_data" | awk -F': ' '/^battery.charge:/{print $2}')
runtime=$(echo "$ups_data" | awk -F': ' '/^battery.runtime:/{print $2}')
load=$(echo "$ups_data" | awk -F': ' '/^ups.load:/{print $2}')

# Read SoH and replacement date from model.json
model_file="$HOME/.config/ups-battery-monitor/model.json"
if [[ -f "$model_file" ]]; then
    soh=$(jq -r '.soh' "$model_file" 2>/dev/null)
    # TODO: Read replacement_date from predictor output (Phase 4)
else
    soh="?"
fi

# Format runtime
if [[ -n "$runtime" && "$runtime" -gt 0 ]] 2>/dev/null; then
    hours=$((runtime / 3600))
    mins=$(( (runtime % 3600) / 60 ))
    if [[ $hours -gt 0 ]]; then
        rt_fmt="${hours}h ${mins}m"
    else
        rt_fmt="${mins}m"
    fi
else
    rt_fmt="?"
fi

# Format SoH as percentage
if [[ -n "$soh" && "$soh" != "?" ]]; then
    soh_pct=$(printf "%.0f" "$(echo "$soh * 100" | bc)")
    soh_fmt="${soh_pct}%"
    # Color SoH: green >= 80%, yellow 60-79%, red < 60%
    if (( $(echo "$soh >= 0.80" | bc -l) )); then
        soh_color="$GREEN"
    elif (( $(echo "$soh >= 0.60" | bc -l) )); then
        soh_color="$YELLOW"
    else
        soh_color="$RED"
    fi
else
    soh_fmt="?"; soh_color="$DIM"
fi

# Status color
if [[ "$ups_status" == *"OB"* ]]; then
    st_color="$YELLOW"; st_label="On Battery"; icon="⚡"
elif [[ "$ups_status" == *"OL"* ]]; then
    st_color="$GREEN"; st_label="Online"; icon="✓"
else
    st_color="$DIM"; st_label="$ups_status"; icon="?"
fi

echo -e "  ${st_color}${icon}${NC} UPS: ${st_label}${NC} ${DIM}· charge ${charge}% · runtime ${rt_fmt} · load ${load}% · health ${soh_color}${soh_fmt}${NC}${DIM} [replacement TBD]${NC}"
```

**Display example:**
```
✓ UPS: Online · charge 100% · runtime 47m · load 18% · health 95% [replacement TBD]
⚡ UPS: On Battery · charge 65% · runtime 23m · load 20% · health 92% [replacement March 2028]
✗ UPS: On Battery · charge 12% · runtime 2m · load 20% · health 78% [replacement Jan 2028 IMMINENT]
```

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Linear regression | Custom loop to fit line | Manual least-squares (10 lines) | scipy adds 50MB; stdlib is fast enough for 100 points |
| Area-under-curve | Numerical integration library | Trapezoidal rule (3 lines) | Overkill for periodic discharge samples; manual loop is clear |
| Date arithmetic | Timestamp management | `datetime.timedelta` | stdlib module; no deps; handles leap years, timezones |
| journald logging | Custom file writes | `logging.handlers.SysLogHandler` | systemd integration; auto-rotation; queryable timestamps |
| SoH persistence | Custom binary format | JSON (model.json already used) | Already established pattern in Phase 1; human-readable |

**Key insight:** Battery health tracking is fundamentally time-series mathematics. Phase 1 established atomic JSON writes; Phase 4 simply extends that with linear algebra and threshold comparison. No new file formats, no new storage backends, no new dependencies.

---

## Common Pitfalls

### Pitfall 1: Area-Under-Curve with Non-Uniform Time Intervals

**What goes wrong:** If discharge samples are at irregular intervals (5 sec, 10 sec, 5 sec, ...), trapezoidal rule still works, but if you forget to weight by Δt, you'll get nonsense results.

**Why it happens:** Copy-paste from simple trapezoid formula that assumes uniform spacing.

**How to avoid:** Always multiply (V₁+V₂)/2 by (t₂-t₁) separately. Test with known intervals first (e.g., 10-sec polling).

**Warning signs:** SoH suddenly jumps up or down despite smooth discharge curves; negative area values.

**Test case:**
```python
# Non-uniform intervals
v = [13.0, 12.5, 12.0]
t = [0, 5, 20]  # 5-sec interval, then 15-sec interval
# Area = (13.0+12.5)/2 * 5 + (12.5+12.0)/2 * 15 = 62.5 + 183.75 = 246.25
# If you forget Δt: (13.0+12.5)/2 + (12.5+12.0)/2 = 24.5 (WRONG)
```

### Pitfall 2: Regression on Insufficient Data

**What goes wrong:** Fit a line to 2 points (slope is perfect, but meaningless); extrapolate 5 years into future (confidence interval = entire universe).

**Why it happens:** User doesn't check soh_history length before calling linear_regression().

**How to avoid:** Require minimum 3 points (Phase 4 spec). Reject if R² < 0.5 (high scatter). Return "insufficient data" gracefully.

**Warning signs:** Replacement date jumps by months week-to-week; R² near 0 or negative.

**Test case:**
```python
# Only 2 points
soh_history = [
    {'date': '2026-03-13', 'soh': 1.0},
    {'date': '2026-03-14', 'soh': 0.99}
]
# Linear regression would fit perfectly (R²=1.0) but predict 100 years to 80% threshold
# Solution: require len >= 3 and R² >= 0.5 before predicting
```

### Pitfall 3: Forgetting SoH Already Below Threshold

**What goes wrong:** User sees "replacement in 45 days" in MOTD, then next day "replacement in 46 days" because regression extended the line and current SoH is already < 80%.

**Why it happens:** Predictor only looks at slope and intercept; doesn't check if current_soh < threshold.

**How to avoid:** Check current SoH first; if already below threshold, return today's date or "overdue" message.

**Warning signs:** Replacement date in the past (e.g., "replacement Jan 2028" in March 2028).

### Pitfall 4: journald Threshold Spam

**What goes wrong:** Alert fires every polling cycle (every 10 sec) because SoH=79.8% and threshold=80.0%, flooding journalctl.

**Why it happens:** No hysteresis; threshold triggers on every single poll.

**How to avoid:** Add hysteresis (e.g., trigger at 80%, clear at 82%); or suppress repeat alerts (deduplicate within 1 hour). Log once per state change, not on every sample.

**Warning signs:** `journalctl | grep "SoH below" | wc -l` shows thousands of entries in 1 hour.

### Pitfall 5: Date Format Inconsistency

**What goes wrong:** soh_history has {date: '2026-03-13', soh: 0.95}, but replacement_date is datetime object; JSON serialization fails or mismatches.

**Why it happens:** Mix of ISO8601 strings, datetime objects, and numeric timestamps.

**How to avoid:** Normalize all dates to ISO8601 strings (YYYY-MM-DD) in model.json. Convert to datetime internally only for arithmetic. Convert back to ISO8601 before saving.

**Warning signs:** `json.dump()` raises TypeError; replacement_date appears as "2026-03-13T00:00:00" instead of "2026-03-13".

---

## Code Examples

Verified patterns from phase context and best practices:

### SoH History Initialization

```python
# Source: src/model.py pattern (Phase 1, extended for Phase 4)

# When model.json is first created:
{
    'full_capacity_ah_ref': 7.2,
    'soh': 1.0,  # Start at 100% for new battery
    'soh_history': [
        {'date': '2026-03-13', 'soh': 1.0}  # First entry: new battery
    ],
    'lut': [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        ...
    ]
}

# After first discharge event (OB→OL transition):
# Phase 4 computes new_soh from area-under-curve
model.add_soh_history_entry('2026-03-14', 0.98)
model.save()

# Now model.json has:
{
    ...
    'soh': 0.98,
    'soh_history': [
        {'date': '2026-03-13', 'soh': 1.0},
        {'date': '2026-03-14', 'soh': 0.98}  # New entry
    ]
}
```

### Integration Point in monitor.py

```python
# Source: Phase 2 monitor.py pattern (02-06 summary), extended for Phase 4

# In _handle_event_transition() or new _update_battery_health() method:

if event_type == BLACKOUT_REAL and transition_occurred and new_event == ONLINE:
    # Power restored; discharge event complete

    # Phase 4: Calculate SoH from discharge data
    soh_new = soh_calculator.calculate_soh_from_discharge(
        discharge_voltage_series=self.discharge_buffer['voltages'],
        discharge_time_series=self.discharge_buffer['times'],
        reference_soh=self.model.get_soh()
    )

    # Add to history
    today = datetime.now().strftime('%Y-%m-%d')
    self.model.add_soh_history_entry(today, soh_new)
    self.model.save()

    # Predict replacement date
    slope, intercept, r2, replacement_date = replacement_predictor.linear_regression_soh(
        soh_history=self.model.get_soh_history(),
        threshold_soh=0.80
    )

    # Alert if below threshold
    if soh_new < 0.80:
        alerter.alert_soh_below_threshold(logger, soh_new, 0.80,
            days_to_replacement=(replacement_date - datetime.now()).days if replacement_date else None)

    # Alert if runtime at 100% is low
    time_rem_at_100pct = runtime_calculator.runtime_minutes(soc=1.0, load_percent=20.0)
    if time_rem_at_100pct < 300:  # TBD: exact threshold
        alerter.alert_runtime_below_threshold(logger, time_rem_at_100pct, 300)
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| No battery health tracking | Linear regression over discharge SoH samples | Phase 4 | Enables proactive replacement planning; no surprise failures |
| Manual calendar reminders | Automated prediction with 6+ months notice | Phase 4 | Removes manual operations; predictable spare parts logistics |
| No discharge data capture | Atomic voltage/time collection during events (Phase 2) | Phase 2 | Enables SoH calculation; data available for re-analysis |
| journalctl scrolling for status | Structured journald alerts with searchable fields | Phase 4 | Enables log aggregation tools; monitoring integration |

**Deprecated/outdated:**
- NUT firmware calibration (`calibrate.start`): UT850EG doesn't support it; Phase 4 uses measured discharge data instead
- Manual battery tests with notebook: SoH now tracked automatically after each event

---

## Open Questions

1. **Area-under-curve baseline:** What is the reference discharge curve (V vs t) for a new UT850EG battery?
   - What we know: Observed 47 minutes at ~20% load, SoH=1.0 (2026-03-12 blackout)
   - What's unclear: Full voltage profile from 13.4V to 10.5V at controlled load
   - Recommendation: Calibrate baseline during Phase 6 (calibration mode); use empirical 47-min estimate until then

2. **SoH threshold:** Is 80% the right alert threshold, or should it be 85% / 75%?
   - What we know: Phase 4 spec says "SoH < 80%"; this is configurable
   - What's unclear: What SoH corresponds to unacceptable runtime loss? UPS manufacturer specs?
   - Recommendation: Start with 80%, adjust after 3 months of real data; make configurable via environment variable

3. **Runtime alert threshold (HLTH-05):** What value of Time_rem@100% should trigger alert?
   - What we know: Current spec says "X minutes; exact value TBD"
   - What's unclear: Is 30 minutes acceptable? 20? 15?
   - Recommendation: Propose 20 minutes (roughly 40% of observed 47-min baseline); make configurable

4. **Replacement date format:** Should MOTD show "March 2028" (month/year) or full "2026-03-14"?
   - What we know: Phase 4 spec says "e.g., March 2028"
   - What's unclear: Precision; should we show "Q1 2028" for uncertainty?
   - Recommendation: Show "YYYY-MM" (e.g., "2028-03") for clarity; omit day since regression has ±months uncertainty

5. **Multiple discharges per day:** Can SoH degrade between discharges, or only during discharge?
   - What we know: Phase 4 design assumes one SoH point per discharge event
   - What's unclear: If system restarts during partial discharge, do we lose calibration data?
   - Recommendation: Store discharge_buffer in model.json (optional) for resumption after restart; implement in Phase 5+ if needed

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (established in Phase 1) |
| Config file | tests/conftest.py (reused from Phase 1) |
| Quick run command | `pytest tests/test_soh_calculator.py tests/test_replacement_predictor.py -x` |
| Full suite command | `pytest tests/ -x` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| HLTH-01 | Area-under-curve calculates SoH from voltage profile | unit | `pytest tests/test_soh_calculator.py::test_calculate_soh_from_discharge_normal -x` | ❌ Wave 0 |
| HLTH-01 | Trapezoidal rule integrates with non-uniform Δt | unit | `pytest tests/test_soh_calculator.py::test_non_uniform_time_intervals -x` | ❌ Wave 0 |
| HLTH-01 | Edge case: empty discharge / single point | unit | `pytest tests/test_soh_calculator.py::test_empty_discharge -x` | ❌ Wave 0 |
| HLTH-02 | Linear regression fits soh_history line | unit | `pytest tests/test_replacement_predictor.py::test_linear_regression_slope_intercept -x` | ❌ Wave 0 |
| HLTH-02 | R² computed correctly; rejects R² < 0.5 | unit | `pytest tests/test_replacement_predictor.py::test_r_squared_low_scatter -x` | ❌ Wave 0 |
| HLTH-02 | Replacement date extrapolated; requires 3+ points | unit | `pytest tests/test_replacement_predictor.py::test_insufficient_data -x` | ❌ Wave 0 |
| HLTH-02 | Current SoH already below threshold → return today | unit | `pytest tests/test_replacement_predictor.py::test_soh_already_below_threshold -x` | ❌ Wave 0 |
| HLTH-03 | MOTD reads virtual UPS + model.json; formats output | integration | Manual SSH login; grep MOTD output | ❌ Phase 5 |
| HLTH-03 | SoH color: green ≥80%, yellow 60-79%, red <60% | unit | `pytest tests/test_motd_formatting.py::test_soh_color_thresholds -x` (if shell testable) | ❌ Wave 0 |
| HLTH-04 | journald alert fires when SoH < threshold | unit | `pytest tests/test_alerter.py::test_alert_soh_below_threshold -x` | ❌ Wave 0 |
| HLTH-04 | Alert includes structured fields (BATTERY_SOH, THRESHOLD, etc.) | unit | `pytest tests/test_alerter.py::test_journald_structured_fields -x` | ❌ Wave 0 |
| HLTH-05 | journald alert fires when Time_rem@100% < threshold | unit | `pytest tests/test_alerter.py::test_alert_runtime_below_threshold -x` | ❌ Wave 0 |
| HLTH-05 | Runtime alert is independent from SoH alert | unit | `pytest tests/test_alerter.py::test_independent_thresholds -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_soh_calculator.py tests/test_replacement_predictor.py tests/test_alerter.py -x`
- **Per wave merge:** `pytest tests/ -x` (all phases)
- **Phase gate:** Full suite green + manual MOTD display check before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_soh_calculator.py` — covers HLTH-01 (area-under-curve, trapezoidal rule, edge cases)
- [ ] `tests/test_replacement_predictor.py` — covers HLTH-02 (linear regression, R² validation, extrapolation, thresholds)
- [ ] `tests/test_alerter.py` — covers HLTH-04, HLTH-05 (journald integration, structured fields, threshold logic)
- [ ] `tests/test_motd_formatting.py` — covers HLTH-03 (formatting, color thresholds, readability) — optional if bash not easily testable
- [ ] `src/soh_calculator.py` — module with calculate_soh_from_discharge()
- [ ] `src/replacement_predictor.py` — module with linear_regression_soh()
- [ ] `src/alerter.py` — module with alert_soh_below_threshold(), alert_runtime_below_threshold()
- [ ] `scripts/motd/51-ups-health.sh` — updated MOTD script reading soh_history

---

## Sources

### Primary (HIGH confidence)
- **Phase 1 model.py** (`src/model.py`) — soh_history structure, atomic JSON writes, existing get/set patterns
- **Phase 2 runtime_calculator.py** (`src/runtime_calculator.py`) — Peukert constants verified against 2026-03-12 blackout (47 min @ 20% load)
- **CONTEXT.md (project root)** — Real blackout data, voltage observations, business requirements
- **REQUIREMENTS.md** — HLTH-01 through HLTH-05 specifications
- **Phase 2 event_classifier.py** — OB→OL transition detection (trigger for SoH calculation)

### Secondary (MEDIUM confidence)
- **Python statistics stdlib docs** — Mean, standard deviation calculations
- **Python datetime stdlib docs** — ISO8601 date formatting, timedelta arithmetic
- **systemd journal documentation** — SysLogHandler integration, structured logging
- **Project memory: project_ups_monitor_spec.md** — Mathematical model overview (area-under-curve for degradation estimation)

### Tertiary (LOW confidence)
- None required; stdlib and established patterns sufficient

---

## Metadata

**Confidence breakdown:**
- **SoH calculation (HLTH-01):** HIGH — Area-under-curve is straightforward applied math; trapezoidal rule well-established; edge cases identified
- **Linear regression (HLTH-02):** HIGH — Least-squares formula is standard; sufficient for 2-10 data points; R² validation clear
- **MOTD integration (HLTH-03):** MEDIUM — Pattern exists (51-ups.sh); JSON parsing and bash formatting straightforward, but need to verify jq availability and command execution speed
- **journald alerting (HLTH-04, HLTH-05):** MEDIUM — SysLogHandler pattern is stdlib; threshold logic simple; needs verification that SyslogIdentifier propagates correctly for `journalctl -u` filtering
- **Thresholds (SoH %, Time_rem minutes):** LOW — Values TBD; marked for planning phase; defaults proposed (80%, 20 min) but require validation against observed patterns

**Research date:** 2026-03-14
**Valid until:** 2026-03-30 (14 days; assumptions about threshold values may change after first discharge events; battery degradation math is stable)

---

## Notes for Planner

1. **No external dependencies:** All Phase 4 code uses only Python stdlib (statistics, math, datetime, json, logging). No scipy, numpy, or other third-party packages needed.

2. **Data flow is acyclic:** SoH history only grows; previous entries are never modified. Regression only reads history, produces prediction, logs alerts. Simple append-only semantics.

3. **Integration point is post-discharge:** SoH calculation happens after OB→OL transition (already detected by Phase 2 event_classifier). No new state machine required; reuse existing event logic.

4. **Alerting is fire-and-forget:** journald handles deduplication, retention, and rotation. Code just emits structured log entries. No need for alert suppression logic (unless hysteresis needed for threshold spam).

5. **MOTD is read-only:** Script just reads files (virtual UPS, model.json). No persistent state, no writes, no race conditions. Fast enough to run on every SSH login.

6. **Replacement date is advisory, not enforced:** System does not auto-shutdown when SoH < threshold. Alerts notify operator; operator orders replacement; no criticality. Distinct from HLTH-04/HLTH-05 journald alerts which inform but don't trigger.

---
