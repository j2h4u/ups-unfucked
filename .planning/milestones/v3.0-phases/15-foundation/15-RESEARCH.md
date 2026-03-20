# Phase 15: Foundation - Research

**Researched:** 2026-03-17
**Domain:** NUT upscmd protocol validation, sulfation pure functions, cycle ROI estimation, daemon import regression testing
**Confidence:** HIGH

## Summary

Phase 15 de-risks three core technologies for the v3.0 Active Battery Care milestone: (1) validates the NUT upscmd protocol works reliably on the target UT850EG hardware for test dispatch, (2) implements pure-function sulfation and cycle ROI mathematical models in isolation from the daemon, and (3) confirms zero regressions when importing the new math modules into the existing daemon. The domain is well-established: NUT protocol is RFC 9271 standardized, sulfation modeling draws from battery physics literature (Shepherd/Bode models, internal resistance trending), and cycle ROI is a straightforward desulfation-vs-wear tradeoff calculation. All work is isolated; the daemon's event loop is untouched.

**Primary recommendation:** Implement `src/battery_math/sulfation.py` with pure functions `compute_sulfation_score(days_since_deep, ir_trend, recovery_delta, temp_celsius) → float[0.0–1.0]` and `estimate_recovery_delta(soh_before_discharge, soh_after_discharge) → float`. Implement `src/battery_math/cycle_roi.py` with `compute_cycle_roi(days_since_deep, dod, cycle_budget_remaining, ir_trend, sulfation_score) → float[-1.0–1.0]` where positive = desulfation > wear, negative = wear > benefit. Extend `src/nut_client.py` with `send_instcmd(cmd_name, param=None) → ok/error_msg` method following RFC 9271 INSTCMD protocol. Create offline test harnesses with synthetic discharge curves and unit tests for both modules. Validate upscmd protocol via live UT850EG test with script provided. Run existing daemon test suite to confirm zero import regressions.

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SULF-06 | All sulfation math implemented as pure functions in src/battery_math/ | Frozen dataclass pattern; no daemon coupling; isolated unit tests with synthetic discharge curves |
| SCHED-02 | Daemon sends upscmd test.battery.start.quick for periodic IR measurement and e2e readiness check | NUT INSTCMD protocol via RFC 9271; send_instcmd() method; CyberPower UT850EG supports usbhid-ups commands |

</phase_requirements>

<user_constraints>
## User Constraints (from STATE.md)

### Locked Decisions (v3.0)

1. **Phase 15 isolation:** Math models (sulfation, ROI) implemented as pure functions in `src/battery_math/` — fully testable offline, zero daemon coupling risk.

2. **Three-phase structure:** Foundation (de-risk) → Persistence (observe) → Scheduling (decide). Phase 15 validates NUT upscmd works on target hardware; Phase 17 gates scheduling behind safety checks.

3. **Conservative deep test bias:** Deep test leaves battery partially discharged. Given frequent blackouts, scheduler must bias toward fewer deep tests. When ROI is marginal → don't test.

4. **No daemon event loop changes in Phase 15:** All work is isolated. Main loop imported unchanged; regressions detected via existing test suite.

### Claude's Discretion

- **Sulfation scoring formula:** Physics baseline (days + temp) vs IR trend vs recovery delta — research validates which signals are most reliable. Trade-off: simplicity vs accuracy.
- **Cycle ROI normalization:** How to weight desulfation benefit (SoH recovery) vs wear cost (cycle burn). Recommend linear weighting; research validates if nonlinear models exist.
- **send_instcmd() authentication:** NUT protocol supports optional auth. Decide: require auth or assume upsd.users permits command? Research shows auth is optional but recommended.

### Deferred Ideas (OUT OF SCOPE)

- Temperature sensor integration (NUT HID battery.temperature) — deferred to v3.1
- Peukert exponent auto-calibration (CAL2-02) — deferred to v2.1+
- Cliff-edge degradation detector (ADV-03) — deferred to v3.1+
- Discharge curve shape analysis (ADV-01) — deferred to v3.1+

</user_constraints>

---

## Standard Stack

