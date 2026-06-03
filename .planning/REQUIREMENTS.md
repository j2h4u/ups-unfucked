# Requirements: v3.2 Honest Monitoring & Diagnostic Verification

**Milestone goal:** Retract the v3.0 "active desulfation via scheduled deep discharges" mechanism (premise disproven — discharge forms sulfate, charging reverses it, and the daemon has no charge control), reframe the scheduler to occasional diagnostic capacity verification, fix the scheduler bootstrap deadlock, and correct the docs.

**Evidence basis (see ADR):** Battery University BU-804b, Power Designers Sibex, Vertiv BattCon, Schneider/APC, Lifeline, IEEE-1188. Verified live: CyberPower UT850 NUT exposes only beeper/driver/load/shutdown/test.battery.start.* — no charge-voltage control.

---

## In Scope

### Retraction (remove disproven-premise code)

- **RET-01** — Remove `src/battery_math/sulfation.py` (`compute_sulfation_score`, `estimate_recovery_delta`) and all production callers.
- **RET-02** — Remove `src/battery_math/cycle_roi.py` (`compute_cycle_roi`) and all production callers.
- **RET-03** — Remove blackout-credit-as-desulfation logic and recovery_delta-as-desulfation-evidence from the discharge path and scheduler.
- **RET-04** — Remove `sulfation_score` / `cycle_roi` (and sulfation-confidence/recovery) fields from the health endpoint, model.json schema/validation, MOTD output, and journald discharge events. Remove the tests that exercise the deleted code (`test_sulfation*.py`, `test_cycle_roi.py`, related scheduler assertions).

### Scheduler reframe (diagnostic-only)

- **SCH-01** — `evaluate_test_scheduling` proposes a test as a **diagnostic capacity/SoH verification**, driven by a simple time-based cadence (≈365 days since last test / IEEE-1188-style), NOT by a sulfation score or cycle ROI.
- **SCH-02** — The trigger reads the **persistent** `last_upscmd_timestamp` from model.json (restart-safe); a daemon restart must not re-trigger a test, and there is no dependency on in-memory sulfation state. This eliminates the confirmed bootstrap deadlock.
- **SCH-03** — Retain safety gates (SoH floor ≥60%, grid-stability cooldown, rate-limit). Default/first test is **quick** (low-risk); deep only rarely if explicitly warranted.

### Documentation

- **DOC-01** — Remove "actively fights sulfation / extends life via desulfation / desulfation tests" claims from PROJECT.md (done at milestone start), README.md, and ROADMAP/MILESTONES; replace with "honest monitoring + periodic diagnostic capacity verification."
- **DOC-02** — Add an ADR recording the premise reversal, the evidence, and the no-charge-control fact.

### Operator reporting

- **RPT-01** — Add a "Maintenance & schedule" section to `scripts/battery-health.py` (human-readable: next diagnostic test, last test run, IR trend, capacity/SoH) reading model.json + the health endpoint — no JSON hand-parsing.

---

## Acceptance Criteria (goal-backward)

- The daemon **never** self-initiates a discharge for "desulfation"; the only autonomous discharge is a rare diagnostic capacity test gated by safety.
- No `sulfation_score` / `cycle_roi` code or fields remain anywhere (vulture clean; grep clean across src/tests/scripts).
- Scheduler bootstrap deadlock is gone: with only short blackouts and no prior test, the daemon still schedules a first diagnostic test on cadence.
- Docs contain no active-desulfation claims; ADR present.
- `battery-health.py` shows the maintenance schedule.
- ruff/pyright/vulture clean; full pytest green; daemon restarts active.

## Out of Scope

- Any charge-side control (float voltage, equalization) — not exposed by CyberPower NUT.
- Backward compatibility / migration of old model.json — single-host, state regenerates (no compat shims).
- Re-deriving the electrochemistry — already cross-checked this cycle (captured in the ADR).

---
*Created: 2026-06-03 — milestone v3.2*
