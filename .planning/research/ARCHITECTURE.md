# Architecture: v3.0 Active Battery Care Integration

**Project:** UPS Battery Monitor (CyberPower UT850EG)
**Researched:** 2026-03-17
**Scope:** Sulfation modeling, smart deep discharge scheduling, cycle ROI metrics, upscmd integration

## Executive Summary

v3.0 transforms the daemon from **passive observer** (read-only via `upsc`) to **active manager** (command dispatch via `upscmd`). This is a significant architectural shift requiring:

1. **New component: Scheduler** — replaces static systemd timers with daemon-driven intelligence
2. **New component: Sulfation model** — pure function in `src/battery_math/` following existing pattern
3. **New component: Cycle ROI calculator** — quantifies desulfation benefit vs wear cost
4. **Extended persistence:** `model.json` now tracks sulfation history, test schedule state, natural blackout credit
5. **Extended NUT integration:** Add write capability (`upscmd`) alongside existing read-only (`upsc`)

**Key insight:** Daemon becomes coordinator of two competing goals — desulfate battery (extend life) vs minimize discharge cycles (reduce wear). ROI metric makes the tradeoff explicit.

---

## Recommended Architecture

### Current v2.0 Data Flow (Read-Only)

```
┌─────────────────┐
│  NUT (upsd)     │ ← polls USB every 1s
│  cyberpower     │
└────────┬────────┘
         │ upsc (TCP read-only)
         ↓
┌─────────────────┐        ┌──────────────────┐
│  NUT Client     │───────→│ EMA + Classifier │
│  (10s poll)     │        │  Event Pipeline  │
└────────┬────────┘        └────────┬─────────┘
         │                          │
         ↓                          ↓
┌─────────────────┐        ┌──────────────────┐
│  Virtual UPS    │        │ Discharge        │
│  (.dev file)    │        │ Handler (SoH)    │
└────────┬────────┘        └────────┬─────────┘
         │                          │
         ↓                          ↓
┌─────────────────┐        ┌──────────────────┐
│  dummy-ups      │        │ model.json       │
│  (tmpfs)        │        │ (SSD, sparse)    │
└────────┬────────┘        └──────────────────┘
         │
    ┌────┴────────────────────┐
    ↓                         ↓
  upsd                    health.json
  (re-export)            (metrics export)
```

### v3.0 Extended Architecture (Read + Write)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ACTIVE BATTERY CARE                          │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────┐                                   ┌──────────────┐
│  NUT (upsd)     │                                   │  Scheduler   │
│  cyberpower     │ ←──── triggers via ─────→ [intelligent test logic]
└────────┬────────┘      natural blackouts          └────────┬──────┘
         │                                                    │
    upsc │ (read TCP)                    upscmd (write TCP) │
         │                                                    │
         ↓                                                    ↓
┌───────────────────────────────────────────────────────────────┐
│              MonitorDaemon (main orchestrator)                │
│                                                               │
│  • Polls NUT (10s)  [existing]                              │
│  • EMA + classify   [existing]                              │
│  • Track discharge  [existing]                              │
│  • COMPUTE metrics  [existing]                              │
│  • SCHEDULE tests   [NEW v3.0]                              │
│  • DISPATCH commands [NEW v3.0]                             │
└───────────────────────────────────────────────────────────────┘
         │                                    │
         ├──────────────────────────────────┬─┘
         ↓                                  ↓
┌─────────────────┐         ┌──────────────────────────────────┐
│  Virtual UPS    │         │ Discharge Handler (v2.0 → v3.0) │
│  (.dev tmpfs)   │         │ • SoH + Peukert [v2.0]          │
└─────────────────┘         │ • Sulfation score [NEW]          │
                             │ • Cycle ROI [NEW]               │
                             │ • Natural blackout credit [NEW] │
                             └──────────────────────────────────┘
         │                                  │
         ↓                                  ↓
┌─────────────────┐         ┌──────────────────────────────────┐
│  dummy-ups      │         │ Battery Math (kernel, SSD)       │
│  (proxy)        │         │ • sulfation.py [NEW module]      │
└─────────────────┘         │ • cycle_roi.py [NEW module]      │
                             │ • soh.py [existing]              │
                             │ • peukert.py [existing]          │
                             └──────────────────────────────────┘
         │                                  │
    ┌────┴──────────────────────────────────┴────┐
    ↓                                             ↓
  upsd                                  model.json (enhanced)
  (re-export)                           • lut, soh [v2.0]
                                        • sulfation_score [NEW]
                                        • test_schedule [NEW]
                                        • natural_blackout_events [NEW]
                                        • cycle_roi_history [NEW]
                                        └──────────────────────────────────┘
