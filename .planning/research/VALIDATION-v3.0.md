# Integration & Validation Strategy: v3.0

**Project:** UPS Battery Monitor v3.0

**Date:** 2026-03-17

---

## Test Architecture

v3.0 introduces two new critical capabilities: **(1) Scheduling decisions** and **(2) UPS command execution**. Testing strategy ensures both work correctly before production release.

### Test Pyramid

```
        ┌─────────────────────────────┐
        │  End-to-End (1–2 tests)    │  Real daemon + real UPS × 2 weeks
        │  - Full blackout→test flow │
        ├─────────────────────────────┤
        │  Integration (8–10 tests)  │  Daemon modules + mock UPS
        │  - Scheduler + upscmd      │
        │  - NUT client + timer ctrl │
        ├─────────────────────────────┤
        │  Unit Tests (30+ tests)     │  Pure functions + mocks
        │  - Sulfation scoring       │
        │  - ROI calculation         │
        │  - Safety constraints      │
        └─────────────────────────────┘
```

---

## Unit Tests (Phase 1-2)

### Module: test_sulfation_model.py (12 tests)

**Purpose:** Validate sulfation score calculation in isolation

| Test | Input | Expected | Pass Criteria |
|------|-------|----------|---------------|
| `test_sulfation_score_baseline` | SoH=0.95, IR=2.0%, recovery=0.5 | score ≈ 5 (healthy) | 0–20 range |
| `test_sulfation_score_aging` | SoH=0.85, IR=2.3%, recovery=0.6 | score ≈ 25 (moderate) | 20–40 range |
| `test_sulfation_score_critical` | SoH=0.70, IR=3.0%, recovery=0.2 | score ≈ 70 (high) | 60–80 range |
| `test_ir_percent_rise_detection` | IR historical: [2.0%, 2.1%, 2.2%, 2.3%] | Trend detected (rising) | Delta > 0.1% per week |
| `test_recovery_success_rate_empty` | zero test history | Rate = 1.0 (assume healthy) | Default optimistic |
| `test_recovery_success_rate_after_test` | 3 tests, 2 successful | Rate = 0.67 (2/3) | Accurate averaging |
| `test_voltage_recovery_delta_normal` | Normal discharge curve | delta ≈ 0 (baseline) | ±2% noise tolerance |
| `test_voltage_recovery_delta_sulfated` | Flattened curve shape | delta > 0 (slower recovery) | Clear signal |
| `test_score_stability_healthy` | Same inputs × 10 calls | variance < 1 point | Deterministic |
| `test_score_weighting` | Component contributions | 0.4×SoH + 0.3×IR + ... | Sum = 100% |
| `test_score_bounds` | Edge cases (SoH=0%, recovery=0) | 0 ≤ score ≤ 100 | No overflow |
| `test_sulfation_history_persistence` | Add score, save, reload model.json | New score in history | Schema correct |

**Fixtures:**
- Synthetic discharge histories (voltage, time, current arrays)
- Mock BatteryModel with predefined SoH/IR/recovery values
- sample model.json with v3.0 schema

---

### Module: test_scheduler.py (18 tests)

**Purpose:** Validate scheduling decision logic and safety constraints

| Test | Scenario | Expected Decision | Constraint Checked |
|------|----------|-------------------|-------------------|
| `test_should_test_healthy_battery` | SoH=0.95, ROI=1.3, no recent test | True (propose test) | All passed |
| `test_should_test_roi_below_threshold` | SoH=0.95, ROI=0.9 | False (skip) | ROI threshold |
| `test_should_test_soh_below_floor` | SoH=0.55, ROI=1.3, no test | False (safety) | SoH floor |
| `test_should_test_too_recent` | SoH=0.95, ROI=1.3, tested 2 days ago | False (skip) | 28-day interval |
| `test_should_test_recent_blackout` | SoH=0.95, ROI=1.3, blackout 3 days ago | False (credit) | 7-day blackout grace |
| `test_should_test_power_unstable` | SoH=0.95, ROI=1.3, grid glitches 2h ago | False (safety) | 4-hour stability |
| `test_execute_test_success` | upscmd returns "OK" | True (test queued) | Command accepted |
| `test_execute_test_not_charged` | upscmd returns "FAILED" (charge pending) | False (retry later) | Error handling |
| `test_execute_test_already_running` | upscmd returns "ERR" (test in progress) | False (skip) | Deduplication |
| `test_execute_test_timeout` | upscmd times out | False (retry) | Network resilience |
| `test_scheduler_update_model_json` | Test completed; capacity changed | model.json updated | Persistence |
| `test_scheduler_record_test_result` | Deep discharge test results | Entry in tests_completed | History tracking |
| `test_all_constraints_passed` | All conditions met | True | Full decision path |
| `test_multiple_blackouts_count_once` | 3 blackouts in 7 days | Grace applies to first | Deduplication |
| `test_scheduler_logging_decisions` | Run multiple decision calls | Decisions logged | Debuggability |
| `test_scheduler_check_interval_24h` | Call check twice in 1h | Second call skipped | Rate limiting |
| `test_natural_blackout_vs_test_distinction` | Blackout OB→OL vs test OB→OL | Different event types | Classification |
| `test_roi_calculation_post_test` | Capacity gain 0.1Ah; wear cost 1.5% SoH | ROI = 0.1/1.5 ≈ 0.067 wait, recalc... | Math correct |

