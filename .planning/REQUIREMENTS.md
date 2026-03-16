# Requirements: UPS Battery Monitor

**Defined:** 2026-03-15
**Core Value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

## v2.0 Requirements

Requirements for v2.0 Actual Capacity Estimation. Each maps to roadmap phases.

### Capacity Estimation

- [ ] **CAP-01**: Daemon measures actual battery capacity (Ah) from deep discharge events (ΔSoC > 50%)
- [ ] **CAP-02**: Daemon accumulates capacity estimates from partial discharges via depth-weighted averaging
- [ ] **CAP-03**: Daemon tracks statistical confidence across multiple discharge measurements
- [ ] **CAP-04**: Daemon replaces rated capacity_ah with measured value when confidence exceeds threshold
- [ ] **CAP-05**: User can signal "new battery installed" to reset capacity estimation baseline

### SoH Recalibration

- [ ] **SOH-01**: SoH recalculates against measured capacity instead of rated when available
- [ ] **SOH-02**: SoH history entries are version-tagged with the capacity_ah_ref used
- [ ] **SOH-03**: SoH regression model ignores entries from different capacity baselines

### Reporting

- [ ] **RPT-01**: MOTD displays rated vs measured capacity and confidence percentage
- [ ] **RPT-02**: journald logs capacity estimation events (new measurement, confidence change, baseline lock)
- [ ] **RPT-03**: Daemon exposes capacity metrics for Grafana scraping

### Validation

- [x] **VAL-01**: Discharge quality filter rejects micro-discharges (< 5 min or < 5% ΔSoC)
- [x] **VAL-02**: Peukert exponent is fixed at 1.2 during capacity estimation phase

## Future Requirements

### Peukert Refinement (v2.1+)

- **CAL2-02**: Auto-calibrate Peukert exponent after capacity is locked (requires stable measured capacity as reference)

### Advanced Estimation (v3.0+)

- **CAP-06**: Voltage sensor drift detection and compensation over time
- **CAP-07**: Cross-validation between load-based and voltage-curve capacity methods

## Out of Scope

| Feature | Reason |
|---------|--------|
| Temperature compensation | Indoor conditions — temperature variation negligible (±3°C year-round) |
| External temperature sensor integration | No hardware change justified for indoor UPS |
| Web UI for capacity tracking | Minimal approach: MOTD + journald + Grafana sufficient |
| Multi-UPS capacity tracking | Single UPS only (CyberPower UT850EG) |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CAP-01 | 12 | Pending |
| CAP-02 | 12 | Pending |
| CAP-03 | 12 | Pending |
| CAP-04 | 12 | Pending |
| CAP-05 | 12 | Pending |
| SOH-01 | 13 | Pending |
| SOH-02 | 13 | Pending |
| SOH-03 | 13 | Pending |
| RPT-01 | 14 | Pending |
| RPT-02 | 14 | Pending |
| RPT-03 | 14 | Pending |
| VAL-01 | 12 | Complete |
| VAL-02 | 12 | Complete |

**Coverage:**
- v2.0 requirements: 13 total
- Mapped to phases: 13
- Coverage: 100% ✓

---

*Requirements defined: 2026-03-15*
*Traceability updated: 2026-03-15 after roadmap creation*