```

---

## Component Breakdown

### 1. Core Components (Existing v2.0, Unchanged)

| Component | File | Responsibility |
|-----------|------|-----------------|
| **NUT Client** | `src/nut_client.py` | TCP socket to upsd; read-only `upsc` protocol |
| **EMA Filter** | `src/ema_filter.py` | Voltage/load smoothing (60-120s window) |
| **Event Classifier** | `src/event_classifier.py` | Distinguish ONLINE / BLACKOUT_REAL / BLACKOUT_TEST |
| **Battery Math Kernel** | `src/battery_math/` | Pure functions: SoC LUT, SoH, Peukert, capacity estimation |
| **Capacity Estimator** | `src/capacity_estimator.py` | Coulomb counting + voltage anchor from discharges |
| **Virtual UPS** | `src/virtual_ups.py` | Write metrics to tmpfs (.dev file) for dummy-ups |
| **Model Persistence** | `src/model.py` | Atomic JSON writes to model.json |
| **SoH Calculator** | `src/soh_calculator.py` | SoH computation orchestrator |

### 2. New Components (v3.0)

#### 2a. Scheduler

**File:** `src/scheduler.py`
**Responsibility:** Decides when to initiate deep/quick discharge tests
**Triggers:** Startup + periodic evaluation (daily or on discharge completion)
**Decision logic:** ROI metric + SoH floor + grid stability check

**Key function:**
```python
class Scheduler:
    def evaluate(self, current_metrics: CurrentMetrics) -> ScheduleDecision:
        """Compute next scheduled test."""
        # 1. Check safety constraints (SoH floor, grid stability)
        # 2. Compute ROI for deep discharge (desulfation benefit vs cost)
        # 3. Apply natural_blackout_credit (recent real blackout = desulfation)
        # 4. Evaluate sulfation urgency (internal resistance trend)
        # 5. Return decision with reasoning
```

**Integration:** Called from `MonitorDaemon.run()` during main poll loop (every 3600s).

#### 2b. Sulfation Model (battery_math)

**File:** `src/battery_math/sulfation.py`
**Type:** Pure function (no state, testable in isolation)
**Inputs:** Discharge curve shape, internal resistance history, temperature

**Physics-based detection:**
- Shepherd/Bode voltage recovery rate
- Internal resistance (IR) trend
- Discharge curve shape (smoothness)
- Mid-SoC anomalies (plate damage indicators)

**Score interpretation:**
- `[0.0 - 0.2]`: Healthy (normal aging)
- `[0.2 - 0.5]`: Early sulfation (desulfation candidate)
- `[0.5 - 0.8]`: Advanced sulfation (urgent desulfation)
- `[0.8 - 1.0]`: Severe (may not recover)

**Integration:** Called from `DischargeHandler.update_battery_health()` after SoH update.

#### 2c. Cycle ROI Calculator (battery_math)

**File:** `src/battery_math/cycle_roi.py`
**Type:** Pure function
**Purpose:** Quantifies tradeoff: desulfation benefit vs. cycle wear cost

**Model:**
```
BENEFIT = capacity_recovery_potential × sulfation_reversibility
COST = cycle_stress (0.5% SoH per deep discharge) × age_factor
ROI = (benefit - cost) / max_benefit ∈ [-1, 1]

roi > 0.2  → RECOMMEND deep discharge
roi < -0.2 → DEFER (cost > benefit)
-0.2 ≤ roi ≤ 0.2 → OPTIONAL (roughly neutral)
```

**Integration:** Called from `Scheduler.evaluate()` to decide test urgency; exported to `health.json` for Grafana.

---

## Modified Components (v3.0)

### 3a. NUT Client Extended

**File:** `src/nut_client.py` → add write capability

New method:
```python
def send_command(self, command_id: str) -> tuple[bool, str]:
    """
    Send instant command to UPS via upscmd protocol.

    Returns: (success: bool, message: str)

    Protocol:
      • Open TCP socket to upsd (same host:port as upsc)
      • Send: "INSTCMD <ups_name> <command_id>\n"
      • Receive: "OK" or "ERR <reason>"
      • Close socket
    """
