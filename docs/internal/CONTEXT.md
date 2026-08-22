# Current contributor context

Keep this project small and safety-first. The runtime is one daemon between a physical UPS and NUT:

```text
physical NUT -> one-second monitor -> safe virtual UPS -> upsmon
                              `-> events/telemetry.jsonl
```

The monitor publishes the virtual UPS from the current physical observation before recording or
diagnostic work. It does not invoke host shutdown. Storage failure may lose optional telemetry, but
must never suppress or delay the safety publication.

## Durable data

The only current event file is `~/.config/ups-battery-monitor/events/telemetry.jsonl`.
It is append-only and contains blackout, test, and recharge samples, including up to 120 seconds of
pre/post context when available. Keep raw lines unchanged. `scripts/blackout-history.py` reads this
file and prints a compact history; it is not a separate persistence layer.

`events/history.jsonl` is the compact derived journal. In addition to episode summaries, it records
the exact before/after values and reason whenever natural-blackout feedback changes `model.json`.

## Automatic quick self-test

When the physical UPS is OL100 and telemetry shows no blackout or calibration/self-test for 14 days,
the daemon may run one quick self-test. The result is operational evidence only. Never use it as a
natural blackout or as proof of capacity or state of health.

## Contributor rules

- Preserve the one-second safety path and the read-only NUT boundary except for the guarded quick test.
- Keep persistence limited to the one telemetry stream.
- Keep user-facing output concise and distinguish observed telemetry from interpretation.
- Use `just check` before handing off a release candidate.
