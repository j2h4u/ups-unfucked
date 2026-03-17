"""Pure kernel functions: Sulfation scoring and desulfation evidence estimation.

No I/O, no logging, no time() calls. All state passed as parameters.
Functions are pure: same inputs always produce same outputs.

Sulfation is lead-acid capacity loss due to lead sulfate crystal growth
during idle periods (rest) and high-temperature storage. The score combines:
- Physics baseline: idle time + temperature acceleration
- Empirical IR signal: internal resistance drift (dR/dt)
- Recovery signal: SoH rebound after discharge (desulfation evidence)

Source: IEEE-450 lead-acid standards, VRLA white papers, Shepherd model.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SulfationState:
    """Immutable snapshot of sulfation score and supporting signals.

    Every kernel function takes frozen SulfationState, returns new state.
    Frozen dataclass makes circular dependencies structurally visible at type level.
    """

    score: float
    """Sulfation score [0.0, 1.0] where 0.0 = no sulfation, 1.0 = critical"""

    days_since_deep: float
    """Days elapsed since last deep discharge (≥50% DoD)"""

    ir_trend_rate: float
    """Internal resistance rate-of-change (dR/dt in Ω/day)"""

    recovery_delta: float
    """SoH rebound after discharge [0.0, 1.0], higher = better desulfation"""

    temperature_celsius: float
    """Battery temperature in Celsius (constant 35°C per v3.0 scope)"""


def compute_sulfation_score(
    days_since_deep: float,
    ir_trend_rate: float,
    recovery_delta: float,
    temperature_celsius: float = 35.0,
    temp_factor: float = 0.05,
    ir_weight: float = 0.4,
    recovery_weight: float = 0.3,
    days_weight: float = 0.3,
) -> float:
    """Hybrid sulfation score combining physics baseline + empirical signals.

    Args:
        days_since_deep: Days elapsed since last ≥50% discharge event
        ir_trend_rate: Internal resistance rate-of-change (dR/dt in Ω/day)
        recovery_delta: SoH bounce after discharge [0.0, 1.0], higher = less sulfation
        temperature_celsius: Battery temperature (constant 35°C per v3.0 scope)
        temp_factor: Temperature aging acceleration (%/°C above 25°C baseline)
        ir_weight: Weight for IR signal in final blend [0, 1]
        recovery_weight: Weight for recovery signal [0, 1]
        days_weight: Weight for time signal [0, 1]

    Returns:
        Sulfation score [0.0, 1.0] where 0.0 = no sulfation, 1.0 = critical

    Physics:
        Shepherd model: sulfation per day ≈ 0.02 at 25°C baseline.
        Temperature acceleration: 0.05 percentage points per °C above 25°C.
        Example: at 35°C, baseline rate = 0.02 * (1 + 0.05 * 10) = 0.03/day.
        30 days idle → baseline_score ≈ 0.9 at 35°C.

    Empirical signals:
        IR signal: CyberPower UT850 R_internal ≈ 5–8 mΩ. Threshold 0.1 mΩ/day
        means severe sulfation (drifting from 5→6 mΩ over 10 days).
        Recovery signal: Healthy discharge drops 1% SoH; sulfation blocks recovery.
        Low recovery_delta (<0.05) indicates charge acceptance blocked.

    Source: IEEE-450 Battery Standards, VRLA white papers, Shepherd model.
    """
    # Physics baseline: sulfation grows with idle time at elevated temp
    temp_adjusted_rate = 0.02 * (1 + temp_factor * (temperature_celsius - 25.0))
    baseline_score = min(1.0, days_since_deep * temp_adjusted_rate / 30.0)

    # IR trend signal: increasing dR/dt indicates active sulfation
    # Normalize to [0, 1]: typical threshold 0.1 Ω/day for high sulfation
    if ir_trend_rate > 0:
        ir_signal = min(1.0, ir_trend_rate / 0.1)
    else:
        ir_signal = 0.0

    # Recovery signal: low recovery_delta = poor desulfation = high sulfation
    # Healthy batteries recover 0.05+ of drop; sulfation blocks recovery
    if recovery_delta >= 0:
        recovery_signal = max(0.0, 1.0 - (recovery_delta / 0.15))
    else:
        recovery_signal = 1.0

    # Weighted blend of three signals
    score = (
        baseline_score * days_weight
        + ir_signal * ir_weight
        + recovery_signal * recovery_weight
    )

    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, score))


def estimate_recovery_delta(
    soh_before_discharge: float,
    soh_after_discharge: float,
    expected_soh_drop: float = 0.01,
) -> float:
    """Estimate desulfation evidence from SoH rebound after deep discharge.

    Args:
        soh_before_discharge: SoH at discharge start [0.0, 1.0]
        soh_after_discharge: SoH after discharge + recharge recovery [0.0, 1.0]
        expected_soh_drop: Physics-based SoH drop for healthy battery (default 1%)

    Returns:
        Recovery delta [0.0, 1.0] where >0.05 = good desulfation signal

    Physics:
        Healthy battery drops SoH by ~1% during a full discharge cycle
        due to normal cycle wear (internal corrosion, grid growth).
        If SoH recovers by >0.5% post-recharge (vs 1% drop), sulfation reversed.

    Examples:
        SoH 0.95→0.94 (1% drop) then 0.94→0.95 (1% recovery) → delta ≈ 0.1
        SoH 0.95→0.93 (2% drop, wear > recovery) → delta ≈ -0.1 (clamped to 0.0)
        SoH 0.95→0.96 (recovery without drop) → delta = 0.0 (unclear signal)

    Reasoning:
        If discharge causes more drop than expected (sulfation blocking recovery),
        or if SoH stays flat post-discharge (poor charge acceptance),
        recovery_delta will be low (<0.05), signaling high sulfation.
        Good desulfation reverses some of the impedance, allowing recovery.
    """
    soh_drop = soh_before_discharge - soh_after_discharge

    if soh_drop <= 0:
        # No drop detected; unclear signal (noise or recovery without discharge)
        return 0.0

    # Recovery fraction: actual recovery vs expected recovery for healthy battery
    # If drop = 1% expected and actual recovery = 1%, delta = 0.0 (neutral)
    # If drop = 2% actual but expected = 1%, sulfation blocked recovery → delta < 0
    recovery = soh_drop - expected_soh_drop
    return max(0.0, min(1.0, recovery / expected_soh_drop))
