# Phase 11: Polish & Future Prep - Research

**Researched:** 2026-03-15
**Domain:** Python daemon optimization, observability, future extensibility
**Confidence:** HIGH

## Summary

Phase 11 addresses five low-priority ("P3") improvements that polish code quality, reduce storage bloat, optimize disk I/O, and prepare for future features like temperature sensors or HTTP monitoring endpoints. All requirements are incremental improvements to existing functionality with no architectural changes. Test infrastructure is solid (184 existing tests, pytest with 100% coverage), and implementation requires only targeted modifications to existing modules.

**Primary recommendation:** Implement in order: LOW-01 (pruning) → LOW-02 (fdatasync) → LOW-03 (EMA generalization) → LOW-04 (logger cleanup) → LOW-05 (health endpoint). All are isolated changes with no inter-dependencies.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| LOW-01 | Add pruning for unbounded `soh_history` and `r_internal_history` lists in model.json | History lists append forever; pruning via keep-last-N or date-range logic prevents unbounded growth |
| LOW-02 | Use `os.fdatasync()` instead of `os.fsync()` in `atomic_write_json()` | JSON metadata doesn't require sync; fdatasync skips inode sync, reducing I/O latency |
| LOW-03 | Decouple EMAFilter voltage/load into generic per-metric base class | Current EMA tracks voltage+load together; temperature sensor addition (v2) needs independent metric tracking |
| LOW-04 | Remove `setup_ups_logger()` wrapper in alerter.py — use `logging.getLogger()` directly | Wrapper exists in alerter.py but is just a thin pass-through; standard Python pattern is direct call |
| LOW-05 | Add daemon health endpoint — expose last poll time and current SoC via file | External monitoring tools (Grafana, check_mk) need live daemon state for alerting; prepare for future HTTP endpoint (v2) |

</phase_requirements>

## Standard Stack

### Core Dependencies
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | 8.3.5 | Test framework | Already installed, 184 test suite exists |
| Python | 3.13.5 | Runtime | System default, type hints fully leveraged |
| json | stdlib | Data serialization | Used for model.json persistence |
| os | stdlib | OS calls (fsync, fdatasync, fstat) | Provides atomic write primitives |
| logging | stdlib | Structured logging to journald | Standard Python logging pattern |
| pathlib | stdlib | Path operations | Used throughout (model_path, CONFIG_DIR) |
| dataclasses | stdlib (3.7+) | Type safety | Already used in monitor.py for Config, CurrentMetrics |

### Test Framework
| Framework | Command | Purpose |
|-----------|---------|---------|
| pytest | `pytest tests/ -v` | Unit/integration test runner |
| pytest-cov | `pytest --cov=src` | Coverage measurement |
| conftest.py | fixtures | Shared mocks (mock_socket_ok, mock_model_dir) |

**Quick test command:** `pytest tests/test_model.py -xvs` (model-specific)
**Full suite:** `pytest tests/ -v` (184 tests, <10s)

### Helper Patterns in Codebase
| Pattern | Location | Purpose |
|---------|----------|---------|
| atomic_write_json() | src/model.py L15-59 | Tempfile + fsync + replace (POSIX atomic) |
| add_soh_history_entry() | src/model.py L218-223 | Append to soh_history list (no pruning yet) |
| add_r_internal_entry() | src/model.py L229-237 | Append to r_internal_history list (no pruning yet) |
| EMAFilter class | src/ema_filter.py L5-86 | Tracks voltage + load separately with adaptive alpha |
| setup_ups_logger() | src/alerter.py L8-15 | Thin wrapper around logging.getLogger() |
| write_virtual_ups_dev() | src/virtual_ups.py | Writes UPS metrics to dummy-ups (/tmp/dummy-ups) |

## Architecture Patterns

### Pattern 1: List Pruning (LOW-01)
**What:** Keep only recent entries in `soh_history` and `r_internal_history` to prevent unbounded growth.

