# UPS Battery Monitor — Roadmap

**Project:** UPS Battery Monitor v1
**Created:** 2026-03-13
**Total v1 Requirements:** 34
**Granularity:** Standard (6 phases)

---

## Phases

- [x] **Phase 1: Foundation — NUT Integration & Core Infrastructure** - Read real UPS data, integrate with NUT, establish data collection pipeline
- [x] **Phase 2: Battery Model — State Estimation & Event Classification** (6/6 plans complete) - Build battery model, estimate SoC/runtime, distinguish blackout from test
- [ ] **Phase 3: Virtual UPS & Safe Shutdown** - Implement dummy-ups proxy, safe LB signaling, shutdown coordination with upsmon
- [ ] **Phase 4: Health Monitoring & Battery Degradation** - Track SoH, predict replacement date, generate alerts
- [ ] **Phase 5: Operational Setup & Systemd Integration** - Install systemd service, logging, production deployment
- [ ] **Phase 6: Calibration Mode** - Manual calibration flag, cliff region acquisition, one-time setup

---

## Phase Details

### Phase 1: Foundation — NUT Integration & Core Infrastructure

**Goal:** Establish reliable data collection from CyberPower UPS through NUT, implement EMA smoothing, and create persistent battery model storage.

**Depends on:** Nothing (first phase)

**Requirements:** DATA-01, DATA-02, DATA-03, MODEL-01, MODEL-02, MODEL-04

**Success Criteria:**
1. Daemon reads `upsc cyberpower@localhost` at configurable interval (10 sec) with zero dropped samples
2. EMA smoothing maintains ~2-minute rolling window for voltage and load; values stabilize within 3 readings
3. model.json is created at startup with standard VRLA curve initialized, updated only on discharge events (no constant disk churn)
4. Ring buffer in RAM holds 120+ seconds of readings for EMA computation without memory leak

**Plans:** 5 plans in 2 waves

Plans:
- [x] 01-01-PLAN.md — Test infrastructure (Wave 0)
- [x] 01-02-PLAN.md — NUT socket client (Wave 1)
- [x] 01-03-PLAN.md — EMA smoothing and IR compensation (Wave 1)
- [x] 01-04-PLAN.md — Battery model persistence (Wave 1)
- [x] 01-05-PLAN.md — Daemon integration and systemd service (Wave 2)

---

### Phase 2: Battery Model — State Estimation & Event Classification

**Goal:** Convert physical voltage measurements into honest battery state estimates, distinguish real blackout from battery test, and prepare shutdown signals.

**Depends on:** Phase 1

**Requirements:** PRED-01, PRED-02, PRED-03, EVT-01, EVT-02, EVT-03, EVT-04, EVT-05

**Success Criteria:**
1. Voltage normalization (IR compensation) using measured load corrects for 0.1–0.2V offset at different load levels
2. LUT lookup with linear interpolation outputs SoC% within 5% of measured charge state during real discharge
3. Peukert calculation predicts remaining runtime within ±10% error against wall-clock time during blackout
4. Real blackout distinguished from battery test by input.voltage threshold (≈0V vs ≈230V) with 100% accuracy
5. ups.status arbiter emits correct OB DISCHRG or OB DISCHRG LB flags based on time-to-empty, not firmware state

**Plans:** 6 plans in 2 waves

Plans:
- [x] 02-01-PLAN.md — Test infrastructure and module implementations (Wave 0: tests + fixtures + implementations) ✓ COMPLETE
- [x] 02-02-PLAN.md — SoC predictor integration (Wave 1) ✓ COMPLETE
- [x] 02-03-PLAN.md — Runtime calculator integration (Wave 1) ✓ COMPLETE
- [x] 02-04-PLAN.md — Event classifier integration (Wave 1) ✓ COMPLETE
- [x] 02-05-PLAN.md — SoC + runtime integration into monitor loop (Wave 2) ✓ COMPLETE
- [x] 02-06-PLAN.md — Event classification and event-driven logic (Wave 2) ✓ COMPLETE

---

