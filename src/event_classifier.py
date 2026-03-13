"""
Event classifier: distinguish real blackout from battery test.

Physical invariant: use input.voltage presence to classify events.
- input.voltage ≈ 0V → mains absent → real blackout
- input.voltage ≈ 230V → mains present → battery test (UPS on battery but AC powered)

State machine (from CONTEXT.md § Различение блекаута и теста батареи):

    State: ONLINE
      ├─ status.OL ∧ input.voltage ≈ 230V → stay in ONLINE
      └─ status.OB ∧ input.voltage ≈ 230V → BLACKOUT_TEST

    State: BLACKOUT_TEST
      ├─ input.voltage ≈ 230V → stay in BLACKOUT_TEST (collect calibration data)
      ├─ input.voltage ≈ 0V → ERROR (shouldn't happen mid-test)
      └─ status.OL ∧ input.voltage ≈ 230V → ONLINE (test complete)

    State: BLACKOUT_REAL
      ├─ input.voltage ≈ 0V → stay in BLACKOUT_REAL (calculate time_rem)
      ├─ input.voltage ≈ 230V → ERROR (shouldn't happen mid-blackout)
      └─ status.OL ∧ input.voltage ≈ 230V → ONLINE (power restored)
"""

from enum import Enum
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Event classification types."""

    ONLINE = "OL"
    BLACKOUT_REAL = "OB_BLACKOUT"
    BLACKOUT_TEST = "OB_TEST"


class EventClassifier:
    """
    Track UPS events (blackout vs test) using physical invariant: input.voltage.

    State machine detects real blackouts (mains absent) vs battery tests
    (UPS on battery but AC mains still present).

    Thresholds (from CONTEXT.md § Различение блекаута и теста батареи):
    - input_voltage > 100V → mains present
    - input_voltage < 50V → mains absent
    - 50V-100V → undefined (log warning, treat as absent)
    """

    def __init__(self):
        """Initialize state machine to ONLINE."""
        self.state = EventType.ONLINE
        self.transition_occurred = False

    def classify(self, ups_status: str, input_voltage: float) -> EventType:
        """
        Classify current event from NUT status and voltage.

        Thresholds (from CONTEXT.md):
        - input_voltage > 100V → mains present
        - input_voltage < 50V → mains absent
        - 50V-100V → undefined (log warning, treat as absent)

        Args:
            ups_status: From NUT (e.g., "OL", "OB DISCHRG", "OB DISCHRG LB")
            input_voltage: From NUT in volts (230V when mains on, ~0V when off)

        Returns:
            EventType: Current state after transition
        """
        # Determine mains presence using hysteresis thresholds
        if input_voltage > 100.0:
            mains_present = True
        elif input_voltage < 50.0:
            mains_present = False
        else:
            # Undefined range (50-100V): log warning, treat as absent
            logger.warning(
                f"input.voltage in undefined range: {input_voltage}V; treating as absent"
            )
            mains_present = False

        # Determine if on battery
        on_battery = "OB" in ups_status

        # State transition logic
        if not on_battery:
            new_state = EventType.ONLINE
        elif on_battery and mains_present:
            new_state = EventType.BLACKOUT_TEST
        else:  # on_battery and not mains_present
            new_state = EventType.BLACKOUT_REAL

        # Detect state change
        self.transition_occurred = self.state != new_state
        self.state = new_state
        return new_state
