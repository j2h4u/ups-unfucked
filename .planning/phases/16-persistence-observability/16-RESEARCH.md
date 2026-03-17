# Phase 16: Persistence & Observability - Research

**Researched:** 2026-03-17
**Domain:** Daemon observability, model persistence, metrics export
**Confidence:** HIGH

## Summary

Phase 16 extends the v3.0 daemon to observe and persist sulfation signals (IR trend, recovery delta, physics baseline) without triggering any tests. All new observability must be in place before Phase 17 activates control logic. Sulfation scoring and cycle ROI calculation from Phase 15 are pure functions; Phase 16 focuses on integration points: model.json schema extension, discharge handler integration, health.json export format, MOTD display, and journald structured event logging.

The phase is read-only from the daemon perspective — no upscmd calls, no behavior changes, no safety decisions. It instruments the existing polling pipeline to capture signals, persist them atomically, and export for human visibility and Grafana ingestion.

**Primary recommendation:** Extend model.json with `sulfation_history`, `discharge_events`, and `roi_history` arrays (persisted on discharge completion). Update `write_health_endpoint()` to include sulfation_score, cycle_roi, next_test_eta, and scheduling_reason. Add journald structured event logging for discharge completion with event.reason field (natural vs test-initiated). Create MOTD module for sulfation display.

## Standard Stack

### Core Infrastructure (v2.0, inherited)
| Component | Version | Purpose | Why Standard |
|-----------|---------|---------|--------------|
| Python | 3.13 | Daemon language | Type hints, match stmt, performance |
| python-systemd | Latest | JournalHandler, systemd notify | Standard NUT daemon pattern |
| pytest | Latest | Test framework | Existing 360+ test suite |
| NUT | 2.8.1+ | UPS communication | Only hardware interface |
| journald | systemd | Event logging | Standard Linux observability |

### Phase 16 New Components
| Component | Version | Purpose | When to Use |
|-----------|---------|---------|-------------|
| model.json extension | existing schema | Persist sulfation/ROI history | On every discharge completion |
| health.json metrics | existing format | Export observability to Grafana | Every poll (10s) |
| Structured journald events | systemd native | Log discharge decisions | Per discharge event |
| MOTD module | shell script | Display on SSH login | User observability |

### No New External Dependencies
**Key point:** Phase 16 uses only Python stdlib + systemd libs already in v2.0. No new pip packages required.

**Installation:** No new packages. Daemon already has all dependencies.

**Version verification:** Verified against project pyproject.toml (2026-03-17). All requirements met by existing stack.

## Architecture Patterns

### Recommended Model.json Schema Extension

Current schema (v2.0):
```json
{
  "lut": [{...}],
  "soh": 1.0,
  "soh_history": [{"date": "...", "soh": 1.0}],
  "physics": {...},
  "battery_install_date": "...",
  "cycle_count": 0,
  "cumulative_on_battery_sec": 0.0
}
```

Phase 16 additions (new top-level keys):
```json
{
  "sulfation_history": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "event_type": "natural" | "test_initiated",
      "sulfation_score": 0.45,
      "days_since_deep": 7.2,
      "ir_trend_rate": 0.008,
      "recovery_delta": 0.12,
      "temperature_celsius": 35.0,
      "confidence_level": "high" | "medium" | "low"
    }
  ],
  "discharge_events": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "event_reason": "natural" | "test_initiated",
      "duration_seconds": 1200,
      "depth_of_discharge": 0.75,
      "measured_capacity_ah": null,
      "cycle_roi": 0.52
    }
  ],
  "roi_history": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "roi": 0.52,
      "sulfation_score": 0.45,
      "cycle_budget_remaining": 150
    }
  ],
  "natural_blackout_events": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "duration_seconds": 180,
      "estimated_desulfation_credit": 0.15
    }
  ]
}
```

**Backward compatibility:** Phase 16 model.json remains readable by v2.0 (missing keys ignored). Phase 16 daemon gracefully handles v2.0 model.json (missing keys → initialize empty arrays).

**Pruning strategy:** Like v2.0, keep last 30 entries per history array. Discharge events are low-frequency (0-2/day), pruning prevents unbounded growth over 1+ year operation.