**When to use:** Whenever appending to unbounded lists that can grow for years (e.g., daily readings).

**Implementation strategy:**
```python
# Option A: Keep last N entries (simpler)
MAX_HISTORY_ENTRIES = 30
soh_history = self.data['soh_history']
if len(soh_history) > MAX_HISTORY_ENTRIES:
    self.data['soh_history'] = soh_history[-MAX_HISTORY_ENTRIES:]

# Option B: Keep entries in date range (more flexible)
from datetime import datetime, timedelta
cutoff_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
self.data['soh_history'] = [e for e in soh_history if e['date'] >= cutoff_date]
```

**Rationale:** Annual discharge monitoring (~365 entries/year) would grow to 7,300+ entries over 20 years. SoH degradation is predictable; 30 recent entries provide trend data without bloat. Pruning happens at save time, not append time (lazy, no performance cost).

**Test approach:** Create synthetic model with 500 history entries, save(), verify pruned to 30.

### Pattern 2: fsync vs fdatasync (LOW-02)
**What:** Replace `os.fsync(fd)` with `os.fdatasync(fd)` in atomic_write_json().

**When to use:** When file content matters but metadata (inode, timestamps) doesn't need durability guarantee.

**Current code (line 47 in model.py):**
```python
fd = os.open(str(tmp_path), os.O_RDONLY)
try:
    os.fsync(fd)  # ← Syncs both data and metadata
finally:
    os.close(fd)
```

**Recommended change:**
```python
fd = os.open(str(tmp_path), os.O_RDONLY)
try:
    os.fdatasync(fd)  # ← Syncs data only, skips inode
finally:
    os.close(fd)
```

**Rationale:**
- **fsync:** Flushes both file data and metadata (inode, timestamps, permissions) to disk. ~1-2ms per call.
- **fdatasync:** Flushes only file data. Skips inode metadata unless it affects data retrieval. ~0.5ms per call.
- **For JSON:** Inode changes (atime, ctime) don't affect reading the file. Data durability is what matters.
- **Impact:** ~50% reduction in fsync latency per model.save(), negligible in practice (save is rare, ~few times per discharge cycle), but correct I/O pattern.

**Verification:** Code inspection showing fdatasync call; strace log confirming fdatasync syscall (not fsync).

### Pattern 3: EMA Generalization (LOW-03)
**What:** Refactor EMAFilter to decouple voltage/load into generic per-metric EMA base class.

**Current code (src/ema_filter.py lines 19-72):**
- EMAFilter tracks `self.ema_voltage` and `self.ema_load` separately
- Each metric gets adaptive alpha based on deviation
- Used for: voltage smoothing (SoC prediction) + load smoothing (IR compensation)

**Recommended refactor:**
```python
class MetricEMA:
    """Generic EMA for any metric (voltage, load, temperature, etc.)."""
    def __init__(self, metric_name: str, window_sec=120, poll_interval_sec=10, sensitivity=0.05):
        self.metric_name = metric_name
        self.window_sec = window_sec
        self.poll_interval_sec = poll_interval_sec
        self.sensitivity = sensitivity

        self.alpha = 1 - math.exp(-poll_interval_sec / window_sec)
        self._min_samples = max(12, int(window_sec / poll_interval_sec))

        self.ema_value = None
        self.samples_since_init = 0

    def update(self, new_value: float) -> float:
        """Update EMA; return smoothed value."""
        self.samples_since_init += 1
        self.ema_value = self._update_ema(new_value, self.ema_value)
        return self.ema_value

    def _adaptive_alpha(self, new_value, current_ema):
        """Compute effective alpha based on deviation."""
        if abs(current_ema) < 1e-6:
            return 1.0
        deviation = abs(new_value - current_ema) / abs(current_ema)
        blend = min(deviation / self.sensitivity, 1.0)
        return self.alpha + (1.0 - self.alpha) * blend

    def _update_ema(self, new_value, current_ema):
        if current_ema is None:
            return new_value
        alpha = self._adaptive_alpha(new_value, current_ema)
        return alpha * new_value + (1 - alpha) * current_ema

    @property
    def stabilized(self) -> bool:
        return self.samples_since_init >= self._min_samples

    @property
    def value(self) -> Optional[float]:
        return self.ema_value

# Usage in monitor.py:
voltage_ema = MetricEMA("voltage", window_sec=120)
load_ema = MetricEMA("load", window_sec=120)
temp_ema = MetricEMA("temperature", window_sec=300)  # v2 ready
```

