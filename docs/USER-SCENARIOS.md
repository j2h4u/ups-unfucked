# User Scenarios

The monitor observes and protects the UPS automatically out of the box. These scenarios describe
optional actions for users who want more control or a reviewed capacity measurement.

## Battery Health Report

The daemon records every discharge event operationally, while authoritative capacity/SoH updates
remain evidence-gated.

```bash
./scripts/battery-health.py
```

Shows: authoritative SoH/capacity state, LUT calibration coverage, journal health, open or
replay-pending events, operational partial/recovered-event count, replacement prediction, and
internal resistance trend. Operational partial/recovered events are explicitly excluded from
authoritative capacity and SoH samples.

Live metrics via NUT: `upsc cyberpower-virtual@localhost`

**When to worry:** SoH below 80%, replacement prediction within 3 months, or R_internal rising sharply.

---

## Interrupted shutdown or reboot

The daemon appends each accepted on-battery observation to
`~/.config/ups-battery-monitor/discharge-events-v1.jsonl` and synchronises it before exposing
the sample as durable. If shutdown interrupts collection, the next boot replays the journal.
When the UPS is still on battery, collection continues under the same event ID with an explicit
reboot gap. When power is online, the event closes at the last confirmed on-battery sample.
Unknown time across the gap is never integrated.

Check the result with:

```bash
./scripts/battery-health.py
upsc cyberpower-virtual@localhost | grep -E 'battery|ups.status'
```

An open, degraded, or replay-pending journal is an operational incident to investigate. It does
not disable the NUT low-battery/shutdown path. A recovered partial event is useful runtime
evidence only; it does not prove full duration, absolute capacity, SoH, or Peukert exponent.

## Rollback and re-upgrade

If a release must be rolled back, stop the daemon while the UPS is online and preserve
`discharge-events-v1.jsonl` and any recovery artifacts. The older daemon will not project new
journal-derived counters while rolled back, so its displayed counters may appear frozen. Do not
delete or manually merge the journal; re-upgrading lets the durable-journal implementation replay
it again.

## Controlled capacity test (written and supervised only)

Do not run an automatic hardware deep test. Do not treat a scheduler suggestion, a short NUT
self-test, or a natural partial blackout as a capacity measurement. A hardware capacity test is
considered only after durable capture is deployed, the written protocol is reviewed, and the
operator gives explicit approval immediately before the test.

The complete preconditions, NUT command/abort checks, independent observation, virtual rehearsal,
recharge, and evidence gate are in
[CONTROLLED-CAPACITY-TEST-PROTOCOL.md](CONTROLLED-CAPACITY-TEST-PROTOCOL.md). If any prerequisite
or abort path is uncertain, do not start the test.

---

## Battery Replacement and BaselineReset

When the battery is replaced, stop the daemon while the UPS is physically online and with no open
discharge event. Run the sanctioned `BaselineReset` operator transaction once; it creates a new
`battery_epoch_id` and starts fresh calibration. It is not an automatic deep test.

**When to replace:** SoH below 80% (MOTD alert), replacement predictor date approaching, or runtime consistently shorter than expected. The daemon auto-detects new batteries: if measured capacity jumps >10% after convergence, MOTD will show an alert prompting you to confirm.

### Steps

1. **Power off the UPS and replace the physical battery.** For CyberPower UT850EG: slide the front panel down, pull the battery tray out, swap the battery, reconnect terminals (red=positive first), slide tray back.

2. **Run BaselineReset while the service is stopped:**

   ```bash
   sudo systemctl stop ups-battery-monitor
   sudo python3 -m src.monitor --new-battery
   ```

   Then start the service normally:

   ```bash
   sudo systemctl start ups-battery-monitor
   ```

3. **Do not use a battery replacement as permission for an automatic deep test.** Follow the
   supervised protocol only if a capacity measurement is explicitly approved.

4. **Verify** after the first discharge event:

   ```bash
   ./scripts/battery-health.py
   ```

   SoH should be ~100%, Peukert back to default 1.2, cycle count 0.

### What BaselineReset resets

| Field | Reset to |
|-------|----------|
| SoH | 1.0 (100%) |
| SoH history | Fresh entry only |
| Peukert exponent | 1.2 (default) |
| IR coefficient and RLS estimators (ir_k, Peukert) | Default IR/Peukert values, P=1.0 (no confidence) |
| Physics parameters | Default physics, including Peukert, IR, and RLS |
| LUT | New standard VRLA curve plus anchor |
| Capacity estimates | Cleared |
| Measured capacity | Cleared |
| R_internal history | Cleared |
| Model-level discharge_events | Cleared |
| Battery epoch | Brand-new UUID |
| Cycle count | 0 |
| Battery install date | Today |

The append-only discharge journal remains untouched as historical raw evidence and is not replayed
into the new epoch. Operational `upscmd` audit metadata remains preserved. BaselineReset creates
exactly one fresh SoH=1 baseline entry; no old learned state is retained. The pre-reset model is
preserved in the existing backup for rollback.

---

## Configuration

All settings are in `~/.config/ups-battery-monitor/config.toml`:

```toml
# UPS device name in NUT (as configured in ups.conf)
ups_name = "cyberpower"

# Initiate shutdown when estimated runtime drops below this (minutes)
shutdown_minutes = 5

# Alert when battery health (SoH) drops below this (0.0-1.0)
soh_alert = 0.80
```

After editing: `sudo systemctl restart ups-battery-monitor`
