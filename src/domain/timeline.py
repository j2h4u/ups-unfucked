"""Canonical monotonic duration, coverage, and gap calculations."""

from dataclasses import dataclass
from math import isfinite

from src.domain.values import PhysicalObservation


@dataclass(frozen=True, slots=True)
class TimelineSummary:
    duration_s: float
    accepted_duration_s: float
    coverage_ratio: float
    max_gap_s: float
    non_increasing_edge_count: int
    reboot_gap_observed: bool


def summarize_timeline(
    observations: tuple[PhysicalObservation, ...],
    max_accepted_gap_s: float,
) -> TimelineSummary:
    """Summarize one interval using only finite, increasing, same-boot edges."""
    if len(observations) < 2:
        return TimelineSummary(0.0, 0.0, 0.0, 0.0, 0, False)

    accepted_duration_s = 0.0
    max_gap_s = 0.0
    non_increasing_edges = 0
    reboot_gap_observed = False
    for left, right in zip(observations, observations[1:], strict=False):
        if left.boot_id != right.boot_id:
            reboot_gap_observed = True
            continue
        delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
        if not isfinite(delta_s) or delta_s <= 0.0:
            non_increasing_edges += 1
            continue
        max_gap_s = max(max_gap_s, delta_s)
        if delta_s <= max_accepted_gap_s:
            accepted_duration_s += delta_s

    same_boot = observations[0].boot_id == observations[-1].boot_id and not reboot_gap_observed
    span_s = (observations[-1].monotonic_ns - observations[0].monotonic_ns) / 1_000_000_000
    duration_s = span_s if same_boot and isfinite(span_s) and span_s > 0.0 else 0.0
    coverage_ratio = accepted_duration_s / duration_s if duration_s > 0.0 else 0.0
    return TimelineSummary(
        duration_s=duration_s,
        accepted_duration_s=accepted_duration_s,
        coverage_ratio=min(1.0, coverage_ratio),
        max_gap_s=max_gap_s,
        non_increasing_edge_count=non_increasing_edges,
        reboot_gap_observed=reboot_gap_observed,
    )