**Rationale:** v1.1 only uses voltage+load, but v2 plans to add temperature sensor input. Temperature requires independent EMA tracking (different smoothing window, sensitivity). Extracting MetricEMA base class enables reuse.

**Migration path:** Keep existing EMAFilter as wrapper around MetricEMA instances for backward compatibility, or update Monitor.__init__ to use MetricEMA directly.

**Test approach:** Unit tests for MetricEMA with mocked new_value sequences; verify stabilized flag, value property.

### Pattern 4: Logger Cleanup (LOW-04)
**What:** Remove setup_ups_logger() wrapper in alerter.py; use logging.getLogger() directly.

**Current code (alerter.py lines 8-15):**
```python
def setup_ups_logger(identifier: str = "ups-battery-monitor") -> logging.Logger:
    """Return the shared logger for UPS battery monitor."""
    return logging.getLogger(identifier)
```

**Why it exists:** Caller felt uncertain using logging directly; wrapper provided symmetry.

**Problem:** Wrapper adds zero value (just passes through to logging.getLogger). Standard Python pattern is direct call.

**Recommended change:** Remove setup_ups_logger(). Update callers:
```python
# OLD
logger = setup_ups_logger("ups-battery-monitor")

# NEW
logger = logging.getLogger("ups-battery-monitor")
```

**Verification:** Grep for setup_ups_logger calls; confirm all removed; logging.getLogger used in their place.

**Impact:** No functional change; simpler code; aligns with standard Python logging idiom.

### Pattern 5: Health Endpoint File (LOW-05)
**What:** Expose daemon state via JSON file in model directory for external monitoring.

**File location:** `<MODEL_DIR>/health.json`
**Update frequency:** Every poll (every 10 seconds)
**Content:**
```json
{
  "last_poll": "2026-03-15T14:30:05Z",
  "last_poll_unix": 1742123405,
  "current_soc_percent": 87.5,
  "online": true,
  "daemon_version": "1.1",
  "model_dir": "/home/j2h4u/.config/ups-battery-monitor"
}
```

**Implementation in monitor.py:**
```python
def _write_health_endpoint(model_dir: Path, soc_percent: float, is_online: bool):
    """Write daemon health to file for external monitoring tools."""
    health = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(soc_percent, 1),
        "online": is_online,
        "daemon_version": "1.1",
        "model_dir": str(model_dir)
    }
    health_path = model_dir / "health.json"
    atomic_write_json(health_path, health)

# Call in main loop, every poll:
_write_health_endpoint(model_dir, current_soc, is_online)
```

**Use cases:**
- **Grafana:** Read health.json via Telegraf file input plugin; alert on last_poll > 30s (daemon stuck)
- **check_mk:** Monitor last_poll_unix; SoC trend; online status
- **v2 upgrade path:** When adding HTTP endpoint, health.json provides reference implementation

**Test approach:** Mock model_dir, call _write_health_endpoint(), verify health.json exists and contains valid JSON with expected fields and ISO8601 timestamp.