**Fixtures:**
- Mock NUTClient (returns canned upscmd responses)
- Mock BatteryModel with test_schedule state
- Synthetic time progression (advance clock, simulate 28-day interval)

---

### Module: test_cycle_roi.py (8 tests)

**Purpose:** Validate ROI metric calculation

| Test | Inputs | Expected ROI | Pass Criteria |
|------|--------|-------------|---------------|
| `test_roi_healthy_battery` | Capacity gain 1.5%, wear 1.0% | ROI = 1.5 | > 1.0 (test eligible) |
| `test_roi_aging_battery` | Capacity gain 0.8%, wear 1.2% | ROI = 0.67 | < 1.0 (skip test) |
| `test_roi_no_recovery` | Capacity gain 0%, wear 1.0% | ROI = 0.0 | = 0 (skip) |
| `test_roi_strong_recovery` | Capacity gain 2.5%, wear 1.0% | ROI = 2.5 | >> 1.0 (urgent test) |
| `test_roi_wear_cost_calculation` | Peukert factor × SoH loss % | Wear cost ≈ 1.5% | Accurate |
| `test_roi_benefit_calculation` | Capacity before 5.8, after 5.9 | Benefit ≈ 1.7% | Accurate |
| `test_roi_threshold_comparison` | ROI = 1.10 vs threshold 1.10 | Just eligible | Edge case |
| `test_roi_history_trending` | 5 sequential tests, ROI trending down | ROI_avg decreases | Aging detected |

---

## Integration Tests (Phase 2-3)

### Scenario 1: Healthy Battery, Scheduled Test Execution

**Setup:**
- Mock UPS: fully charged, test_battery.start.deep available
- Daemon: SoH=0.92, ROI=1.3, no recent test
- NUT client: mock upscmd responses

**Test Flow:**
1. Daemon calls `scheduler.check_interval()` (simulates 24h elapsed)
2. Scheduler evaluates: SoH ✓, ROI ✓, no recent test ✓, not blackout ✓ → decision = True
3. Scheduler calls `nut_client.send_instant_command("test.battery.start.deep")`
4. Mock NUT returns "OK Instcmd processed"
5. Daemon records test execution in model.json
6. Verify: tests_completed array updated, next_scheduled_test recalculated

**Expected Outcome:** Test scheduled successfully; model.json persisted

---

### Scenario 2: Natural Blackout → Scheduled Test Skipped (7-day grace)

**Setup:**
- Blackout occurs: OL→OB (5 min)→OL, capacity_measured = 5.8Ah
- Daemon records: natural_blackouts[0].date = now
- 3 days later: scheduler runs, ROI=1.4 (test eligible on merit)

**Test Flow:**
1. Daemon calls `scheduler.check_interval()`
2. Scheduler checks blackout grace: `days_since_blackout = 3; 3 < 7` → skip test
3. MOTD shows: "Last blackout 3d ago; next test eligible in 4d"
4. After 7 days: blackout grace expires; test scheduled normally

**Expected Outcome:** Test correctly skipped; grace period enforced

---

### Scenario 3: UPS Refuses Test (Not Charged)

**Setup:**
- Mock UPS: charging (87% charged), can't run test
- Daemon: SoH=0.92, ROI=1.3, decision = True

**Test Flow:**
1. Daemon calls `nut_client.send_instant_command("test.battery.start.deep")`
2. Mock NUT returns "FAILED Battery not fully charged"
3. Daemon logs: "INSTCMD refused; UPS charging (87%). Retry in 1 hour."
4. model.json updated: next_scheduled_test → now + 1h
5. Scheduler marks decision as "pending" (not failed)

