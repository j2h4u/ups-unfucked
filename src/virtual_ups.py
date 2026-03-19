"""Virtual UPS module for tmpfs-based atomic metric writing and NUT format compliance.

Phase 3 infrastructure: Writes metrics to /run/ups-battery-monitor/ups-virtual.dev in NUT format
without SSD wear. Enables transparent data source switching for monitoring tools
(upsmon, Grafana) by providing a virtual UPS device that reports calculated values.

Key responsibilities:
- write_virtual_ups_dev(): Atomic tmpfs write with fsync safety
- compute_ups_status_override(): Status override logic for LB flag and event types
"""

import logging
from pathlib import Path
from typing import Any, Dict
from src.event_classifier import EventType
from src.model import atomic_write

logger = logging.getLogger('ups-battery-monitor')


def write_virtual_ups_dev(metrics: Dict[str, Any], ups_name: str = "cyberpower") -> None:
    """
    Atomically write virtual UPS metrics to tmpfs in NUT format.

    Writes to /run/ups-battery-monitor/ups-virtual.dev using atomic pattern
    (tempfile + fsync + rename) to prevent partial writes on crash or power loss.
    Uses tmpfs (/run) for safety and performance (no SSD wear).

    Args:
        metrics: Dict of {field_name: value} to write to virtual UPS
                 Expected keys: battery.voltage, battery.charge, battery.runtime,
                               ups.load, ups.status, input.voltage, etc.
        ups_name: UPS device name for NUT format (default: "cyberpower")

    Raises:
        IOError: If write, fsync, or atomic rename fails
        OSError: If /run/ups-battery-monitor is unavailable or permissions insufficient

    Logging:
        - Success: "Virtual UPS metrics written at {timestamp}"
        - Error: "Failed to write virtual UPS metrics: {e}"
    """
    virtual_ups_path = Path("/run/ups-battery-monitor/ups-virtual.dev")

    try:
        # Guard against symlink attack: refuse to write through symlinks
        if virtual_ups_path.is_symlink():
            raise OSError(f"{virtual_ups_path} is a symlink, refusing to write")

        # Build dummy-ups format: key: value\n per line
        content = "".join(f"{key}: {value}\n" for key, value in metrics.items())

        atomic_write(virtual_ups_path, content)

    except Exception as e:
        logger.error(f"Failed to write virtual UPS metrics: {e}")
        raise


# Hard safety floor: if runtime < 2 min, ALWAYS set LB regardless of event type (F41).
# Prevents deep test from draining battery to hardware cutoff without graceful shutdown.
SAFETY_LB_FLOOR_MINUTES = 2


def compute_ups_status_override(
    event_type: EventType,
    time_rem_minutes: float,
    shutdown_threshold_minutes: int
) -> str:
    """
    Compute UPS status override value for virtual UPS based on event and time remaining.

    Determines the correct ups.status value including LB (LOW_BATTERY) flag based on
    event classification and remaining runtime. This replaces unreliable firmware flags.

    Pattern from RESEARCH.md Phase 3:
    - ONLINE → "OL"
    - Any discharge + time_rem < 2 min → "OB DISCHRG LB" (F41 safety floor)
    - BLACKOUT_TEST → "OB DISCHRG" (no LB, allow calibration data collection)
    - BLACKOUT_REAL + time_rem >= threshold → "OB DISCHRG"
    - BLACKOUT_REAL + time_rem < threshold → "OB DISCHRG LB" (signal LOW_BATTERY to upsmon)

    Args:
        event_type: EventType from event_classifier (ONLINE, BLACKOUT_REAL, BLACKOUT_TEST)
        time_rem_minutes: Calculated runtime remaining (minutes)
        shutdown_threshold_minutes: Threshold below which to set LB flag (minutes)

    Returns:
        str: UPS status string suitable for NUT format
             Examples: "OL", "OB DISCHRG", "OB DISCHRG LB"

    Note:
        LB flag signals to upsmon that LOW_BATTERY condition is detected;
        upsmon will execute SHUTDOWNCMD when LB flag is present.
        Threshold uses < not <= (time_rem exactly at threshold does not trigger LB).
    """
    if event_type == EventType.ONLINE:
        return "OL"
    # F41: Safety floor — any discharge state with <2 min runtime gets LB
    if time_rem_minutes < SAFETY_LB_FLOOR_MINUTES:
        return "OB DISCHRG LB"
    if event_type == EventType.BLACKOUT_TEST:
        return "OB DISCHRG"
    if event_type == EventType.BLACKOUT_REAL:
        if time_rem_minutes < shutdown_threshold_minutes:
            return "OB DISCHRG LB"
        return "OB DISCHRG"
    # Default to online if unknown event type
    return "OL"