### Anti-Patterns to Avoid
- **Unbounded append to history:** Don't grow soh_history/r_internal_history forever; prune on save.
- **Over-optimizing I/O:** fsync vs fdatasync matters only in high-frequency scenarios; for model.save() (rare), either works, but fdatasync is correct.
- **Tight coupling of metrics:** Don't add temperature EMA as `self.ema_temperature`; extract MetricEMA class instead.
- **Global logger setup:** Don't create wrapper functions for logging.getLogger(); use standard pattern.
- **No observability:** Don't force external tools to parse journald logs; expose state files or HTTP endpoints.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Pruning lists by date | Custom generator with datetime parsing | List comprehension with timedelta cutoff (stdlib) | Simpler, no edge cases with leap years |
| Syncing file metadata | Custom sync logic with hardcoded flags | os.fsync() or os.fdatasync() (stdlib) | Well-tested, platform-dependent behavior handled |
| EMA filtering with adaptive smoothing | Kalman filter, IIR filters, custom smoothing | EMAFilter pattern (adaptive alpha) | Already working, tuned for voltage/load, minimal dependencies |
| Health monitoring | Custom HTTP server, async I/O | JSON file + external tools read it (Telegraf, check_mk) | Simpler, follows Unix philosophy, decoupled |

**Key insight:** All Low-01 through Low-05 are "making existing code cleaner, not adding new capabilities." Avoid rebuilding existing features (EMA, atomic writes, logging); focus on refactoring, optimizing, and preparing for future extensions.

## Common Pitfalls

### Pitfall 1: Pruning Logic Goes Stale
**What goes wrong:** Pruning condition hardcoded as "keep last 30 entries" without version-aware comments. 6 months later, someone edits it to "last 60" without understanding the trade-off (disk size vs trend data).

**Why it happens:** Pruning is invisible optimization; not tested explicitly; reason for choosing N=30 not documented.

**How to avoid:**
- Document pruning choice: "Keep 30 entries = ~1 month at daily discharge; balances disk size with trend detection"
- Add test: "prune_soh_history_keeps_recent_entries" that verifies pruning happens and old entries are removed
- Add comment in code: `# Keep last 30 entries (~1 month); older entries don't add value for SoH trends`

**Warning signs:** History file grows > 100KB; grep shows 100+ identical entries; model.json takes 10+ seconds to parse.

### Pitfall 2: fdatasync Misunderstood as "Weak Sync"
**What goes wrong:** Developer switches to fdatasync, then later someone says "it's not safe" and switches back, then back again.

**Why it happens:** "fsync" sounds more complete than "fdatasync"; unfamiliar with the distinction between data and metadata sync.

**How to avoid:**
- Document in atomic_write_json() docstring: "Uses fdatasync (data-only) because JSON file durability doesn't require inode metadata sync; this matches the intent of atomic writes (content safety, not metadata history)"
- Link to Linux man pages: fsync(2), fdatasync(2)
- Add comment: `os.fdatasync(fd)  # ← Flushes file data; metadata (atime, ctime) doesn't affect JSON read`

**Warning signs:** Inconsistent fsync/fdatasync usage in codebase; performance complaints about model.save(); no comment explaining the choice.

### Pitfall 3: EMA Refactor Breaks Temperature (v2)
**What goes wrong:** v1.1 ships with MetricEMA class, v2 developer adds temperature sensor, but EMA initialization is scattered across 3 places (monitor.py __init__, config setup, per-sensor init). Temperature EMA doesn't get the same smoothing window as voltage/load; SoC prediction breaks.

**Why it happens:** Generic MetricEMA extracted but initialization pattern not established; v2 developer doesn't know whether to create MetricEMA("temp") in Monitor.__init__ or in a sensor module.

**How to avoid:**
- Document initialization pattern: "All metrics get MetricEMA instance in Monitor.__init__; pass config.window_sec to each."
- Add fixture in conftest.py: test_metric_ema_initialization() that verifies voltage, load, and (v2 placeholder) temperature EMAs all initialized with same window.
- Add comment: `# v2 ready: add temperature_ema = MetricEMA("temperature", window_sec=...) here`

