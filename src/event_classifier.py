"""Event classification state machine for blackout vs battery test detection (EVT-01)."""

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Event types detected during UPS state changes."""

    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"


class EventClassifier:
    """
    State machine for classifying UPS events based on status and voltage.

    Distinguishes between:
    - ONLINE: UPS on mains power (status="OL")
    - BLACKOUT_REAL: UPS on battery, no mains (status="OB DISCHRG", input.voltage~0V)
    - BLACKOUT_TEST: Battery test with mains present (status="OB DISCHRG", input.voltage~230V)

    The voltage threshold is the only reliable way to distinguish real blackout from
    intentional battery test, since UPS firmware misreports state during calibration.
    """

    def __init__(self):
        """Initialize event classifier with default ONLINE state."""
        self.state = EventType.ONLINE
        self.transition_occurred = False

    def classify(self, ups_status: str, input_voltage: int) -> EventType:
        """
        Classify current event and detect state transitions.

        Args:
            ups_status: UPS status string (e.g., "OL", "OB DISCHRG")
            input_voltage: Input mains voltage in volts (e.g., 0, 230)

        Returns:
            EventType: Current event classification

        Side effects:
            - Updates self.state
            - Sets self.transition_occurred = True if state changed, False if unchanged
            - Logs transitions for debugging
        """
        # Determine new state based on UPS status and voltage
        if ups_status == "OL":
            # UPS on mains power
            new_state = EventType.ONLINE
        elif ups_status == "OB DISCHRG":
            # UPS on battery - distinguish via input voltage
            if input_voltage == 0:
                # No mains voltage detected - real blackout
                new_state = EventType.BLACKOUT_REAL
            elif input_voltage >= 100:
                # Mains voltage present - battery test scenario
                new_state = EventType.BLACKOUT_TEST
            else:
                # Undefined voltage range (50-100V) - treat as real blackout (safe)
                logger.warning(
                    f"Undefined voltage range {input_voltage}V during OB DISCHRG, "
                    f"treating as BLACKOUT_REAL"
                )
                new_state = EventType.BLACKOUT_REAL
        else:
            # Unknown status - stay in current state
            logger.warning(f"Unknown UPS status: {ups_status}, keeping current state {self.state.name}")
            new_state = self.state

        # Detect transition
        if new_state != self.state:
            logger.info(f"Event transition: {self.state.name} → {new_state.name}")
            self.transition_occurred = True
        else:
            self.transition_occurred = False

        # Update state
        self.state = new_state

        return new_state