```

**Safety considerations:**
- Whitelist allowed commands: `test.battery.start.quick`, `test.battery.start.deep`
- upscmd requires NUT authentication (upsd.conf configuration)
- Timeouts prevent daemon hang

### 3b. Monitor Daemon Extended

**File:** `src/monitor.py` → add scheduling loop

New state:
```python
self.scheduler = Scheduler(self.battery_model, config)
self.last_schedule_eval_time = time.time()
self.pending_test = None  # ScheduleDecision | None
```

New methods:
```python
def _evaluate_schedule(self) -> None:
    """Called hourly: decide if test should be scheduled."""

def _check_and_dispatch_test(self) -> None:
    """Called every poll: execute pending test if preconditions allow."""

def _dispatch_test(self) -> None:
    """Send upscmd; track state in model.json."""
```

### 3c. Discharge Handler Extended

**File:** `src/discharge_handler.py` → add sulfation + ROI tracking

In `update_battery_health()`:
```python
# NEW: Compute sulfation score
ir_estimate = self._estimate_internal_resistance(discharge_buffer)
temperature = self.battery_model.get_battery_temperature()
sulfation_score, diagnostic = sulfation.compute_sulfation_score(...)

# NEW: Compute cycle ROI
roi_score, recommendation = cycle_roi.compute_cycle_roi(...)

# Track in model.json
self.battery_model.append_sulfation_score(...)
self.battery_model.append_cycle_roi(...)
```

### 3d. Model Extended

**File:** `src/model.py` → new persistence fields

New `model.json` sections:
```json
{
  "sulfation": {
    "score": 0.25,
    "history": [
      {"date": "2026-03-10T14:23Z", "score": 0.20, "cycle": 42},
      {"date": "2026-03-12T18:40Z", "score": 0.35, "cycle": 44}
    ]
  },
  "test_schedule": {
    "last_deep_discharge": "2026-03-15T10:00Z",
    "last_quick_discharge": "2026-03-12T18:40Z",
    "pending_test": null | {"type": "deep", "scheduled_time": "..."}
  },
  "natural_blackout_events": [
    {"date": "2026-03-12T17:53Z", "duration_sec": 2820, "desulfation_credit": 0.1}
  ],
  "cycle_roi_history": [
    {"date": "2026-03-10", "roi": 0.45, "recommendation": "schedule within 30d"}
  ]
}
```

New methods:
```python
def get_sulfation_score(self) -> float
def append_sulfation_score(self, date, score, diagnostic, cycle_number) -> None
def get_test_schedule(self) -> dict
def append_natural_blackout(self, date, duration_sec, credit) -> None
```

---

## Data Flow: Test Lifecycle

```
1. Startup
   → Load model.json (including pending_test)

2. Main Poll Loop (every 10s)
   → Read NUT, classify events, track discharge

3. Schedule Evaluation (hourly)
   → Compute sulfation_score
   → Compute roi_score
   → Check SoH floor, grid stability
   → Decision: {should_test, test_type, reason}

4. Pre-Test Validation (every poll while pending)
   → Check UPS online (not OB)
   → Check load/voltage stable
   → If ready → dispatch

5. Dispatch Command
   → Send: test.battery.start.<type> via upscmd
   → Record in model.json
   → Log: "Deep test initiated"

6. Test Execution (UPS firmware)
   → Battery discharges
   → Daemon polls continuously
   → EventClassifier detects BLACKOUT_TEST

7. Test Completion (OB→OL transition)
   → DischargeHandler processes discharge
   → Compute SoH, sulfation_score, roi_score
   → Update model.json
   → Clear pending_test

8. Next Evaluation
   → Scheduler considers updated metrics
   → May schedule follow-up test or defer
```

### Natural Blackout Integration

When a real blackout occurs:
1. EventClassifier detects OL→OB transition
2. DischargeHandler processes discharge on OB→OL
3. **NEW:** Record in `natural_blackout_events`:
   - Duration
   - Estimated desulfation benefit
4. **NEW:** Scheduler credits this:
   - If recent blackout was deep, defer daemon test (got some benefit already)
   - Credit survives daemon restart (persisted in model.json)

---

## Integration Points

### Data Dependencies

```
Config → MonitorDaemon
         ├─ NUTClient (read+write)
         ├─ EMAFilter, EventClassifier
         ├─ Scheduler (reads BatteryModel, CurrentMetrics)
         ├─ DischargeHandler
         │  ├─ imports: battery_math.sulfation, cycle_roi
         │  └─ writes: BatteryModel
         └─ BatteryModel
            ├─ reads: model.json (startup)
            └─ writes: model.json (discharge events, schedule changes)
