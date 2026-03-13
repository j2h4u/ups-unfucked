# Milestones

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