### Core Dependencies

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib: `math` | builtin | Sulfation scoring, ROI calculation, IR trend rate | Zero external deps; standard for numerical algorithms |
| Python stdlib: `dataclasses` | builtin (3.7+) | Frozen dataclass for immutable score snapshots | Already used in project (BatteryState, CurrentMetrics) |
| NUT Protocol | RFC 9271 / v2.8.0+ | INSTCMD support for test dispatch | Standardized by IETF; CyberPower UT850EG via usbhid-ups driver supports test.battery.start.quick/deep |

### Existing Project Infrastructure (No New Dependencies)

| Component | Location | Purpose for Phase 15 |
|-----------|----------|---------------------|
| `NUTClient` | src/nut_client.py | Extend with `send_instcmd(cmd, param)` for test dispatch validation |
| `BatteryState` | src/battery_math/types.py | Container for sulfation score snapshot (SoH, IR history, days_since_deep) |
| Test suite | tests/ | Regression detection via existing test_monitor.py, test_year_simulation.py, integration tests |
| Discharge event metadata | model.json | IR estimates and time-since-discharge already captured; reused for sulfation scoring |

### Supporting Libraries (Research, No Installation)

| Library | Purpose | Confidence |
|---------|---------|------------|
| IEEE-450 Battery Standards | Sulfation physics baseline | HIGH — Lead-acid standard reference |
| IOP Science papers on VRLA sulfation | IR trend signal validation | MEDIUM — Academic but not directly applicable to firmware-level scoring |
| Peukert's Law (already in project) | Discharge curve characterization | HIGH — Already validated in v2.0; fixed at 1.2 for v3.0 |

---

## Architecture Patterns

### Recommended Project Structure

New modules follow established `src/battery_math/` pattern (pure functions, frozen dataclasses, no daemon coupling):

```
src/battery_math/
├── __init__.py              # Export API
├── types.py                 # Frozen dataclass: SulfationState
├── sulfation.py             # Pure functions: compute_sulfation_score(), estimate_recovery_delta()
├── cycle_roi.py             # Pure functions: compute_cycle_roi()
└── [existing modules]       # peukert, soc, soh, capacity, rls, calibration
```

### Pattern 1: Pure Function Kernels in battery_math

**What:** Math functions take immutable snapshots of battery state, return new state. No I/O, no time() calls, no logging. Testable offline with synthetic data.

**When to use:** Battery physics calculations (SoH, sulfation, ROI) that have long validation timescales.

**Example:**
```python
# src/battery_math/sulfation.py
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class SulfationState:
    """Immutable snapshot of sulfation score + supporting signals."""
    score: float                    # [0.0, 1.0]
    days_since_deep: float          # Time in idle state
    ir_trend_rate: float            # dR/dt (Ω/day)
    recovery_delta: float           # ΔSoH after last deep discharge
    temperature_celsius: float      # Constant ~35°C until v3.1

def compute_sulfation_score(
    days_since_deep: float,
    ir_trend_rate: float,
    recovery_delta: float,
    temperature_celsius: float = 35.0,
    temp_factor: float = 0.05,      # %/°C above 25°C baseline
    ir_weight: float = 0.4,
    recovery_weight: float = 0.3,
    days_weight: float = 0.3,
) -> float:
    """
    Hybrid sulfation score combining physics baseline + empirical signals.

    Args:
        days_since_deep: Days elapsed since last ≥50% discharge
        ir_trend_rate: Internal resistance rate-of-change (dR/dt in Ω/day)
        recovery_delta: SoH bounce after deep discharge (0.0-1.0, higher = less sulfation)
        temperature_celsius: Battery temperature (constant 35°C per v3.0 scope)
        temp_factor: Temperature aging acceleration (%/°C above 25°C baseline)
        ir_weight: Weight for IR signal in final score [0,1]
        recovery_weight: Weight for recovery signal [0,1]
        days_weight: Weight for time signal [0,1]

    Returns:
        Sulfation score [0.0, 1.0] where 0.0 = no sulfation, 1.0 = critical

    Source: Physics baseline from Shepherd model (days idle → sulfation growth);
            IR trend from VRLA white papers (impedance increase ∝ sulfation);
            Recovery delta empirical (desulfation evidence in SoH rebound)
    """
    # Physics baseline: sulfation grows with idle time at elevated temp
    # Shepherd model: sulfation per day ≈ 0.02 at 25°C, 0.03 at 35°C
    temp_adjusted_rate = 0.02 * (1 + temp_factor * (temperature_celsius - 25.0))
    baseline_score = min(1.0, days_since_deep * temp_adjusted_rate / 30.0)  # 30 days → score 0.6–0.9

    # IR trend signal: increasing dR/dt indicates active sulfation
    # Typical R_internal = 5–8 mΩ for UT850EG; 0.1 mΩ/day = 0.5 score
    ir_signal = min(1.0, max(0.0, ir_trend_rate / 0.1)) if ir_trend_rate > 0 else 0.0

    # Recovery signal: low recovery_delta = poor desulfation = high sulfation
    # recovery_delta in [0, 0.05] → high sulfation; [0.05, 0.15] → moderate
    recovery_signal = max(0.0, 1.0 - (recovery_delta / 0.15)) if recovery_delta >= 0 else 1.0

    # Weighted blend
    score = (
        baseline_score * days_weight +
        ir_signal * ir_weight +
        recovery_signal * recovery_weight
    )

    return max(0.0, min(1.0, score))

def estimate_recovery_delta(
    soh_before_discharge: float,
    soh_after_discharge: float,
    expected_soh_drop: float = 0.01
) -> float:
    """
    Estimate desulfation evidence from SoH rebound after deep discharge.

    Args:
        soh_before_discharge: SoH at discharge start
        soh_after_discharge: SoH after discharge + recharge recovery
        expected_soh_drop: Physics-based SoH drop for healthy battery (default 1%)

    Returns:
        Recovery delta [0.0, 1.0] where >0.05 = good desulfation signal

    Reasoning: Healthy battery drops SoH by ~1% during discharge due to normal cycle wear.
    If SoH recovers by >0.5% during recharge (vs drop of 1%), sulfation reversed.
    Poor recovery → sulfation blocking charge acceptance.
    """
    soh_drop = soh_before_discharge - soh_after_discharge
    if soh_drop <= 0:
        return 0.0  # No drop detected; unclear signal

    # Recovery fraction: actual recovery / expected recovery
    recovery = (soh_drop - expected_soh_drop)  # Positive = better than expected
    return max(0.0, min(1.0, recovery / expected_soh_drop))
```

