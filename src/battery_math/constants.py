"""Cross-cutting constants shared between modules that cannot import each other."""

# CyberPower UT850EG hardware spec — single source of truth for defaults across model/math/config
RATED_CAPACITY_AH = 7.2
NOMINAL_POWER_WATTS = 425.0
# VRLA 12V nominal battery voltage, single source of truth replacing the scattered magic 12.0.
NOMINAL_VOLTAGE = 12.0
