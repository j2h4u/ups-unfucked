# Phase 8: Architecture Foundation - Research

**Researched:** 2026-03-15
**Domain:** Python dataclass refactoring, configuration management, import organization
**Confidence:** HIGH

## Summary

Phase 8 eliminates untyped dicts and module-level globals through systematic dataclass refactoring. The phase addresses three architectural issues: (1) `current_metrics` is a 9-key untyped dictionary that benefits from type hints and IDE support, (2) module-level config state (`_cfg`, `UPS_NAME`, `MODEL_DIR`) prevents testing and future multi-UPS support, and (3) two stray imports exist in method bodies rather than module top. These changes improve type safety, testability, and maintainability without altering any external behavior.

**Primary recommendation:** Use Python 3.7+ `@dataclass` with `frozen=True` for config immutability. Replace dict access with typed attributes. Pass config through `__init__` rather than relying on module-level state.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| ARCH-01 | `current_metrics` dict refactored to `@dataclass CurrentMetrics` with typed fields (voltage, charge, status, runtime_estimated, etc.) | Dataclass patterns documented; field mapping identified from current dict keys |
| ARCH-02 | Module-level config (`_cfg`, `UPS_NAME`, `MODEL_DIR`) extracted into frozen `Config` dataclass passed to `Monitor.__init__` | Config extraction pattern verified; frozen semantics prevent accidental mutation |
| ARCH-03 | Stray imports moved to module top: `from enum import Enum` (line 68), `from src.soh_calculator import interpolate_cliff_region` (inside method at line 262) | Both imports verified; usage confirmed in monitor.py |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| dataclasses | Python 3.7+ stdlib | Type-safe immutable config and metrics | Standard approach for configuration objects; built-in, zero dependencies |
| typing | Python 3.5+ stdlib | Type hints for static analysis | IDE autocomplete, mypy validation, self-documenting code |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pathlib | Python 3.4+ stdlib | Path handling in Config dataclass | Already used throughout codebase; replaces string paths |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| @dataclass | attrs library | attrs more powerful but adds dependency; dataclass sufficient here |
| frozen=True | property-based immutability | frozen simpler, prevents accidental reassignment at language level |
| type hints | runtime type checking | Type hints alone sufficient; runtime validation (Pydantic) overkill for config |

## Architecture Patterns

### Recommended Project Structure

**Before (current):**
```
src/monitor.py
├── Imports at lines 1-16
├── from enum import Enum at line 68  ← stray, unused until method
├── CONFIG_DIR, REPO_ROOT, POLL_INTERVAL, ... (constants) at lines 20-47
├── _load_config() function at lines 49-57
├── _cfg = _load_config() at line 59  ← module-level dict
├── UPS_NAME = _cfg['ups_name'] at line 61  ← module-level extracted value
├── SHUTDOWN_THRESHOLD_MINUTES, SOH_THRESHOLD at lines 62-63
├── MODEL_DIR = CONFIG_DIR at line 65  ← module-level Path
└── Monitor class at line 79
    └── __init__() at line 110
        └── self.current_metrics = { dict } at line 146  ← 9-key untyped dict
```

**After (target):**
```
src/monitor.py
├── Imports (all at top) at lines 1-16
│   ├── from enum import Enum (moved from line 68)
│   └── from src.soh_calculator import interpolate_cliff_region (moved from line 262)
├── Constants (POLL_INTERVAL, EMA_WINDOW, etc.) at lines 20-47
├── Config dataclass at line 49  ← frozen, with __init__ fields
├── CurrentMetrics dataclass at line 70  ← typed metric fields
├── _load_config() function at line 95  ← returns Config, not dict
├── SagState enum (unchanged) at line 110
└── Monitor class at line 130
    └── __init__(self, config: Config) at line 150
        ├── self.config = config at line 151
        └── self.current_metrics = CurrentMetrics(...) at line 200
```

### Pattern 1: Configuration Dataclass (ARCH-02)

