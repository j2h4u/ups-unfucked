# Vulture whitelist — symbols intentionally exempt from the dead-code gate.
# Regenerate the raw list with: uv run vulture --make-whitelist
# Each entry is either a false positive (vulture can't see param/dynamic/test use)
# or a deliberately-kept symbol. Keep the rationale current. Reviewed 2026-06-02.

current_exponent  # FALSE POSITIVE: live parameter of calibrate_peukert() (calibration.py:15)
cumulative_on_battery_sec  # FALSE POSITIVE: dataclass field, accessed dynamically (types.py:32)
status  # FALSE POSITIVE: sd_notify(status) stub signature param (monitor.py:24)
_.is_collecting  # KEEP: read-only observability accessor for collector state
