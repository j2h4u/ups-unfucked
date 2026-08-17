from dataclasses import replace

from src.application.safety_oracle import no_later_lb_oracle
from src.battery_math.lut import LutPoint
from src.domain.values import FrozenModelSnapshot


def snapshot(k: float, *, reference: float = 0.0) -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        schema_revision="2",
        evaluation_revision="1",
        battery_epoch_id="a" * 32,
        scientific_fingerprint="b" * 64,
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=k,
        ir_reference_load_percent=reference,
        lut=(
            LutPoint(13.7, 1.0, "standard"),
            LutPoint(12.7, 0.6, "standard"),
            LutPoint(10.8, 0.0, "anchor"),
        ),
    )


def test_downward_k_is_never_less_conservative_in_reference_zero_frame() -> None:
    accepted, description = no_later_lb_oracle(
        snapshot(0.015),
        snapshot(0.012),
        shutdown_threshold_minutes=17,
    )
    assert accepted
    assert description.startswith("sampled_safety_regression_grid:")


def test_upward_k_is_rejected() -> None:
    accepted, description = no_later_lb_oracle(
        snapshot(0.012),
        snapshot(0.015),
        shutdown_threshold_minutes=17,
    )
    assert not accepted
    assert description.startswith("runtime_increased_at_load_")


def test_nonzero_reference_frame_is_rejected_before_grid() -> None:
    before = snapshot(0.015, reference=20.0)
    accepted, description = no_later_lb_oracle(
        before,
        replace(before, ir_k_v_per_pp=0.012),
        shutdown_threshold_minutes=17,
    )
    assert not accepted
    assert description == "ir_reference_frame_not_transformed"