### Pattern 1: Discharge-Complete Handler Integration

**What:** On OB→OL transition (discharge complete), daemon calls discharge_handler.update_battery_health() which now additionally:
1. Calls sulfation.compute_sulfation_score() with accumulated discharge data
2. Calculates cycle_roi.compute_cycle_roi() using sulfation score + battery state
3. Appends to model.json sulfation_history, discharge_events, roi_history
4. Logs structured journald event with event.reason field
5. Saves model.json atomically (existing pattern)

**When to use:** Every discharge completion (typically 0-2 per day).

**Example flow (pseudocode):**
```python
# In discharge_handler.update_battery_health()
if discharge_duration >= 300:  # Skip micro-discharges
    # Existing: SoH + Peukert calibration
    soh_result = soh_calculator.calculate_soh_from_discharge(...)

    # Phase 16 NEW:
    sulfation_state = sulfation.compute_sulfation_score(
        days_since_deep=self._calculate_days_since_deep(),
        ir_trend_rate=self._estimate_ir_trend(),
        recovery_delta=soh_result.recovery_delta,
        temperature_celsius=35.0
    )

    cycle_roi = cycle_roi.compute_cycle_roi(
        days_since_deep=...,
        depth_of_discharge=dod,
        cycle_budget_remaining=self._estimate_cycles(),
        ir_trend_rate=...,
        sulfation_score=sulfation_state.score
    )

    # Persist to model.json
    self.battery_model.append_sulfation_history({
        'timestamp': datetime.now().isoformat(),
        'event_type': self._classify_event_reason(),  # 'natural' or 'test_initiated'
        'sulfation_score': sulfation_state.score,
        'days_since_deep': sulfation_state.days_since_deep,
        'ir_trend_rate': sulfation_state.ir_trend_rate,
        'recovery_delta': sulfation_state.recovery_delta,
        'temperature_celsius': sulfation_state.temperature_celsius,
        'confidence_level': 'high'  # Always high for now (Phase 17 adds thresholds)
    })

    # Log to journald
    logger.info('Discharge complete', extra={
        'event_type': 'discharge_complete',
        'event_reason': 'natural',  # or 'test_initiated'
        'sulfation_score': round(sulfation_state.score, 3),
        'cycle_roi': round(cycle_roi, 3)
    })

    safe_save(self.battery_model)
```

### Pattern 2: Event Reason Classification

**What:** Distinguish natural blackouts from test-initiated discharges. Natural blackouts indicate unplanned grid loss; test-initiated are daemon-triggered (Phase 17 future). For Phase 16, always "natural" because upscmd calls don't exist yet.

**When to use:** Every discharge event, before logging.

**Implementation approach:**
- Store in discharge_handler: state variable tracking when test was last initiated (Phase 17)
- In discharge complete handler: compare discharge start time to test initiation time
- If within 5 minutes → classify as "test_initiated"
- Otherwise → "natural"
- For Phase 16, hardcode "natural" since upscmd not called yet

### Pattern 3: Health.json Metrics Export

**What:** Extend health.json to include observability metrics exported to Grafana.

**When to use:** Already written every poll (10s). Phase 16 adds fields, maintains atomic write.

**Updated health.json schema:**
```json
{
  "last_poll": "2026-03-17T10:30:00Z",
  "last_poll_unix": 1710758400,
  "current_soc_percent": 75.5,
  "online": true,
  "daemon_version": "1.1.0",
  "poll_latency_ms": 0.3,
  "capacity_ah_measured": 6.8,
  "capacity_ah_rated": 7.2,
  "capacity_confidence": 0.95,
  "capacity_samples_count": 5,
  "capacity_converged": true,

  // Phase 16 NEW:
  "sulfation_score": 0.45,
  "sulfation_score_confidence": "high",
  "days_since_deep": 7.2,
  "ir_trend_rate": 0.008,
  "recovery_delta": 0.12,

  "cycle_roi": 0.52,
  "cycle_budget_remaining": 150,

  "scheduling_reason": "observing",  // Phase 17: "schedule_next_deep" | "skip_marginal_roi"
  "next_test_timestamp": null,  // Phase 17: Unix timestamp

  "last_discharge_timestamp": "2026-03-17T10:00:00Z",
  "natural_blackout_credit": 0.15
}
```