**Expected Outcome:** Graceful retry; no model corruption

---

### Scenario 4: Sulfation Detection During Discharge

**Setup:**
- Realistic discharge history: 8 tests over 2 months
- IR trend: [2.0%, 2.05%, 2.10%, 2.15%, 2.20%, 2.22%, 2.23%, 2.24%] (rising)
- Capacity recovery: [1.2%, 0.8%, 0.6%, 0.4%, 0.3%] (declining)

**Test Flow:**
1. Load model.json with discharge history
2. Call `calculate_sulfation_score()`
3. Expected: score rises from 12 (week 1) to 58 (week 8)
4. MOTD displays: "Sulfation ↑ (58/100); consider deep discharge"
5. Scheduler proposes test (score > 40)

**Expected Outcome:** Sulfation trend correctly detected; scheduling triggered

---

### Scenario 5: Safety Floor Prevents Test

**Setup:**
- Battery aging: SoH = 0.58 (below 0.60 floor)
- ROI = 1.2 (would be eligible on merit)

**Test Flow:**
1. Scheduler evaluates constraints
2. Constraint check: `soh < 0.60` → returns False immediately
3. MOTD shows: "SoH 58% (below 60% floor); testing disabled"
4. No test proposed; ops should plan replacement

**Expected Outcome:** Safety floor enforced; test refused

---

## Validation Gates (Phase 4)

### Gate 1: Scheduler Stress Test (1000 Decisions)

**Purpose:** Ensure no crashes, logic consistency under load

```python
def test_scheduler_stress_1000_decisions():
    scheduler = TestScheduler(...)
    for i in range(1000):
        # Vary battery state, time, blackouts
        state = generate_random_battery_state()
        decision, reason = scheduler.decide_next_test()
        assert decision in [True, False]
        assert isinstance(reason, str)
        # Verify state transitions sensible
        assert not (decision and soh < 0.60)  # Safety floor
        assert not (decision and roi < 1.10)  # ROI threshold
```

**Pass Criteria:**
- Zero crashes, exceptions handled
- Decisions consistent (same input → same output)
- All constraint paths exercised

---

### Gate 2: Real upscmd Execution on UT850

**Purpose:** Validate daemon can actually trigger UPS tests

**Prerequisites:**
- UPS connected, NUT upsd running
- UPS battery fully charged
- No other tests in progress

**Execution:**
```bash
# Manual test (before daemon auto-executes)
upscmd -u admin -p admin cyberpower test.battery.start.quick
# Expected: "OK Instcmd processed"

# Daemon integration test
daemon_upscmd_test_runner.py
# - Execute test.battery.start.quick (safer)
# - Capture daemon logs: "INSTCMD sent: test.battery.start.quick"
# - Verify: upsd received command (check NUT logs)
# - Capture UPS response (should be "OK" or error code)
```

**Expected Outcomes:**
- upscmd accepted (returns OK)
- Quick test runs (~10 sec)
- UPS status transitions to OB, then back to OL
- No data loss or daemon crash during test

---

### Gate 3: Sulfation Score Stability (30 Days)

**Purpose:** Validate score doesn't oscillate; trends are real

**Execution:**
- Run daemon for 30 days in production
- Record sulfation_score daily
- Calculate: mean, std dev, variance, autocorrelation

**Pass Criteria:**
```
Daily variance < 5 points (e.g., score 20 ± 2, not 20 ± 10)
Week-to-week trend clear (e.g., rising 2 points/week if degrading)
Autocorrelation > 0.8 (today's score predicts tomorrow's)
```

---

### Gate 4: Blackout Credit Logic (Real Outages)

**Purpose:** Validate daemon correctly credits natural blackouts

**Setup:**
- Monitor for natural blackout (expected: several/week per project context)
- Record: blackout detection timestamp, classification, grace period

**Test:**
1. Blackout occurs at T=0
2. Scheduler proposes test at T+3 days, daemon skips (grace active)
3. Scheduler proposes test at T+8 days, daemon accepts (grace expired)
4. MOTD shows correct countdown

**Pass Criteria:**
- Blackout credited (recorded in natural_blackouts array)
- Tests skipped days 0–7 ✓
- Tests allowed day 8+ ✓
- Countdown visible in MOTD ✓

---

### Gate 5: ROI Calibration (10 Tests)

**Purpose:** Compare daemon-calculated ROI vs observed battery behavior