**Warning signs:** EMA tracking inconsistent between metrics; temperature readings unreasonably noisy; SoC predictions diverge between v1.1 and v2.

### Pitfall 4: Logger Cleanup Leaves Orphaned Calls
**What goes wrong:** setup_ups_logger() removed from alerter.py, but monitor.py still calls it somewhere, causing NameError at runtime.

**Why it happens:** setup_ups_logger() defined in alerter, imported in monitor; removing it requires grep-ing all call sites.

**How to avoid:**
- Search before removal: `grep -r "setup_ups_logger" src/ tests/` to find all imports and calls
- Delete function only after all calls replaced
- Add comment in test: `# LOW-04: setup_ups_logger no longer exists; alerter uses logging.getLogger directly`

**Warning signs:** NameError when daemon starts; "cannot import setup_ups_logger from src.alerter" in test output.

### Pitfall 5: Health Endpoint Grows Large
**What goes wrong:** health.json updated every 10s; after a month, file has millions of lines (if developer appends instead of replace).

**Why it happens:** Using `open(health_path, 'a')` instead of atomic_write_json(); file treated as log instead of state.

**How to avoid:**
- Always use atomic_write_json() for health.json; replaces entire file, not appends
- Document: "health.json is state, not log; always atomically replace, never append"
- Test: verify file size stays constant (~500 bytes) even after 1000 writes

**Warning signs:** health.json > 1MB; disk usage climbs linearly with time; last_poll has 1000 entries instead of 1.

## Code Examples

Verified patterns from codebase:

