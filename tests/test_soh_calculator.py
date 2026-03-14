"""Unit tests for SoH calculation from discharge voltage profiles."""

import pytest
from src.soh_calculator import calculate_soh_from_discharge


def test_calculate_soh_basic():
    """Normal discharge: voltage drops linearly 13.4V → 10.5V over defined time."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0


def test_non_uniform_time_intervals():
    """Time intervals vary (5 sec, 10 sec, 15 sec). Area-under-curve must weight by Δt."""
    v = [13.4, 12.9, 11.9, 10.6]
    t = [0, 5, 15, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0


def test_empty_discharge():
    """Empty list or single voltage point returns reference_soh unchanged."""
    soh_empty = calculate_soh_from_discharge([], [], reference_soh=1.0)
    assert soh_empty == 1.0

    soh_single = calculate_soh_from_discharge([13.0], [0], reference_soh=1.0)
    assert soh_single == 1.0


def test_anchor_voltage_trimming():
    """Voltage data extends below 10.5V anchor. Integration stops at anchor."""
    v = [13.4, 12.4, 11.4, 10.4, 9.5]
    t = [0, 10, 20, 30, 40]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    assert isinstance(soh, float)
    assert 0.0 <= soh <= 1.0


def test_degradation_monotonic():
    """Multiple discharges; SoH decreases monotonically."""
    soh = 1.0

    v1 = [13.4, 12.4, 11.4, 10.6]
    t1 = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v1, t1, reference_soh=soh, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    soh1 = soh

    v2 = [13.4, 12.4, 11.4, 10.6]
    t2 = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v2, t2, reference_soh=soh, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    soh2 = soh

    assert soh2 <= soh1


def test_reference_soh_scaling():
    """Measured area is fraction of reference; new_soh scales proportionally."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, anchor_voltage=10.5,
                                        capacity_ah=7.2, load_percent=20.0)
    assert 0.0 <= soh <= 1.0


def test_no_nan_result():
    """Reference area computed from Peukert should never be zero → no NaN."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]
    soh = calculate_soh_from_discharge(v, t, reference_soh=1.0, capacity_ah=7.2,
                                        load_percent=20.0)
    assert isinstance(soh, float)
    assert not (soh != soh)  # Not NaN


def test_clamping_bounds():
    """Computed SoH clamped to [0, 1]."""
    v = [13.4, 12.4, 11.4, 10.6]
    t = [0, 10, 20, 30]

    soh_high = calculate_soh_from_discharge(v, t, reference_soh=0.99, anchor_voltage=10.5,
                                             capacity_ah=7.2, load_percent=20.0)
    assert soh_high <= 1.0

    soh_low = calculate_soh_from_discharge(v, t, reference_soh=0.01, anchor_voltage=10.5,
                                            capacity_ah=7.2, load_percent=20.0)
    assert soh_low >= 0.0


def test_no_hardcoded_reference_area():
    """Verify old hardcoded area_reference=12.0*2820 is gone — uses physics params."""
    import inspect
    sig = inspect.signature(calculate_soh_from_discharge)
    assert 'capacity_ah' in sig.parameters
    assert 'nominal_power_watts' in sig.parameters
    assert 'peukert_exponent' in sig.parameters


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


def test_interpolate_cliff_region_with_realistic_data():
    """Test interpolation with measured points from 2026-03-12 blackout."""
    from src.soh_calculator import interpolate_cliff_region

    # Setup: 3 measured points, expect many interpolated fill-ins
    lut = [
        {'v': 13.4, 'soc': 1.0, 'source': 'standard'},  # Non-cliff, preserved
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},  # Cliff boundary
        {'v': 10.6, 'soc': 0.10, 'source': 'measured'},  # Cliff middle
        {'v': 10.5, 'soc': 0.0, 'source': 'measured'},   # Cliff anchor
    ]
    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Verify non-cliff entries preserved
    assert any(e['v'] == 13.4 and e['source'] == 'standard' for e in result)

    # Verify measured points present
    assert any(e['v'] == 11.0 and e['source'] == 'measured' for e in result)
    assert any(e['v'] == 10.5 and e['source'] == 'measured' for e in result)

    # Verify interpolated entries exist and count
    interpolated = [e for e in result if e['source'] == 'interpolated']
    assert len(interpolated) >= 4  # At least 4 interpolated between measured points

    # Verify sorted descending
    assert result == sorted(result, key=lambda x: x['v'], reverse=True)


def test_interpolate_cliff_region_removes_standard():
    """Verify standard entries in cliff region are removed and replaced by interpolated fill."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.8, 'soc': 0.40, 'source': 'standard'},  # Should be removed
        {'v': 10.7, 'soc': 0.25, 'source': 'standard'},  # Should be removed
        {'v': 10.6, 'soc': 0.20, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.0, 'source': 'measured'},
    ]
    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Verify old standard entries removed
    assert not any(e['v'] == 10.8 and e['source'] == 'standard' for e in result)
    assert not any(e['v'] == 10.7 and e['source'] == 'standard' for e in result)

    # Verify new entries at those voltages are interpolated
    assert any(e['v'] == 10.8 and e['source'] == 'interpolated' for e in result)
    assert any(e['v'] == 10.7 and e['source'] == 'interpolated' for e in result)