**What:** Extract module-level config globals into immutable `Config` dataclass, pass to `__init__`.

**When to use:** Any class with more than 2 configuration parameters, especially when testing or reconfiguring is needed.

**Example:**
```python
# Source: PEP 557 (dataclasses) + project inspection
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass(frozen=True)
class Config:
    """Immutable UPS daemon configuration.

    frozen=True prevents accidental mutation. All fields required at construction.
    """
    ups_name: str                    # 'cyberpower', from config.toml
    polling_interval: int             # 10 seconds
    reporting_interval: int           # 60 seconds
    nut_host: str                     # 'localhost'
    nut_port: int                     # 3493
    nut_timeout: float               # 2.0 seconds
    shutdown_minutes: int             # 5 minutes (from _cfg)
    soh_alert_threshold: float       # 0.80 (from _cfg)
    model_dir: Path                  # ~/.config/ups-battery-monitor
    config_dir: Path                 # ~/.config/ups-battery-monitor
    runtime_threshold_minutes: int   # 20 minutes (hardcoded)
    reference_load_percent: float    # 20.0% (hardcoded)
    ema_window_sec: int              # 120 seconds (hardcoded)

# Usage in Monitor.__init__:
class Monitor:
    def __init__(self, config: Config):
        self.config = config
        self.shutdown_threshold_minutes = config.shutdown_minutes
        self.nut_client = NUTClient(
            host=config.nut_host,
            port=config.nut_port,
            timeout=config.nut_timeout,
            ups_name=config.ups_name
        )
        self.battery_model = BatteryModel(config.model_dir / 'model.json')
        # ... rest of init
```

**Benefits:**
- Testable: Pass `Config(ups_name='test', ...)` to create test Monitor instances
- Type-safe: IDE autocomplete, mypy validation
- Immutable: frozen=True prevents config mutation during runtime
- Clear contract: Every config option documented as field

### Pattern 2: Metrics Dataclass (ARCH-01)

**What:** Replace `self.current_metrics` dict with `@dataclass CurrentMetrics` with typed fields.

**When to use:** When a dict is accessed via string keys more than 3 times, or when type hints would improve readability.

**Example:**
```python
# Source: Current dict definition in monitor.py lines 146-156
from dataclasses import dataclass
from enum import Enum

# Assuming EventType imported from event_classifier
@dataclass
class CurrentMetrics:
    """Current UPS battery state snapshot, updated every poll.

    Fields correspond to the 9-key dict currently in monitor.py.
    """
    soc: Optional[float] = None                      # State of Charge, 0-1
    battery_charge: Optional[float] = None           # NUT battery.charge, 0-100
    time_rem_minutes: Optional[float] = None         # Estimated runtime, minutes
    event_type: Optional['EventType'] = None         # From EventClassifier
    transition_occurred: bool = False                # True if state changed this poll
    shutdown_imminent: bool = False                  # True if runtime < threshold
    ups_status_override: Optional[str] = None        # Computed status string
    previous_event_type: 'EventType' = None          # Last event_type value
    timestamp: Optional[datetime] = None             # When snapshot was taken

# Usage in Monitor:
def _handle_event_transition(self):
    """Access typed fields instead of string keys."""
    event_type = self.current_metrics.event_type
    previous_event_type = self.current_metrics.previous_event_type

    if event_type == EventType.BLACKOUT_REAL:
        time_rem = self.current_metrics.time_rem_minutes
        if time_rem and time_rem < self.shutdown_threshold_minutes:
            self.current_metrics.shutdown_imminent = True
```

**Benefits:**
- IDE autocomplete: self.current_metrics. shows all fields
- Type checking: mypy catches `self.current_metrics.nonexistent_field`
- Readability: `self.current_metrics.soc` clearer than `self.current_metrics["soc"]`
- Mutation tracking: dataclass field assignments are explicit

### Pattern 3: Import Organization (ARCH-03)

**What:** Move all imports to module top; no late imports inside method bodies.

