"""Canonical timestamps for the service's one-second observation cadence."""

from datetime import datetime, timezone


def utc_second(value: str) -> str:
    """Normalize an aware timestamp to a whole UTC second."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
