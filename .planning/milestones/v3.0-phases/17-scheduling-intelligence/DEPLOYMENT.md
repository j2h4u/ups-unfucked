# Phase 17 Deployment Checklist

## Pre-Deployment

- [ ] All tests pass: `pytest tests/test_scheduler.py tests/test_dispatch.py tests/test_config.py -v`
- [ ] Code review: scheduler decision engine, dispatch logic, configuration schema
- [ ] Verify config.toml [scheduling] section has all required parameters
- [ ] Review grid_stability_cooldown_hours setting for your grid conditions:
  - Use default 4.0h if grid is stable (rare blackouts)
  - Set to 0.0 if grid has frequent short blackouts (1.5–3 min) — per user feedback

## Deployment Steps

1. **Deploy daemon code:**
   ```bash
   git pull
   pip install -r requirements.txt  # No new dependencies
   sudo systemctl restart ups-battery-monitor
   ```

2. **Verify daemon startup:**
   ```bash
   journalctl -u ups-battery-monitor --since "1 min ago" | grep -i "scheduling\|phase.*17"
   ```
   Should show "Phase 17 scheduling initialized" with parameter values

3. **Disable legacy systemd timers:**
   **IMPORTANT: This is a MANUAL step, not automated by daemon**

   ```bash
   sudo systemctl mask ups-test-quick.timer
   sudo systemctl mask ups-test-deep.timer
   ```

   Verify:
   ```bash
   sudo systemctl status ups-test-quick.timer ups-test-deep.timer
   ```

   Should show `masked` status (not `inactive` or `enabled`)

4. **Stop any running legacy timers:**
   ```bash
   sudo systemctl stop ups-test-quick.timer ups-test-deep.timer
   ```

5. **Verify daemon scheduling is working:**
   - Wait until next scheduler evaluation time (default 08:00 UTC)
   - Check logs: `journalctl -u ups-battery-monitor --since "1 hour ago" | grep scheduler_decision`
   - Should see one "Scheduler decision" log entry per day

## Post-Deployment Verification

### Scheduler Decision Logging

After daemon startup, check that scheduling decisions are logged daily:

```bash
# View today's scheduling decisions
journalctl -u ups-battery-monitor --since "today" | grep scheduler_decision

# Expected output (at scheduler_eval_hour_utc):
# scheduler_decision: action=propose_test|defer_test|block_test reason_code=...
```

### Grid Stability Cooldown Behavior

If `grid_stability_cooldown_hours=0` (grid stability disabled):
- Daemon should NOT defer test due to recent blackouts
- Example: even if blackout 30min ago, if sulfation warrants, test proposed

If `grid_stability_cooldown_hours=4.0` (enabled):
- Daemon defers test if blackout within 4 hours
- Must wait 4+ hours after last blackout before test can be proposed

Test by creating a natural blackout (kill power to UPS) and check logs.

### Blackout Credit Tracking

After natural discharge ≥90% DoD:
- Check logs: should see `blackout_credit_granted` event
- Check health.json: should have `blackout_credit.active=true` and `credit_expires` timestamp
- Scheduler should defer test during credit period

### Rate Limiting

Verify scheduler enforces 1 test per week:
- After test dispatched, next test should be deferred with reason "rate_limit_Xd_remaining"
- Timestamp in logs should reflect ≥7 days to next eligible test

### Precondition Blocking

If preconditions prevent dispatch (SoC <95%, UPS on battery, recent glitches):
- Check logs for `test_precondition_blocked` events
- Should show reason: "SoC_below_95_percent", "UPS_on_battery", etc.

## Troubleshooting

### Daemon won't start: "Configuration errors"
- Check config.toml [scheduling] section for invalid values
- grid_stability_cooldown_hours must be ≥0
- soh_floor_threshold must be in [0.0, 1.0]
- Run: `python3 -c "from src.monitor_config import load_config; load_config()"` to see full error

### No scheduler_decision logs appearing
- Check if current time matches scheduler_eval_hour_utc (default 8=08:00 UTC)
- Check if scheduler_eval_hour_utc is set correctly: `grep scheduler_eval_hour_utc config.toml`
- Verify daemon is running: `systemctl status ups-battery-monitor`

### Test never proposed even when sulfation high
- Check grid_stability_cooldown_hours: if recent blackout <cooldown hours, test deferred
- Check SoH: if <60%, test blocked (hard floor)
- Check last test timestamp: if <7 days ago, rate limited
- Check ROI: if <0.2 and cycle_budget >20, deferred (marginal benefit)
- Check all constraints with: `journalctl -u ups-battery-monitor --since "1 day ago" | grep scheduler_decision`

### Legacy timers still active
- Verify masked: `systemctl status ups-test-deep.timer` should show "masked"
- If showing "enabled" or "active", run: `sudo systemctl mask ups-test-*.timer`
- Check: `ls -la /etc/systemd/system/ups-test-*.timer` should not show symlinks

## Rollback Plan

If Phase 17 scheduling causes issues:

1. **Restore to Phase 16 (observer mode):**
   ```bash
   git checkout origin/phase-16-persistence-observability  # Or previous commit
   pip install -r requirements.txt
   sudo systemctl restart ups-battery-monitor
   ```

2. **Re-enable legacy timers:**
   ```bash
   sudo systemctl unmask ups-test-quick.timer ups-test-deep.timer
   sudo systemctl start ups-test-quick.timer ups-test-deep.timer
   ```

3. **Review Phase 17 logs:**
   ```bash
   journalctl -u ups-battery-monitor --since "24 hours ago" | grep -E "error|failed|exception"
   ```

## Production Notes

- Phase 17 daemon is autonomous: no user intervention needed for daily scheduling
- All scheduling decisions logged to journald for audit and refinement
- Configuration tunable via config.toml [scheduling] section
- No web UI or advisory mode: daemon acts directly when decision made
- Natural blackouts provide free desulfation (skip scheduled test with credit)
- Conservative bias: when ROI marginal, defer test (natural blackouts are free)

---

**Deployment date:** [FILL IN]
**Checked by:** [FILL IN]
**Grid stability cooldown setting:** [FILL IN: default 4.0 or custom]
**Legacy timers masked:** [FILL IN: YES/NO with date]
