# Telemetry JSONL

The monitor writes one append-only stream:

`~/.config/ups-battery-monitor/events/telemetry.jsonl`

Each line is a JSON object describing one physical UPS observation. The stream may contain ordinary
online samples, blackout samples, quick-test samples, and recharge samples. The recorder keeps up to
120 seconds of context before and after a detected event when samples are available.

This file is the raw record. Values that NUT did not provide are `null`; derived output and CLI
summaries must not overwrite or masquerade as raw evidence.

The executable, commented field contract is `TelemetrySample` in
`src/adapters/minimal_event_file.py`. Compact per-episode response fields are defined by
`EpisodeHistoryRecord` in `src/adapters/battery_history.py`; they remain derived facts and never
claim measured capacity, internal resistance, or state of health.

The response baseline is the median voltage from the 30 seconds before an episode, provided those
samples span at least 10 seconds. `early_v` is the minimum reported voltage in the first 15 seconds;
the window accommodates coarse voltage steps that do not necessarily coincide with the status
transition.