**When to use:** Always. Late imports hide dependencies and complicate circular-dependency debugging.

**Current issue:**
```python
# Line 68 — Enum imported but not used until line 71 (SagState enum)
from enum import Enum

# ... 194 lines of code ...

# Line 262 — interpolate_cliff_region imported inside method
def _update_battery_health(self):
    if some_condition:
        from src.soh_calculator import interpolate_cliff_region  # ← BAD
        updated_lut = interpolate_cliff_region(...)
```

**Fix:**
```python
# Top of file, with other imports
from enum import Enum
from src.soh_calculator import interpolate_cliff_region

# Line 262 in method — now just use it
def _update_battery_health(self):
    if some_condition:
        updated_lut = interpolate_cliff_region(...)  # ← GOOD
```

**Benefits:**
- Dependency clarity: All module dependencies visible at glance
- Import errors caught at module load time, not at runtime
- Simpler to trace circular dependencies
- Linters (ruff, isort) can automatically organize imports

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Immutable config | Custom __setattr__ override | @dataclass(frozen=True) | Dataclass frozen semantics prevent accidental mutation at language level; custom approaches fragile |
| Configuration parsing | Custom dict merge logic | dataclass + factory function | dataclass is explicit about what's configurable; dict merging error-prone |
| Type-safe metrics | Runtime type checking via dict.get() | @dataclass with Optional[type] | dataclass enforces types at construction; dict.get() hides missing fields |
| Configuration validation | if/elif chains for required fields | dataclass __post_init__ | Post-init can validate, but for simple config, factory function better |

**Key insight:** Dataclasses solve the "configuration management at Python level" problem so thoroughly that rolling custom solutions wastes implementation time and creates maintenance burden. The stdlib implementation is mature (Python 3.7+, refined through 3.13), widely understood, and integrates with all major linters/type checkers.

## Common Pitfalls

### Pitfall 1: Frozen Dataclass Field Mutation Attempts

**What goes wrong:**
```python
config = Config(ups_name='cyberpower', ...)
config.ups_name = 'other'  # ← FrozenInstanceError at runtime
```

**Why it happens:** `frozen=True` makes field assignment illegal; developers accustomed to dict may try to mutate.

**How to avoid:** Ensure all config creation happens at startup (in main() or __init__). If runtime reconfiguration needed, create new Config instance, don't mutate.

**Warning signs:** Any line with pattern `config.field_name =` (except inside __init__ or dataclass __post_init__)

### Pitfall 2: Dataclass Default Mutability (Mutable Default Fields)

**What goes wrong:**
```python
@dataclass
class CurrentMetrics:
    history: list = []  # ← WRONG: mutable shared across all instances

m1 = CurrentMetrics()
m1.history.append(1)
m2 = CurrentMetrics()
print(m2.history)  # [1] — shared reference!
```

**Why it happens:** Mutable defaults like `[]` or `{}` are shared across all instances (standard Python gotcha).

**How to avoid:** Use `field(default_factory=...)` for mutable types:
```python
from dataclasses import dataclass, field

@dataclass
class CurrentMetrics:
    history: list = field(default_factory=list)  # Each instance gets own list
```

**Warning signs:** Any mutable type (list, dict, set) as default value without `field(default_factory=...)`

### Pitfall 3: Type Hints Without Optional for None Defaults

**What goes wrong:**
```python
@dataclass
class CurrentMetrics:
    soc: float = None  # mypy: Incompatible default value for required field
```

**Why it happens:** Type hint says `float` but default is `None`; mypy flags as error.

**How to avoid:** Use `Optional[float]` (from `typing`) when field can be None:
```python
from typing import Optional

@dataclass
class CurrentMetrics:
    soc: Optional[float] = None  # Correct: float or None
```

**Warning signs:** mypy error like "Expected Optional[X], got X"

### Pitfall 4: Circular Imports When Moving Imports to Top