**Implementation:** In monitor_config.write_health_endpoint(), add parameters for sulfation metrics. Called from MonitorDaemon.run() on every poll. Daemon in-memory state has these values; health.json is read-only snapshot.

### Pattern 4: Journald Structured Events

**What:** Log discharge completion as structured event with field names for log aggregation and alerting.

**When to use:** On discharge complete (OB→OL transition).

**Example (Python logging):**
```python
logger.info('Discharge complete', extra={
    'event_type': 'discharge_complete',  # Machine-readable category
    'event_reason': 'natural',  # 'natural' | 'test_initiated'
    'duration_seconds': 1200,
    'depth_of_discharge': 0.75,
    'sulfation_score': 0.45,
    'sulfation_confidence': 'high',
    'recovery_delta': 0.12,
    'cycle_roi': 0.52,
    'measured_capacity_ah': 6.8,
    'timestamp': '2026-03-17T10:30:00Z',
})
```

**Journald output (human-readable):**
```
Mar 17 10:30:00 senbonzakura ups-battery-monitor[12345]: INFO - Discharge complete
    EVENT_TYPE=discharge_complete
    EVENT_REASON=natural
    DURATION_SECONDS=1200
    SULFATION_SCORE=0.450
    CYCLE_ROI=0.520
```

**Query with journalctl:**
```bash
journalctl -u ups-battery-monitor -o json-seq | jq 'select(.EVENT_TYPE=="discharge_complete")'
```

### Anti-Patterns to Avoid

- **Writing health.json on every sample:** Phase 16 writes only once per poll (10s), not per metric update. Keeps SSD wear minimal.
- **Logging at INFO level for every poll:** Only log on discharge completion or alerts. Normal polls are silent (DEBUG level).
- **Storing unbounded history:** Always prune to keep_count=30 per array. Prevents model.json from growing indefinitely.
- **Hardcoding event_reason as "natural":** Structure the code so Phase 17 can easily swap in classification logic without refactoring.
- **Calculating metrics on every poll:** Sulfation score is expensive (requires full discharge curve). Calculate only on discharge completion or every N hours.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| JSON atomic writes | Custom lock files | model.py atomic_write_json() + fdatasync | Handles partial writes, symlink attacks, rename atomicity |
| Journald event logging | Manual syslog calls | Python logging + JournalHandler | Structured fields, automatic timestamp, level filtering |
| Discharge timing | Ad-hoc time.time() tracking | Existing event_classifier state machine | Deduplication, cooldown handling, multiple discharge detection |
| Health.json versioning | Multiple schema versions | Single schema with optional fields | Backward compatibility, simpler consumer code (Grafana) |

**Key insight:** All persistence infrastructure already exists in v2.0. Phase 16 is integration + schema extension, not new I/O patterns.

## Common Pitfalls

### Pitfall 1: Timestamp Format Mismatch

**What goes wrong:** health.json uses ISO8601 strings, journald uses unix timestamps, model.json uses mixed formats. Grafana queries break when formats diverge.

**Why it happens:** Each system has its native format. Without discipline, timestamps drift.

**How to avoid:**
- All persistent timestamps (model.json, journald fields) → ISO8601 string (datetime.now().isoformat())
- health.json includes both ISO8601 and unix timestamp for flexibility
- health.json export function handles conversion; daemon never mixes formats

**Warning signs:**
- Grafana panel shows "invalid timestamp" errors
- journalctl -o json outputs numeric values for fields that should be strings
- model.json has mix of "2026-03-17T10:30:00Z" and 1710758400

### Pitfall 2: Unbounded History Growth

**What goes wrong:** After 1 year, model.json grows from 50KB to 500KB. Disk I/O slows. Recovery from backups becomes painful.

**Why it happens:** Never pruning history arrays. Easy to forget when adding new fields.

