"""Sampled safety regression for bounded downward IR changes.

The pointwise non-increase guarantee comes from the surrounding policy: both
snapshots use the zero-load reference frame, the committed ``k`` is bounded
downward, loads are non-negative, and the LUT is monotone. The voltage/load
grid below is a regression sampling net for implementation defects, not a
proof over every possible input.
"""

from src.application.safety import NUT_STATUS_LOW_BATTERY, SafetyInputs, calculate_safety
from src.domain.values import BlackoutKind, FrozenModelSnapshot

RUNTIME_EPSILON_MINUTES = 1e-9


def no_later_lb_oracle(
    before: FrozenModelSnapshot,
    after: FrozenModelSnapshot,
    *,
    shutdown_threshold_minutes: int,
) -> tuple[bool, str]:
    """Run the sampled safety regression net for a policy-valid IR change.

    Callers must enforce the bounded downward change before invoking this
    check. A passing grid is evidence that sampled runtime and LB behavior did
    not regress; it is not an all-input proof of pointwise non-increase.
    """
    if before.ir_reference_load_percent != 0.0 or after.ir_reference_load_percent != 0.0:
        return False, "ir_reference_frame_not_transformed"
    if before.battery_epoch_id != after.battery_epoch_id:
        return False, "battery_epoch_changed"
    voltage_grid = {8.0 + index * 0.05 for index in range(141)}
    voltage_grid.update(point.voltage_v for point in before.lut)
    voltage_grid.update(point.voltage_v for point in after.lut)
    case_count = 0
    for load_percent in range(101):
        for voltage_v in sorted(voltage_grid):
            before_result = calculate_safety(
                inputs=SafetyInputs(
                    voltage_v=voltage_v,
                    load_percent=float(load_percent),
                    blackout_kind=BlackoutKind.BLACKOUT_REAL,
                    shutdown_threshold_minutes=shutdown_threshold_minutes,
                ),
                snapshot=before,
            )
            after_result = calculate_safety(
                inputs=SafetyInputs(
                    voltage_v=voltage_v,
                    load_percent=float(load_percent),
                    blackout_kind=BlackoutKind.BLACKOUT_REAL,
                    shutdown_threshold_minutes=shutdown_threshold_minutes,
                ),
                snapshot=after,
            )
            if (
                after_result.runtime_minutes
                > before_result.runtime_minutes + RUNTIME_EPSILON_MINUTES
            ):
                return False, f"runtime_increased_at_load_{load_percent}"
            if (
                before_result.virtual_status == NUT_STATUS_LOW_BATTERY
                and after_result.virtual_status != NUT_STATUS_LOW_BATTERY
            ):
                return False, f"lb_delayed_at_load_{load_percent}"
            case_count += 1
    return True, f"sampled_safety_regression_grid:{case_count}"
