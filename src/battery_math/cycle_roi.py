"""Pure kernel function: Cycle ROI calculation for desulfation vs wear tradeoff.

No I/O, no logging, no time() calls. All state passed as parameters.
Function is pure: same inputs always produce same outputs.

Cycle ROI quantifies the return-on-investment for a single discharge event:
- Positive ROI: desulfation benefit outweighs wear cost (do discharge/test)
- Negative ROI: wear cost exceeds benefit (skip discharge/test)
- Zero ROI: break-even (indifferent; recommend skip to be conservative)

Source: Battery physics (desulfation benefit with time), cycle life curves,
IEEE-450 test standards.
"""

import math


def compute_cycle_roi(
    depth_of_discharge: float,
    cycle_budget_remaining: float,
    ir_trend_rate: float,
    sulfation_score: float,
) -> float:
    """Cycle ROI: Return-on-Investment for single discharge/test event.

    Args:
        depth_of_discharge: DoD [0.0, 1.0] for this proposed discharge
        cycle_budget_remaining: Cycles left at SoH=65% threshold (before mandatory replacement)
        ir_trend_rate: Internal resistance drift (dR/dt Ω/day)
        sulfation_score: Current sulfation score [0.0, 1.0] from compute_sulfation_score()

    Returns:
        ROI [-1.0, +1.0] where:
        +1.0 = pure benefit (severe sulfation, many cycles left, discharge highly beneficial)
        0.0 = break-even (benefit = wear cost, recommend skip to be conservative)
        -1.0 = pure cost (sulfation low, few cycles left, discharge harmful)

    Formula:
        benefit = sulfation_score * 0.7 + min(ir_trend_rate, 0.1) / 0.1 * 0.3
        cost = depth_of_discharge * 0.5 + (1 - cycles_remaining / 100) * 0.5
        roi = (benefit - cost) / (benefit + cost) → saturates to [-1, +1]
    """
    desulfation_benefit = min(
        1.0,
        (sulfation_score * 0.7) +
        (min(ir_trend_rate, 0.1) / 0.1 * 0.3)
    )

    cycle_exhaustion_cost = max(0.0, 1.0 - cycle_budget_remaining / 100.0)
    wear_cost = min(
        1.0,
        (depth_of_discharge * 0.5) +
        (cycle_exhaustion_cost * 0.5)
    )

    # Normalize: ROI = (benefit - cost) / (benefit + cost)
    benefit_plus_cost = desulfation_benefit + wear_cost
    if benefit_plus_cost < 0.001:
        return 0.0

    roi = (desulfation_benefit - wear_cost) / benefit_plus_cost

    return max(-1.0, min(1.0, roi))
