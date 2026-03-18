"""Event classification state machine for blackout vs battery test detection (EVT-01)."""

import logging
from enum import Enum

logger = logging.getLogger('ups-battery-monitor')


# During battery test, UPS switches to battery but mains voltage stays (CyberPower shows ~220V).
# During real blackout, input voltage drops to 0V. 100V separates the two cases.
TEST_INPUT_VOLTAGE_THRESHOLD = 100


class EventType(Enum):
    """Event types detected during UPS state changes."""

    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"


class EventClassifier:
    """
    State machine for classifying UPS events based on status and voltage.

    NUT status is a space-separated set of flags (e.g. "OB LB DISCHRG").
    Flag-based matching (F36): check for individual flags instead of exact string match.
    Battery category is further split by input voltage:
    - 0V → BLACKOUT_REAL (mains lost)
    - ≥100V → BLACKOUT_TEST (mains present, intentional test)
    - 1–99V → BLACKOUT_REAL (undefined range, safe default)
    """

    def __init__(self):
        self.state = EventType.ONLINE
        self.transition_occurred = False
        self.last_raw_status = ""

    def classify(self, ups_status: str, input_voltage: int) -> EventType:
        self.last_raw_status = ups_status

        # Flag-based matching: NUT status is space-separated flags (F36)
        # "OB LB DISCHRG", "OB DISCHRG", "CAL DISCHRG" all contain OB or CAL
        flags = ups_status.split()

        # F38: Unhandled NUT statuses (FSD, BYPASS, OFF, TRIM, BOOST) fall through
        # to category=None → "unknown → keep state". YAGNI for CyberPower UT850EG
        # which only produces OL, OL CHRG, OB DISCHRG, OB LB DISCHRG, CAL DISCHRG.
        # Future UPS models with AVR (TRIM/BOOST) or bypass mode may need expansion.
        if "OB" in flags or "CAL" in flags:
            category = "battery"
        elif "OL" in flags:
            category = "online"
        else:
            category = None

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
