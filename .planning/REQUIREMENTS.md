# Requirements: UPS Battery Monitor

**Defined:** 2026-03-15
**Core Value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

## v1.1 Requirements

All requirements sourced from `docs/EXPERT-PANEL-REVIEW-2026-03-15.md`.

### Safety

- [ ] **SAFE-01**: Virtual UPS metrics written every poll (10s) during OB state instead of every 60s — eliminates stale LB flag lag
- [ ] **SAFE-02**: LB flag decision (`_handle_event_transition`) executes every poll during OB state — ensures timely shutdown signal

### Architecture

- [ ] **ARCH-01**: `current_metrics` dict refactored to `@dataclass` with typed fields — eliminates untyped 10-key god dict
- [ ] **ARCH-02**: Module-level config (`_cfg`, `UPS_NAME`, `MODEL_DIR`) extracted into frozen dataclass passed to `__init__` — enables testing and reconfiguration
- [ ] **ARCH-03**: Stray imports moved to module top — `from enum import Enum` at line 68, `from src.soh_calculator import interpolate_cliff_region` inside method body

### Code Quality

- [ ] **QUAL-01**: Repeated `try/except OSError` for `model.save()` (4 occurrences in monitor.py) extracted to `_safe_save()` helper
- [ ] **QUAL-02**: Hardcoded date `'2026-03-13'` in `_default_vrla_lut()` soh_history replaced with `datetime.now().strftime('%Y-%m-%d')`
- [ ] **QUAL-03**: `soc_from_voltage` docstring corrected — says "binary search" but implementation is linear scan
- [ ] **QUAL-04**: `calibration_write()` batched — accumulate points in memory, single save per REPORTING_INTERVAL instead of per-point atomic write
- [ ] **QUAL-05**: Double error log in `virtual_ups.py` fixed — inner catch (line 90) and outer catch (line 93) both log same failure

### Testing

- [ ] **TEST-01**: Integration test for full OL→OB→OL discharge lifecycle — `_handle_event_transition` + `_update_battery_health` + `_track_discharge` as connected flow
- [ ] **TEST-02**: Unit tests for `_auto_calibrate_peukert` method — verify Peukert exponent recalculation math and edge cases
- [ ] **TEST-03**: Test for `_signal_handler` — verify model save on SIGTERM
- [ ] **TEST-04**: Fix `conftest.py` `mock_socket_ok` to return proper LIST VAR multi-line response for `get_ups_vars` testing
- [ ] **TEST-05**: Address floating-point exact comparison `entry["v"] == voltage` in `soc_from_voltage` (line 37) — replace with tolerance-based comparison or document safety

### Low Priority

- [ ] **LOW-01**: Add pruning for unbounded `soh_history` and `r_internal_history` lists in model.json
- [ ] **LOW-02**: Consider `os.fdatasync` instead of `os.fsync` in `atomic_write_json` (no metadata sync needed)
- [ ] **LOW-03**: Decouple EMAFilter voltage/load into generic per-metric track (prepare for temperature sensor)
- [ ] **LOW-04**: Remove `setup_ups_logger` wrapper in alerter.py — use `logging.getLogger` directly
- [ ] **LOW-05**: Add daemon health endpoint — expose last poll time and current SoC via file for external monitoring

## Out of Scope

| Feature | Reason |
|---------|--------|
| Telegram alerts | Explicitly rejected in v1.0 — MOTD+journald sufficient |
| Multi-UPS support | Only CyberPower UT850EG |
| Web UI / REST API | Minimalism principle |
| New monitoring features | This milestone is fixes only, not new capabilities |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SAFE-01 | 7 | Pending |
| SAFE-02 | 7 | Pending |
| ARCH-01 | 8 | Pending |
| ARCH-02 | 8 | Pending |
| ARCH-03 | 8 | Pending |
| QUAL-01 | 10 | Pending |
| QUAL-02 | 10 | Pending |
| QUAL-03 | 10 | Pending |
| QUAL-04 | 10 | Pending |
| QUAL-05 | 10 | Pending |
| TEST-01 | 9 | Pending |
| TEST-02 | 9 | Pending |
| TEST-03 | 9 | Pending |
| TEST-04 | 9 | Pending |
| TEST-05 | 9 | Pending |
| LOW-01 | 11 | Pending |
| LOW-02 | 11 | Pending |
| LOW-03 | 11 | Pending |
| LOW-04 | 11 | Pending |
| LOW-05 | 11 | Pending |

**Coverage:**
- v1.1 requirements: 19 total
- Mapped to phases: 19
- Unmapped: 0 ✓

---

*Requirements defined: 2026-03-15*
*Last updated: 2026-03-15 after roadmap creation*
