# UPS telemetry JSONL

All useful UPS telemetry lives in one chronological file:

```text
~/.config/ups-battery-monitor/events/telemetry.jsonl
```

Every line is an independent sample with exactly eight fields:

```json
{"at":"2026-08-21T14:37:44Z","battery_v":13.6,"battery_pct":82,"runtime_s":900,"load_pct":19,"input_v":0,"output_v":230,"status":"OB DISCHRG"}
```

The monitor writes samples while the UPS is discharging or recharging. It stops
after the UPS reports ordinary online operation at 100% charge. Blackout and
recharge intervals are reconstructed from `status`, `battery_pct`, and `at`;
the file contains no headers, IDs, hashes, sequence numbers, gaps, summaries,
or sidecars.

The one-time converter merges legacy event files into this stream. Fields that
the old journal did not record are written as `null`; values are never invented.
Run it only while the monitor is stopped:

```text
python3 scripts/migrate-jsonl-remove-hashes.py /path/to/events
python3 scripts/migrate-jsonl-remove-hashes.py /path/to/events --apply
```
