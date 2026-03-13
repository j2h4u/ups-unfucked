# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-13
**Current Focus:** Phase 1 (not yet started)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 1 (Foundation — NUT Integration & Core Infrastructure)
**Status:** Not started
**Progress:** —

### What's Next

- [ ] Plan Phase 1: Decompose requirements DATA-01 through MODEL-04 into executable tasks
- [ ] Verify NUT socket integration, UPS readiness
- [ ] Build ring buffer + EMA core
- [ ] Initialize model.json with standard VRLA curve

---

## Performance Metrics

(Will be populated during execution)

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

**Lessons Learned:** (None yet — ship Phase 1 first)

---

*State initialized: 2026-03-13*
