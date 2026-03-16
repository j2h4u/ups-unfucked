# Phase 14: Capacity Reporting & Metrics - Research

**Researched:** 2026-03-16
**Domain:** Monitoring and telemetry (MOTD, journald, Grafana integration)
**Confidence:** HIGH

## Summary

Phase 14 exposes capacity estimation results (measured Ah, confidence, convergence status) through three reporting channels: MOTD (user-facing login display), journald (searchable structured logging), and /health endpoint (Grafana scraping). The phase builds on stable capacity measurement infrastructure from Phase 12 and SoH recalibration from Phase 13.

All reporting infrastructure already exists in v1.1: MOTD modules in `scripts/motd/`, health endpoint at `/dev/shm/ups-health.json`, and journald integration via systemd.journal. Phase 14 extends these with capacity-specific metrics and event logging.

**Primary recommendation:** Extend existing MOTD modules and health endpoint with capacity fields; add structured journald event logging for capacity measurements, convergence changes, and baseline operations. No new frameworks or external dependencies required.

## User Constraints (from CONTEXT.md)

No CONTEXT.md exists for this phase (no prior discussion). Constraints are inherited from Phase 12-13 decisions:

### Locked Decisions (from STATE.md)
- Peukert exponent stays fixed at 1.2 (VAL-02 constraint)
- Convergence threshold: count ≥ 3 AND CoV < 10% for locked baseline
- Capacity estimates array limited to 30 recent entries (pruned automatically)
- Phase 14 is soft-dependency on Phases 12-13 (reporting works best with stable data)

### Claude's Discretion
- Exact MOTD format (single-line vs multi-line, truncation policy for long outputs)
- journald event naming convention (standardized event types and message structure)
- Grafana dashboard pre-built queries vs user creates custom queries
- Confidence display format (percentage, decimal, confidence band visualization)

### Deferred Ideas (OUT OF SCOPE)
- Web UI for capacity tracking (MOTD + journald + Grafana sufficient for v2.0)
- Multi-UPS capacity tracking (single UPS only)
- Real-time capacity estimation graphs (batch visualization via Grafana)
- Mobile app or push notifications

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| RPT-01 | MOTD displays rated vs measured capacity and confidence percentage | MOTD module exists (51-ups.sh), already shows capacity progress; extend with confidence % |
| RPT-02 | journald logs capacity estimation events (new measurement, confidence change, baseline lock) | systemd.journal integration exists; add structured event logging with tags |
| RPT-03 | Daemon exposes capacity metrics for Grafana scraping | Health endpoint exists (/dev/shm/ups-health.json); add capacity_ah_measured, capacity_ah_rated, capacity_confidence, capacity_samples_count fields |

## Standard Stack

### Core Infrastructure (Already in Place)
| Component | Version | Purpose | Status |
|-----------|---------|---------|--------|
| systemd.journal | Python 3.11+ | Structured logging to journald | ✓ Integrated via `systemd.journal.JournalHandler` |
| Bash scripts | 5.0+ | MOTD module execution | ✓ Existing motd/51-ups.sh, 51-ups-health.sh |
| JSON (atomic writes) | stdlib | Persistence of capacity estimates | ✓ Existing `model.json` with `atomic_write_json()` |
| pytest | 8.3.5+ | Test framework for validation | ✓ Configured in pytest.ini |

### Reporting Channels (No New Dependencies)
| Channel | Format | Frequency | Consumer |
|---------|--------|-----------|----------|
| MOTD (51-ups.sh) | Bash + jq, human-readable text | Every login (via config.fish) | User reading terminal login message |
| journald | Structured logs with MESSAGE, PRIORITY, SYSLOG_IDENTIFIER | Every measurement + convergence change | `journalctl -t ups-battery-monitor \| grep capacity` |
| /health endpoint | JSON at /dev/shm/ups-health.json | Every poll (10s) | Grafana scraper, monitoring tools |

