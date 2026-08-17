# ups-unfucked — Current Context

This document is the current contributor and review context for the unattended natural-blackout
learning runtime. It is intentionally short on historical implementation detail: the accepted
architecture is in [ADR 0003](../adr/0003-domain-jsonl-automatic-blackout-learning.md), while
older plans and reviews remain historical snapshots.

## Product boundary

The daemon sits between a real CyberPower UPS and NUT. Host safety is the first responsibility:
the monitor reads one physical observation per second, calculates safety from one frozen model
snapshot, and publishes the virtual UPS before it queues asynchronous capture or learning work.
The daemon is a data source. `upsmon` owns shutdown decisions; the daemon sends no UPS commands and
does not invoke `systemctl poweroff`.

The unattended application path is:

```text
capture -> assess -> compare -> identify -> decide -> report
```

An event can be useful without being scientifically complete. Partial, reboot-gapped, CAL, and
test events remain explicit operational or censored evidence. Only a stable, independent load
step can authorize a conservative reduction of `ir_k`; no natural event updates capacity, SoH,
Peukert, or LUT values. The learning direction is fail-safe: it may lower `ir_k`, never raise it,
and never make a later shutdown occur later.

## Current runtime topology

```text
read-only NUT telemetry
        |
        v
src/monitor.py (one-second safety loop)
        |\
        | \-> virtual_ups_exporter.py -> upsmon / health / dashboards
        |
        `-- application flow -> domain decisions -> JSONL evidence
                                      |             |
                                      |             `-> index.jsonl / active.json projections
                                      `-> ModelOwner (sole scientific writer)
```

The composition root is `src/monitor.py`. `src/monitor_config.py` owns the fixed one-second
interval and small user configuration. `src/nut_client.py` and
`src/adapters/nut_telemetry.py` read and validate one NUT response. `src/application/safety.py`
calculates the publication from one immutable model snapshot, and
`src/virtual_ups_exporter.py` atomically writes the virtual UPS and health output.

Application orchestration is split across `capture_blackout.py`, `capture_writer.py`,
`assessment_worker.py`, `close_blackout.py`, `background_coordinator.py`, and the bounded
`reporting_scheduler.py` (reporting and index maintenance only). Pure lifecycle, evidence,
comparison, IR identification, learning, decline, and reporting rules live under `src/domain/`.
The JSONL adapters own files, records, indexes, and bounded work state. `src/adapters/model_owner.py`
is the sole mutable scientific model writer.

## Durable evidence and projections

Each blackout owns an append-only JSONL file under
`~/.config/ups-battery-monitor/events/`. This event file is authoritative scientific evidence. It
is hash-linked, synchronised, bounded, and closed with one terminal outcome. Missing samples,
reboot gaps, corruption, and capture damage remain explicit and are never silently interpolated.

`index.jsonl` is a rebuildable bounded summary projection. `active.json` is a bounded work registry.
Neither is evidence and neither replaces the event files. Journald and Grafana can explain or
display an outcome, but cannot reconstruct missing scientific evidence. SQLite is deliberately not
part of the design.

The old Release A
`~/.config/ups-battery-monitor/discharge-events-v1.jsonl` file is preserved byte-for-byte as a
read-only archive. Current runtime code neither opens nor imports it, including during restart,
rollback, or re-upgrade.

The store may be unavailable while the safety lane is already publishing. In that case capture,
assessment, and learning degrade visibly and retry through the bounded background path; storage
failure must not delay or weaken the safety result. A second writer is a fatal ownership conflict,
not an ordinary capture degradation.

## Model and reporting rules

Safety calculations use the persisted model snapshot (including LUT, Peukert, SoH, and load-sag
state) as read-only input for that poll. Model mutation is limited by the domain policy and the
single `ModelOwner` commit lane. Natural evidence can change only `ir_k`, and only after independent
load-step, timing, coverage, quantisation, cohort, revision, epoch, and anti-feedback checks.

Every event receives a durable terminal outcome and a bounded plain-language report. A short event
may correctly report `comparison_not_attempted`; a decline is a trend signal, not a causal battery
diagnosis or a SoH measurement. A vendor self-test can exercise detection, CAL classification,
durability, and reporting, but never authorizes natural comparison or learning.

Physical firmware `LB` is retained as a diagnostic fact. It does not directly set virtual `LB`,
request FSD, or change shutdown policy. Modeled reserve remains authoritative, and a cold start
without a proven physical observation does not invent a synthetic low-battery result.

## Configuration and lifecycle

The user-facing TOML settings are `ups_name`, `shutdown_minutes`, and optional `capacity_ah`.
Other behavior is fixed by code or persisted model state; there is no retired scheduling or broad
auto-calibration configuration surface.

The candidate systemd lifecycle binds the exact virtual NUT driver to the monitor. The monitor
invalidates its runtime output before start and after stop, and a fresh physical observation must
precede driver readiness. Fatal monitor starts are bounded (three attempts in five minutes with a
ten-second delay); transient NUT telemetry loss is retried inside the process. These are repository
contracts to verify during staged installation, not evidence of a live deployment.

The installer renders units, runs `systemd-analyze verify`, snapshots exact files/links and active
states, and restores them on a later transaction failure. It does not mutate a live host during
tests. The pre-transform model backup and all raw JSONL/Release A evidence follow the retention
rules in the operations runbook.

## Operating environment and limits

- CyberPower UT850EG-class UPS over USB through NUT; no battery temperature sensor is available.
- Headless Linux host where an unclean shutdown is a data-loss risk.
- Short and frequent outages are expected; most are partial and therefore censored.
- Available signals cannot identify absolute capacity, SoH, Peukert exponent, or a new voltage-to-
  SoC LUT from a natural partial outage.
- Natural load steps may be rare, so the model may never change. This is a physical limitation, not
  an operator task.

The isolated NUT fixture observed NUT 2.8.1 behavior: unlinking `.dev` while `dummy-ups` remains
running retains stale `OL` in memory; stopping the driver makes `upsc` unavailable; a cold start
without `.dev` is unavailable; and a fresh file followed by a driver restart returns `OL`. The
fixture accepts NUT 2.8 and newer and is not live UPS or deployment evidence.

## Historical material

ADR 0001 records the retracted active-desulfation premise. ADR 0002 records the superseded Release
A global-journal choice. Both remain useful historical records, but neither describes current
runtime ownership or evidence flow. Preserved plans and reviews under `docs/plans/` and
`docs/reviews/` are likewise historical snapshots, not current module maps.

## Useful documents

- [ADR 0003](../adr/0003-domain-jsonl-automatic-blackout-learning.md) — current domain, evidence,
  lifecycle, and learning decisions.
- [ADR 0001](../adr/0001-desulfation-premise-reversal.md) — historical premise reversal.
- [CONTROLLED-CAPACITY-TEST-PROTOCOL.md](../CONTROLLED-CAPACITY-TEST-PROTOCOL.md) — supervised,
  non-automatic hardware-test procedure.
- [OPERATIONS-RUNBOOK.md](../OPERATIONS-RUNBOOK.md) — staged installation, rollback, and live-UAT
  checklist.

The candidate has static/test evidence only until the staged deployment and live UPS acceptance
checks in the runbook are completed by an operator.
