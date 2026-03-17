"""Pure kernel function: Cycle ROI calculation for desulfation vs wear tradeoff.

No I/O, no logging, no time() calls. All state passed as parameters.
Function is pure: same inputs always produce same outputs.

Cycle ROI quantifies the return-on-investment for a single discharge event:
- Positive ROI: desulfation benefit outweighs wear cost (do discharge/test)
- Negative ROI: wear cost exceeds benefit (skip discharge/test)
- Zero ROI: break-even (indifferent; recommend skip to be conservative)

This is a decision kernel used by Phase 17 scheduling logic to determine
when automatic battery tests are beneficial vs risky.

Source: Battery physics (desulfation benefit with time), cycle life curves,
IEEE-450 test standards.
"""

import math


def compute_cycle_roi(
    days_since_deep: float,
    depth_of_discharge: float,
    cycle_budget_remaining: float,
    ir_trend_rate: float,
    sulfation_score: float,
    temp_celsius: float = 35.0,
) -> float:
    """Cycle ROI: Return-on-Investment for single discharge/test event.

    Args:
        days_since_deep: Days since last ≥50% discharge (desulfation opportunity window)
        depth_of_discharge: DoD [0.0, 1.0] for this proposed discharge
        cycle_budget_remaining: Cycles left at SoH=65% threshold (before mandatory replacement)
        ir_trend_rate: Internal resistance drift (dR/dt Ω/day)
        sulfation_score: Current sulfation score [0.0, 1.0] from compute_sulfation_score()
        temp_celsius: Battery temperature (constant 35°C per v3.0 scope)

    Returns:
        ROI [-1.0, +1.0] where:
        +1.0 = pure benefit (severe sulfation, many cycles left, discharge highly beneficial)
        +0.5 = moderate benefit (worth doing)
        0.0 = break-even (benefit = wear cost, recommend skip to be conservative)
        -0.5 = moderate cost (avoid if possible)
        -1.0 = pure cost (sulfation low, few cycles left, discharge harmful)

    Decision rule (Phase 17 safety gate):
        if roi > 0.2 AND sulfation_score > 0.5 AND cycle_budget_remaining > 20:
            → schedule deep test (ROI positive + sulfation present + cycles available)
        if roi < 0.0 OR cycle_budget_remaining < 5:
            → skip deep test (marginal benefit or critical cycles)

    Physics:
        Desulfation benefit peaks when sulfation_score is high (severe) and
        days_since_deep is long (idle time accumulating). Benefit decays
        when battery is already healthy (low sulfation_score).

        Wear cost increases with:
        - Depth of discharge (deeper cycles wear faster)
        - Few cycles remaining (approaching end-of-life cliff)

    Formula:
        benefit = sulfation_score * 0.7 + min(ir_trend_rate, 0.1) / 0.1 * 0.3
        cost = depth_of_discharge * 0.5 + (1 - cycles_remaining / 100) * 0.5
        if benefit + cost < 0.001: return 0.0 (neither significant)
        roi = (benefit - cost) / (benefit + cost) → saturates to [-1, +1]

    Source: Cycle life curves (IEEE-450), desulfation physics (Shepherd model),
    risk assessment (safety gates for v3.0).
    """
    # Desulfation benefit: higher when sulfation is severe or IR drifting
    # Sulfation contributes 70% of benefit signal; IR trend contributes 30%
    desulfation_benefit = min(
        1.0,
        (sulfation_score * 0.7) +  # Sulfation severity (0–70% of score)
        (min(ir_trend_rate, 0.1) / 0.1 * 0.3)  # IR drift (0–30%)
    )

    # Wear cost: higher when doing deep DoD repeatedly or few cycles remain
    # DoD contributes 50% of cost; cycle budget contributes 50%
    # Normalize cycle_budget_remaining to [0, 1]: assume 100 cycles = full budget
    cycle_depletion = max(0.0, 1.0 - cycle_budget_remaining / 100.0)
    wear_cost = min(
        1.0,
        (depth_of_discharge * 0.5) +  # Deep DoD costs (0–50%)
        (cycle_depletion * 0.5)  # Few cycles left (0–50%)
    )

    # Normalize: ROI = (benefit - cost) / (benefit + cost)
    # If both are near-zero, return 0.0 (break-even, skip to be safe)
    total_magnitude = desulfation_benefit + wear_cost
    if total_magnitude < 0.001:
        return 0.0

    roi = (desulfation_benefit - wear_cost) / total_magnitude

    # Saturate to [-1.0, +1.0]
    return max(-1.0, min(1.0, roi))