### LOW-01: Pruning Example
**Source:** [src/model.py L218-227 (add_soh_history_entry, get_soh_history)](https://github.com/j2h4u/ups-battery-monitor/blob/main/src/model.py#L218-227)

Current (no pruning):
```python
def add_soh_history_entry(self, date, soh):
    if 'soh_history' not in self.data:
        self.data['soh_history'] = []
    self.data['soh_history'].append({'date': date, 'soh': soh})
    self.data['soh'] = soh
```

Recommended (with pruning):
```python
def add_soh_history_entry(self, date, soh):
    if 'soh_history' not in self.data:
        self.data['soh_history'] = []
    self.data['soh_history'].append({'date': date, 'soh': soh})
    self.data['soh'] = soh
    self._prune_soh_history()  # ← After append

def _prune_soh_history(self, keep_count=30):
    """Remove old entries; keep only recent 30."""
    soh_hist = self.data.get('soh_history', [])
    if len(soh_hist) > keep_count:
        self.data['soh_history'] = soh_hist[-keep_count:]
```

### LOW-02: fdatasync Example
**Source:** [src/model.py L45-49 (atomic_write_json)](https://github.com/j2h4u/ups-battery-monitor/blob/main/src/model.py#L45-49)

Current (fsync):
```python
fd = os.open(str(tmp_path), os.O_RDONLY)
try:
    os.fsync(fd)  # Syncs data + metadata
finally:
    os.close(fd)
```

Recommended (fdatasync):
```python
fd = os.open(str(tmp_path), os.O_RDONLY)
try:
    os.fdatasync(fd)  # Syncs data only; metadata (inode) not durability-critical for JSON
finally:
    os.close(fd)
```

### LOW-03: MetricEMA Extraction
**Source:** [src/ema_filter.py L5-86 (current EMAFilter)](https://github.com/j2h4u/ups-battery-monitor/blob/main/src/ema_filter.py#L5-86)

**Usage in monitor.py after refactor:**
```python
from src.ema_filter import MetricEMA

# In Monitor.__init__:
self.voltage_ema = MetricEMA("voltage", window_sec=120, poll_interval_sec=10)
self.load_ema = MetricEMA("load", window_sec=120, poll_interval_sec=10)

# In main loop:
v_smooth = self.voltage_ema.update(raw_voltage)
l_smooth = self.load_ema.update(raw_load)
```

### LOW-04: Logger Cleanup
**Source:** [src/alerter.py L8-15 (setup_ups_logger)](https://github.com/j2h4u/ups-battery-monitor/blob/main/src/alerter.py#L8-15)

Current (wrapper):
```python
def setup_ups_logger(identifier: str = "ups-battery-monitor") -> logging.Logger:
    return logging.getLogger(identifier)

# Usage in monitor.py:
logger = setup_ups_logger("ups-battery-monitor")
```

Recommended (direct call):
```python
# src/monitor.py:
import logging

logger = logging.getLogger("ups-battery-monitor")
```

### LOW-05: Health Endpoint
**Location:** Will be added to src/monitor.py main loop

```python
import json
from datetime import datetime, timezone

def _write_health_endpoint(model_dir, soc_percent, is_online):
    """Write daemon health state to file for monitoring tools."""
    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(soc_percent, 1),
        "online": is_online,
        "daemon_version": "1.1",
        "model_dir": str(model_dir)
    }
    from src.model import atomic_write_json
    atomic_write_json(model_dir / "health.json", health_data)

# In Monitor.run() main loop:
_write_health_endpoint(self.model.model_path.parent, current_soc, is_online)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Unbounded history lists | Pruning on save (v1.1) | Phase 11 | Prevents disk bloat over years of operation |
| fsync for all JSON writes | fdatasync for data-only sync (v1.1) | Phase 11 | 50% reduction in fsync latency (negligible in practice, correct pattern) |
| EMAFilter with dual metrics | MetricEMA generic per-metric (v1.1 prepared for v2) | Phase 11 | Enables temperature sensor addition without refactoring |
| setup_ups_logger() wrapper | Direct logging.getLogger() (v1.1) | Phase 11 | Simpler code; standard Python pattern |
| No health monitoring interface | health.json state file (v1.1) | Phase 11 | Enables Grafana/check_mk integration; v2 HTTP upgrade path |

**Deprecated/outdated:**
- No items deprecated in v1.1; all Low-01..05 are additive or minor optimizations.

## Open Questions

1. **LOW-01 Pruning: Keep-last-N vs date-range?**
   - **What we know:** Keep-last-30 is simpler; date-range (90 days) is more flexible
   - **What's unclear:** Which aligns better with typical battery degradation analysis?
   - **Recommendation:** Use keep-last-30 for v1.1 (simple, predictable); add config option for v2 if needed

2. **LOW-05 Health endpoint: Update every poll or every REPORTING_INTERVAL?**
   - **What we know:** Every poll (10s) is most granular; every REPORTING_INTERVAL (60s) reduces I/O
   - **What's unclear:** What frequency do monitoring tools expect?
   - **Recommendation:** Every poll (10s); file system I/O is negligible for JSON file updates; tools can debounce on read

3. **LOW-04 Logger: Remove setup_ups_logger entirely or keep for v2 HTTP endpoint logging?**
   - **What we know:** Currently unused except as pass-through
   - **What's unclear:** Will v2 HTTP logging need a separate setup function?
   - **Recommendation:** Remove for v1.1; v2 can use standard logging.getLogger if needed

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.5 + pytest-cov 5.0.0 |
| Config file | ./pytest.ini (addopts: -v --tb=short) |
| Quick run command | `pytest tests/test_model.py -xvs` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| LOW-01 | soh_history pruned to ≤30 entries on save | unit | `pytest tests/test_model.py::test_prune_soh_history -xvs` | ❌ Wave 0 |
| LOW-01 | r_internal_history pruned to ≤30 entries on save | unit | `pytest tests/test_model.py::test_prune_r_internal_history -xvs` | ❌ Wave 0 |
| LOW-02 | atomic_write_json() calls os.fdatasync, not os.fsync | unit | `pytest tests/test_model.py::test_atomic_write_uses_fdatasync -xvs` | ❌ Wave 0 |
| LOW-03 | MetricEMA class accepts metric_name and tracks single value | unit | `pytest tests/test_ema_filter.py::test_metric_ema_generic -xvs` | ❌ Wave 0 |
| LOW-03 | MetricEMA updates voltage, load, temperature independently | unit | `pytest tests/test_ema_filter.py::test_metric_ema_multiple_metrics -xvs` | ❌ Wave 0 |
| LOW-04 | setup_ups_logger no longer defined in alerter.py | unit | `pytest tests/test_alerter.py::test_no_setup_ups_logger_wrapper -xvs` | ❌ Wave 0 |
| LOW-04 | alert functions use logging.getLogger directly | unit | `pytest tests/test_alerter.py::test_alerter_uses_standard_logging -xvs` | ❌ Wave 0 |
| LOW-05 | health.json written to model_dir on every poll | integration | `pytest tests/test_monitor.py::test_health_endpoint_updates_on_poll -xvs` | ❌ Wave 0 |
| LOW-05 | health.json contains last_poll, soc_percent, online, version | unit | `pytest tests/test_monitor.py::test_health_endpoint_structure -xvs` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_model.py tests/test_ema_filter.py tests/test_alerter.py -v` (model + EMA + alerter only)
- **Per wave merge:** `pytest tests/ -v` (full suite, 184 tests)
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_model.py` — Add test_prune_soh_history, test_prune_r_internal_history, test_atomic_write_uses_fdatasync
- [ ] `tests/test_ema_filter.py` — Add test_metric_ema_generic, test_metric_ema_multiple_metrics
- [ ] `tests/test_alerter.py` — Add test_no_setup_ups_logger_wrapper, test_alerter_uses_standard_logging
- [ ] `tests/test_monitor.py` — Add test_health_endpoint_updates_on_poll, test_health_endpoint_structure
- [ ] `src/model.py` — _prune_soh_history() and _prune_r_internal_history() methods (called from save() or add_*_entry())
- [ ] `src/ema_filter.py` — Extract MetricEMA class; update EMAFilter to use it (backward compatibility wrapper)
- [ ] `src/monitor.py` — Add _write_health_endpoint() function; call in main poll loop
- [ ] `src/alerter.py` — Remove setup_ups_logger() function

## Sources

### Primary (HIGH confidence)
- **src/model.py** — atomic_write_json() implementation (L15-59), history append methods (L218-241), context7 knowledge
- **src/ema_filter.py** — Current EMAFilter implementation (L5-86), metrics structure
- **src/alerter.py** — setup_ups_logger() definition (L8-15), current usage
- **pytest.ini** — Test framework configuration
- **tests/test_model.py, test_alerter.py, test_ema_filter.py** — Existing test patterns
- **REQUIREMENTS.md** — Phase 11 requirement definitions (LOW-01 through LOW-05)
- **STATE.md** — Project context, history tracking implementation (lines 228-280)

### Secondary (MEDIUM confidence)
- Python 3.13 stdlib documentation for os.fsync, os.fdatasync (man pages confirm data-only sync distinction)
- Linux fsync(2), fdatasync(2) man pages (distinction between metadata and data sync)

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — All dependencies already in use (pytest, logging, pathlib, json, os)
- Architecture: **HIGH** — All patterns extracted from existing codebase (atomic_write, pruning, EMA)
- Pitfalls: **HIGH** — All based on common optimization/refactoring mistakes in similar Python projects
- Test requirements: **HIGH** — Existing test suite pattern is clear; new tests follow existing structure

**Research date:** 2026-03-15
**Valid until:** 2026-04-15 (30 days; Python stdlib and pytest stable)

---

**Research complete.** All five Low-priority requirements are isolated improvements with zero inter-dependencies. Implementation can proceed in any order; recommended order (LOW-01 → LOW-02 → LOW-03 → LOW-04 → LOW-05) is for logical clarity and incremental validation.
