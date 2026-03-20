---
phase: 24-temperature-security-hardening
plan: "02"
subsystem: monitor-startup
tags: [temperature, security, nut, documentation, tdd]
dependency_graph:
  requires: []
  provides: [temperature-probe-at-startup, nut-security-docs]
  affects: [src/monitor.py, src/nut_client.py, README.md, tests/test_monitor.py]
tech_stack:
  added: []
  patterns: [TDD red-green, structured logging with extra dict, mock.patch on module-level logger]
key_files:
  created: []
  modified:
    - src/monitor.py
    - src/nut_client.py
    - README.md
    - tests/test_monitor.py
decisions:
  - TestTemperatureProbe uses dedicated helper bypassing make_daemon fixture — fixture patches _probe_temperature_sensor on the class which would suppress the method under test
  - Assert on logger.info call_args event_type (extra dict), not caplog.text — caplog captures propagated records but monitor logger clears handlers and adds stderr handler in fixture
  - _make_daemon_for_probe constructs daemon outside fixture's with-block so _probe_temperature_sensor is not patched at test-call time
metrics:
  duration: "410s"
  completed: "2026-03-21"
  tasks_completed: 2
  files_modified: 4
  tests_added: 3
  tests_total: 88
---

# Phase 24 Plan 02: Temperature + Security Hardening Summary

Temperature probe at daemon startup and NUT empty-PASSWORD documentation.

## What Was Built

### Task 1: Temperature sensor probe at daemon startup

`_probe_temperature_sensor()` added to `MonitorDaemon`. Called from `__init__` immediately after `_check_nut_connectivity()`. Checks NUT for `ups.temperature`, `battery.temperature`, `ambient.temperature` (in that order). Logs structured message with `event_type`:

- `temperature_sensor_found` — sensor found, logs var name and value
- `temperature_sensor_unavailable` — no temperature keys, thermal compensation skipped (35°C constant per v3.0 design)
- Silent return — if NUT unreachable (connectivity already logged by `_check_nut_connectivity`)

Three tests in `TestTemperatureProbe` cover all three paths.

### Task 2: NUT empty PASSWORD documentation

Two documentation artifacts:

1. **`src/nut_client.py`** — 6-line comment at the `send_command('PASSWORD')` call site explaining why it is empty, the single-server loopback assumption, and when to add a real password.

2. **`README.md`** — New `## Security` section (before `## License`) explaining the NUT local-only trust boundary, security implications for local process access, and instructions for network-exposed deployments.

## Commits

| Hash | Message |
|------|---------|
| f1c9403 | test(24-02): add failing tests for temperature sensor probe |
| 7563676 | feat(24-02): add temperature sensor probe at daemon startup |
| e4a612e | feat(24-02): document NUT empty PASSWORD security model |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] make_daemon fixture patched out the method under test**

- **Found during:** Task 1 TDD GREEN phase
- **Issue:** `make_daemon` fixture uses `patch.object(MonitorDaemon, '_check_nut_connectivity')` — after adding `_probe_temperature_sensor`, the fixture also needed to patch it to prevent init-time interference with all other tests. But that same patch suppressed the method when `TestTemperatureProbe` called it directly. The patch persists through the fixture's `yield`, so the test body was calling the MagicMock no-op instead of the real implementation.
- **Fix:** `TestTemperatureProbe` uses a dedicated static helper `_make_daemon_for_probe()` that patches everything EXCEPT `_probe_temperature_sensor`. Updated `make_daemon` to patch `_probe_temperature_sensor` for all other tests.
- **Files modified:** `tests/test_monitor.py`
- **Commit:** 7563676

**2. [Rule 1 - Bug] caplog.text does not contain event_type values from extra dict**

- **Found during:** Task 1 TDD GREEN phase
- **Issue:** Initial test used `assert 'temperature_sensor_found' in caplog.text` — but `caplog.text` contains the formatted log message string, not the `extra` dict values. The actual message is `"Temperature sensor found: ups.temperature=35.0°C"` which does not contain the underscore-joined event_type string. Additionally, the monitor logger clears handlers and adds stderr in the fixture, so records don't propagate through the root logger that caplog intercepts.
- **Fix:** Switch to `patch('src.monitor.logger')` and assert against `mock_logger.info.call_args_list` extracting `extra.event_type` values.
- **Files modified:** `tests/test_monitor.py`
- **Commit:** 7563676

## Self-Check: PASSED

All required artifacts present:
- `src/monitor.py` contains `_probe_temperature_sensor`, `temperature_sensor_found`, `temperature_sensor_unavailable`, temperature var tuple, probe call after connectivity check
- `tests/test_monitor.py` contains `TestTemperatureProbe` with all 3 test methods
- `src/nut_client.py` contains `Security note: empty PASSWORD`, `loopback only`, `LISTEN 127.0.0.1`
- `README.md` contains `## Security`, `NUT authentication`, `empty-password authentication`, `LISTEN 127.0.0.1`
- `## Security` appears at line 140, `## License` at line 151 (correct order)
- All 88 tests pass, zero regressions
