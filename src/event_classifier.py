"""Event classification state machine for blackout vs battery test detection (EVT-01)."""

import logging
from enum import Enum

logger = logging.getLogger(__name__)


# During battery test, UPS switches to battery but mains voltage stays (CyberPower shows ~220V).
# During real blackout, input voltage drops to 0V. 100V separates the two cases.
TEST_INPUT_VOLTAGE_THRESHOLD = 100


class EventType(Enum):
    """Event types detected during UPS state changes."""

    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"


# NUT status string → category for classification
# 'online': UPS on mains power
# 'battery': UPS discharging — need input voltage to distinguish real vs test
_STATUS_CATEGORY = {
    "OL":           "online",
    "OL CHRG":      "online",
    "OB DISCHRG":   "battery",
    "CAL DISCHRG":  "battery",
}


class EventClassifier:
    """
    State machine for classifying UPS events based on status and voltage.

    Status strings are mapped to categories via _STATUS_CATEGORY dict.
    Battery category is further split by input voltage:
    - 0V → BLACKOUT_REAL (mains lost)
    - ≥100V → BLACKOUT_TEST (mains present, intentional test)
    - 1–99V → BLACKOUT_REAL (undefined range, safe default)
    """

    def __init__(self):
        self.state = EventType.ONLINE
        self.transition_occurred = False

    def classify(self, ups_status: str, input_voltage: int) -> EventType:
        category = _STATUS_CATEGORY.get(ups_status)

        if category == "online":
            new_state = EventType.ONLINE
        elif category == "battery":
            if input_voltage >= TEST_INPUT_VOLTAGE_THRESHOLD:
                new_state = EventType.BLACKOUT_TEST
            else:
                if 0 < input_voltage < 100:
                    logger.warning(
                        f"Undefined voltage range {input_voltage}V during {ups_status}, "
                        f"treating as BLACKOUT_REAL"
                    )
                new_state = EventType.BLACKOUT_REAL
        else:
            logger.warning(f"Unknown UPS status: {ups_status}, keeping current state {self.state.name}")
            new_state = self.state

        self.transition_occurred = new_state != self.state
        if self.transition_occurred:
            logger.info(f"Event transition: {self.state.name} → {new_state.name}")

        self.state = new_state
        return new_state
