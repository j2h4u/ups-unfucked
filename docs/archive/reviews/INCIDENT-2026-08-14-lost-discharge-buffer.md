# Incident Report: Lost In-Memory Discharge Buffer (2026-08-14)

**Date:** 2026-08-14, approximately 15:11–17:38 local time (UTC+05)
**Impact:** Safe host shutdown completed, but the active raw discharge buffer was lost. No
completed event, authoritative capacity/SoH/Peukert update, or complete on-battery duration was
available locally.
**Severity:** Evidence loss/data-integrity incident; no confirmed unsafe shutdown-path impact.
**Status:** Historical. The proposed per-event journal was superseded by the current append-only
`events/telemetry.jsonl` stream and compact `events/history.jsonl` aggregates.

## Known timeline

- Approximately **15:11:10 +05** — discharge collection began during the blackout.
- Approximately **15:11:11 +05** — real-blackout classification stabilised.
- Before shutdown — calibration batches reached **162 in-memory observations**.
- Approximately **15:38 +05** — the virtual UPS crossed the configured five-minute low-battery
  threshold.
- Shortly afterwards — `upsmon` initiated the normal automatic power-fail shutdown.
- During SIGTERM — the daemon saved `BatteryModel`, but did not persist the active raw buffer.
- Approximately **17:38 +05** — the next boot started. The interval after host shutdown until
  power return is unobserved.

## Lost data

- The 162-sample in-memory voltage/time buffer, including its full load/time series, event
  identity, and any samples not exported elsewhere.
- A complete event lifecycle and exact full blackout duration.
- Any capacity, SoH, or Peukert result that would have required a complete scientifically eligible
  event. No such result is inferred from this incident.

## Preserved data

- The host shut down through the existing NUT/upsmon low-battery path.
- `model.json` was saved. At diagnosis it retained `cycle_count=21`, but did not include this
  event in `cumulative_on_battery_sec`; `discharge_events=[]`, capacity sample count was zero,
  and SoH remained the default `1.0`.
- Thirteen deduplicated LUT points from the event remained. They are lossy, omit load and the
  full time series, and use SoC derived from the existing LUT; they are not an independent raw
  checkpoint and are not rewritten or promoted.
- Grafana Cloud retained secondary scrape evidence through Alloy. A read-only datasource-proxy
  query verified **133 distinct real scrape timestamps** in the 15:05–15:45 window, a maximum
  real gap of 15 seconds, and a final real scrape at **15:37:59 +05**. Prometheus lookback
  repetitions after that timestamp are not fresh observations.

## Root cause

The primary observation path was RAM-only. The SIGTERM handler persisted model state but did not
persist or replay the active discharge buffer. Timestamp deduplication/checkpointing could remove
duplicate exported points but could not provide durability for data that had never been written.
This is the obsolete 2026-03-14 decision reversed by [ADR 0002](../../adr/0002-durable-discharge-journal.md).

## Recovery limits

Grafana may support a reviewed partial operational trace: observed voltage/load trajectory,
runtime to the safe low-battery threshold, and a clearly labelled lower-bound energy estimate.
It cannot establish the post-shutdown battery behaviour, full blackout duration, exact 10-second
EMA buffer, exact Ah without measured current, absolute capacity/SoH, or an exact Peukert exponent.
The recovery artifact is not imported into `model.json` or the LUT without separate explicit
approval.

## Resolution

The daemon now records event evidence continuously in `events/telemetry.jsonl`, keeps derived
episode and feedback receipts in `events/history.jsonl`, and treats shutdown-truncated observations
as incomplete evidence. It does not run an automatic deep-discharge test.