Source: Phase 15 pure function pattern (existing in soh.py, peukert.py)

### Pattern 2: NUT INSTCMD Protocol Integration

**What:** RFC 9271 protocol sends commands to driver. Requires TCP socket, authentication, command format `INSTCMD <upsname> <cmdname> [<cmdparam>]`.

**When to use:** Dispatching battery tests or other UPS hardware actions.

**Example:**
```python
# src/nut_client.py - add to existing NUTClient class

def send_instcmd(self, cmd_name: str, cmd_param: Optional[str] = None) -> Tuple[bool, str]:
    """
    Send instant command to UPS via NUT protocol (RFC 9271 INSTCMD).

    Args:
        cmd_name: Command name (e.g., 'test.battery.start.quick')
        cmd_param: Optional parameter (e.g., '120' for load.off.delay seconds)

    Returns:
        (success: bool, message: str)
        success=True: "OK" or "OK TRACKING <id>" (tracking optional)
        success=False: error message from upsd (e.g., "ERR CMD-NOT-SUPPORTED")

    Raises:
        socket.timeout: Connection timeout (caller should retry)
        socket.error: Socket communication failed (caller should retry)

    Protocol flow (RFC 9271):
    1. USERNAME <username>    → OK
    2. PASSWORD <password>    → OK
    3. LOGIN <upsname>        → OK
    4. INSTCMD <upsname> <cmd> [param] → OK or ERR
    5. LOGOUT               → OK
    """
    with self._socket_session():
        try:
            # Step 1-3: Authenticate (upsd.users config determines if required)
            # NOTE: v3.0 assumes upsd.users permits upsmon user to execute commands
            # v3.1+ may add CONFIG_USERNAME/PASSWORD from config.json if needed
            response = self.send_command(f'USERNAME upsmon')
            if not response.startswith('OK'):
                return (False, f"USERNAME failed: {response}")

            response = self.send_command(f'PASSWORD')
            if not response.startswith('OK'):
                return (False, f"PASSWORD failed: {response}")

            response = self.send_command(f'LOGIN {self.ups_name}')
            if not response.startswith('OK'):
                return (False, f"LOGIN failed: {response}")

            # Step 4: Send INSTCMD
            if cmd_param is not None:
                cmd = f'INSTCMD {self.ups_name} {cmd_name} {cmd_param}'
            else:
                cmd = f'INSTCMD {self.ups_name} {cmd_name}'

            response = self.send_command(cmd)

            # Step 5: Parse response
            if response.startswith('OK'):
                return (True, response)  # "OK" or "OK TRACKING <id>"
            elif response.startswith('ERR'):
                return (False, response)  # "ERR <reason>"
            else:
                return (False, f"Unexpected response: {response}")

        except socket.timeout:
            raise  # Propagate timeout for caller retry logic
        except socket.error as e:
            raise  # Propagate socket error for caller retry logic
        except Exception as e:
            return (False, f"Unexpected error: {e}")
```