### Libraries Already in Use
- **python-systemd**: JournalHandler for structured journald integration (already a dependency in pyproject.toml)
- **json**: Atomic persistence and telemetry serialization (stdlib)
- **jq**: MOTD parsing of model.json (system package, already used in 51-ups.sh)

## Architecture Patterns

### Pattern 1: MOTD Module Extension
**What:** Bash shell script in `scripts/motd/NN-name.sh` executed sequentially at login.

**When to use:** For user-facing status displays on SSH login, cron job outputs, or diagnostic dashboards.

**Characteristics:**
- Runs with user environment (HOME, PATH, etc. available)
- Must exit cleanly (exit 0) regardless of data availability
- Typically uses jq for JSON parsing (faster than Python subprocess)
- Output goes to stdout (captured by runner.sh)
- Should be idempotent (no side effects)

**Current implementation (51-ups.sh):**
- Reads `~/.config/ups-battery-monitor/model.json`
- Extracts `capacity_estimates[]` array
- Computes confidence via Python subprocess for numerical accuracy
- Formats output: "Capacity: X.XAh (measured) vs Y.YAh (rated), Z/3 deep discharges, NN% confidence"
- Shows new battery alert if `new_battery_detected == true`

**Example:**
```bash
# Source: /home/j2h4u/repos/j2h4u/ups-battery-monitor/scripts/motd/51-ups.sh (existing, lines 27-68)
latest_ah=$(jq -r '.capacity_estimates[-1].ah_estimate' "$MODEL_FILE" 2>/dev/null)
confidence_percent=$(python3 << 'PYTHON_EOF' 2>/dev/null || echo "0"
import json
estimates = json.load(...)['capacity_estimates']
cov = std_ah / mean_ah
confidence = max(0, min(100, int((1 - cov) * 100)))
print(confidence)
PYTHON_EOF
)
echo "  Capacity: ${latest_ah}Ah (measured) vs ${rated_ah}Ah (rated), ${sample_count}/3 deep discharges, ${confidence_percent}% confidence"
```

### Pattern 2: Structured journald Logging
**What:** Use `logger` or `systemd.journal.send()` to write structured events with MESSAGE, PRIORITY, and custom fields.

**When to use:** For:
- Event tracking (measurement collection, convergence reached)
- Searchable audit trails (`journalctl -t ups-battery-monitor -j capacity_measurement`)
- Integration with centralized logging (Grafana Loki, ELK stack)
- Debugging discharge sequences and capacity trends

**Fields to log (custom MESSAGE + standard journald):**
```json
{
  "MESSAGE": "capacity_measurement: 6.95Ah (±0.12), CoV=0.073 (3 samples, 92% confidence)",
  "SYSLOG_IDENTIFIER": "ups-battery-monitor",
  "PRIORITY": "6",  // INFO
  "EVENT_TYPE": "capacity_measurement",
  "EVENT_VERSION": "1",
  "CAPACITY_AH": "6.95",
  "CONFIDENCE": "0.92",
  "SAMPLE_COUNT": "3",
  "DELTA_SOC_PERCENT": "72.5",
  "DURATION_SEC": "3124"
}
```

**Python implementation pattern (existing in monitor.py):**
```python
# Source: monitor.py (using systemd.journal.JournalHandler)
logger = logging.getLogger(__name__)
# Handler already configured in _setup_logging() — just call:
logger.info(f"capacity_measurement: {ah_estimate:.2f}Ah (±{std_ah:.2f}), CoV={cov:.3f} ({sample_count} samples, {confidence_pct:.0f}% confidence)",
    extra={
        'EVENT_TYPE': 'capacity_measurement',
        'CAPACITY_AH': f'{ah_estimate:.2f}',
        'CONFIDENCE': f'{confidence:.2f}',
        'SAMPLE_COUNT': str(sample_count),
        'DELTA_SOC_PERCENT': f'{metadata["delta_soc_percent"]:.1f}',
        'DURATION_SEC': str(int(metadata['duration_sec']))
    }
)
```

