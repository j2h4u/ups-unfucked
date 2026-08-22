# User scenarios

## Normal online operation

The daemon polls the physical UPS through NUT once per second and publishes a safe virtual UPS.
`upsmon` can consume that state. The daemon records available telemetry without blocking the safety
loop.

## A blackout occurs

The physical UPS reports on-battery status. The daemon continues publishing the virtual state and
records blackout samples in `events/telemetry.jsonl`, including up to 120 seconds of prior and later
context when available. If power returns, recharge samples are recorded. If the host shuts down,
that is a safety boundary, not proof of an empty battery.

## A quick self-test occurs

At OL100, after 14 days without a blackout or calibration/self-test, the daemon may run one automatic
quick self-test. Its samples remain operational evidence and cannot be used to claim capacity, state
of health, or natural-blackout behavior.

## Reviewing history

The operator runs `scripts/blackout-history.py`. It reads the one telemetry file and prints a compact
summary without changing the raw telemetry.

## Telemetry is incomplete

Missing NUT fields remain missing. The monitor may still publish safety state, but a history entry
must not be presented as a precise capacity, SoH, temperature, current, or energy measurement.

## Storage or diagnostics fail

The daemon keeps the one-second virtual UPS safety path running. The operator inspects the service
journal and filesystem, preserves the raw telemetry, and repairs the installation only after the
physical UPS and `upsmon` path are safe.
