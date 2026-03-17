# Architecture Patterns: v3.0 Active Battery Care

**Domain:** VRLA UPS battery management with intelligent desulfation scheduling
**Researched:** 2026-03-17

## Recommended Architecture

### System Overview

v3.0 transforms the daemon from passive observer (v2.0) to active battery manager. The core change: **daemon now owns test scheduling via intelligent sulfation detection**, replacing static systemd timers.

```
┌─────────────────────────────────────────────────────────────────┐
│ UPS Battery Monitor v3.0 (Single Python Process)                │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Main Event Loop (asyncio)                               │   │
│  │ ├─ Polling thread: upsc UT850 (every 10s)              │   │
│  │ ├─ Discharge detection: OL→OB state change              │   │
│  │ ├─ Scheduler: daily check for test need (apscheduler)  │   │
│  │ └─ Signal handler: clean shutdown on SIGTERM            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────┴──────────────────────────────────┐  │
│  │ v3.0 Sulfation Model                                     │  │
│  │ ├─ IR Trending (internal resistance from quick tests)    │  │
│  │ │  └─ Voltage sag ΔV/I = R_internal estimate            │  │
│  │ ├─ Recovery Delta Tracking (post-discharge voltage rise) │  │
│  │ │  └─ 30s post-discharge recovery indicates health       │  │
│  │ ├─ Cycle Count (OL→OB transitions)                       │  │
│  │ ├─ Temperature Compensation (35°C fallback or NUT HID)   │  │
│  │ └─ Sulfation Score (0-100) from hybrid indicators        │  │
│  └────────────────────────────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────┴──────────────────────────────────┐  │
│  │ v3.0 Intelligent Scheduler                               │  │
│  │ ├─ Decision Tree:                                        │  │
│  │ │  IF sulfation_score > 65                               │  │
│  │ │   AND soh > 50%                                        │  │
│  │ │   AND days_since_test > 7                              │  │
│  │ │   AND NOT recent_blackout_credit                       │  │
│  │ │  THEN schedule_test()                                  │  │
│  │ ├─ upscmd Integration (daemon calls NUT directly)        │  │
│  │ │  └─ upscmd UT850 test.battery.start                    │  │
│  │ └─ Logging to journald with decision reasons             │  │
│  └────────────────────────────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────┴──────────────────────────────────┐  │
│  │ v3.0 Cycle ROI Metric                                    │  │
│  │ ├─ Benefit = measured SoH delta post-test (%)            │  │
│  │ ├─ Cost = estimated capacity loss from discharge         │  │
│  │ └─ ROI = Benefit / Cost (export to health.json)          │  │
│  └────────────────────────────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────┴──────────────────────────────────┐  │
│  │ Persistence Layer                                        │  │
│  │ ├─ model.json (sulfation history, IR baseline, schedule) │  │
│  │ ├─ health.json (for Grafana: sulfation_score, roi, eta)  │  │
│  │ └─ journald (structured events: @fields.reason, delta)   │  │
│  └────────────────────────────────────────────────────────┘   │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Component Boundaries

| Component | Responsibility | Communicates With | Notes |
|-----------|---------------|-------------------|-------|
| **Polling Thread** | Read UPS metrics every 10s via `upsc` | NUT, Metrics Store | Non-blocking; isolated from scheduler |
| **Discharge Detector** | Identify OL→OB→OL cycles, collect samples | Polling Thread, Model Store | Uses StateChange pattern; writes discharge_buffer |
| **Sulfation Model** | Compute IR trend, recovery delta, score | Model Store, Metrics | Physics + data-driven; reads quick test results |
| **Intelligent Scheduler** | Decision logic: should we test today? | Sulfation Model, NUT (upscmd) | Runs daily; logs every decision to journald |
| **Test Executor** | Call `upscmd test.battery.start` | NUT, Discharge Detector | Spawns subprocess; error handling required |
| **ROI Calculator** | SoH delta / capacity loss | Sulfation Model, Metrics | Post-test analysis; exports to health.json |
| **Journald Writer** | Structured events: @fields.reason, eta | journald | Single writer, multiple readers (Grafana Alloy) |
| **Config Loader** | Parse v3_sulfation + v3_roi sections | File system | Defaults provided; missing sections OK |

## Data Flow

### Normal Polling (Every 10s)

```
UPS Hardware (CyberPower UT850EG)
    ↓ (USB via NUT driver)
