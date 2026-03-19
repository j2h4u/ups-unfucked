"""Cross-cutting constants shared between modules that cannot import each other."""

# Discharges shorter than 5 min have terrible signal-to-noise (incident 2026-03-16)
MIN_DISCHARGE_DURATION_SEC = 300
