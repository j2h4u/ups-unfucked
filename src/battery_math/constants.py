"""Cross-cutting constants shared between modules that cannot import each other."""

# Discharges shorter than 5 min have terrible signal-to-noise (incident 2026-03-16)
MIN_DISCHARGE_DURATION_SEC = 300

# CyberPower UT850EG hardware spec — single source of truth for defaults across model/math/config
RATED_CAPACITY_AH = 7.2
NOMINAL_POWER_WATTS = 425.0
