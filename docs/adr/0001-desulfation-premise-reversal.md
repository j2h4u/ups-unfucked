# ADR 0001: Desulfation Premise Reversal

**Status:** Accepted, 2026-06-03

---

## Context

v3.0 shipped "active desulfation via scheduled deep discharges" on the premise that periodic
deep discharges break up lead-sulfate crystals on battery plates, thereby reversing capacity
loss. The daemon modelled a sulfation score (idle time × temperature × IR drift), tracked
desulfation evidence from SoH rebounds after discharge, and used a cycle ROI metric to decide
when the desulfation benefit outweighed the wear cost of an additional discharge cycle.

That premise is electrochemically wrong.

**The discharge / charge asymmetry:**
During discharge, lead (Pb) at the negative plate and lead dioxide (PbO₂) at the positive
plate react with sulfate ions to form lead sulfate (PbSO₄) — the battery *forms* more sulfate.
During charging, the applied current drives the reverse reaction, converting PbSO₄ back to
Pb, PbO₂, and H₂SO₄. Desulfation is therefore a *charging-side* process. A discharge-only
actuator cannot desulfate; it can only add more sulfate and impose another wear cycle.

**No charge-side control (verified live):**
The CyberPower UT850EG via usbhid-ups/NUT exposes only the following commands:
`beeper.enable`, `beeper.disable`, `driver.reload`, `load.off`, `load.off.delay`,
`shutdown.reboot`, `shutdown.reboot.graceful`, `shutdown.return`, `shutdown.stayoff`,
`test.battery.start.deep`, `test.battery.start.quick`, `test.battery.stop`.
There are no settable charge-voltage variables (`input.transfer.low`, float voltage,
equalization voltage, or any charge-side command). The daemon has no lever to influence
the charging process. Float-voltage-based desulfation (the real mitigation) is fully inside
the CyberPower charger firmware and is inaccessible.

**Evidence basis:**
- Battery University BU-804b — "Sulfation of Lead Acid Batteries": sulfate forms on discharge,
  reverses on charge; partial charging over time causes hard sulfation
- Power Designers / Sibex application note: controlled constant-current charge at elevated
  voltage (equalization) is the industrial desulfation method; deep discharge accelerates
  plate corrosion without recovery benefit
- Vertiv BattCon battery care guidance: periodic capacity tests (discharge) measure SoH;
  they are diagnostic, not therapeutic
- Schneider/APC battery application manual: recommends controlled equalization charges for
  sulfation mitigation; scheduled discharges are listed as capacity verification only
- Lifeline / Concorde battery documentation: deep cycling lead-acid batteries for
  "conditioning" shortens life; float voltage control is the correct maintenance lever
- IEEE-1188 (Recommended Practice for Maintenance, Testing, and Replacement of Valve
  Regulated Lead-Acid Batteries): capacity tests are for *measuring* SoH, not improving it;
  cadence is approximately annual

---

## Decision

1. **Retract the sulfation/cycle-ROI machinery.** Remove `src/battery_math/sulfation.py`
   (`compute_sulfation_score`, `estimate_recovery_delta`), `src/battery_math/cycle_roi.py`
   (`compute_cycle_roi`), all production callers, the `sulfation_score`/`cycle_roi` fields
   from the health endpoint, model.json, MOTD output, and journald discharge events, and the
   test files exercising the deleted code.

2. **Reframe the scheduler to diagnostic-only capacity verification.** The scheduler
   proposes a *single, safety-gated diagnostic test* on an IEEE-1188-style annual cadence
   (~365 days since the last test, or "never tested" → propose immediately after safety gates
   pass). The only autonomous discharge is a diagnostic capacity/SoH verification — not a
   therapeutic desulfation attempt.

3. **Replace sulfation-score and cycle-ROI as proposal drivers with a persistent time
   cadence.** The trigger reads `last_upscmd_timestamp` from model.json (restart-safe);
   a daemon restart does not re-trigger a test. This fixes the confirmed bootstrap deadlock
   where a daemon with only short blackouts and no prior test never proposed a test
   (because `last_sulfation_score` and `last_cycle_roi` were permanently zero).

4. **Retain all safety gates.** SoH floor (≥60%), rate limit (minimum days between tests),
   grid-stability cooldown (after recent blackout), and cycle budget remain. The default
   and first-ever test type is `quick` (lower wear cost).

5. **No charge-side control is planned.** Float-voltage desulfation is inside the CyberPower
   firmware and is not accessible via NUT. Any future charge-side feature would require
   hardware replacement or a UPS model with exposed charge-voltage variables. This is
   explicitly out of scope.

---

## Consequences

### Positive

- **Honest metrics.** The daemon no longer exports a `sulfation_score` or `cycle_roi` that
  imply a therapeutic effect that does not exist.
- **Bootstrap deadlock eliminated.** A fresh daemon with only short blackouts now schedules
  its first diagnostic test within ~365 days (or immediately on first start if never tested),
  rather than never scheduling one.
- **Smaller surface.** Two pure-function modules (`sulfation.py`, `cycle_roi.py`) and
  ~200 lines of wiring code are removed, reducing maintenance surface and test count.
- **Correct framing for the operator.** Discharge tests are clearly labelled as
  *diagnostic capacity verification* — the operator understands what is being measured
  (SoH/capacity), not misled into thinking the daemon is extending battery life by cycling it.

### Negative (accepted)

- **Loses the "active care" narrative.** v3.0's product story — "the daemon fights back
  against sulfation" — was compelling but wrong. The corrected framing is accurate but
  less dramatic.
