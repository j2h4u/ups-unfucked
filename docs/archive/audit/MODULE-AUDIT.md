# Module Audit — Expert Panel Reviews

Systematic review of each daemon module by expert panel. Findings tracked per module, critical items fixed immediately, medium deferred or forwarded to dependent modules.

## Cumulative Severity Summary

- **Critical:** 2 findings (F19, F36a)
- **High:** 7 findings (F13✅, F20, F21, F24, F30, F36, F41, F53)
- **Medium:** 14 findings
- **Low/Info:** 19 findings

---

## Module Audits

### [Module 1: Signal Processing](./audit/audit-01-signal-processing.md)
EMA filtering, adaptive alpha calibration. Key finding: F3 deferred to Module 2 (IR compensation validity during OB discharge).

### [Module 2: SoC Lookup](./audit/audit-02-soc-lookup.md)
LUT deduplication, IR compensation physics. Resolves F3 from Module 1. Key finding: cliff region (10.5-11.0V) lacks measured data, improves organically with discharge cycles.

### [Module 3: Runtime Prediction](./audit/audit-03-runtime-prediction.md)
Peukert formula, load effects, validation. Key finding: F13 (load=0 edge case) fixed. Peukert exponent empirically validated at 1.15 on 2026-03-12 blackout.

### [Module 4: SoH Estimation](./audit/audit-04-soh-estimation.md)
**CRITICAL BUGS:** F19 (formula measures discharge fraction, not health — SoH 100%→55% on 7-min blackout), F20/F21 (0.30 constant unjustified, "Bayesian" blend incorrect). Root cause of 2026-03-16 incident. Redesign options: ΔSoC normalization, capacity-based SoH, or hybrid approach.

### [Module 5: Capacity Estimation](./audit/audit-05-capacity-estimation.md)
Coulomb counting, convergence gates. Key finding: F24 (ΔSoC ≥ 25% gate rejects typical 1-2 min blackouts, only monthly tests converge). Recommended lower to 15%.

### [Module 6: RLS Calibration](./audit/audit-06-rls-calibration.md)
Adaptive Peukert exponent, IR compensation. Key finding: F30 (short discharges clamp to 1.4, RLS converges wrong value — root cause of Peukert 1.2→1.4 incident). Cross-dependency: F31 (SoH broken, masks dependency).

### [Module 7: Event Classification](./audit/audit-07-event-classification.md)
**CRITICAL BUG:** F36a (unrecognized status generates false "OL" in virtual UPS, misinforms upsmon during battery discharge). Related: F36 (LB flag breaks exact string matching). Fix: flag-based matching ("OB" in status) + fallback to original ups.status.

### [Module 8: Persistence & Export](./audit/audit-08-persistence-export.md)
Model state, prune limits, logging. Key finding: F41 (BLACKOUT_TEST suppresses LB unconditionally — test can drain to hardware cutoff without graceful shutdown). Add hard floor: runtime < 2 min → LB regardless of event type.

### [Module 9: Orchestration](./audit/audit-09-orchestration.md)
Main loop, call ordering, cooldown logic. All findings OK. Pipeline is well-ordered and correctly implements cooling/flicker-suppression. Note: F58 — monitor.py is 1300+ lines, decompose at next milestone.

### [Module 10: Replacement Prediction](./audit/audit-10-replacement-prediction.md)
Linear regression on SoH history. Key finding: F53 (depends on broken SoH from F19 — convergence gate is necessary but not sufficient). F54 (capacity_ah_ref filter not wired in caller). Predictor code is clean; fix is upstream.

---

## Cross-Module Dependencies

```
F53 (replacement prediction) ← F19 (SoH formula broken)
F30 (Peukert RLS clamping) ← F19 (SoH formula broken)
F15 (runtime accuracy at low SoC) ← F9 (cliff data gap)
F31 (Peukert SoH dependency) ← F19 + F30 (masked by clamp)
```

**Critical path:** F19 (SoH redesign) blocks F53 (replacement prediction) and F30 (Peukert calibration accuracy). This is the highest-leverage fix.

## Fix Priority (recommended order)

1. **F36 + F36a** — Event classifier: flag-based matching + fallback. Safety-critical, one-file change.
2. **F41** — BLACKOUT_TEST LB floor. Safety-critical, one-line change.
3. **F19 + F20 + F21** — SoH formula redesign. Highest-leverage: unblocks F53, F30, F31.
4. **F30** — Peukert RLS: skip clamped values. One-line guard.
5. **F24** — Capacity ΔSoC gate: lower from 25% to 15%. One-line change.
6. **F42** — Virtual UPS log level: INFO→DEBUG. One-line change.
7. **F54** — Wire capacity_ah_ref in replacement prediction caller. One-line fix.
8. **F58** — Decompose monitor.py. Refactoring milestone.