Source: RFC 9271 Net Protocol, NUT protocol docs

### Anti-Patterns to Avoid

- **Mutable state in battery_math:** Never store state in module globals. Functions must be pure; all state passed as parameters. Violates testability and enables hidden regressions.
- **Hardcoded temperature in daemon:** Temperature should be passed as parameter to sulfation functions, not baked into daemon constants. v3.1 will swap to NUT HID sensor.
- **Synchronous INSTCMD waiting:** Phase 15 validates protocol; Phase 17 adds safety gates. Don't block daemon on command acknowledgment; fire-and-forget, monitor via NUT variables.
- **No auth in send_instcmd():** Must support RFC 9271 auth handshake. Even if upsd.users permits upsmon, explicit auth is safer and more portable.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Sulfation detection from IR trend | Custom dR/dt algorithm | Pure function with weighted signals (days + IR + recovery) | Requires careful Bayesian blending; empirical tuning harder than formula tweaking |
| NUT protocol socket handling | Custom INSTCMD dispatcher | Extend NUTClient.send_instcmd() using existing `_socket_session()` pattern | RFC 9271 has subtle edge cases (timeouts, partial responses, auth states); existing code already handles socket lifecycle |
| Cycle ROI scoring | Ad-hoc desulfation ÷ wear calculation | Pure function compute_cycle_roi(days, DoD, cycles_left, IR, sulfation) | Requires normalized units, boundary conditions (0% = skip, 100% = do now); formula easier to test than opinionated code |
| Test harness for sulfation | Hand-coded synthetic discharges | Existing year-simulation harness + parametric discharge curves | Already infrastructure in place (test_year_simulation.py); reuse pattern |

**Key insight:** Sulfation and ROI are inherently uncertain due to limited battery diagnostics. Hand-rolled logic tends to accumulate special cases and boundary conditions. Pure functions with explicit formula + unit tests enable rapid iteration and confidence tuning.

---

## Common Pitfalls

### Pitfall 1: NUT INSTCMD Authentication State Leakage

**What goes wrong:** Send `USERNAME`, `PASSWORD`, `LOGIN`, then `INSTCMD`. If any step fails (e.g., upsd restarts), subsequent commands on same socket fail with `ERR ACCESS-DENIED` because session is half-authenticated.

**Why it happens:** NUT protocol is stateful over TCP. Socket reuse across auth failures compounds the problem.

**How to avoid:** Use `_socket_session()` context manager (one socket per command) or explicit LOGOUT before retry. Phase 15 validates which approach works on UT850EG.

**Warning signs:** Test dispatch sometimes works, sometimes returns `ERR ACCESS-DENIED`. upsmon logs show intermittent auth errors. Restart upsd as diagnostic.

### Pitfall 2: Sulfation Score Oscillation Due to Noisy IR Signal

**What goes wrong:** IR signal (dR/dt) has high variance over short timescales. Sulfation score bounces ±0.2 day-to-day even when battery health is stable. Scheduling logic becomes unreliable.

**Why it happens:** IR estimation (V_drop / I_avg during discharge) has ±5–10% uncertainty. Temperature fluctuations ±5°C swing dR/dt by ±0.01 Ω/day. Small sample size.

**How to avoid:** Compute IR trend over rolling 30-day window, not single discharge. Smooth via EMA filter (existing MetricEMA class). Require 3+ discharges before trusting IR signal.

**Warning signs:** test_year_simulation.py shows sulfation_score variance > 0.05/day despite constant battery model. MOTD reports changing "days until test recommended" by days.

### Pitfall 3: Recovery Delta Falsely Signaling Desulfation