upsc UT850@localhost
    ↓
Polling Thread (reads voltage, load, status)
    ↓
CurrentMetrics (dataclass)
    ↓
Discharge Detector (state_previous vs state_current)
    ├─ OL → OB: start_discharge_buffer()
    ├─ OB: accumulate current/voltage samples
    └─ OB → OL: process discharge_buffer → capacity estimate, SoH update
    ↓
Model Store (model.json update)
    ↓
MOTD Module + health.json + Grafana
```

### Daily Scheduling (Async, runs once per day)

```
apscheduler triggers daily at 08:00 (configurable)
    ↓
Intelligent Scheduler reads:
    ├─ Current SoH (from model.json)
    ├─ Sulfation Model state (IR history, recovery delta, score)
    ├─ Recent blackouts (check journald for OB→OL events)
    └─ Last test timestamp
    ↓
Decision Tree evaluation:
    IF (all conditions met) THEN:
        ├─ Call upscmd UT850 test.battery.start
        ├─ Log to journald: "scheduled_test reason=sulfation_score:72 soh:85% eta:+7d"
        └─ Update model.json: next_test_eta
    ELSE:
        └─ Log: "test_deferred reason=blackout_credit days_remaining:3"
    ↓
health.json updated: sulfation_score, next_test_reason, next_test_eta, roi_percent
    ↓
Grafana dashboard reflects new state
```

### Test Execution (When Triggered)

```
upscmd UT850 test.battery.start
    ↓ (NUT daemon spawns 10-second discharge)
Voltage measurement pre-test: V_start
    ├─ Current draw: I (measured via load estimation)
    └─ IR estimate: ΔV / I
    ↓
10-second test completes
    ↓
Voltage measurement post-test: V_min
    ↓
Recovery monitoring (30s passive wait)
    ↓
Voltage at T+30s: V_recovery
    ├─ Recovery delta = V_start - V_recovery
    └─ Store in model.json: recovery_history[]
    ↓
SoH re-measurement (via Peukert model)
    ↓
Journald: "test_complete soh_delta:+1.5% ir_delta:-5mv recovery_delta:120mv roi:11.1x"
```

## Patterns to Follow

### Pattern 1: Sulfation Detection (Hybrid Physics + Data-Driven)

**What:** Combine Shepherd discharge model (theoretical) with empirical IR trending (observed).

**When:** After every quick test (daily) or during long discharge events.

**Example:**

```python
# Baseline: predict voltage using Shepherd model
predicted_voltage = shepherd_model(capacity, depth, temperature)

# Actual: measure from hardware
actual_voltage = measure_battery_voltage()

# Deviation indicates sulfation (curves don't match)
curve_deviation = abs(predicted_voltage - actual_voltage)

# Trend IR over 5+ tests
ir_trend = compute_trend(ir_history[-5:])  # rising = sulfation

# Recovery delta (voltage recovery post-discharge)
recovery_rate = (v_pre_test - v_at_30s) / time_elapsed

# Composite score (0-100)
sulfation_score = (
    (curve_deviation / max_acceptable) * 40 +
    (ir_rise_percent / 30) * 35 +
    (1 - recovery_rate / expected_rate) * 25
)
```

### Pattern 2: Safe Scheduling (Conservative Thresholds)

**What:** Prevent unsafe discharge by enforcing minimum SoH and rate limits.

**When:** Before every test trigger.

**Example:**

```python
def should_schedule_test():
    sulfation_score = compute_sulfation_score()
    soh = compute_soh()
    days_since_test = (now() - last_test_time).days
    recent_blackout = has_recent_blackout(days=7, depth_percent=90)

    # All conditions must pass
    if not (
        sulfation_score >= config.sulfation_threshold and
        soh >= config.soh_floor and
        days_since_test >= config.min_interval_days and
        not recent_blackout and
        test_count_this_week < config.max_tests_per_week
    ):
        return False, "condition_not_met"

    return True, "all_conditions_met"