**How to avoid:**
- Every model.save() call runs _prune_* methods (already in v2.0)
- Add new methods: _prune_sulfation_history(keep_count=30), _prune_discharge_events(keep_count=30)
- Test: write 1000 events, verify only 30 persisted

**Warning signs:**
- model.json file size grows >1MB
- Daemon startup takes >1s to load model.json
- parse errors from json.load() hint corruption from disk full

### Pitfall 3: Stale Metrics in health.json

**What goes wrong:** Grafana shows old sulfation_score even after discharge. Dashboard looks broken.

**Why it happens:** health.json is written every poll, but daemon doesn't update sulfation_score until discharge completion. If discharge happens between polls, health.json lags.

**How to avoid:**
- Daemon keeps sulfation_score in memory on every poll
- health.json written every poll includes current in-memory value
- Call write_health_endpoint() AFTER discharge handler updates metrics
- Test: trigger discharge, query health.json within 10s, verify new sulfation_score

**Warning signs:**
- Grafana dashboard shows old values for >60s after discharge
- health.json last_poll timestamp is current, but sulfation_score is stale

### Pitfall 4: Event Reason Classification Too Aggressive

**What goes wrong:** Phase 16 flags all discharges as "natural". Phase 17 will add test-initiated logic. If classification is wrong, Grafana alerts fire incorrectly.

**Why it happens:** Threshold tuning without real data. First natural blackout is gold data; misclassifying it breaks analysis.

**How to avoid:**
- Phase 16 hardcodes "natural" for all events (safe, conservative)
- Phase 17 adds state tracking to detect test-initiated discharges
- Test with synthetic data: mock timestamp, verify classification matches expectations
- Real blackout validation: operator confirms blackout reason in journalctl output

**Warning signs:**
- Grafana shows "test_initiated" for a power outage (verify with /var/log/syslog timing)
- natural_blackout_credit goes negative (indicates misclassification)

### Pitfall 5: Cyclic Dependency: Sulfation Score Needs History Data

**What goes wrong:** Computing sulfation_score requires days_since_deep, which requires looking back in discharge_events. Circular dependency if not careful.

**Why it happens:** Sulfation scoring takes historical context. Easy to create tight coupling.

**How to avoid:**
- All historical lookups (days_since_deep, ir_trend_rate) happen in discharge_handler, BEFORE calling sulfation.compute_sulfation_score()
- sulfation module receives pre-computed values as parameters
- discharge_handler owns history; sulfation module is stateless pure function
- Test: mock history, verify sulfation score deterministic with same input

**Warning signs:**
- sulfation module imports model.py or discharge_handler.py (circular)
- Days_since_deep computation fails on first discharge (no history to query)

## Code Examples

Verified patterns from official sources (existing v2.0 codebase + Phase 15 modules):

### Example 1: Extend BatteryModel for Sulfation History

Source: src/model.py (existing BatteryModel class)

```python
# In src/model.py, add methods to BatteryModel class:

def append_sulfation_history(self, entry: dict) -> None:
    """Append sulfation measurement to history.

    Args:
        entry: {
            'timestamp': ISO8601 string,
            'event_type': 'natural' | 'test_initiated',
            'sulfation_score': float [0, 1],
            'days_since_deep': float,
            'ir_trend_rate': float,
            'recovery_delta': float,
            'temperature_celsius': float,
            'confidence_level': 'high' | 'medium' | 'low'
        }
    """
    self.data.setdefault('sulfation_history', []).append(entry)

def append_discharge_event(self, event: dict) -> None:
    """Append discharge completion to history.

    Args:
        event: {
            'timestamp': ISO8601 string,
            'event_reason': 'natural' | 'test_initiated',
            'duration_seconds': float,
            'depth_of_discharge': float,
            'measured_capacity_ah': float | None,
            'cycle_roi': float
        }
    """
    self.data.setdefault('discharge_events', []).append(event)

def _prune_sulfation_history(self, keep_count: int = 30) -> None:
    """Prune old sulfation entries, keep most recent."""
    hist = self.data.get('sulfation_history', [])
    if len(hist) > keep_count:
        self.data['sulfation_history'] = hist[-keep_count:]

def _prune_discharge_events(self, keep_count: int = 30) -> None:
    """Prune old discharge events, keep most recent."""
    events = self.data.get('discharge_events', [])
    if len(events) > keep_count:
        self.data['discharge_events'] = events[-keep_count:]

# In save() method, add calls:
def save(self):
    self._prune_soh_history()
    self._prune_r_internal_history()
    self._prune_lut()
    self._prune_capacity_estimates()
    self._prune_sulfation_history()      # NEW
    self._prune_discharge_events()       # NEW
    atomic_write_json(self.model_path, self.data)
```