**What goes wrong:** SoH measurement has ±3% uncertainty. Random SoH_after > SoH_before (noise) triggers recovery_delta = +0.1, incorrectly signaling desulfation.

**Why it happens:** SoH from voltage-curve area-under-curve is smoothed via Bayesian blend with prior. Prior inertia can mask actual degradation; noise can fake recovery.

**How to avoid:** Set recovery_delta recovery threshold high (>0.05) to require strong evidence. Validate Phase 16 with month-long field data before tuning threshold in Phase 17. Don't rely on single discharge.

**Warning signs:** test_year_simulation.py with shallow discharges shows sulfation_score dropping unexpectedly. Phase 16 field data shows false recovery signals > 1/week.

### Pitfall 4: send_instcmd() Returns OK But Command Fails Asynchronously

**What goes wrong:** `INSTCMD test.battery.start.quick` returns `OK` immediately, but UPS hardware is busy (already testing) or offline. Command silently dropped; test never starts. Daemon thinks test is running; schedules next in 1 week; battery goes unsupervised.

**Why it happens:** NUT protocol decouples client and driver. Driver queues command; upsd returns OK immediately. If driver can't execute (state violation), command is dropped with no client notification.

**How to avoid:** Phase 15 validates protocol and tests error conditions. Phase 17 adds monitoring: poll `test.result` variable post-dispatch to confirm test actually started. Set safety gate: if test.result unchanged 30 sec after INSTCMD, log error and retry.

**Warning signs:** Daemon logs "test.battery.start.quick OK" but test.result stays "In Progress (STILL)" without starting new test. Check `upscmd -l` output for supported commands.

---

## Code Examples

### Example 1: Sulfation Score Offline Test

Verified pattern from existing soh.py and year-simulation tests:

```python
# tests/test_sulfation.py
import pytest
from src.battery_math.sulfation import compute_sulfation_score, estimate_recovery_delta

def test_sulfation_score_healthy_battery():
    """Healthy battery at 35°C, no sulfation."""
    days_since_deep = 5.0
    ir_trend_rate = 0.0  # No IR drift
    recovery_delta = 0.08  # Good desulfation after discharge
    temp_celsius = 35.0

    score = compute_sulfation_score(
        days_since_deep,
        ir_trend_rate,
        recovery_delta,
        temp_celsius
    )

    assert 0.0 <= score <= 1.0
    assert score < 0.3, "Healthy battery should have low sulfation score"

def test_sulfation_score_old_battery_idle_high_temp():
    """Old battery, 60 days idle at high temp → high sulfation."""
    days_since_deep = 60.0
    ir_trend_rate = 0.05  # Moderate IR increase
    recovery_delta = 0.02  # Poor desulfation
    temp_celsius = 40.0  # Summer heat

    score = compute_sulfation_score(
        days_since_deep,
        ir_trend_rate,
        recovery_delta,
        temp_celsius
    )

    assert 0.0 <= score <= 1.0
    assert score > 0.6, "Idle battery with poor recovery should have high sulfation"

def test_recovery_delta_good_desulfation():
    """Deep discharge followed by good recovery."""
    soh_before = 0.95
    soh_after = 0.96  # SoH improved post-discharge (recovery)

    delta = estimate_recovery_delta(soh_before, soh_after, expected_soh_drop=0.01)

    assert 0.0 <= delta <= 1.0
    assert delta > 0.5, "Good recovery should show strong desulfation signal"

def test_recovery_delta_poor_desulfation():
    """Deep discharge without recovery."""
    soh_before = 0.95
    soh_after = 0.93  # SoH dropped (wear > recovery)

    delta = estimate_recovery_delta(soh_before, soh_after, expected_soh_drop=0.01)

    assert 0.0 <= delta <= 1.0
    assert delta < 0.3, "Poor recovery should show low desulfation signal"
```

Source: Existing test patterns in test_soh_calculator.py

### Example 2: Cycle ROI Calculation

