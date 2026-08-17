"""Health-only CLI/MOTD projection for the running monitor."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

MAX_HEALTH_READ_BYTES = 64 * 1024
MAX_FIELD_TEXT = 512


def render_motd(*, health_path: Path) -> str:
    """Render bounded key/value status without opening model or event history."""
    health = _load_health(Path(health_path))
    storage = _mapping(health.get("storage"))
    capture = _mapping(health.get("capture_queue"))
    report = _mapping(health.get("last_report"))
    report_lines = report.get("lines")
    if not isinstance(report_lines, list):
        report_lines = []
    decline = health.get("decline_evidence")
    if not isinstance(decline, list):
        decline = []
    decline_by_metric = {
        item.get("metric"): item
        for item in decline[:3]
        if isinstance(item, dict) and isinstance(item.get("metric"), str)
    }
    fields: tuple[tuple[str, object], ...] = (
        ("physical_status", health.get("physical_status")),
        ("virtual_status", health.get("virtual_status")),
        ("raw_lb_observed", health.get("raw_lb_observed")),
        ("virtual_lb", health.get("virtual_lb")),
        ("virtual_lb_source", health.get("virtual_lb_source")),
        ("event_class", health.get("event_class")),
        ("modeled_runtime_minutes", health.get("modeled_runtime_minutes")),
        ("model_scientific_fingerprint", health.get("model_scientific_fingerprint")),
        (
            "load_sag_coefficient_v_per_load_percent",
            health.get("load_sag_coefficient_v_per_load_percent"),
        ),
        (
            "load_sag_reference_load_percent",
            health.get("load_sag_reference_load_percent"),
        ),
        ("capture_available", capture.get("capture_available")),
        ("capture_maintenance_queued", capture.get("maintenance_queued")),
        ("capture_max_busy_time_s", capture.get("max_busy_time_s")),
        ("capture_oldest_queue_age_s", capture.get("oldest_queue_age_s")),
        ("queued_observations", storage.get("queued_observations")),
        ("active_event_phase", storage.get("active_phase")),
        ("durability_lag_s", storage.get("durability_lag_s")),
        ("storage_alarm", storage.get("alarm")),
        ("index_available", storage.get("index_available")),
        ("index_rebuild_in_progress", storage.get("rebuild_in_progress")),
        ("index_rebuild_stalled", storage.get("rebuild_stalled")),
        (
            "consumed_evidence_budget_remaining",
            storage.get("consumed_step_budget_remaining"),
        ),
        ("last_blackout_id", report.get("blackout_id")),
        ("last_blackout_disposition", report.get("disposition")),
        ("last_blackout_report", " | ".join(_text(line) for line in report_lines[:8])),
        (
            "decline_load_sag_trend",
            _mapping(decline_by_metric.get("load_sag_trend")).get("verdict"),
        ),
        (
            "decline_firmware_lb_reserve_proxy",
            _mapping(decline_by_metric.get("firmware_lb_reserve_proxy")).get("verdict"),
        ),
        (
            "decline_long_partial_curve",
            _mapping(decline_by_metric.get("long_partial_curve")).get("verdict"),
        ),
        ("bounded_error", health.get("bounded_error")),
        ("poll_error", _mapping(health.get("error_channels")).get("poll")),
        ("capture_error", _mapping(health.get("error_channels")).get("capture")),
        ("storage_error", _mapping(health.get("error_channels")).get("storage")),
        ("report_error", _mapping(health.get("error_channels")).get("report")),
        ("background_error", _mapping(health.get("error_channels")).get("background")),
    )
    return "\n".join(f"{key}={_text(value)}" for key, value in fields)


def main() -> None:
    """Write the bounded health projection to stdout."""
    configured = os.environ.get("UPS_HEALTH_PATH")
    health_path = (
        Path(configured) if configured else Path("/run/ups-battery-monitor/ups-health.json")
    )
    sys.stdout.write(render_motd(health_path=health_path) + "\n")


def _load_health(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_HEALTH_READ_BYTES:
            return {}
        raw = os.read(descriptor, MAX_HEALTH_READ_BYTES + 1)
        if len(raw) > MAX_HEALTH_READ_BYTES:
            return {}
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value if isinstance(value, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return " ".join(str(value).replace("=", " ").split())[:MAX_FIELD_TEXT]


if __name__ == "__main__":
    main()