### Example 2: Discharge Handler Integration

Source: src/discharge_handler.py (existing DischargeHandler.update_battery_health)

```python
from src.battery_math.sulfation import compute_sulfation_score
from src.battery_math.cycle_roi import compute_cycle_roi

def update_battery_health(self, discharge_buffer: DischargeBuffer) -> None:
    """Process discharge event: SoH, Peukert, sulfation, ROI, alerts."""

    if len(discharge_buffer.voltages) < 2:
        return

    discharge_duration = discharge_buffer.times[-1] - discharge_buffer.times[0]
    if discharge_duration < 300:
        logger.info(f"Discharge too short ({discharge_duration:.0f}s); skipping")
        return

    # Existing v2.0: SoH + Peukert calibration
    avg_load = sum(discharge_buffer.loads) / len(discharge_buffer.loads) if discharge_buffer.loads else 20.0
    soh_result = soh_calculator.calculate_soh_from_discharge(...)

    # Phase 16 NEW: Compute sulfation signals
    days_since_deep = self._calculate_days_since_deep()  # Query discharge_events history
    ir_trend_rate = self._estimate_ir_trend()  # Query r_internal_history
    recovery_delta = soh_result.soh_change if soh_result else 0.0

    sulfation_state = compute_sulfation_score(
        days_since_deep=days_since_deep,
        ir_trend_rate=ir_trend_rate,
        recovery_delta=recovery_delta,
        temperature_celsius=35.0  # Constant per v3.0 spec
    )

    # Phase 16 NEW: Compute cycle ROI
    depth_of_discharge = self._estimate_dod_from_buffer(discharge_buffer)
    cycle_budget = self._estimate_cycle_budget()

    roi = compute_cycle_roi(
        days_since_deep=days_since_deep,
        depth_of_discharge=depth_of_discharge,
        cycle_budget_remaining=cycle_budget,
        ir_trend_rate=ir_trend_rate,
        sulfation_score=sulfation_state.score
    )

    # Phase 16 NEW: Persist to model.json
    self.battery_model.append_sulfation_history({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'event_type': self._classify_event_reason(discharge_buffer),  # 'natural' for Phase 16
        'sulfation_score': round(sulfation_state.score, 3),
        'days_since_deep': round(days_since_deep, 1),
        'ir_trend_rate': round(ir_trend_rate, 6),
        'recovery_delta': round(recovery_delta, 3),
        'temperature_celsius': 35.0,
        'confidence_level': 'high'
    })

    self.battery_model.append_discharge_event({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'event_reason': 'natural',  # Phase 16: always natural (Phase 17 adds test_initiated)
        'duration_seconds': discharge_duration,
        'depth_of_discharge': round(depth_of_discharge, 2),
        'measured_capacity_ah': soh_result.measured_capacity_ah if soh_result else None,
        'cycle_roi': round(roi, 3)
    })

    # Phase 16 NEW: Journald structured event
    logger.info('Discharge complete', extra={
        'event_type': 'discharge_complete',
        'event_reason': 'natural',
        'duration_seconds': int(discharge_duration),
        'sulfation_score': round(sulfation_state.score, 3),
        'cycle_roi': round(roi, 3),
        'depth_of_discharge': round(depth_of_discharge, 2),
    })

    # Existing: save to disk
    safe_save(self.battery_model)

def _classify_event_reason(self, discharge_buffer: DischargeBuffer) -> str:
    """Classify event as natural or test-initiated.

    Phase 16: Always 'natural' (upscmd not called yet).
    Phase 17: Compare discharge_buffer start time to last upscmd timestamp.
    """
    return 'natural'  # Hardcoded for Phase 16
```

