"""Read-only status renderer for MOTD login banners.

This is the reporting/CLI side of the package, not the daemon: it never starts a
poll loop or touches NUT. It loads the same state the daemon persists and emits a
flat ``key=value`` block so the MOTD bash modules need neither ``jq`` nor embedded
Python to read JSON.

Field sources are split by where the data lives:
  - model.json (via BatteryModel): SoH, replacement date, new-battery flags, and
    capacity convergence (computed canonically by ``get_convergence_status`` — the
    bash modules used to reimplement this).
  - the daemon's runtime health endpoint: next-test timestamp,
    which is a per-poll scheduler output not persisted in model.json.

Invoke: ``python3 -m src.motd_status`` → one ``key=value`` line per field on stdout.
Absent values render as an empty string so the bash side can skip lines cleanly.
"""

import json
import os
from pathlib import Path

from src.model import BatteryModel


def _load_health(health_path: Path) -> dict:
    """Read the daemon's health endpoint JSON. Missing or invalid → empty dict."""
    try:
        with open(health_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _fraction_to_pct(value: float | None) -> str:
    """Render a [0.0, 1.0] fraction as an integer-percent string, or '' if absent."""
    return str(round(value * 100)) if value is not None else ""


def _capacity_status(sample_count: int, converged: bool) -> str:
    """'locked' once converged, 'measuring' while sampling, 'unknown' with no data."""
    if sample_count <= 0:
        return "unknown"
    return "locked" if converged else "measuring"


def _health_bool(value: object) -> str:
    """Render an optional health boolean without treating missing data as healthy."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _health_text(value: object, *, limit: int = 256) -> str:
    """Keep health values safe for the flat key=value MOTD protocol."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("=", " ").split())[:limit]


def _health_count(value: object) -> str:
    """Render a non-negative counter, or empty when the endpoint did not provide one."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return ""


def render_motd(*, model_path: Path, health_path: Path) -> str:
    """Build the ``key=value`` block consumed by the MOTD modules.

    Args:
        model_path: Explicit path to model.json.
        health_path: Explicit path to the health endpoint.

    Returns:
        Newline-joined ``key=value`` lines. Absent values render as empty strings.
    """
    health = _load_health(Path(health_path))
    # Source soh_alert_threshold from the daemon's health endpoint so the MOTD replacement
    # date matches the daemon for any configured value; 0.80 only when no daemon wrote it.
    model = BatteryModel(Path(model_path), soh_threshold=health.get("soh_alert_threshold", 0.80))
    state = model.state
    convergence = model.get_convergence_status()

    latest_ah = convergence.latest_ah

    fields = {
        # --- Battery health (model.json) ---
        "soh_pct": _fraction_to_pct(state.get("soh")),
        "replacement_due": model.compute_replacement_due() or "",
        "new_battery_detected": "true" if state.get("new_battery_detected") else "false",
        "new_battery_timestamp": state.get("new_battery_detected_timestamp") or "",
        # --- Capacity estimation (model.json, computed by get_convergence_status) ---
        "capacity_measured_ah": "" if latest_ah is None else str(latest_ah),
        "capacity_rated_ah": str(convergence.rated_ah),
        "capacity_samples": str(convergence.sample_count),
        "capacity_status": _capacity_status(convergence.sample_count, convergence.converged),
        "capacity_confidence_pct": str(round(convergence.confidence_percent)),
        # --- Durable discharge journal (runtime health endpoint) ---
        # These operational fields are deliberately separate from the authoritative
        # model capacity/SoH fields above.  A partial/recovered event is evidence of
        # observed runtime, not a capacity or SoH sample.
        "journal_healthy": _health_bool(health.get("journal_healthy")),
        "active_event_id": _health_text(health.get("active_event_id")),
        "journal_last_synced_seq": _health_count(health.get("journal_last_synced_seq")),
        "journal_last_error": _health_text(health.get("journal_last_error")),
        "pending_replay": _health_bool(health.get("pending_replay")),
        "recovered_partial_events": _health_count(health.get("recovered_partial_events")),
        "capacity_evidence": "authoritative_model_only",
        "operational_evidence_note": "partial_or_recovered_events_excluded_from_capacity_soh",
        # --- Scheduling (runtime health endpoint) ---
        "next_test_timestamp": health.get("next_test_timestamp") or "",
    }
    return "\n".join(f"{key}={value}" for key, value in fields.items())


def main() -> None:
    """Print the MOTD status block to stdout.

    The CLI is the production composition root and is therefore the only place
    that supplies the standard locations. UPS_MODEL_PATH / UPS_HEALTH_PATH
    override them for local invocations and tests.
    """
    model_env = os.environ.get("UPS_MODEL_PATH")
    health_env = os.environ.get("UPS_HEALTH_PATH")
    model_path = (
        Path(model_env)
        if model_env
        else Path.home() / ".config" / "ups-battery-monitor" / "model.json"
    )
    health_path = (
        Path(health_env) if health_env else Path("/run/ups-battery-monitor/ups-health.json")
    )
    print(
        render_motd(
            model_path=model_path,
            health_path=health_path,
        )
    )


if __name__ == "__main__":
    main()