```

### File I/O Pattern

| Operation | File | Frequency | Atomicity |
|-----------|------|-----------|-----------|
| Read config | `config.json` | Startup | N/A |
| Read/write battery model | `model.json` | Sparse (~1-4/week) | Atomic fdatasync |
| Write health metrics | `health.json` | Every 10 polls (~100s) | Atomic |
| Write virtual UPS | `/run/ups-battery-monitor/ups-virtual.dev` | Every 10s | Atomic |

---

## Build Order (Phase Sequencing)

### Phase 1: Foundation (Safe, Self-Contained)

1. **NUT write support** (`src/nut_client.py`)
   - Add `send_command()` method
   - Test with mock upsd
   - No integration risk

2. **Sulfation model** (`src/battery_math/sulfation.py`)
   - Pure function, fully testable
   - No daemon changes

3. **Cycle ROI calculator** (`src/battery_math/cycle_roi.py`)
   - Pure function, synthetic data testing
   - No integration needed

**Rationale:** Non-invasive additions. Daemon unchanged.

### Phase 2: Persistence Integration

4. **Extend model.json** (`src/model.py`)
   - Add sulfation, test_schedule, natural_blackout_events, cycle_roi_history
   - Backward compatible (old files load, new fields init)

5. **Extend DischargeHandler** (`src/discharge_handler.py`)
   - Call sulfation/ROI functions
   - Record results in model.json
   - Log alerts

**Rationale:** Observability phase. Daemon still read-only.

### Phase 3: Scheduling Intelligence

6. **Implement Scheduler** (`src/scheduler.py`)
   - Evaluate ROI, SoH floor, grid stability
   - Unit test with synthetic data

7. **Integrate Scheduler** (`src/monitor.py`)
   - Hourly evaluation call
   - Track pending_test state
   - Add precondition checks

**Rationale:** Logic isolated. Can enable/disable via config.

### Phase 4: Active Control (Ops Validation Required)

8. **Enable upscmd dispatch** (`src/nut_client.py`, `src/monitor.py`)
   - Requires NUT configuration (upsd.conf permissions)
   - Likely needs systemd unit update

9. **Integration testing** (real UPS)
   - Test preconditions on hardware
   - Validate response handling
   - Test discharge buffer fills correctly

10. **Monitoring + alerting** (health.json, MOTD)
    - Export sulfation_score, roi_score
    - Add MOTD module
    - Validate Grafana parsing

**Rationale:** Phases 8-10 alter UPS behavior. Ops sign-off required.

---

## Critical Design Decisions

| Decision | Rationale | Tradeoffs |
|----------|-----------|-----------|
| Sulfation as pure function in battery_math/ | Decouples physics from daemon; testable | Longer function signatures |
| Cycle ROI balanced metric | Makes competing goals explicit | Weighting is empirical, may need tuning |
| Scheduler runs hourly, not per-poll | Avoids thrashing; respects grid stability | 1h latency acceptable (battery timescales: weeks-months) |
| Natural blackout event credit | Captures real desulfation; avoids unnecessary tests | Credit formula is heuristic |
| Test state in model.json | Survives daemon restart; queryable | Schema complexity; must prune history |
| Precondition checks | Safety: prevent test during emergencies | Adds state machine complexity |

---

## Pitfalls & Mitigations

### P1: Command Injection via upscmd

**Risk:** Malicious command_id injection
**Mitigation:** Whitelist allowed commands before socket send

```python
ALLOWED_COMMANDS = frozenset(['test.battery.start.quick', 'test.battery.start.deep'])
if command_id not in ALLOWED_COMMANDS:
    return (False, f"Unknown command: {command_id}")
