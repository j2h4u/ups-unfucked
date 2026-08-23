# Telemetry JSONL

The monitor writes one append-only raw stream at:

`~/.config/ups-battery-monitor/events/telemetry.jsonl`

Each line is exactly one JSON object with these eight keys:

| Key | Type | Meaning |
|---|---|---|
| `at` | UTC timestamp string | Observation time, canonical `Z` timestamp |
| `battery_v` | number or `null` | NUT `battery.voltage` |
| `battery_pct` | number or `null` | NUT `battery.charge` |
| `runtime_s` | number or `null` | NUT `battery.runtime` |
| `load_pct` | number or `null` | NUT `ups.load` |
| `input_v` | number or `null` | NUT `input.voltage` |
| `output_v` | number or `null` | NUT `output.voltage` |
| `status` | non-empty string | NUT `ups.status` |

The executable contract is `TelemetrySample` in `src/adapters/minimal_event_file.py`. Numeric values
preserve NUT readings; unavailable values are JSON `null`; no derived value, lifecycle ID, or
learning result belongs in this stream. The writer records outage/test/recharge evidence and up to
120 seconds of available context around an episode; it does not continuously append every online
poll.

`events/history.jsonl` is separate derived state containing compact episode summaries, IR
observations, and model-update receipts. CLI summaries must not overwrite or masquerade as raw
telemetry. `scripts/blackout-history.py` reads both files.
