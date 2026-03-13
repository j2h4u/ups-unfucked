---
phase: 01-foundation-nut-integration-core-infrastructure
planning_complete: true
plans_created: 5
waves: 2
all_requirements_mapped: true
---

# Phase 1 Planning Summary

**Date:** 2026-03-13
**Planner:** Claude (Haiku 4.5)
**Status:** Complete and committed to git

## Overview

Phase 1 Foundation has been decomposed into **5 executable plans** organized into **2 execution waves** (Wave 0 + Wave 1 parallel + Wave 2 sequential).

All **7 Phase 1 requirements** (DATA-01 through MODEL-04) have been mapped to specific tasks with:
- Clear acceptance criteria (what passes tests)
- Specific file paths (what gets created)
- Dependency graphs (execution order)
- Automated verification (pytest commands)

## Wave Structure

### Wave 0 (Prerequisites)
| Plan | Purpose | Tasks | Scope |
|------|---------|-------|-------|
| 01-01 | Test infrastructure | 5 | pytest framework, conftest fixtures, test stubs |

### Wave 1 (Parallel - 3 independent plans)
| Plan | Purpose | Tasks | Scope | Depends On |
|------|---------|-------|-------|-----------|
| 01-02 | NUT socket client | 2 | TCP communication with 2-sec timeout | Wave 0 |
| 01-03 | EMA smoothing | 2 | Ring buffer, exponential averaging, IR compensation | Wave 0 |
| 01-04 | Model persistence | 2 | Atomic JSON writes, VRLA LUT initialization | Wave 0 |

### Wave 2 (Sequential)
| Plan | Purpose | Tasks | Scope | Depends On |
|------|---------|-------|-------|-----------|
| 01-05 | Daemon integration | 5 | Main loop, config, logging, systemd service | Wave 1 (all 3 plans) |

## Requirements Mapping

| Requirement | Plan | Task | Component |
|-------------|------|------|-----------|
| DATA-01 | 01-02 | 1-2 | NUTClient (socket communication) |
| DATA-02 | 01-03 | 1 | EMABuffer (exponential moving average) |
| DATA-03 | 01-03 | 2 | ir_compensate (load normalization) |
| MODEL-01 | 01-04 | 1 | BatteryModel (persistent storage) |
| MODEL-02 | 01-04 | 2 | Standard VRLA LUT initialization |
| MODEL-04 | 01-04 | 1 | atomic_write_json (crash-safe writes) |

Note: MODEL-03 (SoH history) deferred to Phase 2+; Phase 1 initializes structure.

## Files Created

### Plan Files
```
.planning/phases/01-foundation-nut-integration-core-infrastructure/
  ├── 01-01-PLAN.md (Test infrastructure)
  ├── 01-02-PLAN.md (NUT client)
  ├── 01-03-PLAN.md (EMA & IR compensation)
  ├── 01-04-PLAN.md (Model persistence)
  └── 01-05-PLAN.md (Daemon integration)
```

### Source Files (to be created during execution)
```
src/
  ├── __init__.py
  ├── monitor.py (MonitorDaemon main loop)
  ├── nut_client.py (NUTClient socket communication)
  ├── ema_ring_buffer.py (EMABuffer + ir_compensate)
  ├── model.py (BatteryModel + atomic_write_json)
  ├── config.py (Config class with defaults)
  └── logger.py (systemd journald logging)

systemd/
  └── ups-battery-monitor.service

tests/
  ├── __init__.py
  ├── conftest.py (pytest fixtures)
  ├── test_nut_client.py
  ├── test_ema.py
  └── test_model.py
```

## Key Technical Decisions

### 1. Language & Dependencies
- **Python 3.13+** with standard library only
- No external dependencies in hot path (socket, json, collections, logging are stdlib)
- python-systemd 234+ for journald (already available in Debian 13)
- Avoids PyNUT (too heavy; socket library is cleaner at 50 lines)

### 2. Polling Interval
- **Default: 10 seconds** (chosen over 5 sec option)
- Rationale: Sufficient for 2-min EMA window, lower CPU overhead
- Configurable via `UPS_MONITOR_POLL_INTERVAL` environment variable

### 3. EMA Smoothing
- **Window:** 120 seconds (2 minutes)
- **Formula:** α = 1 - exp(-Δt/τ) where τ = 120 sec
- **For 10-sec interval:** α ≈ 0.0787
- **Convergence:** ~90% by sample 5 (~50 seconds)
- **Stabilization gate:** Predictions blocked until 3+ samples (prevents startup noise)

