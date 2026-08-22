# Telemetry JSONL

The monitor writes one append-only stream:

`~/.config/ups-battery-monitor/events/telemetry.jsonl`

Each line is a JSON object describing one physical UPS observation. The stream may contain ordinary
online samples, blackout samples, quick-test samples, and recharge samples. The recorder keeps up to
120 seconds of context before and after a detected event when samples are available.

This file is the raw record. Values that NUT did not provide are `null`; derived output and CLI
summaries must not overwrite or masquerade as raw evidence.
