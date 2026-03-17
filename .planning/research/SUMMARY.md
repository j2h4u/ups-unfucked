# Research Summary: v3.0 Active Battery Care

**Project:** UPS Battery Monitor v3.0 — Sulfation Modeling & Smart Scheduling
**Domain:** VRLA UPS battery active management (daemon-driven desulfation)
**Researched:** 2026-03-17
**Confidence:** MEDIUM-HIGH (grounded in physics + enterprise practice; validation gates required)

---

## Executive Summary

v3.0 transitions the daemon from **passive observer** (read-only monitoring via `upsc`) to **active battery manager** (intelligent scheduling + command dispatch via `upscmd`). This architectural shift enables three interdependent capabilities: sulfation detection via internal resistance trending + discharge curve analysis, daemon-controlled deep discharge scheduling with safety guardrails, and a cycle ROI metric quantifying desulfation benefit vs wear cost.

The recommended approach prioritizes **safety through constraint enforcement** (SoH floor 60%, rate limiting, blackout credit logic) over aggressive automation. Technology stack remains minimal — zero new external dependencies — using pure Python electrochemical models + subprocess for systemd timer integration. Critical unknown: **sulfation model confidence depends on 30-day field validation** (score stability variance < 5 points/day); until then, scheduling thresholds should be conservative (ROI > 1.10, avoid aggressive tuning).

**Key risks:** daemon-initiated discharge during natural blackout (collision risk), upscmd silent failures (no heartbeat), recovery delta noise masking signal, temperature estimation error propagating through decisions. All have specific mitigation strategies (precondition checks, state machine, pooled recovery, configurable constant temp). Pitfalls research is comprehensive (11 documented scenarios); most critical are Pitfalls 1-3 (blackout collision, upscmd reliability, temperature sensitivity).

---

## Key Findings

### Recommended Stack

v3.0 stack is **zero-dependency addition** to v2.0 core. No external Python packages required; all new features implemented with stdlib + existing subprocess/systemd patterns.

**Core technologies (unchanged from v2.0):**
- **Python 3.13** — daemon core + sulfation model (pure math library sufficient)
- **systemd 249+** — service/timer management, journald structured logging (already integrated)
- **NUT 2.8.1+** — read-only monitoring (`upsc`) + write commands (`upscmd`)
- **Peukert discharge model** — capacity estimation from voltage curves (existing, reused)

**New mathematical modules (pure Python, no external deps):**
- **`battery_math/sulfation.py`** — hybrid physics + data-driven model: Shepherd curve shape analysis + IR trending + recovery delta + temperature compensation (constant fallback 35°C)
- **`battery_math/cycle_roi.py`** — ROI metric: benefit (SoH recovery %) / cost (estimated capacity loss per discharge)
- **`scheduler.py`** — intelligent scheduling: decision tree with safety constraints (SoH floor 60%, rate limiting 1 test/week, blackout grace 7d)

**Systemd integration:**
- Subprocess-based timer control (no D-Bus deps) — `subprocess.run(['systemctl', 'disable', timer])` idiomatic, one-time call per startup
- journald structured logging — already in v2.0, extended for v3.0 scheduling events

**Why no new dependencies:**
- D-Bus adds external dep + debugging complexity for negligible latency gain (one-time startup call)
- Shepherd state-space fitting deferred to v3.1 (requires 6+ months historical data for parameter optimization)
- Temperature sensor polling deferred (UT850 lacks HID sensor; file fallback architecture designed for future USB probe integration)

### Expected Features

**Must-have (table stakes for credible active battery management):**
1. **Sulfation detection** — Rising IR trend + discharge curve shape analysis; identify when battery needs desulfation before capacity loss becomes severe
2. **Safe discharge triggering** — Minimum SoH floor (60%), rate limiting (≤1 test/week), grid stability check (no test if recent power glitches)
3. **Cycle ROI metric** — Quantify desulfation benefit (SoH recovery %) vs wear cost; export to health.json for Grafana trending
4. **Natural blackout credit** — If OL→OB event (≥90% depth) occurred <7 days ago, defer scheduled test (equivalent desulfation already provided)
5. **Cycle count accumulation** — Track OL→OB transitions; persist in model.json for lifecycle analysis