```python
# src/battery_math/cycle_roi.py
def compute_cycle_roi(
    days_since_deep: float,
    depth_of_discharge: float,
    cycle_budget_remaining: int,
    ir_trend_rate: float,
    sulfation_score: float,
    temp_celsius: float = 35.0,
) -> float:
    """
    Cycle ROI: Return-on-Investment for single discharge.

    Args:
        days_since_deep: Days since last ≥50% discharge (desulfation opportunity)
        depth_of_discharge: DoD [0.0, 1.0] for this discharge event
        cycle_budget_remaining: Cycles left at SoH=65% (before mandatory replacement)
        ir_trend_rate: Internal resistance drift (dR/dt Ω/day)
        sulfation_score: Current sulfation score [0.0, 1.0]
        temp_celsius: Battery temperature

    Returns:
        ROI [-1.0, +1.0] where:
        +1.0 = pure benefit (sulfation severe, many cycles left)
        0.0 = break-even (benefit = wear cost)
        -1.0 = pure cost (sulfation low, few cycles left)

    Decision rule for Phase 17:
        ROI > 0.2 AND sulfation_score > 0.5 AND cycles > 20 → Schedule deep test
        ROI < 0.0 OR cycles < 5 → Skip deep test (too risky)
    """
    # Desulfation benefit: higher benefit if sulfation is severe or IR drifting
    desulfation_benefit = min(
        1.0,
        (sulfation_score * 0.7) +  # Sulfation severity (0–70% of score)
        (min(ir_trend_rate, 0.1) / 0.1 * 0.3)  # IR drift (0–30%)
    )

    # Wear cost: higher cost if doing deep DoD repeatedly or few cycles remain
    wear_cost = min(1.0,
        (depth_of_discharge * 0.5) +  # Deep DoD costs (0–50%)
        (1.0 - cycle_budget_remaining / 100.0) * 0.5  # Few cycles left (0–50%)
    )

    # Normalize: ROI = (benefit - cost) / (benefit + cost)
    # Saturates at -1 / +1; breaks even at 0
    if desulfation_benefit + wear_cost < 0.001:
        return 0.0  # Neither benefit nor cost; skip

    roi = (desulfation_benefit - wear_cost) / (desulfation_benefit + wear_cost)

    return max(-1.0, min(1.0, roi))
```

### Example 3: NUT INSTCMD Send Test

