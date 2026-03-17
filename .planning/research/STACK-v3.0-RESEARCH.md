# Technology Stack: v3.0 Active Battery Care

**Project:** UPS Battery Monitor v3.0
**Researched:** 2026-03-17
**Status:** Builds on v2.0; no new dependencies introduced

## Recommended Stack (No Changes from v2.0)

### Core Daemon
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| **Python** | 3.13 | Primary language | v2.0 baseline; type hints + dataclasses enable testability for new sulfation model |
| **systemd** | Debian 13 native | Daemon lifecycle, logging | Already integrated; v3.0 adds structured journald events (no new deps) |
| **NUT (Network UPS Tools)** | 2.8.1+ | UPS communication | Existing stack; v3.0 extends upscmd integration (test scheduling) |

### v3.0-Specific Additions (Minimal)

| Component | Technology | Purpose | Why |
|-----------|-----------|---------|-----|
| **Scheduling** | Python `asyncio` + `apscheduler` | Daemon-driven test scheduling | Replaces static systemd timers (ups-test-deep.timer) with intelligent scheduling. Already in Python stdlib + minimal deps. |
| **Config** | YAML (existing) | Sulfation thresholds, temperature fallback | No new format; extend existing config schema |
| **Metrics export** | health.json (existing) | Sulfation score, ROI, test schedule | Extends existing health endpoint; no new tech |
| **Logging** | journald (existing) | Structured scheduling events | v2.0 already logs to journald; v3.0 adds @fields (tags, reason codes) |

### No New External Dependencies

- **apscheduler:** May need `pip install apscheduler` if not already present. Check v2.0 environment.
- **Everything else:** Existing v2.0 stack (requests, dataclasses, systemd Python bindings via `systemd` module already used)

## Database / Persistence (No Changes)

| Technology | Purpose | Notes |
|-----------|---------|-------|
| **model.json** | Battery state (SoH, capacity, cycle count) | Extends schema: add `sulfation_history[]`, `ir_baseline_mv`, `test_schedule` |
| **systemd journal** | Event log | Already used for structured logging; v3.0 adds scheduling decision fields |

## Infrastructure / Deployment (No Changes)

| Technology | Purpose | Notes |
|-----------|---------|-------|
| **systemd unit** | Daemon management | Rename/extend `ups-battery-monitor.service`; disable old `ups-test-deep.timer` |
| **NUT dummy-ups** | Virtual UPS proxy | Already running; v3.0 no changes needed |
| **Grafana** | Metrics visualization | Extends health.json schema; Grafana queries unchanged (backward compat) |

## Installation & Configuration

### Dependencies (v3.0)

```bash
# Core (already installed for v2.0)
python3 -m pip install -r /path/to/requirements.txt

# v3.0 additional (if not present)
python3 -m pip install apscheduler>=3.10

# Verify NUT version (need 2.8.1+ for upscmd test.battery.start)
upsc --version
```

### Configuration File (config.json extension)

```json
{
  "nut": {
    "ups_name": "UT850",
    "polling_interval_sec": 10
  },
  "battery": {
    "type": "VRLA",
    "rated_capacity_ah": 7.2,
    "nominal_voltage_v": 12
  },
  "v3_sulfation": {
    "enabled": true,
    "ir_baseline_mv": null,
    "ir_trend_window_tests": 5,
    "ir_rise_threshold_percent": 25,
    "recovery_delta_threshold_mv": 50,
    "sulfation_score_threshold": 65,
    "soh_floor_percent": 50,
    "min_interval_between_tests_days": 7,
    "max_tests_per_week": 1,
    "temperature_constant_celsius": 35,
    "blackout_credit_depth_percent": 90,
    "blackout_credit_defer_days": 7
  },
  "v3_roi": {
    "enabled": true,
    "capacity_loss_per_cycle_percent": 0.15,
    "roi_threshold_accept": 5.0,
    "roi_threshold_marginal": 1.0
  }
}
```

### Systemd Service Changes

**Old (v2.0):**
```
[Unit]
Description=UPS Battery Monitor
...

[Service]
ExecStart=/usr/local/bin/ups-battery-monitor
...
```

**New (v3.0):**
```
[Unit]
Description=UPS Battery Monitor v3.0 (Active Battery Care)
...

[Service]
ExecStart=/usr/local/bin/ups-battery-monitor --daemon --scheduler
...
```

### Disable Old Timers

```bash
systemctl disable ups-test-quick.timer
systemctl disable ups-test-deep.timer
# Daemon now owns test scheduling
```

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| **Scheduling** | Python asyncio + apscheduler | Node.js + node-cron | Keep Python monolithic; no JS runtime dependency |
| **Scheduling** | Python asyncio + apscheduler | Explicit systemd timer + DBus signals | Scheduling logic hidden in timer config; not observable/loggable |
| **Config format** | YAML/JSON (existing) | TOML | No benefit; already using JSON; adds parsing dependency |
| **Metrics export** | Extend health.json | New CSV file | JSON already works with Grafana; avoid filesystem sprawl |
| **Test trigger** | `upscmd UT850 test.battery.start` | Direct USB command | Use NUT abstraction; CyberPower UT850 communicates only via NUT |
| **ML for RUL** | Reject (physics-based SoH) | LSTM/XGBoost | No training data; overfitting risk; unmaintainable |

## Versioning & Compatibility

**v2.0 → v3.0 Migration:**
- model.json schema is **backward compatible** — new fields added with sensible defaults
- health.json extended but existing fields unchanged
- Old `ups-test-deep.timer` disabled; daemon takes over test scheduling
- No breaking changes to MOTD format
- NUT communication unchanged (upsc + dummy-ups)

**Forward Compatibility:**
- Config schema allows optional v3_sulfation section (falls back to defaults if missing)
- Existing deployments can enable v3.0 features gradually (flag in config)

## Development Environment

```bash
# Same as v2.0
python3.13 -m venv /path/to/venv
source /path/to/venv/bin/activate
pip install -r requirements.txt
pip install apscheduler pytest pytest-cov

# Run tests
pytest tests/ -v --cov=src/
```

## Sources

**Scheduler:**
- APScheduler docs: https://apscheduler.readthedocs.io/ — job scheduling, advanced cron patterns, observability

**NUT Integration:**
- NUT upscmd documentation: https://networkupstools.org/docs/man/upscmd.1.html — test.battery.start command reference

**Python Async:**
- asyncio docs: https://docs.python.org/3/library/asyncio.html — event loop for daemon integration

**Systemd:**
- systemd journal structured fields: https://www.freedesktop.org/wiki/Software/systemd/json-fields/ — adding @fields to logs

---

*Technology stack for: UPS Battery Monitor v3.0*
*Researched: 2026-03-17*
*No new critical dependencies; extends v2.0 cleanly*
