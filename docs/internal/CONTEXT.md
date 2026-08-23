# Current contributor context

Keep the runtime small and safety-first:

```text
physical NUT -> one-second monitor -> virtual UPS publication -> upsmon
                                      `-> raw telemetry + derived history
```

The monitor reads NUT and publishes the safety projection before writing telemetry or history. It
does not invoke host shutdown; the guarded quick self-test is the only automatic NUT command, and
`upsmon` owns host shutdown. A publication failure invalidates stale output and stops the daemon.

## Durable data

The raw stream is `~/.config/ups-battery-monitor/events/telemetry.jsonl`. Its exact eight-field
`TelemetrySample` contract is documented in [MINIMAL_EVENT_JSONL.md](MINIMAL_EVENT_JSONL.md) and
implemented in `src/adapters/minimal_event_file.py`. Keep raw lines unchanged; missing NUT values
remain `null`.

`events/history.jsonl` is derived state: episode summaries, IR observations, and model-update
receipts. `scripts/blackout-history.py` reads both files. `model.json` is the validated model state;
none of these derived values are proof of capacity, SoH, temperature, or battery current.

## Contributor rules

- Preserve one-second polling and the read-only NUT boundary except for the guarded quick test.
- Keep safety publication ordered before persistence and diagnostics.
- Keep observed raw telemetry distinct from derived interpretation.
- Run `just check` before handing off a release candidate.