**Query examples for user:**
```bash
# All capacity events
journalctl -t ups-battery-monitor -j EVENT_TYPE=capacity_measurement

# Find when convergence occurred
journalctl -t ups-battery-monitor -j EVENT_TYPE=baseline_lock -n 10

# Track confidence growth
journalctl -t ups-battery-monitor -j EVENT_TYPE=confidence_update --output=json | jq '.[] | .CONFIDENCE'
```

### Pattern 3: Health Endpoint Extension
**What:** Atomic JSON file at `/dev/shm/ups-health.json` written every poll (10s), scraped by Grafana.

**When to use:** For real-time metrics export to monitoring systems (Prometheus, Grafana, custom collectors).

**Characteristics:**
- Written atomically (tempfile + fdatasync + rename) to prevent partial reads
- Located in /dev/shm (tmpfs) for fast I/O and automatic cleanup on reboot
- Single source of truth for daemon state during current poll
- No persistence across daemon restart (design choice: state is in model.json)

**Current structure (monitor.py, _write_health_endpoint()):**
```json
{
  "last_poll": "2026-03-16T10:30:45.123456+00:00",
  "last_poll_unix": 1710584445,
  "current_soc_percent": 75.3,
  "online": true,
  "daemon_version": "1.1.0",
  "poll_latency_ms": 2.1
}
```

**Phase 14 extension (capacity fields):**
```json
{
  "last_poll": "2026-03-16T10:30:45.123456+00:00",
  "last_poll_unix": 1710584445,
  "current_soc_percent": 75.3,
  "online": true,
  "daemon_version": "1.1.0",
  "poll_latency_ms": 2.1,
  "capacity_ah_measured": 6.95,
  "capacity_ah_rated": 7.2,
  "capacity_confidence": 0.92,
  "capacity_samples_count": 3,
  "capacity_converged": true,
  "convergence_status": "locked (3 samples, CoV=0.073)"
}
```

**Grafana scraping pattern:**
```
# Prometheus scrape config (if used)
- job_name: 'ups-battery-monitor'
  scrape_interval: 30s
  static_configs:
    - targets: ['localhost:9100']  # Node exporter or custom endpoint

# Or direct JSON parsing in Grafana:
# Data source: JSON API → URL: http://localhost:9100/ups-health
# Extract fields: capacity_ah_measured, capacity_confidence, etc.
```

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Structured logging to journald | Custom subprocess calls to `logger` binary | `systemd.journal` Python module (already imported in monitor.py) | Properly handles field escaping, priority levels, identifier tags; integrates with systemd log rotation and retention |
| JSON parsing in MOTD | Custom regex or shell parsing of model.json | `jq` command-line tool (system package, already used) | Robust JSON path extraction, handles escaping, null values, missing keys gracefully |
| Atomic file writes | Simple `echo > file` | `atomic_write_json()` pattern (already implemented in model.py) | Prevents corruption on crash/power loss; uses fdatasync for data safety without inode overhead |
| Confidence calculation | Shell arithmetic (bc/awk) | Python subprocess in MOTD or Python-calculated at daemon-side | Floating-point arithmetic accuracy; CoV formula avoids rounding errors; Python available in MOTD already |
| Grafana dashboard creation | Hand-coded JSON dashboard | Pre-built query templates + user customization | Dashboards change frequently; templates let users fork and adjust without re-implementing logic |

**Key insight:** Reporting is already ~90% done (MOTD framework, health endpoint, journald handler). Phase 14 is mostly wiring: thread capacity_estimates fields through existing channels, add event tagging, extend JSON schema.

## Common Pitfalls

### Pitfall 1: MOTD Over-Complication
**What goes wrong:** Adding too much logic to MOTD bash scripts makes them brittle. Slow subprocess calls (Python, external commands) delay login. Complex numerical calculations in bash introduce rounding errors.

**Why it happens:** Temptation to "calculate confidence at display time" or fetch data from external sources. MOTD should be read-only, fast, and handle missing data gracefully.

