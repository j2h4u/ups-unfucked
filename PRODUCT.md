# Product statement

UPS Battery Monitor protects one host and keeps a small, durable record of what its UPS actually
experienced.

## Jobs to be done

1. During a mains interruption, publish a conservative virtual UPS state every second so `upsmon`
   can shut the host down safely.
2. Record blackouts, quick tests, and the following recharge in one append-only
   `events/telemetry.jsonl` file, with up to 120 seconds of context before and after an event.
3. Make the result understandable through the compact `blackout-history.py` CLI and the optional
   MOTD line.
4. Run unattended. Ordinary telemetry I/O failures do not precede safety publication; publication
   failure remains safety-critical and must fail closed.

## Product promises

- One daemon and one physical NUT input are the runtime boundary.
- Within each successful poll, the virtual UPS is published before optional recording, history, and
  feedback work.
- `events/telemetry.jsonl` is append-only raw evidence; `events/history.jsonl` is a derived aggregate,
  not the evidence source. Missing or uncertain values are shown as missing, never invented from a model.
- An eligible natural blackout may produce a load-step observation; a bounded IR update requires a
  consistent cohort and records its reason.
- A quick self-test may run automatically at OL100 when no blackout or calibration/self-test has
  occurred for 14 days. It validates operational behavior only; it cannot establish capacity,
  state of health, or a future runtime guarantee.
- Installation has one supported path, `scripts/install.sh`; the MOTD is optional.

## Honest limitations

The available NUT fields do not provide an independently measured battery current, temperature,
returned energy, or a guaranteed empty-battery endpoint. Therefore the product does not promise
exact amp-hours, state of health, a Peukert exponent, or a complete voltage-to-charge curve.
Host shutdown is a safety boundary, not proof that the battery reached zero.

## Operational boundary

`upsmon` owns host shutdown. The daemon sends no UPS or power commands except the narrowly guarded
automatic quick self-test described above. It does not run a deep discharge or expose an operator
workflow for scientific calibration. There is no operator-facing configuration file; `model.json` is
managed runtime state.