### Example 3: Health Endpoint Export

Source: src/monitor_config.py (extend write_health_endpoint)

```python
def write_health_endpoint(
    soc_percent: float,
    is_online: bool,
    poll_latency_ms: Optional[float] = None,
    capacity_ah_measured: Optional[float] = None,
    capacity_ah_rated: float = 7.2,
    capacity_confidence: float = 0.0,
    capacity_samples_count: int = 0,
    capacity_converged: bool = False,
    # Phase 16 NEW:
    sulfation_score: Optional[float] = None,
    sulfation_confidence: str = 'high',
    days_since_deep: Optional[float] = None,
    ir_trend_rate: Optional[float] = None,
    recovery_delta: Optional[float] = None,
    cycle_roi: Optional[float] = None,
    cycle_budget_remaining: Optional[int] = None,
    scheduling_reason: str = 'observing',
    next_test_timestamp: Optional[int] = None,
    last_discharge_timestamp: Optional[str] = None,
    natural_blackout_credit: Optional[float] = None,
) -> None:
    """Write daemon health state to file for external monitoring."""
    try:
        daemon_version = importlib.metadata.version('ups-unfucked')
    except:
        daemon_version = "unknown"

    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(soc_percent, 1),
        "online": is_online,
        "daemon_version": daemon_version,
        "poll_latency_ms": round(poll_latency_ms, 1) if poll_latency_ms else None,
        # Existing v2.0:
        "capacity_ah_measured": round(capacity_ah_measured, 2) if capacity_ah_measured else None,
        "capacity_ah_rated": round(capacity_ah_rated, 2),
        "capacity_confidence": round(capacity_confidence, 3),
        "capacity_samples_count": capacity_samples_count,
        "capacity_converged": capacity_converged,
        # Phase 16 NEW:
        "sulfation_score": round(sulfation_score, 3) if sulfation_score is not None else None,
        "sulfation_score_confidence": sulfation_confidence,
        "days_since_deep": round(days_since_deep, 1) if days_since_deep is not None else None,
        "ir_trend_rate": round(ir_trend_rate, 6) if ir_trend_rate is not None else None,
        "recovery_delta": round(recovery_delta, 3) if recovery_delta is not None else None,
        "cycle_roi": round(cycle_roi, 3) if cycle_roi is not None else None,
        "cycle_budget_remaining": cycle_budget_remaining,
        "scheduling_reason": scheduling_reason,
        "next_test_timestamp": next_test_timestamp,
        "last_discharge_timestamp": last_discharge_timestamp,
        "natural_blackout_credit": round(natural_blackout_credit, 3) if natural_blackout_credit is not None else None,
    }

    # Existing atomic write pattern (no changes)
    health_path = HEALTH_ENDPOINT_PATH
    tmp_path = None
    try:
        if health_path.is_symlink():
            raise OSError(f"{health_path} is a symlink, refusing to write")
        health_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=str(health_path.parent), delete=False, suffix='.tmp') as tmp:
            json.dump(health_data, tmp, indent=2)
            tmp.flush()
            os.fdatasync(tmp.fileno())
            os.fchmod(tmp.fileno(), 0o644)
            tmp_path = Path(tmp.name)
        tmp_path.replace(health_path)
        logger.debug(f"Health endpoint written to {health_path}")
    except Exception as e:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        logger.error(f"Failed to write health endpoint: {e}")
        raise
```

### Example 4: MOTD Module for Sulfation Display

Source: Create new file ~/scripts/motd/55-sulfation.sh