```

### Pattern 3: Cycle ROI (Benefit-Cost Analysis)

**What:** Quantify desulfation benefit (SoH recovery) vs. wear cost (capacity loss).

**When:** After every test, for analysis/logging.

**Example:**

```python
def compute_cycle_roi():
    # Measure benefit: SoH improvement post-test
    soh_pre_test = model.soh_history[-2]  # before test
    soh_post_test = model.soh_history[-1]  # after test
    benefit = soh_post_test - soh_pre_test  # % recovery

    # Estimate cost: capacity loss per discharge cycle
    # Physics model: each full discharge = ~0.15% capacity loss
    discharge_depth_percent = compute_discharge_depth()
    cost = 0.0015 * discharge_depth_percent

    # ROI
    roi = benefit / cost if cost > 0 else float('inf')

    # Logging
    journald_write(
        "test_roi",
        soh_delta_percent=benefit,
        capacity_loss_percent=cost,
        roi_ratio=roi
    )

    return roi
```

### Pattern 4: Natural Blackout Credit

**What:** If recent full discharge occurred, skip scheduled test (maintenance already done).

**When:** During scheduling decision.

**Example:**

```python
def has_blackout_credit():
    # Query journald for recent OB→OL transitions
    blackouts = journald_query(
        last_n_days=config.blackout_credit_defer_days,
        unit="ups-battery-monitor.service",
        filter="discharge_depth_percent >= 90"
    )

    if blackouts:
        days_until_credit_expires = (
            config.blackout_credit_defer_days -
            (now() - blackouts[0].timestamp).days
        )

        journald_write("scheduling_decision",
            reason="blackout_credit",
            days_remaining=days_until_credit_expires
        )
        return True

    return False
```

### Pattern 5: Intelligent Logging (Structured journald Events)

**What:** Every scheduling decision logged with fields (reason, score, ETA).

**When:** Continuously; one entry per decision point.

**Example:**

```python
def log_scheduling_decision(decision, reason, details):
    systemd.journal.send(
        "scheduling_decision",
        MESSAGE=f"Test {decision}: {reason}",
        PRIORITY=systemd.journal.LOG_INFO,
        **{
            "fields.reason": reason,
            "fields.sulfation_score": details.get("score"),
            "fields.soh_percent": details.get("soh"),
            "fields.days_since_test": details.get("days_since"),
            "fields.next_test_eta": details.get("eta_iso8601"),
            "fields.roi_last_test": details.get("roi"),
        }
    )
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Static Test Schedule in Config

**What:** User edits config to set test time: "run test every 14 days"

**Why bad:** Ignores sulfation condition; if sulfation is high, 14 days is too long. If stable, wasting wear.

**Instead:** Expose tuning parameters (sulfation_threshold, min_interval) but run scheduling algorithm in daemon code. Log decisions to journald so visibility is high.

### Anti-Pattern 2: Test Without Safety Floor

**What:** Schedule test whenever sulfation_score > 60, regardless of SoH.

**Why bad:** Discharging below 50% SoH removes recovery margin; battery may not recover from discharge if SoH=40%.

**Instead:** AND gate: `IF sulfation_score > threshold AND soh > floor THEN test()`. Conservative floor = 50%.

### Anti-Pattern 3: Excessive Testing (Wear Acceleration)

**What:** Daemon triggers test daily if sulfation detected.

**Why bad:** VRLA aging accelerates with cycle count. 1 test/day = 365 cycles/year. Each cycle ~0.15% capacity loss = 55% loss/year (battery dead in 2 years).

**Instead:** Rate limit: `max_tests_per_week = 1`. Combine with blackout credit: if natural blackout occurred, defer scheduled test.

### Anti-Pattern 4: Machine Learning RUL (Overfitting)

