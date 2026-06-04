"""Tests for DischargeHandler helpers that can be exercised in isolation."""

from types import SimpleNamespace

from src.discharge_handler import DischargeHandler
from src.model import BatteryModel
from src.soc_predictor import soc_from_voltage


def _dod(model: BatteryModel, voltages: list[float]) -> float:
    """Call _estimate_dod_from_buffer in isolation.

    The method only reads self.battery_model and the buffer's .voltages, so we bypass the
    heavyweight __init__ (capacity_estimator, RLS, config, ...) and bind just the model.
    """
    handler = DischargeHandler.__new__(DischargeHandler)
    handler.battery_model = model
    return handler._estimate_dod_from_buffer(SimpleNamespace(voltages=voltages))


class TestEstimateDodFromBuffer:
    """IN-04: DoD is the SoC span over the LUT, not a linear voltage-swing proxy."""

    def test_dod_equals_soc_span_over_lut(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        lut = model.get_lut()
        voltages = [12.4, 12.1, 11.6, 11.2]

        expected = soc_from_voltage(max(voltages), lut) - soc_from_voltage(min(voltages), lut)
        assert _dod(model, voltages) == max(0.0, min(1.0, expected))

    def test_deeper_discharge_yields_larger_dod(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        shallow = _dod(model, [12.4, 12.0])
        deep = _dod(model, [12.4, 10.7])
        assert deep > shallow

    def test_already_sagged_deep_discharge_is_not_underreported(self, tmp_path):
        # A discharge that begins already sagged (low V_max) but is driven deep used to
        # collapse to a tiny DoD under the swing proxy. The SoC span still registers it.
        model = BatteryModel(model_path=tmp_path / "model.json")
        lut = model.get_lut()
        voltages = [11.6, 11.2, 10.7]
        expected = soc_from_voltage(11.6, lut) - soc_from_voltage(10.7, lut)
        assert _dod(model, voltages) == max(0.0, min(1.0, expected))
        assert _dod(model, voltages) > 0.0

    def test_result_is_clamped_to_unit_interval(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        # Above-LUT max and below-LUT anchor clamp to SoC 1.0 and 0.0 → DoD 1.0.
        assert _dod(model, [99.0, 0.0]) == 1.0

    def test_fewer_than_two_samples_returns_zero(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert _dod(model, [12.0]) == 0.0
        assert _dod(model, []) == 0.0
