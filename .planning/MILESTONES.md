# Milestones

## v2.0 Actual Capacity Estimation (Shipped: 2026-03-16)

**Delivered:** Measures real battery capacity from discharge events, replaces rated label value with measured estimate, and recalibrates SoH against actual capacity — enabling accurate health tracking from day one.

**Phases completed:** 4 phases, 15 plans, 23 tasks
**Tests:** 291 passing | **LOC:** 11,602 Python | **Commits:** 96 (v1.1..HEAD)
**Timeline:** 3 days (2026-03-14 → 2026-03-16)
**Git range:** v1.1..190b7b1

**Key accomplishments:**
- Extracted all battery math into pure-function kernel package (`src/battery_math/`) with year-long simulation harness proving formula stability
- Deep discharge capacity estimation with coulomb counting + voltage anchor + Peukert correction (CAP-01–04)
- Statistical confidence tracking with CoV-based convergence detection (3+ samples, CoV<10%)
- SoH recalibration against measured capacity with history versioning and baseline filtering (SOH-01–03)
- New battery detection post-discharge (>10% threshold) with CLI `--new-battery` confirmation flow (CAP-05)
- Full reporting pipeline: MOTD convergence display, structured journald events, Grafana health endpoint metrics (RPT-01–03)

---

## v1.1 Expert Panel Review Fixes (Shipped: 2026-03-14)

**Phases completed:** 5 phases, 14 plans, 33 tasks

**Tests:** 205 passing | **LOC:** 6,596 Python | **Commits:** 111 (v1.0..HEAD)
**Timeline:** 2 days (2026-03-13 → 2026-03-14)
**Git range:** v1.0..61ce215

**Key accomplishments:**
- Per-poll virtual UPS writes during blackout — eliminates 60s LB flag lag (safety-critical)
- Frozen Config dataclass (13 fields) + CurrentMetrics dataclass — replaces untyped dicts and module globals
- Full OL→OB→OL integration test + Peukert/signal handler coverage (205 tests total)
- Batch calibration writes (60x SSD wear reduction) + consolidated error handling
- History pruning (30 entries max) + fdatasync optimization + health.json endpoint for external monitoring
- MetricEMA generic class — extensible per-metric EMA for future sensors

---

## v1.0 MVP (Shipped: 2026-03-14)

**Delivered:** Honest battery monitoring daemon for CyberPower UT850EG — replaces unreliable firmware readings with physics-based estimation, enabling reliable automatic shutdown during blackouts.

**Phases completed:** 6 phases, 21 plans, 51 tasks
**Tests:** 160 passing | **LOC:** 5,003 Python | **Commits:** 103
**Timeline:** 2 days (2026-03-13 → 2026-03-14)
**Git range:** 60c7c2e..161ab11

**Key accomplishments:**
- NUT integration with EMA smoothing and IR compensation for honest battery estimation from physical voltage/load
- Runtime prediction via Peukert's Law, tuned to real 47-min blackout data (2026-03-12)
- Virtual UPS proxy (dummy-ups) — transparent override of battery.runtime, battery.charge, ups.status
- Safe shutdown via LB arbitration — daemon controls low-battery signal, bypassing firmware bug
- Battery health tracking with SoH degradation, replacement date prediction, MOTD + journald alerts
- Calibration mode for one-time cliff region acquisition with fsync persistence and auto-interpolation

---