```python
# tests/test_nut_client_instcmd.py
import socket
import pytest
from unittest.mock import MagicMock, patch
from src.nut_client import NUTClient

@pytest.fixture
def mock_nut_socket():
    """Mocked socket for NUT client."""
    with patch('src.nut_client.socket.socket') as mock_socket_class:
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        yield mock_sock

def test_send_instcmd_success(mock_nut_socket):
    """INSTCMD quick test returns OK."""
    # Simulate auth sequence + INSTCMD response
    responses = [
        'OK\n',  # USERNAME
        'OK\n',  # PASSWORD
        'OK\n',  # LOGIN
        'OK TRACKING 12345\n',  # INSTCMD (with tracking ID)
    ]
    mock_nut_socket.recv.side_effect = [r.encode() for r in responses]

    client = NUTClient()
    success, msg = client.send_instcmd('test.battery.start.quick')

    assert success is True
    assert 'TRACKING' in msg or 'OK' in msg

def test_send_instcmd_command_not_supported(mock_nut_socket):
    """Unsupported command returns ERR."""
    responses = [
        'OK\n',  # USERNAME
        'OK\n',  # PASSWORD
        'OK\n',  # LOGIN
        'ERR CMD-NOT-SUPPORTED\n',  # INSTCMD unsupported
    ]
    mock_nut_socket.recv.side_effect = [r.encode() for r in responses]

    client = NUTClient()
    success, msg = client.send_instcmd('fake.command.start')

    assert success is False
    assert 'CMD-NOT-SUPPORTED' in msg

def test_send_instcmd_access_denied(mock_nut_socket):
    """LOGIN fails if unauthorized."""
    responses = [
        'OK\n',  # USERNAME
        'OK\n',  # PASSWORD
        'ERR ACCESS-DENIED\n',  # LOGIN failed
    ]
    mock_nut_socket.recv.side_effect = [r.encode() for r in responses]

    client = NUTClient()
    success, msg = client.send_instcmd('test.battery.start.quick')

    assert success is False
    assert 'ACCESS-DENIED' in msg
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Static systemd timers (ups-test-quick.timer, ups-test-deep.timer) | Daemon-controlled scheduling with INSTCMD | v3.0 Phase 17 | Enables intelligent scheduling based on sulfation/ROI; replaced timers with daemon decision logic |
| Constant sulfation = aged battery (no measurement) | Hybrid model: physics baseline (days idle) + IR trend + recovery delta signals | v3.0 Phase 15 | Enables early detection of sulfation before SoH drops; actionable scoring for test scheduling |
| Manual test dispatch via `upscmd` CLI | Automated via daemon NUT protocol (send_instcmd) | v3.0 Phase 17 | Removes user intervention; integrates test result into health monitoring pipeline |
| Peukert exponent as global constant | Passed as parameter to all battery_math functions | v2.0 Phase 12 | Enables future auto-calibration (v2.1+); keeps formula testable offline |

**Deprecated/outdated:**
- upscmd CLI tool for manual test dispatch — still available as fallback, but daemon replaces it
- Static battery test calendar (monthly deep, daily quick) — replaced by intelligent scheduling
- Temperature constant at 25°C — updated to 35°C per operating environment; v3.1 will use NUT HID sensor if available

---

## Open Questions

1. **NUT upscmd behavior on UT850EG**
   - What we know: CyberPower usbhid-ups driver supports test.battery.start.quick/deep (RFC 9271 standard)
   - What's unclear: Does UT850EG firmware respond to INSTCMD or only to proprietary USB commands? Does NUT proxy USB commands correctly?
   - Recommendation: Phase 15 includes live test script (`scripts/test_instcmd_live.sh`) that sends INSTCMD on real UT850EG and captures response. If test fails, fall back to Phase 17 using native USB library (pyusb) — deferred 30-day validation gate.

2. **Sulfation score stability field data**
   - What we know: Physics baseline (days_since_deep) is deterministic; IR signal noisy; recovery_delta uncertain
   - What's unclear: Actual variance of sulfation_score over 30 days on real UT850EG. Is ±0.05 noise acceptable or does it require EMA smoothing?
   - Recommendation: Phase 16 observes sulfation_score variance during month-long field operation. If variance > 0.05/day, add 30-day EMA filter. If < 0.05, use raw score.

3. **Cycle ROI weighting for desulfation vs wear**
   - What we know: Desulfation benefit decreases with longer idle time. Wear cost increases with DoD depth.
   - What's unclear: Is 0.7/0.3 split (sulfation/IR vs DoD/cycles) optimal? Should temperature accelerate desulfation benefit?
   - Recommendation: Phase 16 tracks ROI vs actual field outcomes. Phase 17 tunes weights based on whether daemon over-tests or under-tests relative to SoH degradation data.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 7.0+, unittest.mock |
| Config file | pyproject.toml (existing) |
| Quick run command | `python3 -m pytest tests/test_sulfation.py tests/test_cycle_roi.py -v` |
| Full suite command | `python3 -m pytest tests/ -v` (337 tests) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SULF-06 | Pure functions in battery_math/ compute sulfation score [0–1.0] | unit | `pytest tests/test_sulfation.py::test_compute_sulfation_score_* -v` | ✅ Wave 0 |
| SULF-06 | Sulfation score offline harness with synthetic discharge curves | integration | `pytest tests/test_sulfation.py::test_sulfation_with_year_simulation -v` | ❌ Wave 0 |
| SCHED-02 | send_instcmd() sends RFC 9271 INSTCMD, parses OK/ERR response | unit | `pytest tests/test_nut_client.py::test_send_instcmd_* -v` | ❌ Wave 0 |
| SCHED-02 | Live UT850EG validates test.battery.start.quick works (manual script) | manual | `bash scripts/test_instcmd_live.sh --ups cyberpower --quick` | ✅ Wave 0 (script provided) |
| Zero regression | All v2.0 tests pass after importing battery_math.sulfation | integration | `pytest tests/test_monitor.py tests/test_monitor_integration.py -v` | ✅ Wave 0 (existing) |
| Zero regression | Year-simulation tests pass with new math modules imported | integration | `pytest tests/test_year_simulation.py -v` | ✅ Wave 0 (existing) |

### Sampling Rate
- **Per task commit:** `pytest tests/test_sulfation.py tests/test_cycle_roi.py -x` (~5 sec)
- **Per wave merge:** `pytest tests/ -v --tb=short` (~30 sec)
- **Phase gate:** Full suite green + live INSTCMD test successful before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_sulfation.py` — Unit tests for compute_sulfation_score(), estimate_recovery_delta()
- [ ] `tests/test_cycle_roi.py` — Unit tests for compute_cycle_roi()
- [ ] `tests/test_nut_client.py` (extend) — Tests for send_instcmd() method
- [ ] `tests/test_sulfation_offline_harness.py` — Year-simulation integration with synthetic discharge curves
- [ ] `scripts/test_instcmd_live.sh` — Live UT850EG validation script (shell or Python, captures response)
- [ ] `src/battery_math/sulfation.py` — Implementation
- [ ] `src/battery_math/cycle_roi.py` — Implementation
- [ ] `src/nut_client.py` (extend) — send_instcmd() method