```bash
#!/bin/bash
# MOTD module: Display sulfation status and next test countdown

# Path to health.json (same as daemon writes)
HEALTH_FILE="/run/ups-battery-monitor/ups-health.json"

# Exit cleanly if health.json not found or invalid
if [[ ! -f "$HEALTH_FILE" ]]; then
    exit 0
fi

# Parse JSON safely
sulfation=$(jq -r '.sulfation_score // "N/A"' "$HEALTH_FILE" 2>/dev/null)
roi=$(jq -r '.cycle_roi // "N/A"' "$HEALTH_FILE" 2>/dev/null)
next_test=$(jq -r '.next_test_timestamp // null' "$HEALTH_FILE" 2>/dev/null)

# Format for display
if [[ "$sulfation" != "N/A" ]]; then
    # Sulfation score as percentage
    score_pct=$(echo "$sulfation * 100" | bc -l | xargs printf "%.0f")

    # Days until next test (if scheduled)
    if [[ "$next_test" != "null" && "$next_test" != "" ]]; then
        now=$(date +%s)
        days_until=$((($next_test - $now) / 86400))
        if [[ $days_until -lt 0 ]]; then
            test_str="overdue"
        else
            test_str="in ${days_until}d"
        fi
    else
        test_str="none scheduled"
    fi

    echo "Battery health: Sulfation ${score_pct}% · Next test ${test_str}"
else
    echo "Battery health: Sulfation data not ready"
fi
```

## State of the Art

| Old Approach (v2.0) | Current Approach (v3.0 Phase 16) | When Changed | Impact |
|---------------------|----------------------------------|--------------|--------|
| Health.json reports capacity only | Health.json includes sulfation + ROI metrics | Phase 16 | Grafana dashboards can visualize battery health trend |
| SoH history only; no discharge reason | Discharge events tagged with reason + ROI | Phase 16 | Operator can distinguish natural blackouts from tests |
| No IR trend tracking | IR trend rate stored in sulfation_history | Phase 16 | Detects active sulfation vs passive aging |
| No cycle ROI metric | ROI calculated and persisted per discharge | Phase 16 | Validates that tests are beneficial before Phase 17 activates |
| All discharge events treated equally | Natural vs test-initiated classification | Phase 16 | Operator can see if blackout credit is available |
| Manual MOTD static display | Dynamic MOTD from health.json | Phase 16 | Next test countdown updates daily without code changes |

**Deprecated/outdated:**
- **Fixed test schedule (systemd timers):** Replaced by daemon-driven scheduling (Phase 17). v2.0 systemd timers (ups-test-quick.timer, ups-test-deep.timer) will be disabled in Phase 17, but Phase 16 leaves them active (no decision logic yet).

## Open Questions

1. **Event reason classification accuracy in production**
   - What we know: Phase 16 hardcodes "natural" for all discharges (safe, conservative)
   - What's unclear: Will real blackout timing data align with discharge_buffer timestamps? Could power flicker cause misclassification in Phase 17?
   - Recommendation: Phase 16 observes for 30 days. Operator confirms event_reason in journalctl for natural blackouts. Phase 17 uses field data to tune thresholds.

2. **Sulfation score stability during operation**
   - What we know: Score combines physics baseline (days) + IR trend + recovery delta. Variance depends on discharge depth variation and measurement noise.
   - What's unclear: Will daily variance be <5% (acceptable for alerting) or >5% (requires smoothing)?
   - Recommendation: Phase 16 persists raw values. MOTD can apply simple 7-day rolling average if noise is high (Phase 17 optional).

3. **Temperature constant vs sensor**
   - What we know: Phase 16 hardcodes 35°C. NUT 2.8.1+ supports battery.temperature for some UPS models (not yet validated on UT850).
   - What's unclear: Does CyberPower UT850 expose battery.temperature via HID? If yes, can we switch without model migration?
   - Recommendation: Phase 16 architecture ready for replacement. If temperature sensor becomes available, sulfation.compute_sulfation_score() API unchanged (same parameter). No refactoring needed.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.0+ (existing) |
