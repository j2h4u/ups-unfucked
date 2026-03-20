# Deferred Items — Phase 23

## Out-of-scope issues discovered during plan 23-01 execution

### test_per_poll_writes_during_blackout failure
- **File:** tests/test_monitor.py
- **Error:** `TypeError: tracking_write() takes 0 positional arguments but 3 were given`
- **Cause:** Introduced by parallel agent 23-02 which added `output_path` DI parameter to `write_virtual_ups_dev()` — the test mock signature was not updated
- **Scope:** Out of scope for 23-01 (touches test_monitor.py, not files modified by this plan)
- **Action:** Needs fix in 23-02 or subsequent plan