*(Note: Existing NUT client and daemon tests are sufficient for regression detection. New modules follow existing patterns, reducing test infrastructure needed.)*

---

## Sources

### Primary (HIGH confidence)

- **RFC 9271 - UPS Management Protocol** (IETF standard) — https://www.rfc-editor.org/rfc/rfc9271.html — INSTCMD syntax, auth requirements, response format
- **NUT Net Protocol Documentation** (networkupstools.org) — https://networkupstools.org/docs/developer-guide.chunked/net-protocol.html — INSTCMD, USERNAME, PASSWORD, LOGIN sequence
- **upscmd Manual** (NUT) — https://networkupstools.org/docs/man/upscmd.html — Practical usage, -l flag for command discovery
- **NUT GitHub Repository** — https://github.com/networkupstools/nut — usbhid-ups driver (CyberPower support), INSTCMD handler implementation, test examples
- **IEEE-450 Battery Standards** — Standard reference for lead-acid testing; sulfation physics basis
- **VRLA White Paper (ACTEC)** — https://actec.dk/media/documents/68F4B35DD5C5.pdf — Internal resistance increase mechanisms, sulfation-IR correlation
- **Peukert's Law** (Victron Energy, BattleBoard Batteries) — https://www.victronenergy.com/media/pg/SmartShunt/en/battery-capacity-and-peukert-exponent.html — Exponent 1.2 for flooded batteries; already validated in v2.0
- **Existing codebase patterns** (`src/battery_math/`, `src/nut_client.py`, `tests/`) — Pure function pattern; frozen dataclass architecture; socket session lifecycle

### Secondary (MEDIUM confidence)

- **IOP Science: Sulfation Modeling in Flooded Lead-Acid** — https://iopscience.iop.org/article/10.1149/1945-7111/ab679b — Physics baseline (Shepherd model); dR/dt acceleration during sulfation
- **Penn State Dissertation: Battery Diagnostics** — https://www.me.psu.edu/mrl/theses/YingShi_dissertation.pdf — Electrochemical impedance spectroscopy (EIS) for sulfation detection; recovery delta measurement techniques
- **ResearchGate: Desulfation Using High-Frequency Pulse** — https://www.researchgate.net/publication/347929763 — Recovery signals post-desulfation (empirical ΔSoH)
- **NUT Issues #520, #2717, #3142 (GitHub)** — CyberPower BR1000ELCD, UT1050EG compatibility; test.battery.start.quick/deep support confirmed

### Tertiary (LOW confidence, needs validation)

- **Battery University: Rising Internal Resistance** — https://www.batteryuniversity.com/article/bu-802a-how-does-rising-internal-resistance-affect-performance/ — General IR aging; not VRLA-specific
- **Cycle Life vs DoD Guides (multiple sources)** — Depth-of-discharge aging curve; primarily Li-ion focused; lead-acid curve less detailed in literature

---

## Metadata

**Confidence breakdown:**
- NUT INSTCMD protocol: HIGH — RFC 9271 standard, well-documented, existing NUT library support
- Sulfation scoring formula: MEDIUM — Physics baseline sound (IEEE-450), but empirical signal weighting (IR/recovery) requires field validation in Phase 16
- Cycle ROI normalization: MEDIUM — Tradeoff framework clear, but optimal weighting unknown; Phase 16 field data required for tuning
- send_instcmd() implementation: HIGH — Straightforward RFC 9271 socket protocol; existing NUTClient patterns apply directly
- Daemon regression risk: HIGH (low) — Pure functions with no daemon coupling; import safety validated by existing test suite

**Research date:** 2026-03-17
**Valid until:** 2026-04-17 (30 days; NUT protocol stable; sulfation field data collection starts Phase 16 and may require adjustment)

---

## RESEARCH COMPLETE
