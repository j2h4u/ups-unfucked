# Expert Panel Review — 2026-03-15

Comprehensive review: architecture, code quality, security, performance, clean code, code smells.
Panel: System Architect, Security Analyst, SRE, QA Engineer, Kaizen Master.

## Scope

- **What**: ~2,000 LoC Python daemon replacing CyberPower UPS firmware metrics with physics-based calculations
- **Blast radius**: `host-level` — incorrect LB flag = premature shutdown or data loss from no shutdown
- **Decision type**: Stress-testing existing codebase

---

## P0 — Safety Critical

- [ ] **Stale metrics during blackout** (SRE): `_compute_metrics()` and `_write_virtual_ups()` only execute on `poll_count % 6 == 0`. During real blackout, virtual UPS shows stale data for up to 50 seconds. If battery drains fast (last 5 minutes), LB flag update lag delays shutdown by ~1 minute. **Fix**: during OB state, compute and write metrics every poll (10s), not every 6th. Keep 60s cadence for ONLINE.
- [ ] **`_handle_event_transition()` also gated by 6-poll interval** (SRE): LB flag decision (`shutdown_imminent`) computed only every 60s. Worst case: battery crosses threshold at poll N+1, LB flag not written until poll N+6. Must be ungated during OB.

---

## P1 — High Priority

### Architecture

- [ ] **Coupling to module-level state** (Architect): `_cfg`, `UPS_NAME`, `MODEL_DIR`, `logger` computed at import time. Makes testing fragile (`test_monitor.py` mocks `sys.modules['systemd']` before import), prevents running two daemon instances or reconfiguring without restart. Extract config into a frozen dataclass passed to `__init__`.
- [ ] **`current_metrics` god dict** (Architect + Kaizen): 10 untyped keys, mutated everywhere, read from everywhere. Primary source of coupling. **Decision: refactor to `@dataclass` with typed fields.** Eliminates typo keys, wrong types, adds IDE support.

### Testing Gaps

- [ ] **`test_monitor.py` is thin** (QA): 189 lines, ~7 tests for the most complex module. No tests for `run()` loop behavior, `_track_discharge()` full cycle, or the 6-poll cadence logic.
- [ ] **No integration test for OL→OB→OL full lifecycle** (QA): `_handle_event_transition` + `_update_battery_health` + `_track_discharge` chain only tested individually, never as connected flow.
- [ ] **`_auto_calibrate_peukert` has zero tests** (QA): Complex math (Peukert exponent recalculation) completely untested. `test_auto_calibration_end_to_end` name is misleading — tests LUT calibration + cliff interpolation, not Peukert auto-calibration.
- [ ] **No test for `_signal_handler`** (QA): Signal handling (model save on SIGTERM) untested.

---

## P2 — Medium Priority

### Code Quality

- [ ] **Late/stray imports** (Architect): `from enum import Enum` at line 68 (after function definitions). `from src.soh_calculator import interpolate_cliff_region` inside `_handle_event_transition()` method body (line 262). Move to module top.
- [ ] **Repeated `try/except OSError` for `model.save()`** (Kaizen): Appears at monitor.py lines 272, 309, 422, 440. Extract to `_safe_save()` helper.
- [ ] **Hardcoded date in `_default_vrla_lut()`** (Kaizen): model.py line 145 hardcodes `'2026-03-13'` in soh_history. Should use `datetime.now().strftime('%Y-%m-%d')`.
- [ ] **`soc_from_voltage` docstring says "binary search"** (Kaizen): Actually does linear scan. For 7-20 entries linear is fine, but docstring is misleading. Fix comment.
- [ ] **`calibration_write()` does per-point fsync** (Kaizen + SRE): During long discharge (1000 samples), up to ~167 fsync calls (1000/6). Each fsync on ext4 costs ~2-10ms. Total: ~0.3-1.7s blocking I/O spread across discharge. On ext4 (`~/.config/`) this is real. **Fix**: batch — accumulate points in memory, single save per REPORTING_INTERVAL_POLLS.
- [ ] **Double error log in `virtual_ups.py`** (SRE): Lines 90 and 93-94 both catch and log. Failed atomic rename → two error lines for same event.

### Testing

- [ ] **`conftest.py` `mock_socket_ok` always returns same response** (QA): Regardless of command sent. `get_ups_vars()` (LIST VAR) would receive a single VAR line instead of proper LIST response. `get_ups_vars` never properly unit-tested through this mock.
- [ ] **Floating point exact comparison in `soc_from_voltage`** (QA): `entry["v"] == voltage` (line 37) uses exact float equality. Works because LUT voltages are rounded, but fragile if measured voltages ever stored unrounded.

---

## P3 — Low Priority / Deferred

- [ ] **Unbounded `soh_history` and `r_internal_history`** (SRE): Lists grow forever in `model.json`. At 200+ events/year, fine for years, but no pruning exists. Revisit at year 3.
- [ ] **`EMAFilter` two parallel tracks** (Kaizen): Voltage and load duplicated code. Not a problem now, but if temperature sensor added, becomes tech debt. YAGNI — acceptable.
- [ ] **`alerter.py` `setup_ups_logger` is pointless** (Kaizen): Just `logging.getLogger(identifier)`. Adds no value. Only 3 lines, not worth changing.
- [ ] **`atomic_write_json` calls `os.fsync` on read-only FD** (SRE): Technically correct on Linux but unusual. Some argue `os.fdatasync` more appropriate (no metadata sync needed).
- [ ] **No health endpoint** (SRE): No way to check daemon liveness beyond systemd watchdog. Could expose basic metrics (last poll time, current SoC) via file or simple HTTP.

---

## Security Assessment (no action items)

- No input validation on NUT data — acceptable, localhost-only trusted source
- Temp file permissions before rename have default umask — brief window, controlled environment
- `model.json` world-readable — no secrets, battery data only
- Symlink guard on `/dev/shm` write — good
- `_parse_var_line` trusts NUT response format — worst case `ValueError` → string, acceptable

---

## Panel Conflicts

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| Metrics update frequency during blackout | SRE: every 10s (safety-critical LB flag) | Kaizen: keep simple, 60s is fine | **SRE wins** — safety > simplicity. Stale LB flag = data loss risk |
| `current_metrics` refactor to dataclass | Architect: do it now (prevents bugs) | Kaizen: works fine, YAGNI | **Architect wins** — user decision: proceed with dataclass |
| Calibration write batching | Kaizen: batch for efficiency | SRE: per-point writes survive power loss | **Compromise** — batch per REPORTING_INTERVAL. Power loss loses ≤6 points (60s), acceptable for calibration |

---

## Resolved Open Questions

- **P0 fix (per-poll metrics during OB)**: Yes, implement
- **`current_metrics` dataclass**: Yes, refactor
- **Why 6-poll cadence during blackout?** (Architect): No good reason — was designed for ONLINE state log noise reduction, accidentally applied to OB too
- **CI pipeline?**: Not visible in repo, pytest configuration not found