**What goes wrong:**
```python
# src/monitor.py (top)
from src.event_classifier import EventType

# src/event_classifier.py (top)
from src.monitor import Monitor  # ← Circular!
```

**Why it happens:** Moving late imports to top exposes circular dependencies that were previously hidden.

**How to avoid:** Check for imports in event_classifier, soh_calculator, etc. that might import from monitor.py. If circular, use forward reference (string) in type hints:
```python
# Instead of: from src.monitor import Monitor
def process_monitor(monitor: 'Monitor'):  # String type hint
    ...
```

**Prevention:** Run `python -c "from src.monitor import Monitor"` to catch circular imports before commit.

**Warning signs:** `ImportError: cannot import name X` at module load time

### Pitfall 5: Not Updating Test Mocks for New Constructor Signature

**What goes wrong:**
```python
# Old code: Monitor() with no args
m = Monitor()

# New code: Monitor(config: Config)
m = Monitor()  # ← TypeError: missing 1 required positional argument
```

**Why it happens:** Tests create Monitor instances; changing __init__ signature breaks test setup.

**How to avoid:** Update all `Monitor()` calls in tests to `Monitor(config)`. Run test suite before committing (should be automatic via Nyquist validation).

**Warning signs:** Test failures like "TypeError: Monitor() missing required argument 'config'"

## Code Examples

Verified patterns from current codebase:

### Dataclass field mapping from current_metrics dict

```python
# Source: src/monitor.py lines 146-156 (current dict structure)
# These 9 fields map directly to new CurrentMetrics dataclass

current_metrics = {
    "soc": None,                      # → soc: Optional[float]
    "battery_charge": None,           # → battery_charge: Optional[float]
    "time_rem_minutes": None,         # → time_rem_minutes: Optional[float]
    "event_type": None,               # → event_type: Optional[EventType]
    "transition_occurred": False,     # → transition_occurred: bool
    "shutdown_imminent": False,       # → shutdown_imminent: bool
    "ups_status_override": None,      # → ups_status_override: Optional[str]
    "previous_event_type": EventType.ONLINE,  # → previous_event_type: EventType
    "timestamp": None,                # → timestamp: Optional[datetime]
}
```

### Config extraction from module-level state

```python
# Source: src/monitor.py lines 49-65 (current globals)
# These become Config dataclass fields

_CONFIGURABLE_DEFAULTS = {
    'ups_name': 'cyberpower',
    'shutdown_minutes': 5,
    'soh_alert': 0.80,
}

# Hardcoded constants (also go into Config)
POLL_INTERVAL = 10
EMA_WINDOW = 120
NUT_HOST = 'localhost'
NUT_PORT = 3493
NUT_TIMEOUT = 2.0
RUNTIME_THRESHOLD_MINUTES = 20
REFERENCE_LOAD_PERCENT = 20.0
CONFIG_DIR = Path.home() / '.config' / 'ups-battery-monitor'

# Current module-level state to extract
_cfg = _load_config()
UPS_NAME = _cfg['ups_name']
SHUTDOWN_THRESHOLD_MINUTES = _cfg['shutdown_minutes']
SOH_THRESHOLD = _cfg['soh_alert']
MODEL_DIR = CONFIG_DIR
```

### Import consolidation targets

