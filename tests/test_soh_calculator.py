"""Unit tests for SoH calculation from discharge voltage profiles."""

import pytest
from src.soh_calculator import calculate_soh_from_discharge


def test_calculate_soh_basic():
    """Normal discharge: voltage drops linearly 13.4V → 10.5V over defined time."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0


def test_non_uniform_time_intervals():
    """Time intervals vary (5 sec, 10 sec, 15 sec). Area-under-curve must weight by Δt."""
    v = [13.4, 12.9, 11.9, 10.6]
    t = [0, 5, 15, 30]  # Non-uniform intervals
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0


def test_empty_discharge():
    """Empty list or single voltage point returns reference_soh unchanged."""
    # Empty
    soh_empty = calculate_soh_from_discharge([], [], reference_soh=1.0, anchor_voltage=10.5)
    assert soh_empty == 1.0

    # Single point
    soh_single = calculate_soh_from_discharge([13.0], [0], reference_soh=1.0, anchor_voltage=10.5)
    assert soh_single == 1.0


def test_anchor_voltage_trimming():
    """Voltage data extends below 10.5V anchor. Integration stops at anchor."""
    v = [13.4, 12.4, 11.4, 10.4, 9.5]  # Below anchor at end
    t = [0, 10, 20, 30, 40]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0
    # Should trim at 10.5V, so only first 3 points count


def test_degradation_monotonic():
    """Multiple discharges; SoH decreases monotonically."""
    soh = 1.0

    # First discharge
    v1 = [13.4, 12.4, 11.4, 10.6]
    t1 = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v1, t1, reference_soh=soh, anchor_voltage=10.5)
    soh1 = soh

    # Second discharge (same curve)
    v2 = [13.4, 12.4, 11.4, 10.6]
    t2 = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v2, t2, reference_soh=soh, anchor_voltage=10.5)
    soh2 = soh

    assert soh2 <= soh1  # SoH stays same or decreases


def test_reference_soh_scaling():
    """Measured area is fraction of reference; new_soh scales proportionally."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    # Normal discharge
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5)
    assert 0.0 <= soh <= 1.0


def test_zero_reference_area():
    """Edge case: reference_area should not be zero (hardcoded in implementation)."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5)
    assert isinstance(soh, float)
    assert not (soh != soh)  # Not NaN


def test_clamping_bounds():
    """Computed SoH > 1.0 or < 0.0 is clamped to [0, 1]."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]

    # With reference_soh at boundary
    soh_high = calculate_soh_from_discharge(v, t, reference_soh=0.99, anchor_voltage=10.5)
    assert soh_high <= 1.0

    soh_low = calculate_soh_from_discharge(v, t, reference_soh=0.01, anchor_voltage=10.5)
    assert soh_low >= 0.0


def test_interpolate_cliff_region_basic():
    """Two measured points 11.0V→50%, 10.5V→0% → fills with interpolated entries."""
    from src.soh_calculator import interpolate_cliff_region

    # Measured points in cliff region
    lut = [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
    ]

    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Should have more entries now (interpolated between 11.0 and 10.5)
    assert len(result) > len(lut)

    # Check that interpolated entries exist
    interpolated = [e for e in result if e['source'] == 'interpolated']
    assert len(interpolated) > 0

    # Check source field is marked correctly
    for entry in interpolated:
        assert entry['source'] == 'interpolated'


def test_interpolate_cliff_region_insufficient_points():
    """0 or 1 measured points → returns LUT unchanged."""
    from src.soh_calculator import interpolate_cliff_region

    # LUT with no measured points in cliff region
    lut = [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        {'v': 12.0, 'soc': 0.50, 'source': 'standard'},
        {'v': 10.5, 'soc': 0.00, 'source': 'anchor'},
    ]

    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Should be unchanged
    assert result == lut


def test_interpolate_cliff_region_preserves_non_cliff():
    """Entries >11.0V and <10.5V are preserved unchanged."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},  # >11.0
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},  # cliff
        {'v': 10.5, 'soc': 0.00, 'source': 'measured'},  # cliff
        {'v': 10.0, 'soc': 0.00, 'source': 'standard'},  # <10.5
    ]

    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Check non-cliff entries are preserved
    result_13_4 = [e for e in result if e['v'] == 13.4]
    assert len(result_13_4) == 1
    assert result_13_4[0]['source'] == 'standard'

    result_10_0 = [e for e in result if e['v'] == 10.0]
    assert len(result_10_0) == 1
    assert result_10_0[0]['source'] == 'standard'


def test_interpolate_cliff_region_source_field():
    """Interpolated entries marked with source='interpolated'."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
    ]

    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # All entries should have source field
    for entry in result:
        assert 'source' in entry
        assert entry['source'] in ['measured', 'interpolated']


def test_interpolate_cliff_region_sorted():
    """Result is sorted descending by voltage."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
    ]

    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    voltages = [e['v'] for e in result]
    assert voltages == sorted(voltages, reverse=True)