### Phase 3: Virtual UPS & Safe Shutdown

**Goal:** Implement transparent dummy-ups proxy that intercepts honest metrics and coordinates shutdown with upsmon without modifying NUT configuration.

**Depends on:** Phase 2

**Requirements:** VUPS-01, VUPS-02, VUPS-03, VUPS-04, SHUT-01, SHUT-02, SHUT-03

**Success Criteria:**
1. All real UPS fields transparently mirrored to /dev/shm/ups-virtual.dev (tmpfs, zero SSD wear)
2. Three fields overridden with calculated values: battery.runtime (Time_rem), battery.charge (SoC%), ups.status (LB arbiter)
3. dummy-ups source configured in NUT reads virtual device and provides metrics to upsmon without changing upsd.conf
4. upsmon receives LB signal and initiates graceful shutdown within expected threshold (configurable); shutdown does not happen before Time_rem expires

**Plans:** TBD

---

### Phase 4: Health Monitoring & Battery Degradation

**Goal:** Track battery health trajectory, predict replacement date, and alert via MOTD and journald when degradation reaches thresholds.

**Depends on:** Phase 2

**Requirements:** HLTH-01, HLTH-02, HLTH-03, HLTH-04, HLTH-05

**Success Criteria:**
1. SoH recalculated after each discharge event using area-under-curve (voltage × time); value stored in soh_history
2. Linear regression over soh_history produces replacement prediction (e.g., "March 2028") with at least 6 months notice before SoH < threshold
3. MOTD module displays real-time status: charge%, runtime (mins), load%, SoH, and replacement date in single human-readable line
4. journald alert triggered when SoH degrades below configured threshold (e.g., 80%)
5. journald alert triggered when calculated Time_rem@100% falls below alert threshold (e.g., X minutes; exact value TBD)

**Plans:** TBD

---

### Phase 5: Operational Setup & Systemd Integration

**Goal:** Package daemon as production-ready systemd service with logging, installation script, and minimal privilege requirements.

**Depends on:** Phase 1

**Requirements:** OPS-01, OPS-02, OPS-03, OPS-04

**Success Criteria:**
1. Systemd unit file enables daemon auto-start on boot; daemon restarts automatically on crash
2. Install script copies binaries to system paths, configures NUT dummy-ups source, enables service, with zero manual steps after script completion
3. Daemon runs without root in hot path (reading UPS data, computing metrics); privileged operations (NUT communication) isolated to systemd socket
4. All output logged to journald with structured identifiers (unit name, PID, log level) searchable via `journalctl`

**Plans:** TBD

---

### Phase 6: Calibration Mode

**Goal:** Provide one-time manual calibration capability to acquire cliff region data by controlled discharge to cutoff without production shutdown.

**Depends on:** Phase 3

**Requirements:** CAL-01, CAL-02, CAL-03

**Success Criteria:**
1. `--calibration-mode` flag reduces shutdown threshold to ~1 minute; daemon does not initiate critical shutdown until Time_rem ≈ 1 min
2. In calibration mode, each datapoint written to disk with fsync; model.json updated in real-time (one-time cost, not repeated)
3. After calibration event completes, cliff region (11.0V–10.5V) auto-interpolated to anchor (10.5V, 0 min); measured points replace "standard" entries
4. User can repeat calibration only after battery replacement; normal operation uses measured cliff region from first calibration

**Plans:** TBD

---

## Progress Tracking

| Phase | Name | Plans Complete | Status | Completed |
|-------|------|----------------|--------|-----------|
| 1 | Foundation | 5/5 | Complete | 2026-03-13 |
| 2 | Battery Model | 0/6 | Planning revised | — |
| 3 | Virtual UPS & Shutdown | 0/TBD | Not started | — |
| 4 | Health Monitoring | 0/TBD | Not started | — |
| 5 | Operational Setup | 0/TBD | Not started | — |
| 6 | Calibration Mode | 0/TBD | Not started | — |

---

**Next:** `/gsd:execute-phase 02-battery-model-state-estimation-event-classification`