**How to avoid:**
- Compute confidence at daemon side (monitor.py during _handle_discharge_complete); store in model.json
- MOTD reads pre-computed values only; optional Python subprocess for final formatting (already done for CoV)
- Exit cleanly if model.json missing (exit 0, not error)
- Use jq with default fallbacks (`// "?"`, `// "N/A"`)

**Warning signs:**
- MOTD script takes >100ms to run (time it with `time ./scripts/motd/51-ups.sh`)
- Subprocess calls without timeout (could hang on model.json lock)
- Complex floating-point arithmetic in bash (`bc` context loss)

### Pitfall 2: journald Field Over-Specification
**What goes wrong:** Adding too many custom fields to journald entries creates maintenance burden. Field names change, parsers break. Inconsistent naming (capacity_ah vs capacityAh vs CAPACITY_AH) makes querying unreliable.

**Why it happens:** Desire for perfect traceability. But journald is optimized for a few key fields + MESSAGE text.

**How to avoid:**
- Define field naming convention upfront: SCREAMING_SNAKE_CASE for custom fields
- Standardize event types: capacity_measurement, confidence_update, baseline_lock, baseline_reset (4 types total)
- Keep MESSAGE human-readable; custom fields are for filtering/aggregation only
- Document query patterns in RESEARCH.md for future reference

**Warning signs:**
- `journalctl -F` returns >50 fields (bloat)
- Same data logged in both MESSAGE and custom field (redundancy)
- Field values change between log entries (inconsistent schema)

### Pitfall 3: Health Endpoint Schema Drift
**What goes wrong:** Adding capacity fields to /health endpoint without versioning breaks existing Grafana dashboards. Dashboards reference field names; if field renamed or removed, queries silently return null.

**Why it happens:** JSON schema is unversioned; assumption that "new fields won't hurt" leads to schema becoming unplanned superset of old + new + experimental fields.

**How to avoid:**
- Extend schema, don't replace: add `capacity_ah_measured`, `capacity_ah_rated`, `capacity_confidence` alongside existing fields
- Include daemon version in endpoint (already done: `daemon_version`); Grafana can warn if scraping version mismatch
- Document expected fields in code comment above _write_health_endpoint()
- Test that old field names still exist (test_health_endpoint_soc_precision covers this)

**Warning signs:**
- Health endpoint JSON size growing unbounded (>10KB)
- Field names changing between commits (grep history)
- Missing fields when capacity_estimates array empty (should have fallbacks)

### Pitfall 4: Confidence Confidence (Expressing Uncertainty)
**What goes wrong:** Displaying "92% confidence" when only 3 samples collected creates false precision. User thinks "highly sure," but early measurements are noisy.

**Why it happens:** Confidence formula (1 - CoV) is mathematically correct but not a statistical confidence interval. For n=3, CoV < 10% happens frequently by chance.

**How to avoid:**
- Show both sample count AND confidence: "2/3 samples, 45% confidence" (already done in 51-ups.sh)
- In Grafana, plot confidence over time with confidence band (scatter + envelope)
- Document in MOTD output or tooltip: "Confidence increases with more deep discharges; 3+ samples locked at 95%+"
- In user docs: explain that "confidence" is convergence_score, not statistical CI

**Warning signs:**
- User asks "why does confidence drop sometimes?" (it fluctuates with new measurements)
- Confidence calculation changes between releases (MOTD and daemon compute differently)

### Pitfall 5: Timestamp Inconsistency Between Channels
**What goes wrong:** MOTD shows "measured at 2026-03-16T10:30", journald logs "2026-03-16 10:30:15.123", health endpoint "last_poll_unix: 1710584400". User can't correlate events across channels.

**Why it happens:** Each channel uses different timestamp format/source. MOTD reads model.json (stored as ISO8601 string), journald gets syslog timestamp, health endpoint uses time.time().

