"""Virtual UPS module for tmpfs-based atomic metric writing and NUT format compliance.

Writes metrics to /run/ups-battery-monitor/ups-virtual.dev in NUT format without SSD wear.
Enables transparent data source switching for monitoring tools (upsmon, Grafana) by providing
a virtual UPS device that reports calculated values.

Key responsibilities:
- write_virtual_ups_dev(): Atomic tmpfs write with fsync safety
- compute_ups_status_override(): Status override logic for LB flag and event types
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional
from src.event_classifier import EventType
from src.model import atomic_write

logger = logging.getLogger('ups-battery-monitor')


def write_virtual_ups_dev(
    metrics: Dict[str, Any],
    ups_name: str = "cyberpower",
    output_path: Optional[Path] = None,
) -> None:
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
        output_path: Optional override for output file path. Defaults to
                     /run/ups-battery-monitor/ups-virtual.dev. Used by tests
                     to write to tmp_path instead of production tmpfs.

    Raises:
        IOError: If write, fsync, or atomic rename fails
        OSError: If /run/ups-battery-monitor is unavailable or permissions insufficient

    Logging:
        - Success: "Virtual UPS metrics written at {timestamp}"
        - Error: "Failed to write virtual UPS metrics: {e}"
    """
    virtual_ups_path = output_path or Path("/run/ups-battery-monitor/ups-virtual.dev")

    # Guard against symlink attack: refuse to write through symlinks
    if virtual_ups_path.is_symlink():
        raise OSError(f"{virtual_ups_path} is a symlink, refusing to write")

    # Sanitize keys and values: strip newlines/colons from keys to prevent NUT field injection
    def _safe_key(k: str) -> str:
        return str(k).replace('\n', '').replace('\r', '').replace(':', '.')

    sanitized = {_safe_key(k): str(v).replace('\n', '').replace('\r', '') for k, v in metrics.items()}
    content = "".join(f"{key}: {value}\n" for key, value in sanitized.items())

    atomic_write(virtual_ups_path, content, mode=0o644)


# NUT protocol wire-format status strings.
# External consumers: dummy-ups driver, upsmon, nut_exporter, Grafana Alloy.
# Changing these values will silently break the monitoring pipeline.
NUT_STATUS_ONLINE = "OL"
NUT_STATUS_DISCHARGING = "OB DISCHRG"
NUT_STATUS_LOW_BATTERY = "OB DISCHRG LB"

# Hard safety floor: if runtime < 2 min, ALWAYS set LB regardless of event type.
# Prevents deep test from draining battery to hardware cutoff without graceful shutdown.
SAFETY_LB_FLOOR_MINUTES = 2  # Triggers upsmon FSD (forced shutdown) via LB flag


def compute_ups_status_override(
    event_type: EventType,
    time_rem_minutes: float,
    shutdown_threshold_minutes: int
) -> str:
    """
    Compute UPS status override value for virtual UPS based on event and time remaining.

    Determines the correct ups.status value including LB (LOW_BATTERY) flag based on
    event classification and remaining runtime. This replaces unreliable firmware flags.

    Decision logic:
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
        return NUT_STATUS_ONLINE
    # Safety floor — any discharge state with <2 min runtime gets LB
    if time_rem_minutes < SAFETY_LB_FLOOR_MINUTES:
        return NUT_STATUS_LOW_BATTERY
    if event_type == EventType.BLACKOUT_TEST:
        return NUT_STATUS_DISCHARGING
    if event_type == EventType.BLACKOUT_REAL:
        if time_rem_minutes < shutdown_threshold_minutes:
            return NUT_STATUS_LOW_BATTERY
        return NUT_STATUS_DISCHARGING
    return NUT_STATUS_ONLINE
