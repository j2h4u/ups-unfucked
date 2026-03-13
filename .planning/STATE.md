# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-13
**Current Focus:** Phase 1 (Wave 0 Complete — Test Infrastructure Ready)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 1 (Foundation — NUT Integration & Core Infrastructure)
**Current Plan:** 05 (Daemon Integration & Systemd Service — Wave 2)
**Status:** Plan 01-01 Complete; 01-02, 01-03, 01-04 Complete; 01-05 Ready
**Progress:** 4/5 plans completed (80%)

### Completed Plans

- [x] 01-01: Test infrastructure
- [x] 01-02: NUT socket client (COMPLETE)
- [x] 01-03: EMA smoothing and IR compensation (COMPLETE)
- [x] 01-04: Battery model persistence (COMPLETE)
- [ ] 01-05: Daemon integration and systemd service

### What's Next

- [ ] Plan 01-05: Integrate daemon and systemd service (Wave 2)

---

## Performance Metrics

| Plan | Duration | Tasks | Tests | Completion Date |
|------|----------|-------|-------|-----------------|
| 01-01 | 8 min | 5 | 38 | 2026-03-13 |
| 01-02 | 45 min | 2 | 4 | 2026-03-13 |
| 01-03 | — | — | 14 | 2026-03-13 |
| 01-04 | 98 sec | 1 | 20 | 2026-03-13 |

---

## Accumulated Context

### Key Decisions Baked Into Roadmap

| Decision | Phase | Why |
|----------|-------|-----|
| dummy-ups as transparent proxy | Phase 3 | Grafana and upsmon don't change; data source just switches to virtual |
| Distinguish blackout/test via input.voltage | Phase 2 | Physical invariant, independent of firmware interpretation |
| We control ups.status and LB flag | Phase 3 | Bypasses `onlinedischarge_calibration` bug without touching NUT config |
| LUT + IR + Peukert, not formulas | Phase 2 | VRLA curve is individual per battery; only LUT + measured points work |
| model.json as persistent storage | Phase 1 | Discharge events are rare (monthly); SSD not worn by constant writes |
| SoH via area-under-curve (V×time) | Phase 4 | Only way to measure degradation without access to `calibrate.start` |

### Open Questions (Deferred to Planning)

- **Polling interval:** 5 sec vs 10 sec? (Phase 1 plan will confirm)
- **Shutdown threshold:** How many minutes before end is "safe" to trigger LB? (Phase 3 plan)
- **SoH alert threshold:** SoH < 80%? (Phase 4 plan)
- **Runtime alert threshold:** Time_rem@100% < X minutes? (Phase 4 plan, X value TBD)
- **Installation method:** Script, Makefile, or package? (Phase 5 plan)

---

## Session Continuity

**When you resume:** Check `Current Position` above. If phase is "Not started", start with `/gsd:plan-phase N` where N is the phase number.

**Blockers:** None currently.

**Lessons Learned:**

- Stateless socket polling (connect/send/recv/close per poll) enables automatic NUT restart recovery without daemon changes
- Socket timeout (2.0 sec) prevents daemon hangs; exceptions re-raised for daemon-level retry logic
- Mock socket parameter in __init__ provides clean testing without patching built-in socket module
- Python stdlib socket is sufficient; PyNUT library not needed for this use case
- Test fixtures (conftest.py) with Mock sockets provide comprehensive coverage for socket edge cases

---

*State updated: 2026-03-13 after plan 01-01 completion*
