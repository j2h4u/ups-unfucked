"""Read-only status renderer for MOTD login banners.

This is the reporting/CLI side of the package, not the daemon: it never starts a
poll loop or touches NUT. It loads the same state the daemon persists and emits a
flat ``key=value`` block so the MOTD bash modules need neither ``jq`` nor embedded
Python to read JSON.

Field sources are split by where the data lives:
  - model.json (via BatteryModel): SoH, replacement date, new-battery flags, and
    capacity convergence (computed canonically by ``get_convergence_status`` — the
    bash modules used to reimplement this).
  - the daemon's runtime health endpoint: sulfation score and next-test timestamp,
    which are per-poll scheduler outputs not persisted in model.json.

Invoke: ``python3 -m src.motd_status`` → one ``key=value`` line per field on stdout.
Absent values render as an empty string so the bash side can skip lines cleanly.
"""

import json
import os
from pathlib import Path

from src.model import BatteryModel

# Mirrors monitor_config.HEALTH_ENDPOINT_PATH. Defined here instead of imported
# because importing monitor_config runs `git describe` at module load (it computes
# DAEMON_VERSION eagerly) — too heavy for a banner that runs on every shell login.
DEFAULT_HEALTH_PATH = Path("/run/ups-battery-monitor/ups-health.json")


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


def render_motd(model_path: Path | None = None, health_path: Path | None = None) -> str:
    """Build the ``key=value`` block consumed by the MOTD modules.

    Args:
        model_path: Path to model.json. Defaults to BatteryModel's standard location.
        health_path: Path to the health endpoint. Defaults to DEFAULT_HEALTH_PATH.

    Returns:
        Newline-joined ``key=value`` lines. Absent values render as empty strings.
    """
    model = BatteryModel(model_path)
    state = model.state
    convergence = model.get_convergence_status()
    health = _load_health(health_path or DEFAULT_HEALTH_PATH)

    latest_ah = convergence.latest_ah

    fields = {
        # --- Battery health (model.json) ---
        "soh_pct": _fraction_to_pct(state.get("soh")),
        "replacement_due": model.get_replacement_due() or "",
        "new_battery_detected": "true" if state.get("new_battery_detected") else "false",
        "new_battery_timestamp": state.get("new_battery_detected_timestamp") or "",
        # --- Capacity estimation (model.json, computed by get_convergence_status) ---
        "capacity_measured_ah": "" if latest_ah is None else str(latest_ah),
        "capacity_rated_ah": str(convergence.rated_ah),
        "capacity_samples": str(convergence.sample_count),
        "capacity_status": _capacity_status(convergence.sample_count, convergence.converged),
        "capacity_confidence_pct": str(round(convergence.confidence_percent)),
        # --- Sulfation / scheduling (runtime health endpoint) ---
        "sulfation_pct": _fraction_to_pct(health.get("sulfation_score")),
        "next_test_timestamp": health.get("next_test_timestamp") or "",
    }
    return "\n".join(f"{key}={value}" for key, value in fields.items())


def main() -> None:
    """Print the MOTD status block to stdout.

    Paths default to the standard locations; UPS_MODEL_PATH / UPS_HEALTH_PATH
    override them (used by the MOTD scripts' local-test path and by tests).
    """
    model_env = os.environ.get("UPS_MODEL_PATH")
    health_env = os.environ.get("UPS_HEALTH_PATH")
    print(
        render_motd(
            model_path=Path(model_env) if model_env else None,
            health_path=Path(health_env) if health_env else None,
        )
    )


if __name__ == "__main__":
    main()
