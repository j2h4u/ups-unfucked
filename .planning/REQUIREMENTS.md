# Requirements: UPS Battery Monitor v3.0

**Defined:** 2026-03-17
**Core Value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

## v3.0 Requirements

Requirements for v3.0 Active Battery Care. Daemon transitions from passive observer to active battery manager.

### Sulfation Model

- [ ] **SULF-01**: Daemon computes sulfation score [0.0–1.0] from hybrid model (physics baseline + empirical signals)
- [ ] **SULF-02**: Physics baseline tracks days since last deep discharge with temperature factor (configurable constant, default 35°C)
- [ ] **SULF-03**: IR trend signal detects sulfation via internal resistance growth rate (dR/dt acceleration)
- [ ] **SULF-04**: Recovery delta signal measures SoH bounce after deep discharge as desulfation evidence
- [ ] **SULF-05**: Sulfation score persisted in model.json with history for trend analysis
- [ ] **SULF-06**: All sulfation math implemented as pure functions in src/battery_math/

### Test Scheduling

- [ ] **SCHED-01**: Daemon sends upscmd test.battery.start.deep when sulfation score warrants and safety gates pass
- [ ] **SCHED-02**: Daemon sends upscmd test.battery.start.quick for periodic IR measurement and e2e readiness check
- [ ] **SCHED-03**: Natural blackout credit — skip scheduled deep test when recent blackouts already desulfated battery
- [ ] **SCHED-04**: Safety gate: no test when UPS is on battery (OB state)
- [ ] **SCHED-05**: Safety gate: no deep test when SoH below floor threshold (65%)
- [ ] **SCHED-06**: Safety gate: no deep test when grid unstable (blackouts in last 24h)
- [ ] **SCHED-07**: Daemon replaces static systemd timers (ups-test-quick.timer, ups-test-deep.timer) entirely
- [ ] **SCHED-08**: Daemon distinguishes self-initiated tests from natural blackouts in event metadata

### Cycle ROI

- [ ] **ROI-01**: Daemon computes cycle ROI metric per discharge: desulfation benefit vs wear cost
- [ ] **ROI-02**: ROI factors: days since last deep discharge, depth of discharge, remaining cycle budget, IR trend, sulfation score
- [ ] **ROI-03**: ROI exported to health.json for Grafana visualization (not MOTD)

### Reporting

- [ ] **RPT-01**: Sulfation score exported to health.json
- [ ] **RPT-02**: Scheduling decisions (test recommended/skipped/executed + reason) logged as journald structured events
- [ ] **RPT-03**: Next scheduled test time and reason exported to health.json

## v3.1+ Requirements

Deferred to future release. Tracked but not in current roadmap.

### Temperature Enhancement

- **TEMP-01**: If NUT HID exposes battery.temperature on CyberPower, use real sensor data instead of constant
- **TEMP-02**: Seasonal thermal correction based on ambient temperature variation

### Advanced Detection

- **ADV-01**: Discharge curve shape analysis (cliff region expansion as sulfation indicator)
- **ADV-02**: Peukert exponent auto-calibration from deep discharge data (CAL2-02)
- **ADV-03**: Cliff-edge degradation detector (Bayesian SoH inertia at rapid degradation)

## Out of Scope

| Feature | Reason |
|---------|--------|
| External temperature sensor (USB/I2C) | Hardware mod, out of scope for software project |
| Web UI for scheduling control | No web UI policy — daemon is autonomous |
| User-defined test schedules | Daemon decides based on math, not operator opinion |
| Shallow discharge equalization | Anti-feature: adds wear without meaningful desulfation |
| ML-based remaining useful life | Overkill for single UPS; physics model sufficient |
| Backward compatibility / staged rollout | Single server, single UPS, single user — not needed |
| Dry-run / advisory mode | No obligation to be conservative — daemon acts directly |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SULF-01 | — | Pending |
| SULF-02 | — | Pending |
| SULF-03 | — | Pending |
| SULF-04 | — | Pending |
| SULF-05 | — | Pending |
| SULF-06 | — | Pending |
| SCHED-01 | — | Pending |
| SCHED-02 | — | Pending |
| SCHED-03 | — | Pending |
| SCHED-04 | — | Pending |
| SCHED-05 | — | Pending |
| SCHED-06 | — | Pending |
| SCHED-07 | — | Pending |
| SCHED-08 | — | Pending |
| ROI-01 | — | Pending |
| ROI-02 | — | Pending |
| ROI-03 | — | Pending |
| RPT-01 | — | Pending |
| RPT-02 | — | Pending |
| RPT-03 | — | Pending |

**Coverage:**
- v3.0 requirements: 20 total
- Mapped to phases: 0
- Unmapped: 20 ⚠️

---
*Requirements defined: 2026-03-17*
*Last updated: 2026-03-17 after initial definition*