**How to avoid:**
- Adopt single canonical timestamp: ISO8601 UTC (like health endpoint's `last_poll`)
- All channels reference same timestamp or clearly label differences
- In capacity_estimates array, store `timestamp: "2026-03-16T10:30:45Z"` (ISO8601)
- MOTD display: "Capacity: 6.95Ah (measured at 2026-03-16 10:30)"
- journald: extract timestamp from message for consistency

**Warning signs:**
- User asks "which measurement does this correspond to?" when reading journald output
- MOTD shows different timestamp than latest model.json entry

## Code Examples

Verified patterns from official sources and existing codebase:

### Example 1: Extending /health Endpoint with Capacity Fields
```python
# Source: src/monitor.py, _write_health_endpoint() function (lines ~1280-1310)
def _write_health_endpoint(soc_percent: float, is_online: bool, poll_latency_ms: Optional[float] = None,
                           capacity_ah_measured: Optional[float] = None,
                           capacity_ah_rated: float = 7.2,
                           capacity_confidence: float = 0.0,
                           capacity_samples_count: int = 0,
                           capacity_converged: bool = False) -> None:
    """Extend health endpoint with capacity metrics."""
    try:
        daemon_version = importlib.metadata.version('ups-unfucked')
    except importlib.metadata.PackageNotFoundError:
        daemon_version = "unknown"

    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(soc_percent, 1),
        "online": is_online,
        "daemon_version": daemon_version,
        "poll_latency_ms": round(poll_latency_ms, 1) if poll_latency_ms is not None else None,
        # Phase 14: capacity fields
        "capacity_ah_measured": round(capacity_ah_measured, 2) if capacity_ah_measured else None,
        "capacity_ah_rated": round(capacity_ah_rated, 2),
        "capacity_confidence": round(capacity_confidence, 3),
        "capacity_samples_count": capacity_samples_count,
        "capacity_converged": capacity_converged,
    }

    health_path = Path("/dev/shm/ups-health.json")
    try:
        atomic_write_json(health_path, health_data)
        logger.debug(f"Health endpoint written with capacity metrics to {health_path}")
    except Exception as e:
        logger.error(f"Failed to write health endpoint: {e}")
```

### Example 2: Structured Journald Event Logging
```python
# Source: src/monitor.py, in _handle_discharge_complete() method
# When a capacity measurement is recorded:
if capacity_estimate is not None:
    ah_estimate, confidence, metadata = capacity_estimate
    sample_count = self.capacity_estimator.get_convergence_status()[2]  # returns (converged, measured_ah, count)

    # Compute CoV for reporting
    estimates = self.model.data.get('capacity_estimates', [])
    ah_values = [e['ah_estimate'] for e in estimates]
    if len(ah_values) >= 2:
        mean_ah = sum(ah_values) / len(ah_values)
        std_ah = (sum((x - mean_ah) ** 2 for x in ah_values) / len(ah_values)) ** 0.5
        cov = std_ah / mean_ah
    else:
        cov = 0.0

    confidence_pct = int((confidence) * 100) if confidence else 0

    # Log to journald with structured fields
    logger.info(
        f"capacity_measurement: {ah_estimate:.2f}Ah (±{std_ah:.2f}), CoV={cov:.3f} "
        f"({sample_count} samples, {confidence_pct}% confidence)",
        extra={
            'EVENT_TYPE': 'capacity_measurement',
            'CAPACITY_AH': f'{ah_estimate:.2f}',
            'CONFIDENCE_PERCENT': str(confidence_pct),
            'SAMPLE_COUNT': str(sample_count),
            'DELTA_SOC_PERCENT': f'{metadata["delta_soc_percent"]:.1f}',
            'DURATION_SEC': str(int(metadata['duration_sec'])),
            'LOAD_AVG_PERCENT': f'{metadata["load_avg_percent"]:.1f}',
        }
    )

    # When convergence detected:
    if self.capacity_estimator.get_convergence_status()[0]:
        logger.info(
            f"baseline_lock: capacity converged at {ah_estimate:.2f}Ah after {sample_count} deep discharges",
            extra={
                'EVENT_TYPE': 'baseline_lock',
                'CAPACITY_AH': f'{ah_estimate:.2f}',
                'SAMPLE_COUNT': str(sample_count),
                'TIMESTAMP': datetime.now(timezone.utc).isoformat(),
            }
        )
```

### Example 3: MOTD Integration with Convergence Status
```bash
# Source: scripts/motd/51-ups.sh (enhancement to existing lines 16-69)
# Read convergence status via Python for accuracy
convergence_status=$(python3 << 'PYTHON_EOF' 2>/dev/null || echo "unknown,0,0"
import json
import os

try:
    with open(os.path.expanduser('~/.config/ups-battery-monitor/model.json')) as f:
        model = json.load(f)
except:
    print("unknown,0,0")
    exit(0)

estimates = model.get('capacity_estimates', [])
if not estimates:
    print("unknown,0,0")
else:
    sample_count = len(estimates)
    ah_values = [e['ah_estimate'] for e in estimates]
    mean_ah = sum(ah_values) / len(ah_values) if ah_values else 0

    if len(ah_values) < 3:
        status = "measuring"
        convergence_score = 0
    else:
        variance = sum((x - mean_ah) ** 2 for x in ah_values) / len(ah_values)
        std_ah = variance ** 0.5
        cov = std_ah / mean_ah if mean_ah > 0 else 0
        convergence_score = int(max(0, min(100, (1 - cov) * 100)))
        status = "locked" if cov < 0.10 else "measuring"

    print(f"{status},{sample_count},{convergence_score}")
PYTHON_EOF
)

IFS=',' read -r status sample_count confidence_pct <<< "$convergence_status"

# Format output with status badge
if [[ "$status" == "locked" ]]; then
    status_badge="✓ LOCKED"
    status_color="$GREEN"
elif [[ "$status" == "measuring" ]]; then
    status_badge="⟳ MEASURING"
    status_color="$YELLOW"
else
    status_badge="? UNKNOWN"
    status_color="$DIM"
fi

echo "  Capacity: ${latest_ah}Ah (measured) vs ${rated_ah}Ah (rated) · ${status_color}${status_badge}${NC} · ${sample_count}/3 samples · ${confidence_pct}% confidence"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| **No capacity reporting** | Expose via MOTD, journald, /health | Phase 14 (2026-03-16) | Users can now see convergence progress and confidence; Grafana can plot trends |
| **Ad-hoc logging** | Structured journald with EVENT_TYPE tags | Phase 14 | Enables `journalctl -j EVENT_TYPE=capacity_measurement` queries; integrates with log aggregation |
| **Health endpoint with 6 fields** | Extended to 10+ fields (capacity + convergence) | Phase 14 | Grafana dashboards can now visualize capacity metrics alongside SoC, runtime, SoH |
| **MOTD module shows "2/3" progress** | Now includes confidence %, status badge, convergence lock state | Phase 14 | User understands when measurement is ready vs still collecting data |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.5 (configured in pytest.ini) |
| Config file | pytest.ini (testpaths=tests, addopts=-v --tb=short) |
| Quick run command | `pytest tests/test_motd.py -v` (~2 seconds) |
| Full suite command | `pytest tests/ -v` (~10 seconds, 295+ tests) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| RPT-01 | MOTD displays capacity, confidence, sample count, convergence status | integration | `pytest tests/test_motd.py::test_motd_capacity_displays -v` | ❌ Wave 0 |
| RPT-01 | MOTD handles missing capacity_estimates gracefully (no crash) | unit | `pytest tests/test_motd.py::test_motd_handles_empty_estimates -v` | ❌ Wave 0 |
| RPT-02 | journald logs capacity_measurement events with EVENT_TYPE tag | unit | `pytest tests/test_monitor.py::test_journald_capacity_event_logged -v` | ❌ Wave 0 |
| RPT-02 | journald logs baseline_lock when convergence detected | unit | `pytest tests/test_monitor.py::test_journald_baseline_lock_event -v` | ❌ Wave 0 |
| RPT-02 | journald logs are queryable by EVENT_TYPE (integration) | integration | `pytest tests/test_monitor_integration.py::test_journald_event_filtering -v` | ❌ Wave 0 |
| RPT-03 | /health endpoint includes capacity_ah_measured, capacity_ah_rated, capacity_confidence fields | unit | `pytest tests/test_monitor.py::test_health_endpoint_capacity_fields -v` | ❌ Wave 0 |
| RPT-03 | /health endpoint capacity_converged flag matches estimator state | unit | `pytest tests/test_monitor.py::test_health_endpoint_convergence_flag -v` | ❌ Wave 0 |
| RPT-03 | /health endpoint updates capacity fields after each discharge | integration | `pytest tests/test_monitor_integration.py::test_health_endpoint_capacity_persistence -v` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_motd.py tests/test_monitor.py -v --tb=short` (capacity-specific tests)
- **Per wave merge:** `pytest tests/ -v` (full suite, ensure no regressions)
- **Phase gate:** Full suite green + manual verification that MOTD displays correctly, journalctl queries work, Grafana can scrape /health

### Wave 0 Gaps
- [ ] `tests/test_motd.py::test_motd_capacity_displays` — MOTD shows correct Ah, confidence%, sample count format
- [ ] `tests/test_motd.py::test_motd_handles_empty_estimates` — MOTD exits cleanly if capacity_estimates missing
- [ ] `tests/test_motd.py::test_motd_convergence_status_badge` — MOTD shows ✓ LOCKED or ⟳ MEASURING badge
- [ ] `tests/test_monitor.py::test_journald_capacity_event_logged` — Verify EVENT_TYPE=capacity_measurement in journald
- [ ] `tests/test_monitor.py::test_journald_baseline_lock_event` — Verify EVENT_TYPE=baseline_lock when CoV < 10%
- [ ] `tests/test_monitor_integration.py::test_journald_event_filtering` — Query `journalctl -j EVENT_TYPE=capacity_measurement`
- [ ] `tests/test_monitor.py::test_health_endpoint_capacity_fields` — /health endpoint contains new capacity fields (JSON structure test)
- [ ] `tests/test_monitor.py::test_health_endpoint_convergence_flag` — capacity_converged flag matches get_convergence_status()
- [ ] `tests/test_monitor_integration.py::test_health_endpoint_capacity_persistence` — Verify /health persists across discharge cycles

**Framework install:** pytest already installed (`pip3 install pytest python-systemd`), no additional setup needed.

## Confidence Breakdown

### Standard Stack: HIGH
- MOTD framework, health endpoint, journald integration all proven in v1.1 codebase
- Zero new external dependencies (jq system package, python-systemd already imported)
- Code patterns established: atomic JSON writes, structured logging via systemd.journal, bash script conventions
- Test infrastructure in place: 295 existing tests, pytest.ini configured

### Architecture Patterns: HIGH
- MOTD extension: follow existing 51-ups.sh pattern (lines 27-76), add convergence status display
- journald logging: systemd.journal.JournalHandler already initialized in monitor.py, just add EVENT_TYPE custom fields
- Health endpoint: atomic write pattern proven (_write_health_endpoint exists), extend JSON schema with capacity fields
- All patterns verified in Phase 12-13 code and test suites

### Pitfalls: MEDIUM
- MOTD over-complication: mitigated by existing CoV calculation done in Python (confirmed in 51-ups.sh lines 32-62)
- journald field consistency: proposed naming (SCREAMING_SNAKE_CASE) aligns with systemd conventions, but not yet verified in codebase
- Health endpoint schema drift: existing tests verify field presence (test_health_endpoint_soc_precision), can extend
- Confidence precision: existing MOTD already shows "NN% confidence" (51-ups.sh line 68), verified to work
- Timestamp consistency: model.json stores ISO8601 (confirmed in capacity_estimates array), MOTD reads it

### Common Pitfalls: MEDIUM
- Pitfalls identified through expert panel review (STATE.md lines 203-209) and existing MOTD/health endpoint tests
- Mitigation strategies match v1.1 patterns: exit gracefully, use jq, compute early, store atomically
- Warning signs documented in codebase (test_health_endpoint_*) — no new validation needed

## Open Questions

1. **Grafana pre-built dashboards vs user custom queries?**
   - What we know: /health endpoint provides raw metrics; Grafana can scrape and plot
   - What's unclear: should Phase 14 include example dashboard JSON (Grafana export format)?
   - Recommendation: Provide Grafana query examples in RESEARCH.md for users; defer pre-built dashboard to optional Phase 15 (out of scope for v2.0)

2. **MOTD multi-line vs single-line format?**
   - What we know: existing output is single-line with status badge
   - What's unclear: should convergence progress be on same line or separate line for readability?
   - Recommendation: Keep single-line format to avoid MOTD output bloat. Users who want detailed trend can use `journalctl` or Grafana.

3. **How to handle temporary confidence drops?**
   - What we know: confidence = 1 - CoV fluctuates as new measurements arrive
   - What's unclear: should MOTD show moving average? Should we filter transient drops?
   - Recommendation: Show raw convergence_score for transparency; document in tooltip that "confidence improves with more samples." No smoothing needed.

4. **Backward compatibility for capacity_estimates array?**
   - What we know: Phase 12 stores capacity_estimates in model.json
   - What's unclear: if user upgrades to Phase 14, old estimates without timestamp field cause MOTD parse issues?
   - Recommendation: Model.load() already handles schema evolution; MOTD uses jq default fallbacks (`// "?"`) for missing fields. Test with old model.json (Phase 12 final format).

5. **journald retention: how long to keep events?**
   - What we know: systemd journald default retention is 1 week
   - What's unclear: should Phase 14 configure custom retention for capacity events (longer history for trending)?
   - Recommendation: Use system defaults (1 week, ~100M). Users who want long-term trend use Grafana persistence. Capacity_estimates array already stored in model.json for long-term history.

## Sources

### Primary (HIGH confidence)
- **Existing codebase:** src/monitor.py (_write_health_endpoint, logger initialization), scripts/motd/51-ups.sh (MOTD pattern), src/model.py (atomic_write_json, capacity_estimates array schema)
- **Test infrastructure:** tests/test_monitor.py (health endpoint tests), tests/test_motd.py (MOTD integration), pytest.ini configuration
- **Python stdlib:** systemd.journal module (python-systemd package), json, logging

### Secondary (MEDIUM confidence)
- **STATE.md:** Phase 12 completion details, convergence_status() method, capacity_estimates structure
- **REQUIREMENTS.md:** RPT-01, RPT-02, RPT-03 requirement definitions
- **Expert panel notes (STATE.md):** Validation gates for capacity reporting, field naming recommendations

### Tertiary (Information only)
- Project memory: MEMORY.md research files on battery metrics (background context, not directly used for Phase 14 planning)

## Metadata

**Confidence breakdown:**
- Standard Stack: HIGH — all components exist and proven in v1.1, no new dependencies
- Architecture Patterns: HIGH — patterns established in Phase 12-13, tests in place
- Pitfalls: MEDIUM — identified through expert review and existing MOTD analysis, mitigation strategies known
- Common Pitfalls: MEDIUM — source material is STATE.md expert panels, patterns verified in codebase
- Validation: MEDIUM — test gaps identified (Wave 0 list); existing test framework strong, new tests straightforward

**Research date:** 2026-03-16
**Valid until:** 2026-03-23 (stable domain, no fast-moving dependencies)

**Key assumption:** Phases 12-13 complete with capacity_estimates array populated and convergence_status() method working. Phase 14 is purely reporting layer; no algorithmic changes.