```

### P2: Test Dispatch During Blackout

**Risk:** Daemon dispatches test as power fails
**Mitigation:** Preconditions: `ups.status == 'OL'`, input voltage stable, no recent flickers

### P3: Sulfation Score Noise

**Risk:** Early measurements produce noisy scores; alarm fatigue
**Mitigation:** Require minimum history (5+ discharges); use EMA smoothing; reserve judgment for anomalies

### P4: upscmd Permission Errors

**Risk:** Silent failures if NUT doesn't permit battery commands
**Mitigation:** Log all dispatch attempts; escalate permission errors; provide NUT config docs

### P5: Scheduler Thrashing

**Risk:** ROI metric too aggressive, tests daily
**Mitigation:** Hard minimum (no test <7 days); natural blackout credit; hysteresis on roi threshold

### P6: Circular Dependencies

**Risk:** Scheduler needs BatteryModel; BatteryModel needs Scheduler
**Mitigation:** Scheduler stateless; MonitorDaemon orchestrates (eval → dispatch → discharge → update)

---

## New vs Modified Files

| File | Type | Change |
|------|------|--------|
| `src/nut_client.py` | Modified | Add `send_command()` for upscmd |
| `src/battery_math/sulfation.py` | New | Pure function: sulfation scoring |
| `src/battery_math/cycle_roi.py` | New | Pure function: ROI metric |
| `src/scheduler.py` | New | Scheduling intelligence |
| `src/model.py` | Modified | Extend schema (sulfation, test_schedule, etc.) |
| `src/discharge_handler.py` | Modified | Call sulfation/ROI; record in model.json |
| `src/monitor.py` | Modified | Add scheduler eval + dispatch |
| `src/monitor_config.py` | Modified | New config knobs (schedule_eval_interval, min_soh_for_test) |
| `tests/test_sulfation.py` | New | Unit tests |
| `tests/test_cycle_roi.py` | New | Unit tests |
| `tests/test_scheduler.py` | New | Unit tests |

---

## Testing Strategy

**Unit Tests (Phase 1-2):**
- `test_sulfation.compute_sulfation_score()` with synthetic curves
- `test_cycle_roi.compute_cycle_roi()` across SoH/age ranges
- BatteryModel schema upgrades (backward compat)

**Integration Tests (Phase 3):**
- Scheduler.evaluate() with real model.json samples
- DischargeHandler calls sulfation/ROI correctly
- model.json persistence across restart

**E2E Tests (Phase 4, Real UPS):**
- Trigger test.battery.start.deep via upscmd
- Verify preconditions prevent test during brownout
- Validate dispatcher retry on permission errors

**Chaos Tests (Post-Release):**
- Kill daemon mid-test; verify recovery
- Simulate upsd restart; verify NUT client reconnects
- Simulate disk full during write; verify atomic rename

---

## Backward Compatibility

**Schema Migration:**
```python
# Old v2.0
{"lut": [...], "soh": 0.95, "physics": {...}}

# Load in v3.0
model = BatteryModel()  # Old file loads fine
model.data['sulfation'] = {'score': 0.0, 'history': []}
model.data['test_schedule'] = {'last_deep': None, ...}
model.data['natural_blackout_events'] = []
model.data['cycle_roi_history'] = []
model.save()  # Enhanced file written
```

**Config:** New knobs have sensible defaults. Old config.json loads successfully.

---

## Deployment Checklist

- [ ] NUT config: verify upsd permits battery commands
- [ ] Systemd unit: daemon has sufficient privileges
- [ ] Config file: populate new schedule_eval_interval, min_soh_for_test
- [ ] MOTD: add module for sulfation score display
- [ ] Grafana: add panels for sulfation_score, roi_score timeseries
- [ ] Monitoring: alert if sulfation_score > 0.7 or roi_score < -0.3
- [ ] Documentation: update README with v3.0 features
- [ ] Dry-run: enable Scheduler but disable dispatch (log-only) for 1 week
- [ ] Safety rollout: enable dispatch in staging first

---

## Sources

1. **NUT upscmd(8)** — https://networkupstools.org/docs/man/upscmd.html
2. **VRLA Sulfation Physics** — https://actec.dk/media/documents/68F4B35DD5C5.pdf
3. **Battery ROI Optimization** — https://www.nature.com/articles/s41598-025-02690-9
4. **DoD Impact on Cycle Life** — https://www.sciencedirect.com/science/article/pii/S2352152X23025422
5. **IEEE 450 Standard** — Battery testing & maintenance
6. **Python subprocess Best Practices** — https://docs.python.org/3/library/subprocess.html

---

**Last updated:** 2026-03-17
**Confidence:** HIGH (existing codebase well-understood; v3.0 features grounded in research)