**Should-have (competitive differentiators):**
1. **Intelligent scheduling (daemon-driven)** — Daemon calls `upscmd` directly (replaces static systemd timers); adapts test frequency based on sulfation score + SoH trend + recent blackouts; every scheduling decision logged with reason code
2. **Recovery delta tracking** — Measure voltage recovery 30s post-discharge; trending over 3+ tests enables confidence in recovery signal vs measurement noise
3. **Structured journald events** — Every test trigger/skip logged with: reason, sulfation_score, SoH, next_test_eta, cycle_roi; enables root-cause analysis
4. **Temperature compensation fallback** — Currently constant (35°C configurable); architecture ready for USB sensor or MQTT endpoint in future (file polling in v3.1)

**Defer to v3.1+ (post-validation):**
1. **Shepherd state-space fitting** — Parameter learning after 6+ months discharge history accumulated
2. **NUT HID temperature integration** — When user adds USB probe or future UPS model includes sensor
3. **Peukert exponent auto-calibration** — Requires circular dependency resolution (deferred from v2.0)
4. **Shallow test as leading indicator** — Quick test before scheduling deep discharge to forecast desulfation need
5. **Multi-UPS support** — Architecture ready; single UT850 only for v3.0

### Architecture Approach

v3.0 extends v2.0's data flow with two new orchestration layers: **Sulfation Model** (computes score from discharge history + IR trend) and **Intelligent Scheduler** (evaluates ROI + constraints + natural blackout credit to decide test trigger). Both feed into existing persistence layer (model.json) and reporting pipeline (health.json, journald).

**Major components:**

1. **Sulfation Model** (`src/battery_math/sulfation.py`) — Pure function computing score (0-100) from: SoH baseline trend (40% weighting), IR percent rise (30%), voltage recovery delta (20%), recovery success rate (10%). Threshold: score ≥65 indicates test candidate; score ≥80 indicates urgent desulfation.

2. **Intelligent Scheduler** (`src/scheduler.py`) — Decision engine runs daily; evaluates: sulfation_score, SoH floor (≥60%), days_since_last_test (≥7 for minimum interval), natural_blackout_credit (if recent OB event, defer 7 days), ROI threshold (≥1.10). Calls `upscmd` only if all constraints pass; logs every decision to journald.

3. **Test Executor** (modified `src/nut_client.py`) — Spawns `upscmd` via subprocess with safety preconditions: verify UPS online, battery fully charged (≥95%), no test already running. Implements state machine: poll test.result every 30s, timeout safeguard (hard abort at 15 min if test overshoots expected 10-12 min window).

4. **Cycle ROI Calculator** (`src/battery_math/cycle_roi.py`) — Pure function: ROI = (capacity_recovery_percent) / (cycle_wear_percent). Wear cost estimated from Peukert factor (0.15% per cycle at 90% depth). Exported to health.json, trending over time.

5. **Model Persistence** (extended `src/model.py`) — New schema sections: sulfation history, test_schedule state, natural_blackout_events (with grace period tracking), cycle_roi_history. Backward compatible (v2.0 files load, new fields init).

**Data flow highlights:**
- Every discharge (natural blackout or scheduled test) → DischargeHandler processes → SoH recalculated → sulfation_score computed + stored → journald logging → health.json export
- Daily scheduler evaluation → reads model.json (sulfation history, last test, blackout credit) → computes decision → records pending_test or logs deferral reason
- On test dispatch → precondition checks (SoC >80%, no recent glitches) → upscmd subprocess → polling loop → discharge detector captures test as OB event → postprocessing updates ROI

### Critical Pitfalls

Identified 11 domain pitfalls in PITFALLS-V3.md; top 5 require explicit mitigation:

1. **Daemon-Initiated Test During Natural Blackout** — If daemon schedules test at 2:55 AM and blackout occurs at 3:02 AM during test, battery partially discharged when real load hits → runtime drops from 47 min to 20 min → unclean shutdown risk. **Mitigation:** Block test scheduling if recent blackout (last 2h), require SoC ≥80% before dispatch, implement test abort protocol (call `upscmd test.battery.stop`), limit test window to historically stable hours.

2. **upscmd Silent Failures & State Ambiguity** — UPS may reject test silently (not fully charged, test already running, firmware hang) without heartbeat; daemon doesn't know if test actually running. **Mitigation:** Verify preconditions before dispatch (battery.charge ≥95%), implement polling state machine (poll test.result every 30s for 15 min), validate test actually happened by checking SoC drop matches expected ΔSoC (±10% tolerance).