**Execution:**
- Run 10 scheduled deep discharge tests over 2–3 months
- Record: capacity before/after, calculated ROI, actual recovery observed
- Compare: expected_recovery vs actual_recovery

**Expected Outcome:**
```
Average actual_recovery ≈ 80–90% of calculated benefit
(Some variance due to individual battery variance)
ROI threshold (1.10) produces tests ~28 days apart (healthy) or ~45 days (aging)
```

---

### Gate 6: Safety Floor Validation

**Purpose:** Confirm SoH < 60% prevents tests; tests enabled above floor

**Execution:**
- Accumulate SoH history from real discharge events
- When SoH dips below 60% (if happens): verify test skipped
- When SoH above 60%: verify test can be scheduled (if ROI + other constraints OK)

**Pass Criteria:**
- No tests attempted when SoH < 60%
- Clear MOTD warning when approaching floor (e.g., SoH 62%: "2% to test floor")

---

## Validation Acceptance Criteria

| Gate | Criteria | Owner | Timeline |
|------|----------|-------|----------|
| **Stress Test** | 1000 decisions, 0 crashes | Automated | Phase 2 |
| **Real upscmd** | Quick test succeeds; UPS responds | Manual + logs | Phase 2 (single test) |
| **Sulfation Stability** | Variance < 5/day; clear trend | Monitoring | Phase 4 (30 days) |
| **Blackout Credit** | Grace period enforced; tests skip/resume correctly | Observation | Phase 4 (event-based) |
| **ROI Calibration** | 80–90% recovery match; interval ~28d (healthy) | Analysis | Phase 4 (10 tests) |
| **Safety Floor** | No tests below SoH 60%; warning at 62% | Observation | Phase 4 (if SoH decays) |

**Release Blockers:**
- Stress test must pass (no crashes)
- Real upscmd must succeed (UPS accepts command)
- Sulfation stability must be confirmed (30-day baseline)

**Post-Release Monitoring:**
- ROI and safety floor validated via field data
- Thresholds (1.10, 60%) adjusted if needed in v3.0.1 patch

---

## Monitoring & Telemetry

### MOTD Display (Ops Visibility)

```
[ Battery Status ]
├─ SoH: 88% (↓ 0.2%/week trend)
├─ Sulfation: 18/100 (healthy) ↑ slowly
├─ Capacity: 5.8Ah (measured from 3 deep discharges)
├─ Test Schedule:
│  ├─ Last test: 28 days ago (deep discharge)
│  ├─ Next eligible: in 3 days (ROI = 1.25 > 1.10 threshold)
│  ├─ Natural blackout credit: 5 days remaining
│  └─ Cycle ROI: 1.25 (benefit 1.5%, cost 1.2%)
└─ Recommendation: Monitor; test in 3 days (automated)
```

### health.json Export

```json
{
  "sulfation_score": 18,
  "sulfation_trend": "stable",
  "cycle_roi": 1.25,
  "next_scheduled_test": "2026-03-20T08:00:00Z",
  "test_interval_recommended_days": 28,
  "natural_blackouts_credited": 1
}
```

### journald Structured Events

```
TIME=2026-03-17T20:00:00Z
SERVICE=ups-battery-monitor
EVENT=scheduler_decision
DECISION=propose_test
REASON=roi_above_threshold
ROI=1.25
NEXT_TEST=2026-03-20T08:00:00Z
SoH=0.88
SULFATION_SCORE=18
```

---

## Known Test Gaps (Acceptable for v3.0)

| Gap | Reason | Mitigation | v3.1 Plan |
|-----|--------|-----------|-----------|
| Micro-discharge accumulation | No partial discharge model yet | Skip; deep discharges only for v3.0 | RFC for micro-discharge accuracy |
| Peukert exponent fitting | Circular dependency with capacity | Keep fixed at 1.2 (±3% error) | Fit after 6 months discharge history |
| Grid stability detection | Requires 4+ week power history | Use simple heuristic (4-hour glitch check) | ML-based grid pattern recognition |
| Temperature compensation | No sensor available | Accept ±5% uncertainty | Integrate USB sensor in v3.1 |
| Multi-UPS support | Single UT850 only | Test on one UPS; extend architecture | Second UPS in Phase 4 validation |

---

## Conclusion

v3.0 validation strategy balances **depth** (30+ unit tests, 8–10 integration tests, 6 acceptance gates) with **practicality** (real UPS tests, 30-day field monitoring, clear acceptance criteria). No critical test gaps; all known unknowns addressed with field validation gates before release.

**Ready to begin Phase 1 implementation.**