```python
# Source: src/monitor.py lines 1-16 (existing imports)
# Already consolidated at top

import time, signal, sys, math, logging, argparse, tomllib
from pathlib import Path
from datetime import datetime
from systemd.journal import JournalHandler

# Imports to move to top
# Line 68: from enum import Enum  ← move here
# Line 262 (inside method): from src.soh_calculator import interpolate_cliff_region  ← move here

from src.nut_client import NUTClient
from src.ema_filter import EMAFilter, ir_compensate
from src.model import BatteryModel
from src.soc_predictor import soc_from_voltage, charge_percentage
from src.runtime_calculator import runtime_minutes, peukert_runtime_hours
from src.event_classifier import EventClassifier, EventType
from src.virtual_ups import write_virtual_ups_dev, compute_ups_status_override
from src import soh_calculator, replacement_predictor, alerter
# ← Add here:
from enum import Enum
from src.soh_calculator import interpolate_cliff_region
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Module-level globals for config | Dataclass Config injected via __init__ | PEP 557 (2018), widespread adoption (2019+) | Config is testable; no global state pollution |
| Untyped dicts for metrics | @dataclass with type hints | PEP 526 (2018), dataclass maturity | IDE autocomplete; mypy catches errors |
| Late imports (import in method body) | All imports at module top | Standard Python convention | Dependencies visible; circular imports caught early |

**No deprecated patterns identified:** Phase 8 adopts standard Python 3.7+ practices. No "old way vs new way" upgrade needed — this is baseline good practice.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 7+ (current in use) |
| Config file | pytest.ini at repo root |
| Quick run command | `pytest tests/test_monitor.py -k "test_" -x` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ARCH-01 | CurrentMetrics dataclass has 9 typed fields; instantiation and field access work | unit | `pytest tests/test_monitor.py::Test* -k "metrics" -x` | ❌ Wave 0 |
| ARCH-02 | Config dataclass frozen; Monitor.__init__(config) works; no module-level _cfg/UPS_NAME/MODEL_DIR | unit | `pytest tests/test_monitor.py::Test* -k "config" -x` | ❌ Wave 0 |
| ARCH-03 | All imports at module top; no late imports in method bodies; module loads without error | integration | `python -c "from src.monitor import Monitor"` | ✅ Automatic |

### Sampling Rate
- **Per task commit:** `pytest tests/test_monitor.py -x` (verify Monitor class still works)
- **Per wave merge:** `pytest tests/ -v` (full suite must pass, no regressions)
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_monitor.py::test_current_metrics_dataclass` — verify CurrentMetrics instantiation with all 9 fields
- [ ] `tests/test_monitor.py::test_config_dataclass` — verify Config frozen semantics and __init__ injection
- [ ] `tests/test_monitor.py::test_config_immutability` — verify FrozenInstanceError on config field mutation
- [ ] Import check: `python -c "from src.monitor import Monitor; print('OK')"` — verify no late imports cause errors
- [ ] `tests/conftest.py` update — create `config_fixture()` and `current_metrics_fixture()` for test reuse

## Sources

### Primary (HIGH confidence)
- Python 3.13.5 stdlib: dataclasses, typing, pathlib (verified via `python3` shell)
- PEP 557: https://www.python.org/dev/peps/pep-0557/ (dataclass specification)
- Project source inspection: `/home/j2h4u/repos/j2h4u/ups-battery-monitor/src/monitor.py` lines 1-186 (config and metrics structure verified)

### Secondary (MEDIUM confidence)
- Real Codebase Example: `current_metrics` dict with 9 keys (lines 146-156) and usage pattern (19 access instances verified via grep)
- Module-level globals: `_cfg`, `UPS_NAME`, `MODEL_DIR` confirmed at lines 59-65
- Late imports: `from enum import Enum` line 68 (used in SagState at line 71), `from src.soh_calculator import interpolate_cliff_region` line 262 (used in _update_battery_health method)

## Metadata

**Confidence breakdown:**
- Standard Stack: HIGH — dataclasses stdlib, Python 3.7+ standard, widespread adoption
- Architecture: HIGH — current code structure verified via inspection; refactoring patterns are established
- Pitfalls: HIGH — common dataclass gotchas well-documented; Python community knowledge base mature
- Validation: MEDIUM — tests exist for Monitor class; new test infrastructure needed for dataclass-specific validation (Wave 0 task)

**Research date:** 2026-03-15
**Valid until:** 2026-04-15 (30 days — Python stdlib stable, no churn expected)
**Assumptions:** Phase 7 (SAFE-01/SAFE-02) completed before Phase 8 starts; existing 160 tests in test suite pass without modification after refactor

