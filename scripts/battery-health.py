#!/usr/bin/env python3
"""Bounded operator report from the daemon-owned health projection."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.motd_status import render_motd

DEFAULT_HEALTH_PATH = Path("/run/ups-battery-monitor/ups-health.json")


def health_path() -> Path:
    """Resolve the read-only health projection without opening model state."""
    configured = os.environ.get("UPS_HEALTH_PATH")
    return DEFAULT_HEALTH_PATH if configured is None else Path(configured)


def render_report(path: Path) -> str:
    """Render the same bounded status contract used by MOTD."""
    return render_motd(health_path=path)


def main() -> None:
    sys.stdout.write(render_report(health_path()) + "\n")


if __name__ == "__main__":
    main()
