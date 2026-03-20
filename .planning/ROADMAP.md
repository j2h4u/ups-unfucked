# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation** — Phases 12-14 (shipped 2026-03-16)
- ✅ **v3.0 Active Battery Care** — Phases 15-17 (shipped 2026-03-20)
- ✅ **v3.1 Code Quality Hardening** — Phases 18-24 (shipped 2026-03-21)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-6) — SHIPPED 2026-03-14</summary>

- [x] Phase 1: Foundation — NUT Integration & Core Infrastructure (5/5 plans) — completed 2026-03-13
- [x] Phase 2: Battery Model — State Estimation & Event Classification (6/6 plans) — completed 2026-03-14
- [x] Phase 3: Virtual UPS & Safe Shutdown (4/4 plans) — completed 2026-03-14
- [x] Phase 4: Health Monitoring & Battery Degradation (2/2 plans) — completed 2026-03-14
- [x] Phase 5: Operational Setup & Systemd Integration (2/2 plans) — completed 2026-03-14
- [x] Phase 6: Calibration Mode (2/2 plans) — completed 2026-03-14

</details>

<details>
<summary>✅ v1.1 Expert Panel Review Fixes (Phases 7-11) — SHIPPED 2026-03-14</summary>

- [x] Phase 7: Safety-Critical Metrics (2 plans) — completed 2026-03-14
- [x] Phase 8: Architecture Foundation (4 plans) — completed 2026-03-15
- [x] Phase 9: Test Coverage (3 plans) — completed 2026-03-14
- [x] Phase 10: Code Quality & Efficiency (2 plans) — completed 2026-03-14
- [x] Phase 11: Polish & Future Prep (3 plans) — completed 2026-03-14

</details>

<details>
<summary>✅ v2.0 Actual Capacity Estimation (Phases 12-14) — SHIPPED 2026-03-16</summary>

- [x] Phase 12: Deep Discharge Capacity Estimation (4/4 plans) — completed 2026-03-16
- [x] Phase 12.1: Math Kernel Extraction & Formula Stability Tests (6/6 plans, INSERTED) — completed 2026-03-16
- [x] Phase 13: SoH Recalibration & New Battery Detection (2/2 plans) — completed 2026-03-16
- [x] Phase 14: Capacity Reporting & Metrics (3/3 plans) — completed 2026-03-16

</details>

<details>
<summary>✅ v3.0 Active Battery Care (Phases 15-17) — SHIPPED 2026-03-20</summary>

- [x] Phase 15: Foundation — Sulfation model, cycle ROI, NUT INSTCMD (5/5 plans) — completed 2026-03-17
- [x] Phase 16: Persistence & Observability — health.json, journald, MOTD (6/6 plans) — completed 2026-03-17
- [x] Phase 17: Scheduling Intelligence — daemon-controlled test scheduling (2/2 plans) — completed 2026-03-17

</details>

<details>
<summary>✅ v3.1 Code Quality Hardening (Phases 18-24) — SHIPPED 2026-03-21</summary>

- [x] Phase 18: Unify Coulomb Counting — single integrate_current() (1/1 plans) — completed 2026-03-20
- [x] Phase 19: Extract SagTracker — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 20: Extract SchedulerManager — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 21: Extract DischargeCollector — own module + sulfation split (2/2 plans) — completed 2026-03-20
- [x] Phase 22: Naming + Docs Sweep — renames + docstrings (2/2 plans) — completed 2026-03-20
- [x] Phase 23: Test Quality Rewrite — outcome assertions + DI (4/4 plans) — completed 2026-03-20
- [x] Phase 24: Temperature + Security Hardening — NUT probe, validation, auth docs (2/2 plans) — completed 2026-03-21

</details>

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 5/5 | Complete | 2026-03-13 |
| 2. Battery Model | v1.0 | 6/6 | Complete | 2026-03-14 |
| 3. Virtual UPS | v1.0 | 4/4 | Complete | 2026-03-14 |
| 4. Health Monitoring | v1.0 | 2/2 | Complete | 2026-03-14 |
| 5. Operational Setup | v1.0 | 2/2 | Complete | 2026-03-14 |
| 6. Calibration Mode | v1.0 | 2/2 | Complete | 2026-03-14 |
| 7. Safety-Critical Metrics | v1.1 | 2/2 | Complete | 2026-03-14 |
| 8. Architecture Foundation | v1.1 | 4/4 | Complete | 2026-03-15 |
| 9. Test Coverage | v1.1 | 3/3 | Complete | 2026-03-14 |
| 10. Code Quality & Efficiency | v1.1 | 2/2 | Complete | 2026-03-14 |
| 11. Polish & Future Prep | v1.1 | 3/3 | Complete | 2026-03-14 |
| 12. Deep Discharge Capacity Estimation | v2.0 | 4/4 | Complete | 2026-03-16 |
| 12.1 Math Kernel & Stability Tests | v2.0 | 6/6 | Complete | 2026-03-16 |
| 13. SoH Recalibration & New Battery | v2.0 | 2/2 | Complete | 2026-03-16 |
| 14. Capacity Reporting & Metrics | v2.0 | 3/3 | Complete | 2026-03-16 |
| 15. Foundation | v3.0 | 5/5 | Complete | 2026-03-17 |
| 16. Persistence & Observability | v3.0 | 6/6 | Complete | 2026-03-17 |
| 17. Scheduling Intelligence | v3.0 | 2/2 | Complete | 2026-03-17 |
| 18. Unify Coulomb Counting | v3.1 | 1/1 | Complete | 2026-03-20 |
| 19. Extract SagTracker | v3.1 | 1/1 | Complete | 2026-03-20 |
| 20. Extract SchedulerManager | v3.1 | 1/1 | Complete | 2026-03-20 |
| 21. Extract DischargeCollector | v3.1 | 2/2 | Complete | 2026-03-20 |
| 22. Naming + Docs Sweep | v3.1 | 2/2 | Complete | 2026-03-20 |
| 23. Test Quality Rewrite | v3.1 | 4/4 | Complete | 2026-03-20 |
| 24. Temperature + Security Hardening | v3.1 | 2/2 | Complete | 2026-03-21 |

---

*Roadmap updated: 2026-03-21 after v3.1 milestone completion*
