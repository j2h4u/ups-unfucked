# Vulture whitelist — symbols intentionally exempt from the dead-code gate.
# Regenerate the raw list with: uv run vulture --make-whitelist
# Each entry is either a false positive (vulture can't see param/dynamic/test use)
# or a deliberately-kept symbol. Keep the rationale current. Reviewed 2026-06-02.

current_exponent  # FALSE POSITIVE: live parameter of calibrate_peukert() (calibration.py:15)
status  # FALSE POSITIVE: sd_notify(status) stub signature param (monitor.py:24)
_.is_collecting  # KEEP: read-only observability accessor for collector state
_.is_measuring  # KEEP: read-only observability accessor for sag state
_.reboot_gaps  # KEEP: journal projection view of explicit reboot-gap evidence
_.observed_duration  # KEEP: journal duration API covered by one-sample/gapped recovery contract
torn_tail_recovered  # KEEP: public JournalProjection recovery observability field

# FALSE POSITIVES: ModelState TypedDict fields (model.py). Accessed via state["<key>"]
# string keys and reflectively through KNOWN_STATE_KEYS = frozenset(ModelState.__annotations__),
# so vulture sees the annotations as unused class variables. They are the persisted schema.
capacity_estimates
cumulative_on_battery_sec
battery_install_date
new_battery_detected
new_battery_detected_timestamp
last_upscmd_timestamp
last_upscmd_type
last_upscmd_status
battery_epoch_id  # KEEP: strict persisted battery-epoch schema key