### 4. IR Compensation
- **Formula:** V_norm = V_ema + k*(L_ema - L_base)
- **Default k:** 0.015 V per 1% load
- **Default L_base:** 20% (typical server load on senbonzakura)
- **Purpose:** Normalizes voltage to reference load for load-independent battery lookup

### 5. Model Storage & Atomicity
- **Path:** ~/.config/ups-battery-monitor/model.json
- **Auto-creation:** Directory created if missing
- **Atomic writes:** tempfile → fsync → os.replace (prevents corruption on power loss)
- **Write timing:** Only on discharge event completion, NOT per sample
- **SSD wear:** Reduced from 1000s writes/month to ~1 write/month

### 6. Logging & Observability
- **Framework:** systemd journald via python-systemd
- **Handler:** JournalHandler with structured formatting
- **Query:** `journalctl -t ups-battery-monitor -f` (real-time follow)
- **Levels:** DEBUG for detailed, WARNING for errors, INFO for milestone events
- **No file logging:** All output to journald (searchable, rotated by systemd)

### 7. Systemd Service
- **Type:** simple (daemon runs in foreground)
- **Restart:** on-failure with 10-sec delay (automatic recovery)
- **Ordering:** After=nut-server.service (waits for NUT to be ready)
- **Security:** ProtectSystem=strict, MemoryLimit=64M, CPUQuota=5%
- **Paths:** ReadWritePaths to ~/.config/ups-battery-monitor only

## Testing Strategy

### Framework
- **pytest 8.0+** (available in Debian 13)
- **Coverage:** pytest-cov plugin
- **Quick feedback:** ~30 seconds full suite

### Test Commands
```bash
# Quick unit tests (5 seconds)
pytest tests/test_nut_client.py tests/test_ema.py -v

# Full suite with coverage (30 seconds)
pytest tests/ -v --cov=src

# After Wave 0: All tests fail as expected (no implementations)
# After Wave 1: Core tests pass (implementations complete)
# After Wave 2: Full suite green (all components integrated)
```

### Test Structure
- **test_nut_client.py:** DATA-01 (socket communication, timeout, reconnect)
- **test_ema.py:** DATA-02 (convergence, stabilization gate, memory bounds)
- **test_model.py:** DATA-03, MODEL-01, MODEL-02, MODEL-04 (IR, LUT, atomicity)
- **conftest.py:** Shared fixtures (mock NUT responses, temp paths)

## Execution Order

1. **Execute Wave 0:**
   ```
   /gsd:execute-phase 01-foundation-nut-integration-core-infrastructure --plan=01
   ```
   Creates test infrastructure; all tests fail (expected).

2. **Execute Wave 1 (parallel):**
   ```
   /gsd:execute-phase 01-foundation-nut-integration-core-infrastructure --wave=1
   ```
   Implements NUTClient, EMABuffer, BatteryModel. Tests turn green sequentially.

3. **Execute Wave 2:**
   ```
   /gsd:execute-phase 01-foundation-nut-integration-core-infrastructure --wave=2
   ```
   Integrates components into MonitorDaemon. Full suite green.

## Success Criteria

Phase 1 is complete when:

1. **All tests passing:** `pytest tests/ -v --cov=src` → 13 tests, >80% coverage
2. **Daemon starts:** `python -m src.monitor` runs without crash
3. **NUT polling:** `upsc cyberpower@localhost` successfully read (0 dropped samples)
4. **EMA stabilization:** Voltage oscillation < ±0.1V after 2 minutes
5. **Model persistence:** model.json unchanged after 24h operation (only writes on discharge)
6. **Configuration:** Accepts UPS_MONITOR_* environment variables
7. **Logging:** Entries visible in `journalctl -t ups-battery-monitor`
8. **Systemd ready:** Service unit passes `systemd-analyze verify`

## Next Steps

After Phase 1 execution completes:
1. Verify all test passing: `pytest tests/ -v --cov=src`
2. Capture SUMMARY.md files for each plan (execution output)
3. Update STATE.md with Phase 1 completion
4. Schedule Phase 2 planning (battery state estimation, event classification)

Phase 2 will extend the daemon to:
- Lookup battery state-of-charge (SoC) from voltage via LUT
- Calculate remaining runtime via Peukert's law
- Distinguish real blackout from battery test via input.voltage
- Emit correct ups.status flags for shutdown coordination

---

**Committed:** 2026-03-13 (git: 3f44d31)
**Ready for execution:** Yes
**Estimated Phase 1 execution time:** 3-4 hours (including testing and verification)
