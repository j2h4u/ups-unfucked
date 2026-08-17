# User Scenarios

**Status:** These scenarios describe the domain-JSONL candidate. They do not claim live deployment.

The monitor protects the host first and processes blackout evidence automatically. Users do not
classify samples, repair event files, or approve model changes.

## Read the current result

The bounded health projection is the normal read-only operator view:

```bash
./scripts/battery-health.py
python3 -m src.motd_status
upsc cyberpower-virtual@localhost
```

It shows physical and virtual safety status, model fingerprint, capture/storage health, the latest
plain-language outcome, and bounded decline evidence. It does not scan raw event history or mutate
`model.json`.

## A short natural outage

Mains power fails and returns before the trusted evaluated window reaches 180 seconds.

The monitor publishes physical and virtual safety once per second, stores the event under
`~/.config/ups-battery-monitor/events/`, and closes it after `OL` returns. The final report says
`comparison_not_attempted`. This is useful operational history, not failed learning. The event does
not update capacity, SoH, Peukert, LUT, or `ir_k`.

## A longer partial outage

Mains power returns after a well-covered trusted interval. The monitor automatically compares the
frozen model with observed voltage behaviour in short-window or full mode.

Passing comparison does not make the partial event a complete discharge. The observed duration is
not total runtime. It does not measure capacity or SoH. If no independent eligible load step and
multi-event cohort exist, the normal outcome is `recorded_only` with exact reasons.

## Natural load steps become eligible evidence

Several ordinary blackouts contain independent stable load steps, including both upward and
downward steps, within one battery epoch and evaluator revision. The estimator recomputes the raw
step evidence and excludes previously consumed hashes.

If every timing, quantisation, agreement, dispersion, and cohort gate passes, the model owner may
apply a bounded downward `ir_k` change. A commit never changes capacity, SoH, Peukert, or LUT and
must leave shutdown unchanged or more conservative. No operator handles the data.

## Load sag appears to increase

Independent steps estimate an `ir_k` above the active value. The monitor preserves the estimate as
possible decline and does not apply it. An upward coefficient could delay shutdown at some loads,
so reporting is deliberately separate from the safety model.

The message means "comparable evidence shows more load sag." It does not mean measured SoH, prove a
cause, or replace a battery inspection.

## Physical firmware LB appears

The physical UPS reports raw `LB` while the modeled runtime is still above its shutdown threshold.
The monitor stores the first-LB point and later observed reserve as diagnostics. Raw `LB` does not
directly set virtual `LB`, request FSD, stop capture, or authorize learning.

Modeled runtime and the existing hard floor remain the safety authority. A future raw-LB safety
rule would require a separate reviewed decision and live evidence.

## A vendor self-test runs

An explicitly authorized short self-test produces `CAL`. The monitor may verify detection, JSONL
durability, and reporting. The event remains operational evidence only.

A self-test is never treated as a natural outage and cannot authorize forward comparison, `ir_k`
learning, capacity, SoH, Peukert, or LUT changes. The daemon never schedules or sends a UPS command
as part of automatic learning.

## The service restarts during an outage

The monitor does not treat an open registry entry as proof that virtual `LB` was previously
published. On cold start without a proven physical observation, the virtual output remains
unavailable through the finite startup grace and then fails closed; it does not synthesise `LB`.
Once a current physical observation arrives, startup publishes from that observation before deferred
recovery work. If the UPS is still on battery, the same blackout continues with an explicit
cross-boot gap. If it is online, the stranded event closes at its last durable observation and a
fresh physical `OL` publishes virtual `OL`.

If durable recovery is still pending after the in-process sticky-`LB` deadline of five seconds, the
next fresh physical `OL` is published, capture and learning are marked unhealthy, and the monitor
requests a controlled restart. History remains available, but unknown cross-boot time is never
integrated and the event cannot authorize science across the gap.

## NUT telemetry or safety publication fails

The physical poll is fixed at one second. If a NUT read fails, the last virtual result remains
temporarily visible for the derived default 30-second grace while health records the poll error. After that grace,
the exporter publishes an explicit `OB DISCHRG LB` fail-safe with zero runtime/charge and stops
refreshing the watchdog.

If the safety-file publication itself fails or misses its deadline, the exporter invalidates the old
virtual-UPS file immediately and stops the safety loop. It never leaves stale `OL` data in place and
never turns this failure into an event-store retry. No UPS control/test command is issued by this
path.

The accepted derived-telemetry policy uses a default 30-second grace for a routine `upsd`/NUT restart. This
avoids a false shutdown during a transient telemetry-server restart. Under the default five-minute
modeled threshold and two-minute hard floor, the default grace is 30 seconds and its upper bound is
`5m - 2m = 3m`, so a larger transport budget cannot consume the hard-floor reserve. It is not
physical evidence: simultaneous blackout and NUT loss are indistinguishable with the available
sensors and remain a live-UAT risk.

The candidate exact-instance systemd lifecycle removes the stale projection: the driver is bound
to and ordered after the monitor, while the monitor unlinks `.dev` before start and after stop and
becomes ready only after a current observation and atomic publication. The virtual instance is not
a member of `nut-driver.target`. Fatal monitor starts are limited to three attempts in five minutes;
ordinary telemetry loss retries inside the process. These are candidate guarantees, not deployment
claims.