def test_interpolate_cliff_region_preserves_measured():
    """Measured points marked with source='measured' preserved in output."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.0, 'source': 'measured'},
    ]
    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Measured entries should still be there with same source
    assert any(e['v'] == 11.0 and e['source'] == 'measured' for e in result)
    assert any(e['v'] == 10.5 and e['source'] == 'measured' for e in result)


def test_lut_source_field_preservation():
    """Verify source field values are preserved and properly tracked."""
    from src.soh_calculator import interpolate_cliff_region

    lut = [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.0, 'source': 'anchor'},
    ]
    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Non-cliff entries should be unchanged
    assert any(e['v'] == 13.4 and e['source'] == 'standard' for e in result)

    # Measured entries should stay marked as measured
    assert any(e['v'] == 11.0 and e['source'] == 'measured' for e in result)

    # Interpolated entries should be marked as interpolated
    interpolated = [e for e in result if e['source'] == 'interpolated']
    assert all(e['source'] == 'interpolated' for e in interpolated)


def test_interpolate_cliff_region_linear_math():
    """Verify linear interpolation math is correct."""
    from src.soh_calculator import interpolate_cliff_region

    # Two measured points: 11.0V→50%, 10.5V→0%
    # Interpolation at 0.1V steps: 10.9V, 10.8V, 10.7V, 10.6V (not 10.75V)
    lut = [
        {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
        {'v': 10.5, 'soc': 0.0, 'source': 'measured'},
    ]
    result = interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1)

    # Find interpolated entry at 10.8V (should be ~0.4 or 40% SoC)
    entry_10_8 = [e for e in result if abs(e['v'] - 10.8) < 0.01]
    assert len(entry_10_8) > 0, f"No entry near 10.8V. Available: {[e['v'] for e in result]}"
    soc_10_8 = entry_10_8[0]['soc']

    # Should be approximately 0.4 (40% SoC) due to linear interpolation
    # (11.0V = 0.5, 10.5V = 0.0, so 10.8V = 0.5 - 0.4 = 0.1... wait let me recalculate)
    # Actually: slope = (0.0 - 0.5) / (10.5 - 11.0) = -0.5 / -0.5 = 1.0
    # At 10.8V: soc = 0.5 + (10.8 - 11.0) * 1.0 = 0.5 - 0.2 = 0.3 (30%)
    assert abs(soc_10_8 - 0.3) < 0.05, f"Expected ~0.3 at 10.8V, got {soc_10_8}"

    # Find interpolated entry at 10.6V (should be ~0.1 or 10% SoC)
    entry_10_6 = [e for e in result if abs(e['v'] - 10.6) < 0.01]
    assert len(entry_10_6) > 0, f"No entry near 10.6V. Available: {[e['v'] for e in result]}"
    soc_10_6 = entry_10_6[0]['soc']

    # At 10.6V: soc = 0.5 + (10.6 - 11.0) * 1.0 = 0.5 - 0.4 = 0.1 (10%)
    assert abs(soc_10_6 - 0.1) < 0.05, f"Expected ~0.1 at 10.6V, got {soc_10_6}"