**What:** Use LSTM/XGBoost to predict remaining useful life from discharge curves.

**Why bad:** Requires training data (100+ discharge curves) from identical battery units. CyberPower UT850 public data unavailable. Model will overfit to synthetic data → confidence bias.

**Instead:** Physics-based SoH (from Peukert + voltage LUT) + empirical replacement threshold (SoH < 30% = 6 months to failure). Simple, interpretable, works with sparse data.

### Anti-Pattern 5: Ignoring Blackout Pattern

**What:** Schedule test every month, regardless of grid stability.

**Why bad:** If blackouts occur 2-5x/week, battery already gets maintenance from production discharges. Scheduled test = redundant wear.

**Instead:** Track recent blackouts; defer scheduled test if recent full discharge occurred. Adapt scheduling to operational reality.

## Scalability Considerations

v3.0 daemon is single-threaded (one Python process); scaling is not a concern for single UPS. However, patterns enable future multi-UPS:

| Concern | At 1 UPS | At 5 UPS | At 50 UPS |
|---------|----------|----------|-----------|
| **Polling load** | 1 upsc call/10s per UPS | 500ms total overhead | Negligible (async I/O) |
| **State storage** | model.json per UPS | Separate files or DB | Database backend (SQLite) |
| **Scheduling** | apscheduler in-process | Shared scheduler instance | Distributed scheduler (Celery) |
| **Logging** | journald (local) | journald (still works) | Central log aggregation (Grafana Alloy) |
| **Config** | Single config.json | YAML per UPS or templated | Config management system (Ansible) |

**For v3.0 (single UPS):** No changes needed. Architecture is already clean; component boundaries defined for future multi-UPS refactor.

## State Management

All state stored in **model.json** (single source of truth):

```json
{
  "battery": {
    "soh_percent": 92.5,
    "soc_percent": 87.0,
    "capacity_ah_measured": 5.8,
    "cycle_count": 342
  },
  "v3_sulfation": {
    "ir_baseline_mv": 85.0,
    "ir_history": [
      {"timestamp": "2026-03-17T08:00:00Z", "ir_mv": 88.2},
      {"timestamp": "2026-03-16T08:00:00Z", "ir_mv": 86.5},
      ...
    ],
    "recovery_delta_history": [
      {"timestamp": "2026-03-17T08:00:00Z", "mv": 120},
      ...
    ],
    "sulfation_score": 72,
    "sulfation_confidence": "medium"
  },
  "v3_scheduling": {
    "last_test_timestamp": "2026-03-10T09:15:00Z",
    "next_test_eta": "2026-03-17T08:00:00Z",
    "next_test_reason": "sulfation_score:72_soh:92_interval:7d",
    "test_count_this_week": 0
  },
  "v3_roi": {
    "last_test_roi_ratio": 11.1,
    "roi_trend": [10.5, 11.2, 11.1]
  }
}
```

Reads/writes are **atomic** (use fsync); updates only on state change (test trigger or daily scheduling check).

## Sources

**Scheduler Design:**
- APScheduler architecture: https://apscheduler.readthedocs.io/en/3.10.x/userguide/scheduling.html
- Cron expression format: https://en.wikipedia.org/wiki/Cron

**VRLA Sulfation Detection (Enterprise):**
- Eaton ABM: Advanced Battery Management — automated scheduling logic
- Vertiv White Paper: Internal resistance trending methodology
- Sandia SAND2004-3149: Lead-acid maintenance discharge frequency

**Python Async Daemon:**
- asyncio event loop best practices: https://docs.python.org/3/library/asyncio.html
- systemd integration: https://www.freedesktop.org/wiki/Software/systemd/dbus/

**Journald Structured Logging:**
- systemd.journal Python bindings: https://manpages.debian.org/buster/python3-systemd/systemd.journal.html
- JSON field conventions: https://www.freedesktop.org/wiki/Software/systemd/json-fields/

---

*Architecture for: UPS Battery Monitor v3.0 (Active Battery Care)*
*Researched: 2026-03-17*
*Status: Ready for detailed design phase*