3. **Sulfation Model Temperature Sensitivity** — Hard-coded 35°C assumption; actual variation ±10°C → model error ±30% on recovery prediction. Winter 15°C: underestimate sulfation, skip tests, battery sulfates. Summer 35°C: overcredit tests. **Mitigation:** Document temperature_estimated ± temperature_uncertainty in model.json, use empirical recovery measurement (adjust per-test calibration factor), never use sulfation model alone (combine with IR trend + SoC recovery speed as tie-breaker).

4. **Recovery Delta Noise Masking Signal** — Single test SoH measurement ±1% noise floor; can't distinguish +0.3% recovery from noise. Accumulating noisy credit over 12 tests = false confidence. **Mitigation:** Require 3+ tests pooled over 2 weeks before crediting recovery, implement per-test confidence scoring (high confidence only if discharge >10 min, deep >50% DoD, low voltage noise), trend recovery rate over 6 tests (ignore magnitude, track direction).

5. **Race Condition: Daemon Test Scheduling + Systemd Timers** — Both systemd timer and daemon scheduler can fire simultaneously, triggering two tests back-to-back. **Mitigation:** Migrate to daemon-only scheduling (disable systemd timers on v3.0 startup), document migration guide for ops, implement fcntl lock if both schedulers coexist (lock held during test execution, loser backs off).

Other significant pitfalls: (6) deep test on degraded battery accelerates failure — SoH floor check mandatory (65% proposed, 60% in recommendation); (7) cycle ROI metric instability due to both numerator + denominator uncertainty — report confidence intervals not point estimates; (8) cycle wear estimation 10x off from literature — measure empirically after 6 months; (9) temperature from heuristic wrong — accept constant only; (10) test abort incomplete — verify stop command actually worked; (11) capacity not converged before scheduling starts — gate scheduling on capacity confidence >80%.

---

## Implications for Roadmap

Based on research, suggested phase structure balances **non-invasive observability first** (phases 1-2) with **controlled activation** (phases 3-4):

### Phase 1: Foundation (NUT Write + Math Models)

**Rationale:** De-risk core technologies without daemon integration. Sulfation model and ROI calculator are pure functions, fully testable in isolation. NUT upscmd capability validated before integration.

**Delivers:**
- `src/nut_client.py` enhanced with `send_command()` method (upscmd protocol)
- `src/battery_math/sulfation.py` pure function + unit tests (12 tests)
- `src/battery_math/cycle_roi.py` pure function + unit tests (8 tests)

**Avoids pitfalls:** No daemon changes; no race condition risk; pure functions testable offline

---

### Phase 2: Persistence & Observability (Model Integration)

**Rationale:** Extend model.json schema and discharge handler to track sulfation history + ROI + natural blackouts. Daemon still read-only; no scheduling yet. All observability in place before active control.

**Delivers:**
- Extended `model.json` schema (sulfation, test_schedule, natural_blackout_events, cycle_roi_history)
- Modified `src/discharge_handler.py` calls sulfation/ROI functions post-discharge, logs results
- MOTD module showing sulfation_score, next_test_eta, blackout_credit countdown
- health.json export of sulfation_score, roi, scheduling reason, next_test_timestamp
- journald structured events for all discharge analysis (reason, soh_delta, ir_delta, roi)

**Avoids pitfalls:** Daemon still passive; no test dispatch; recovery delta thresholding not yet applied (just measured + logged)

---

### Phase 3: Scheduling Intelligence (Daemon-Driven Decision Logic)

**Rationale:** Implement scheduler as pure decision function; integrate into daemon main loop. Test logic validated before enabling upscmd dispatch.

**Delivers:**
- `src/scheduler.py` with decision tree: evaluate ROI, SoH floor, days_since_test, blackout_credit, grid_stability
- Integration into `src/monitor.py` main loop: hourly scheduler evaluation call
- Pending_test state tracking in model.json
- Precondition checks before dispatch (SoC ≥80%, no recent glitches, no test already running)
- Logging of every scheduling decision (propose_test, test_deferred, test_blocked) with reason

**Avoids pitfalls:** Scheduler still doesn't call upscmd; can test logic in log-only mode; safety constraints validated before any hardware interaction; race condition prevention: systemd timers disabled on daemon startup

---

### Phase 4: Active Control & Field Validation (upscmd Dispatch)

**Rationale:** Enable actual test dispatch only after phases 1-3 validated. Real UPS testing required; ops sign-off needed. Longest phase; includes 30-day field monitoring gates.

