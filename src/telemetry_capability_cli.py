"""Composition root for the read-only Slice-0 capability baseline CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.adapters.telemetry_capability_baseline import (
    ARTIFACT_FILENAME,
    NUTEndpoint,
    TelemetryCapabilityError,
    record_baseline,
    verify_baseline,
)
from src.monitor_config import ConfigError, load_config
from src.nut_client import NUTClient


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse arguments for recording or verifying one physical UPS baseline."""
    parser = argparse.ArgumentParser(
        description="Record a 60-reply read-only physical NUT telemetry capability baseline"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            f"destination path whose filename must be {ARTIFACT_FILENAME}; "
            f"owner-only no-clobber (default: ~/.config/ups-battery-monitor/{ARTIFACT_FILENAME})"
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="validate the existing artifact and match its identity with one ordinary LIST VAR reply",
    )
    return parser.parse_args(args)


def main(args: Sequence[str] | None = None) -> int:
    """Run the baseline operation and return its process exit code."""
    arguments = parse_args(args)
    try:
        config = load_config()
        if config.ups_name.endswith("-virtual"):
            raise TelemetryCapabilityError(
                "configured UPS is virtual; baseline requires the physical UPS"
            )
        destination = arguments.output or config.model_dir / ARTIFACT_FILENAME
        client = NUTClient(
            host=config.nut_host,
            port=config.nut_port,
            timeout=config.nut_timeout,
            ups_name=config.ups_name,
        )
        if arguments.verify:
            verify_baseline(
                destination,
                client,
                host=config.nut_host,
                port=config.nut_port,
                ups_name=config.ups_name,
            )
            print(f"verified {destination}")
        else:
            record_baseline(
                client,
                destination,
                endpoint=NUTEndpoint(
                    host=config.nut_host,
                    port=config.nut_port,
                    ups_name=config.ups_name,
                ),
            )
            print(f"recorded {destination}")
    except (ConfigError, OSError, TelemetryCapabilityError, ValueError) as exc:
        print(f"telemetry baseline refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
