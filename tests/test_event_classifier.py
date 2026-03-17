"""Unit tests for event classifier module (EVT-01)."""

import pytest
from src.event_classifier import EventClassifier, EventType


class TestEventClassification:
    """Tests for event type classification based on UPS status and input voltage."""

    def test_classify_online(self):
        """Test ups.status='OL' with input.voltage=230V returns ONLINE."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OL", input_voltage=230)
        assert result == EventType.ONLINE, f"Expected ONLINE, got {result}"

    def test_classify_real_blackout(self):
        """Test ups.status='OB DISCHRG' with input.voltage=0V returns BLACKOUT_REAL."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB DISCHRG", input_voltage=0)
        assert result == EventType.BLACKOUT_REAL, f"Expected BLACKOUT_REAL, got {result}"

    def test_classify_battery_test(self):
        """Test ups.status='OB DISCHRG' with input.voltage=230V returns BLACKOUT_TEST."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB DISCHRG", input_voltage=230)
        assert result == EventType.BLACKOUT_TEST, f"Expected BLACKOUT_TEST, got {result}"


class TestEventStateTransitions:
    """Tests for state machine transitions and transition detection."""

    def test_transition_ol_to_real_blackout(self):
        """Test transition from ONLINE to BLACKOUT_REAL is detected."""
        classifier = EventClassifier()
        # Start online
        classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.state == EventType.ONLINE
        assert classifier.transition_occurred == False

        # Transition to blackout
        classifier.classify(ups_status="OB DISCHRG", input_voltage=0)
        assert classifier.state == EventType.BLACKOUT_REAL
        assert classifier.transition_occurred == True, "Transition flag should be set"

    def test_transition_real_blackout_to_ol(self):
        """Test transition from BLACKOUT_REAL back to ONLINE (power restored)."""
        classifier = EventClassifier()
        # Start in blackout
        classifier.classify(ups_status="OB DISCHRG", input_voltage=0)
        assert classifier.state == EventType.BLACKOUT_REAL

        # Transition back to online
        classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.state == EventType.ONLINE
        assert classifier.transition_occurred == True, "Transition flag should be set"

    def test_no_transition_when_state_unchanged(self):
        """Test transition_occurred=False when state doesn't change."""
        classifier = EventClassifier()
        # Stay online
        classifier.classify(ups_status="OL", input_voltage=230)
        classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.transition_occurred == False, "No transition should occur"


class TestEventUndefinedVoltage:
    """Tests for undefined input voltage ranges (50-100V)."""

    def test_undefined_voltage_range_70v(self):
        """Test input.voltage=75V (undefined range) handled gracefully."""
        classifier = EventClassifier()
        # Should not crash; should handle defensively
        result = classifier.classify(ups_status="OB DISCHRG", input_voltage=75)
        # Should treat as real blackout or log warning
        assert result in [EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST], \
            f"Expected safe handling, got {result}"

    def test_undefined_voltage_range_50v(self):
        """Test input.voltage=50V (boundary of undefined range)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB DISCHRG", input_voltage=50)
        assert result in [EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST], \
            f"Expected safe handling at boundary, got {result}"

    def test_undefined_voltage_range_100v(self):
        """Test input.voltage=100V (boundary of undefined range)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB DISCHRG", input_voltage=100)
        assert result in [EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST], \
            f"Expected safe handling at boundary, got {result}"


class TestFlagBasedMatching:
    """Tests for F36: flag-based status matching instead of exact string match."""

    def test_ob_lb_dischrg_classified_as_blackout_real(self):
        """F36: 'OB LB DISCHRG' + input=0V → BLACKOUT_REAL (was failing with dict match)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB LB DISCHRG", input_voltage=0)
        assert result == EventType.BLACKOUT_REAL

    def test_lb_ob_dischrg_flag_order_irrelevant(self):
        """F36: 'LB OB DISCHRG' (different flag order) → still BLACKOUT_REAL."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="LB OB DISCHRG", input_voltage=0)
        assert result == EventType.BLACKOUT_REAL

    def test_ob_without_dischrg_flag(self):
        """F36: 'OB' alone → battery category."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB", input_voltage=0)
        assert result == EventType.BLACKOUT_REAL

    def test_fsd_unknown_keeps_state(self):
        """F36: 'FSD' (forced shutdown) → unknown, keeps previous state, logs warning."""
        classifier = EventClassifier()
        classifier.classify(ups_status="OL", input_voltage=230)
        result = classifier.classify(ups_status="FSD", input_voltage=0)
        assert result == EventType.ONLINE  # Kept previous state

    def test_ob_lb_with_high_voltage_is_test(self):
        """F36: 'OB LB DISCHRG' + input=220V → BLACKOUT_TEST (mains present)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OB LB DISCHRG", input_voltage=220)
        assert result == EventType.BLACKOUT_TEST

    def test_ol_chrg_classified_as_online(self):
        """F36: 'OL CHRG' → ONLINE (OL flag present)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="OL CHRG", input_voltage=230)
        assert result == EventType.ONLINE

    def test_cal_dischrg_classified_as_battery(self):
        """F36: 'CAL DISCHRG' → battery category (CAL flag)."""
        classifier = EventClassifier()
        result = classifier.classify(ups_status="CAL DISCHRG", input_voltage=220)
        assert result == EventType.BLACKOUT_TEST

    def test_last_raw_status_tracked(self):
        """F36a: last_raw_status is set for fallback use."""
        classifier = EventClassifier()
        classifier.classify(ups_status="OB LB DISCHRG", input_voltage=0)
        assert classifier.last_raw_status == "OB LB DISCHRG"


class TestEventInitialization:
    """Tests for EventClassifier initialization."""

    def test_initial_state_is_online(self):
        """Test EventClassifier starts in ONLINE state."""
        classifier = EventClassifier()
        assert classifier.state == EventType.ONLINE, "Initial state should be ONLINE"

    def test_initial_transition_flag_false(self):
        """Test transition_occurred starts as False."""
        classifier = EventClassifier()
        assert classifier.transition_occurred == False, "Initial transition_occurred should be False"


class TestEventConsistency:
    """Tests for state machine consistency across multiple operations."""

    def test_multiple_online_events_no_transition(self):
        """Test multiple consecutive ONLINE events don't trigger transition."""
        classifier = EventClassifier()
        for _ in range(3):
            classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.transition_occurred == False, "Repeated same state shouldn't trigger transition"

    def test_state_after_multiple_transitions(self):
        """Test state consistency after multiple transitions."""
        classifier = EventClassifier()
        classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.state == EventType.ONLINE

        classifier.classify(ups_status="OB DISCHRG", input_voltage=0)
        assert classifier.state == EventType.BLACKOUT_REAL

        classifier.classify(ups_status="OL", input_voltage=230)
        assert classifier.state == EventType.ONLINE

        classifier.classify(ups_status="OB DISCHRG", input_voltage=230)
        assert classifier.state == EventType.BLACKOUT_TEST
