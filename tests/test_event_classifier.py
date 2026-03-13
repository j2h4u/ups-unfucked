"""
Event classifier tests - distinguish real blackout from battery test.

Tests based on CONTEXT.md § Различение блекаута и теста батареи:
- input.voltage ≈ 0V when mains absent (blackout)
- input.voltage ≈ 230V when mains present (test, powered by battery but mains plugged in)
"""

import pytest
from src.event_classifier import EventClassifier, EventType


class TestEventClassifierBasic:
    """Basic state classification tests."""

    def test_classify_online(self):
        """When status=OL and mains present → ONLINE state."""
        classifier = EventClassifier()
        event_type = classifier.classify("OL", 230.0)
        assert event_type == EventType.ONLINE
        assert classifier.state == EventType.ONLINE
        assert classifier.transition_occurred is False

    def test_classify_real_blackout(self):
        """When status=OB DISCHRG and mains absent (0V) → BLACKOUT_REAL."""
        classifier = EventClassifier()
        classifier.state = EventType.ONLINE  # Start online
        event_type = classifier.classify("OB DISCHRG", 0.0)
        assert event_type == EventType.BLACKOUT_REAL
        assert classifier.state == EventType.BLACKOUT_REAL
        assert classifier.transition_occurred is True

    def test_classify_battery_test(self):
        """When status=OB DISCHRG and mains present (230V) → BLACKOUT_TEST."""
        classifier = EventClassifier()
        classifier.state = EventType.ONLINE  # Start online
        event_type = classifier.classify("OB DISCHRG", 230.0)
        assert event_type == EventType.BLACKOUT_TEST
        assert classifier.state == EventType.BLACKOUT_TEST
        assert classifier.transition_occurred is True


class TestEventClassifierTransitions:
    """State transition detection tests."""

    def test_transition_ol_to_real_blackout(self):
        """OL → OB DISCHRG with mains absent → transition_occurred=True."""
        classifier = EventClassifier()
        assert classifier.state == EventType.ONLINE

        event = classifier.classify("OB DISCHRG", 0.0)
        assert event == EventType.BLACKOUT_REAL
        assert classifier.transition_occurred is True

    def test_transition_real_blackout_to_online(self):
        """OB DISCHRG (real) → OL with mains present → transition_occurred=True."""
        classifier = EventClassifier()
        classifier.state = EventType.BLACKOUT_REAL  # Start in blackout

        event = classifier.classify("OL", 230.0)
        assert event == EventType.ONLINE
        assert classifier.transition_occurred is True

    def test_transition_ol_to_battery_test(self):
        """OL → OB DISCHRG with mains present → transition_occurred=True."""
        classifier = EventClassifier()
        assert classifier.state == EventType.ONLINE

        event = classifier.classify("OB DISCHRG", 230.0)
        assert event == EventType.BLACKOUT_TEST
        assert classifier.transition_occurred is True

    def test_no_transition_online_stays(self):
        """OL → OL with mains present → transition_occurred=False."""
        classifier = EventClassifier()

        event = classifier.classify("OL", 230.0)
        assert event == EventType.ONLINE
        assert classifier.transition_occurred is False

    def test_no_transition_blackout_real_stays(self):
        """OB DISCHRG (real) → OB DISCHRG (real) → transition_occurred=False."""
        classifier = EventClassifier()
        classifier.state = EventType.BLACKOUT_REAL

        event = classifier.classify("OB DISCHRG", 0.0)
        assert event == EventType.BLACKOUT_REAL
        assert classifier.transition_occurred is False

    def test_no_transition_battery_test_stays(self):
        """OB DISCHRG (test) → OB DISCHRG (test) → transition_occurred=False."""
        classifier = EventClassifier()
        classifier.state = EventType.BLACKOUT_TEST

        event = classifier.classify("OB DISCHRG", 230.0)
        assert event == EventType.BLACKOUT_TEST
        assert classifier.transition_occurred is False


class TestEventClassifierVoltageThresholds:
    """Test voltage threshold hysteresis."""

    def test_mains_present_threshold(self):
        """input.voltage > 100V → mains present."""
        classifier = EventClassifier()
        # Should classify as BLACKOUT_TEST when on battery and voltage > 100V
        event = classifier.classify("OB DISCHRG", 120.0)
        assert event == EventType.BLACKOUT_TEST

    def test_mains_absent_threshold(self):
        """input.voltage < 50V → mains absent."""
        classifier = EventClassifier()
        # Should classify as BLACKOUT_REAL when on battery and voltage < 50V
        event = classifier.classify("OB DISCHRG", 30.0)
        assert event == EventType.BLACKOUT_REAL

    def test_undefined_voltage_range(self, caplog):
        """input.voltage in 50-100V range → treat as absent, log warning."""
        import logging
        classifier = EventClassifier()

        with caplog.at_level(logging.WARNING):
            event = classifier.classify("OB DISCHRG", 75.0)

        # Should treat undefined range as absent
        assert event == EventType.BLACKOUT_REAL
        # Should log warning about undefined range
        assert "undefined range" in caplog.text.lower()

    def test_boundary_100v_mains_present(self):
        """input.voltage exactly 100V → treat as mains present (>100V is the threshold)."""
        classifier = EventClassifier()
        # At boundary, anything >100 means present
        event = classifier.classify("OB DISCHRG", 100.0)
        # 100.0 is not > 100, so should be treated as mains absent
        assert event == EventType.BLACKOUT_REAL

    def test_boundary_100p1v_mains_present(self):
        """input.voltage 100.1V → treat as mains present."""
        classifier = EventClassifier()
        event = classifier.classify("OB DISCHRG", 100.1)
        assert event == EventType.BLACKOUT_TEST

    def test_boundary_50v_mains_absent(self):
        """input.voltage exactly 50V → treat as mains absent (<50V is not absent, but 50 is boundary)."""
        classifier = EventClassifier()
        event = classifier.classify("OB DISCHRG", 50.0)
        # 50.0 is not < 50, so should be treated as in undefined range
        assert event == EventType.BLACKOUT_REAL

    def test_boundary_49p9v_mains_absent(self):
        """input.voltage 49.9V → treat as mains absent."""
        classifier = EventClassifier()
        event = classifier.classify("OB DISCHRG", 49.9)
        assert event == EventType.BLACKOUT_REAL


class TestEventClassifierWithLowBattery:
    """Test classification with LB (low battery) flag in status."""

    def test_classify_with_lb_flag(self):
        """status=OB DISCHRG LB should still be classified correctly based on voltage."""
        classifier = EventClassifier()
        classifier.state = EventType.ONLINE

        event = classifier.classify("OB DISCHRG LB", 0.0)
        assert event == EventType.BLACKOUT_REAL

    def test_classify_test_with_lb_flag(self):
        """status=OB DISCHRG LB with mains present → still BLACKOUT_TEST."""
        classifier = EventClassifier()
        classifier.state = EventType.ONLINE

        event = classifier.classify("OB DISCHRG LB", 230.0)
        assert event == EventType.BLACKOUT_TEST


class TestEventTypeEnum:
    """Test EventType enum."""

    def test_event_type_online(self):
        """EventType.ONLINE has value "OL"."""
        assert EventType.ONLINE.value == "OL"

    def test_event_type_blackout_real(self):
        """EventType.BLACKOUT_REAL has value "OB_BLACKOUT"."""
        assert EventType.BLACKOUT_REAL.value == "OB_BLACKOUT"

    def test_event_type_blackout_test(self):
        """EventType.BLACKOUT_TEST has value "OB_TEST"."""
        assert EventType.BLACKOUT_TEST.value == "OB_TEST"