The isolated NUT fixture (local observed version 2.8.1; the contract accepts 2.8+) demonstrated why
this matters: unlink-only while `dummy-ups` stayed alive retained stale `OL` in memory; stopping
the driver made `upsc` unavailable. A cold driver start without `.dev` stayed unavailable, and a
fresh file plus driver restart returned `OL`. This is offline fixture evidence, not live UAT.

## Planned maintenance stop

Stopping the monitor is a safety-visible maintenance state: the virtual UPS becomes unavailable
and unmonitored. Before maintenance, keep physical power stable, confirm that no outage or UPS test
is in progress, preserve an independent operator/control path, and record health and model identity.
Do not leave or trust `.dev`, and do not run only `dummy-ups` while the monitor is stopped. After
maintenance, start the monitor through its normal dependency path and require a fresh physical
observation and publication before treating host protection as healthy. This scenario describes the
candidate contract, not a deployed service.

## Event storage is unavailable at startup

Safety starts independently of the scientific store. The first physical poll and virtual
publication happen before deferred JSONL recovery. While the event directory is unavailable,
capture and learning are disabled and health reports a degraded storage state; the background
coordinator retries recovery. If another process owns `monitor.lock`, the writer conflict is fatal
and the daemon stops rather than starting a second writer or deleting the lock.

## Storage or capture fails

Safety publication continues at one-second cadence. Health at
`/run/ups-battery-monitor/ups-health.json` exposes the failure. If possible, the writer records a
durable gap or damaged outcome. Corrupt bytes are preserved in a separate segment rather than
silently repaired.

The affected event is rejected for scientific use. A missing observation is never reconstructed
from journald, Grafana, or model output.

If storage becomes writable again while the process is alive, the first retained battery sample is
written with an explicit gap and rejected outcome. Graceful service stop waits boundedly for that final
lifecycle record before closing the writer. A hard process or host loss before any writable command becomes
durable can still lose the RAM-only first sample; with no second sensor or independent storage authority,
the service reports the storage failure but cannot invent event bytes.

## The summary index is unavailable

Raw per-event JSONL stays authoritative. New capture continues, while cohort science and reports
that require the projection refuse with an explicit reason. Bounded lazy maintenance rebuilds the
index after safety publication; no operator scans or imports history.

## A decline report changes after software update

Decline reports are recomputed from comparable sealed raw evidence under the current evaluator.
They may change when evaluation logic improves without rewriting any event. This is expected: the
report is a current projection, not stored scientific truth.

Only three bounded signals are reported: load-sag trend, firmware-LB reserve proxy, and comparable
long-partial voltage curve. Each can be insufficient or inconclusive. None is SoH or a causal
diagnosis.

## The battery is physically replaced

Battery replacement is an explicit operator fact performed while the UPS is safely online and no
event is open. The sanctioned reset starts a new `battery_epoch_id`; future cohorts cannot mix old
and new batteries.

Historical event JSONL and the Release A archive remain unchanged. A replacement does not authorize
an automatic deep test or import old learned evidence into the new epoch.

## A separately supervised capacity test

The historical supervised protocol remains available in
[CONTROLLED-CAPACITY-TEST-PROTOCOL.md](CONTROLLED-CAPACITY-TEST-PROTOCOL.md). It is outside automatic
blackout learning and requires explicit approval plus independent instrumentation and observation.
The daemon does not start it. A natural partial outage or short self-test must never be relabeled as
a capacity or SoH measurement.

## Live candidate acceptance

Follow [OPERATIONS-RUNBOOK.md](OPERATIONS-RUNBOOK.md). The target is a bounded 360-second physical
outage with independent read-only observation and immediate safety aborts. A correct
`recorded_only` result can pass because one event normally cannot satisfy the learning cohort.

Before any manual acceptance, the candidate must pass `just check`: CRAP at most 30 per function,
production modules at most 800 lines and classes at most 500, Ruff structural checks,
`lint-imports`, `tach check`, and `tach check --exact` (plus type, dead-code, formatting, and test
checks). These are release gates, not evidence of live UPS deployment.

The complete automatic path and conservative refusal are product behaviour. A real commit is
tested deterministically and may occur later when qualifying natural evidence accumulates.

## Historical Release A data and rollback

ADR 0002's `~/.config/ups-battery-monitor/discharge-events-v1.jsonl` remains a byte-for-byte
read-only archive. The new runtime never opens or imports it. Rollback preserves both this archive
and all files under `events/`; it does not merge one format into the other.

The detailed stop/transform/verify/start and rollback procedure is in the operations runbook. The
installer additionally stages and verifies rendered systemd units before mutation, backs up
the exact service/drop-in/NUT/upsmon files, and snapshots active/enabled state. A later failure
restores those files and states, restarting only services that were active before installation;
inactive services remain inactive. A partial restart failure is surfaced as incomplete rollback.
The temporary transaction backup is cleaned after the transaction; the runbook's pre-transform
model backup, event JSONL, and Release A archive remain retained for rollback and diagnosis. The
existence of these scenarios does not mean the candidate is deployed.