**Delivers:**
- Enable `scheduler.execute_test()` → `nut_client.send_command("test.battery.start.deep")`
- Precondition validation: battery fully charged ≥95%, UPS online, no power glitches last 4h
- Test state machine: poll NUT every 30s during test, detect completion, validate SoC drop matches expected
- Retry logic: if UPS refuses (charging), queue for 1h later; if timeout (>15 min), log ERROR + escalate
- Disable systemd timers: `ups-test-deep.timer`, `ups-test-quick.timer` disabled on daemon startup
- Integration testing: real deep discharge test on UT850, verify daemon handles preconditions + responses

**Release blockers:** All 6 validation gates must pass (stress test, real upscmd, 30-day stability, blackout credit, ROI calibration, safety floor)

---

### Research Flags

**Phases likely needing deeper research during planning:**
- **Phase 2:** Blackout event classification — verify event classifier correctly labels natural vs test-induced OB
- **Phase 3:** Grid stability detection — 4h glitch window heuristic sufficient for deployment
- **Phase 4:** Temperature sensor integration — document future path if USB probe added

**Phases with standard patterns (skip research-phase):**
- **Phase 1:** Pure function testing, subprocess I/O — well-documented patterns
- **Phase 2:** JSON schema extension, logging — standard systemd/journald practices
- **Phase 3:** Scheduler decision tree — established daemon pattern

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| **Stack** | HIGH | Zero new dependencies verified; Python 3.13 + systemd + NUT established patterns |
| **Features** | MEDIUM-HIGH | Grounded in enterprise practice (IEEE 1188, Eaton/Schneider standards); must-have features well-defined |
| **Architecture** | HIGH | v2.0 codebase well-understood; v3.0 adds clean component boundaries (pure functions) |
| **Pitfalls** | HIGH | 11 domain pitfalls systematically researched with specific mitigation strategies |
| **Validation Gates** | MEDIUM | 6 acceptance gates defined; first 2 automated, last 4 require 30+ days field monitoring |

**Overall confidence: MEDIUM-HIGH**

Research thoroughly covers physics (VRLA sulfation), enterprise practice (Eaton/Schneider scheduling), and domain pitfalls (11 scenarios). Stack validated (no blocking dependencies). Architecture patterns established. Only gap: **field validation requires 30+ days real operation** to confirm sulfation score stability (variance <5 points/day) and ROI calibration (recovery 80-90% of prediction). Mitigation: extended Phase 4 with monitoring gates before release.

### Gaps to Address

1. **Sulfation Score Stability** — Must validate in Phase 4 with 30-day monitoring: score variance <5 points/day, autocorrelation >0.8
2. **Recovery Delta as Signal vs Noise** — Validate signal-to-noise ratio; empirical convergence via pooling already designed
3. **ROI Model Calibration** — Across 10 real tests, does actual SoH recovery match model prediction ±20%?
4. **Blackout Depth Classification** — Validate blackout ≥90% depth is actually desulfating
5. **Temperature Impact** — If facility temperature varies ±10°C seasonally, measure sulfation_score correlation
6. **UPS Firmware upscmd Behavior** — Manual test required: does upscmd work on target UT850EG hardware?

---

## Sources

**Primary (HIGH confidence):**
- STACK-v3.0.md, FEATURES-v3.0.md, ARCHITECTURE-v3.0.md, ARCHITECTURE.md, DECISIONS-v3.0.md, PITFALLS-V3.md
- NUT documentation (upscmd protocol, usbhid-ups driver, INSTCMD protocol)
- IEEE standards (IEEE 1188 VRLA, IEEE 450 VLA, IEC 61427-2)

**Secondary (MEDIUM confidence):**
- VALIDATION-v3.0.md (test architecture, 30+ unit tests, 6 validation gates)
- Real project context (CyberPower UT850EG, 2026-03-12 blackout event, 2-5 blackouts/week)

**Tertiary (References in source documents):**
- Enterprise manuals (Eaton ABM, Schneider monitoring, CyberPower UT850 guide)
- Academic sources (MDPI Energies, ScienceDirect, ResearchGate)

---

## Summary for Roadmapper

**v3.0 is a well-scoped, risk-mitigated progression from v2.0.** Four-phase structure recommended with clear handoffs. All critical decisions made (stack, architecture, thresholds). All major pitfalls identified with mitigation strategies. Phase 4 validation gates realistic; no gates require innovations or new research.

**Ready for roadmap creation and detailed requirements definition.**

---

*Research completed: 2026-03-17*
*Confidence: MEDIUM-HIGH (field validation gates required before release)*
*Ready for roadmap: YES*