| Config file | `/home/j2h4u/repos/j2h4u/ups-battery-monitor/pytest.ini` |
| Quick run command | `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -v` |
| Full suite command | `python3 -m pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SULF-01 | Daemon computes sulfation score [0–1.0] | unit | `pytest tests/test_sulfation.py::TestComputeSulfationScore -v` | ✅ (Phase 15) |
| SULF-02 | Physics baseline tracks days + temp | unit | `pytest tests/test_sulfation.py -k "physics\|baseline" -v` | ✅ (Phase 15) |
| SULF-03 | IR trend signal detects growth rate | unit | `pytest tests/test_sulfation.py -k "ir_trend" -v` | ✅ (Phase 15) |
| SULF-04 | Recovery delta measures SoH bounce | unit | `pytest tests/test_sulfation.py -k "recovery" -v` | ✅ (Phase 15) |
| SULF-05 | Sulfation history persisted in model.json | integration | ❌ Wave 0 | Create `tests/test_sulfation_persistence.py` |
| ROI-01 | Daemon computes ROI per discharge | unit | `pytest tests/test_cycle_roi.py -v` | ✅ (Phase 15) |
| ROI-02 | ROI factors: days, depth, budget, IR, score | unit | `pytest tests/test_cycle_roi.py::TestROIFactors -v` | ✅ (Phase 15) |
| RPT-01 | Sulfation score exported to health.json | integration | ❌ Wave 0 | Create `tests/test_health_endpoint_v16.py` |
| RPT-02 | Discharge decisions logged to journald | integration | ❌ Wave 0 | Create `tests/test_journald_sulfation_events.py` |
| RPT-03 | Next test eta + reason exported to health.json | integration | ❌ Wave 0 | Create `tests/test_health_endpoint_scheduling.py` |

### Sampling Rate
- **Per task commit:** `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -x` (Phase 15 foundation tests)
- **Per wave merge:** `python3 -m pytest tests/ -x` (full suite, 360+ tests)
- **Phase gate:** Full suite green + integration tests green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_sulfation_persistence.py` — covers SULF-05 (model.json schema, append methods, pruning)
- [ ] `tests/test_health_endpoint_v16.py` — covers RPT-01 (health.json includes sulfation_score, confidence, days_since_deep)
- [ ] `tests/test_journald_sulfation_events.py` — covers RPT-02 (structured event logging with event_type, event_reason fields)
- [ ] `tests/test_discharge_event_logging.py` — covers RPT-03 (discharge_events array in model.json persisted)
- [ ] Framework: `pytest` already installed. No additional test dependencies needed. Systemd journal testing uses mocked JournalHandler (existing pattern in test_logging.py)

*(All integration test infrastructure exists; only test files need creation in Phase 16 Plan 01)*

## Sources

### Primary (HIGH confidence)
- Phase 15 completion summary (`.planning/phases/15-foundation/15-05-SUMMARY.md`) — verified sulfation + ROI pure functions pass all tests
- Existing codebase (`src/model.py`, `src/discharge_handler.py`, `src/monitor_config.py`) — current v2.0 patterns for persistence + health.json
- REQUIREMENTS.md + STATE.md — Phase 16 scope locked to 10 requirements (SULF-01 through RPT-03)
- sulfation.py + cycle_roi.py (Phase 15) — confirmed pure function APIs and parameter contracts

### Secondary (MEDIUM confidence)
- IEEE-450 standards (cited in sulfation.py docstrings) — sulfation physics baseline
- NUT 2.8.1 upscmd protocol (validated in Phase 15 test_nut_client.py) — event reason classification framework ready
- systemd journald JSON format (Python logging + JournalHandler existing pattern) — structured event logging proven in test_monitor.py

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — All v2.0 infrastructure exists; no new dependencies required
- Architecture patterns: **HIGH** — Model.json extension follows existing schema (backward compatible); health.json pattern proven (v2.0); discharge handler integration documented in Phase 15
- Persistence strategy: **HIGH** — Atomic writes + pruning strategy identical to v2.0 (verified in 360+ tests)
- Pitfalls: **MEDIUM** — Timestamp formats and event classification require real-world validation. Unbounded history already solved by v2.0 pruning. IR trend rate estimation has unknowns (Phase 16 observes)
- Test coverage: **MEDIUM** — Phase 15 math tests complete (360 pass). Integration tests (model.json persistence, health.json export, journald events) need creation in Phase 16 Plan 01

**Research date:** 2026-03-17
**Valid until:** 2026-04-17 (30 days — domain stable, no API changes expected)

---

*Research complete. Ready for Phase 16 planning.*
