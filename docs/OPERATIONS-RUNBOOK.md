# Automatic Blackout Learning Operations Runbook

**Status:** Candidate acceptance and rollback procedure. This document does not claim deployment.

This runbook covers the per-event JSONL candidate described by ADR 0003. Run it only against a
pinned release candidate that has passed its automated and review gates. Safety has priority over
data collection or learning.

Normative companions: [ADR 0003](adr/0003-domain-jsonl-automatic-blackout-learning.md),
[Glossary](GLOSSARY.md), and [user scenarios](USER-SCENARIOS.md).

The release gates are fixed: per-function CRAP must be at most 30, every production module at most
800 physical source lines and every class at most 500, and the Ruff, `lint-imports`, `tach check`,
`tach check --exact`, type, dead-code, formatting, and test checks must pass. Run `just check` at
the release-candidate boundary. A green gate is not live UPS validation and does not imply
deployment.

## Current architecture map

This is the normative map for the candidate used by this runbook. Historical plans and review
receipts under `docs/plans/` and `docs/reviews/` remain preserved evidence; do not use their
intermediate module counts or dependency maps as deployment instructions.

```mermaid
flowchart LR
    NUT[Read-only NUT] --> MON[monitor.py\nsafety first]
    MON --> UPS[virtual UPS\nhealth]
    MON --> APP[application use cases]
    APP --> DOM[domain decisions]
    APP --> JSONL[JSONL evidence\nfacade / catalog / index]
    APP --> MODEL[ModelOwner\nsole model writer]
    APP --> SINK[bounded report + health sinks]
```

Text fallback: `NUT -> monitor safety -> virtual UPS`; only after publication does the monitor
queue application work. Application code calls domain decisions, JSONL evidence, the sole model
writer, and bounded sinks. The runtime has no UPS command path. This diagram describes code only;
it is not deployment or live-UAT evidence.

## State and evidence paths

| Purpose | Path |
|---|---|
| Active model | `~/.config/ups-battery-monitor/model.json` |
| Per-event evidence and bounded registry/index | `~/.config/ups-battery-monitor/events/` |
| Event-store writer lock | `~/.config/ups-battery-monitor/monitor.lock` |
| Runtime health | `/run/ups-battery-monitor/ups-health.json` |
| Virtual UPS publication | `/run/ups-battery-monitor/ups-virtual.dev` |
| NUT production configuration | `/etc/nut/ups.conf`, `/etc/nut/upsmon.conf` |
| Release A historical archive | `~/.config/ups-battery-monitor/discharge-events-v1.jsonl` |

The event JSONL files are scientific evidence. Journald is the human/event log. Do not edit,
merge, truncate, or reverse-import either evidence format. Do not create a SQLite copy for runtime
use. Preserve event files and the Release A journal during rollback.

## Read-only preflight

Run these checks while the service is running and the UPS is physically online. They do not send a
UPS command:

```bash
upsc cyberpower@localhost ups.status
upsc cyberpower-virtual@localhost ups.status
upsc cyberpower@localhost battery.charge
upsc cyberpower-virtual@localhost battery.runtime
systemctl is-active ups-battery-monitor.service
jq -e '
  .schema_version == 2 and
  .capture_queue.capture_available == true and
  .capture_queue.observations_queued == 0 and
  .capture_queue.observation_overflow_count == 0 and
  .capture_queue.lifecycle_overflow_count == 0 and
  .storage.capture_available == true and
  .storage.alarm == null and
  .storage.rebuild_stalled == false
' /run/ups-battery-monitor/ups-health.json
python3 -m src.motd_status
sha256sum ~/.config/ups-battery-monitor/model.json
cat /proc/sys/kernel/random/boot_id
if test -d ~/.config/ups-battery-monitor/events; then
  find ~/.config/ups-battery-monitor/events -maxdepth 1 -type f \
    -printf '%f %s bytes\n' | sort
else
  printf '%s\n' 'events directory is absent before candidate cutover'
fi
journalctl -u ups-battery-monitor.service --since today --no-pager
```

Record the command output with the candidate artifact identity and current model scientific
fingerprint. Check all of the following before stopping anything:

- Physical and virtual status are `OL`; neither contains `LB` or `FSD`.
- Virtual publication is no older than the derived default 30 seconds
  (`publication_max_age_s = 30.0`), and NUT fields pass normal validation.
- Reported charge is at least 90% and modeled reserve is at least 18 minutes at current load.
- Load is inside the UPS and acceptance-test safe range.
- No shutdown is pending and the physical control path remains available.
- The daemon is the sole writer. Capture/storage health is green, the observation queue is empty,
  and no rebuild is stalled.
- Record the event inventory, boot ID, model hash/fingerprint, and service-log excerpt. The daemon is
  read-only with respect to NUT: this runbook does not provide or invent a UPS test/power command.
- Verify the pinned Release A binary/config and pre-transform model backup offline.
- On a copy, calculate and register the exact source and expected target fingerprints.
- The complete candidate has passed automated and Cross-AI release gates.

Abort preflight on any missing, stale, contradictory, or unexplained value. Do not repair JSONL or
`model.json` by hand.

### Safety publication and startup failure semantics

The safety path is independent of event persistence. `src/monitor.py` reads NUT once per second,
calculates from one immutable `ModelOwner` snapshot, atomically publishes the virtual UPS, and only
then submits capture work. The publication freshness bound is the derived default 30 seconds. Once a failed
telemetry read exceeds that bound, the exporter writes an explicit fail-safe containing
`ups.status: OB DISCHRG LB`, zero runtime/charge, and `ups.safety.freshness: stale_failed`; the
watchdog is no longer healthy.

The accepted derived-telemetry policy has a separate default 30-second grace for a routine `upsd`/NUT
restart. It prevents a transient telemetry-server restart from looking like a real outage while
keeping its upper bound at `5m - 2m = 3m` under the default modeled threshold and hard floor; a
larger transport budget cannot consume that two-minute hard-floor reserve. This is not new physical
evidence. A simultaneous blackout and NUT loss is indistinguishable with the available sensors, so
the grace remains a bounded residual risk for live acceptance rather than a proof of mains state.

At cold start there is no safe prior physical state to convert. If no current physical observation
has produced a publication, the exporter keeps the virtual output unavailable through the finite
startup grace and then fails closed without synthesising `LB`. A prior `.dev` file is not proof of
current `OL`: the monitor removes it before start and after stop. The candidate exact-instance unit
is ordered after the monitor and can pass its regular-file wait only after monitor readiness,
which follows a new physical observation and atomic publication. Its `BindsTo=`/`PartOf=` lifecycle
removes the driver on monitor failure instead of handing a stale projection to `upsmon`; this does
not claim that the exact unit is already deployed.

A failure during the safety-file write (including a deadline, `EIO`, or `ENOSPC`) invalidates the
old virtual-UPS file immediately and terminates the safety loop. It must never leave a stale `OL`
file trusted by `upsmon`. This is not an event-store failure and is not retried by a background
worker.

When an in-process recovery remains unfinished, virtual `LB` is sticky for a maximum of five
seconds. After that deadline, the first fresh physical `OL` is published, but capture and learning
are marked unhealthy and the monitor requests a controlled restart. A healthy restart must again
prove a fresh physical observation before the virtual driver can return. Fatal service starts are
limited to three attempts in five minutes with `RestartSec=10`; ordinary NUT telemetry loss retries
inside the running process.

The isolated external NUT fixture observed NUT 2.8.1 (the test contract accepts NUT 2.8 and newer):
deleting only `.dev` while `dummy-ups` remained alive retained stale `OL` in driver memory; stopping
the driver made `upsc` unavailable; starting without `.dev` stayed unavailable; and a fresh file
followed by driver restart returned `OL`. These observations are offline fixture evidence, not a
live service or UPS UAT result.

### Planned maintenance stop

A maintenance stop makes the virtual UPS unavailable and unmonitored; it is not a transparent
pause. Before stopping the monitor, keep mains stable, confirm that no blackout or UPS test is in
progress, preserve an independent operator/control path, and record the current health and model
identity. Do not infer safety from a left-over `.dev` file, and do not start only `dummy-ups` while
the monitor is stopped. After maintenance, start the monitor through its normal dependency path,
then require a fresh physical observation and publication before declaring virtual UPS or host
protection healthy. No deployment or live-UAT result is implied by this procedure.

Event-store startup is deliberately deferred until after the first successful safety publication.
If the event directory cannot be opened, the daemon remains safety-only and reports degraded
capture/storage health while the background coordinator retries. If another process owns
`monitor.lock`, startup recovery raises a fatal writer conflict and the daemon stops; do not start a
second copy or remove the lock by hand.

## Candidate cutover

Use the exact reviewed release artifact. The reference-frame transformation is a one-shot deploy
operation; candidate startup must never perform it. Prepare the verified backup, registered
fingerprints, and exact configured `shutdown_minutes` value before the stop window. The transform
must prove equivalence against the same shutdown threshold that the candidate will use at runtime.
The runtime value must be an integer greater than the two-minute safety floor; the shipped default is
`5` minutes. Values `1` and `2` are rejected because they would leave no valid reserve margin for
telemetry-loss handling.

The ordered operation is:

1. Recheck physical and virtual `OL`, reserve, health, writer identity, and model hash.
2. Stop Release A and verify it released the writer lock.
3. As the configured service user, run the one-shot transformation:

   ```bash
   scripts/reparameterize-ir-reference \
     --model ~/.config/ups-battery-monitor/model.json \
     --backup ~/.config/ups-battery-monitor/model.json.pre-domain-jsonl \
     --source-fingerprint "$SOURCE_FINGERPRINT" \
     --target-fingerprint "$TARGET_FINGERPRINT" \
     --shutdown-minutes "$SHUTDOWN_MINUTES"
   ```

4. Require a successful receipt and verify that the actual target fingerprint equals the
   pre-registered value. A held lock, unexpected schema/fingerprint, failed safety-equivalence
   oracle, or rerun is a refusal, not a reason to force the write.
5. Start the complete candidate. Require a fresh physical and virtual safety publication before
   recovery, assessment, or index maintenance is treated as healthy.
6. Re-run the read-only preflight. Confirm one model owner, reference load zero, green capture, and
   no unexplained fingerprint change.

An open recovered capture does not restore an LB latch: the registry does not record prior virtual
publication. The first post-restart poll recomputes safety from the current observation and frozen
model. A first post-restart physical `OL` must publish virtual `OL`, even when recovery still needs
to close an event; current physical `OB` uses the configured modeled-runtime threshold. Sticky `LB`
survives later in-process polls only after this process actually publishes it. An unclassified
status is the candidate's pre-registered conservative difference from Release A: it is evaluated
as a real blackout, but healthy modeled reserve still publishes `OB DISCHRG` without `LB`.

Do not expose a half-migrated runtime. Do not run the transform while any writer is active.

## Staged live acceptance

### Stage 1: concurrent observation

Start independent, read-only observation of physical status, virtual status, safety source, model
fingerprint, active event, storage health, durability lag, queue depth, and service logs. Keep the
physical power restore path and SSH/control path available.

### Stage 2: optional short self-test

A vendor short self-test may verify detection, CAL classification, JSONL durability, and reporting.
It is optional and must be separately authorized through the site's existing UPS procedure. This
runbook intentionally gives no UPS command for starting or stopping it. It cannot prove
natural-blackout comparison, IR learning, capacity, SoH, or recovery. Test restart/recovery with a
monitor-only fixture, not by rebooting during the live UPS acceptance.

### Stage 3: bounded natural outage

The product target is 360 durable seconds. Immediately before starting, repeat the preflight:
physical `OL`, charge at least 90%, modeled reserve at least 18 minutes, no raw or virtual `LB`,
empty capture queue, and healthy storage.

The user removes UPS input power. The daemon must not issue a test or power command. Restore input
power at 360 seconds, or immediately when any abort condition below appears, whichever comes first.

During the event, verify:

- physical polling and virtual publication continue at one-second cadence;
- each accepted observation becomes durable at one-second cadence;
- physical raw `LB` remains a diagnostic and does not directly set virtual `LB` or FSD;
- virtual output is fresh and capture/storage errors are visible in health;
- if raw `LB` appears, its first modeled runtime and later observed on-battery duration are recorded.

Raw `LB` by itself is not an abort. It does not authorize a model change.

### Stage 4: automatic close

After physical `OL`, require the unattended path:

```text
end -> assessment -> comparison -> identification -> decision -> outcome
```

No operator handles event data. Record duration, deduplicated points, gaps, comparison mode/result,
the exact `ir_k` estimate, before/after value if applied, or exact reasons for no change. Confirm
that shutdown rules are unchanged or more conservative.

## Pass criteria

Operational capture/safety acceptance requires:

- at least 300 durable seconds;
- coverage at least 0.90;
- at least 270 deduplicated raw observations;
- maximum raw gap no greater than 5 seconds;
- steady-state durability lag no greater than 2 seconds and never above 5 seconds;
- zero queue overflow;
- exactly one terminal outcome.

A safety-aborted event before 300 seconds may pass only operational safety/capture checks. Scientific
comparison remains separate:

- short-window comparison needs at least 180 evaluated seconds after `evaluation_origin`, roughly
  255 physical seconds;
- full comparison needs at least 300 evaluated seconds, roughly 375 physical seconds, plus enough
  normalized endpoint movement;
- an event ending before 180 evaluated seconds reports `comparison_not_attempted`.

The 360-second acceptance targets short-window mode. Full mode is optional. An optional host-load
change of at least 15 percentage points, with total load at most 50%, may exercise step detection.
It is not required, and one event normally cannot form the multi-event learning cohort.

A scientifically correct `recorded_only` outcome passes when capture, automatic processing, and the
refusal explanation are correct. The real commit path is proven deterministically in automated
tests and may apply later only when a natural multi-event cohort qualifies.

## Abort conditions

Restore input power immediately on:

- virtual `LB`;
- modeled runtime at or below 10 minutes;
- capture degradation, queue overflow, durability lag above 5 seconds, or hidden/unexplained storage
  state;
- stale virtual publication;
- loss of SSH, observation, or the physical control path;
- any operator concern.

Rollback the candidate on later modeled `LB`, any influence of raw physical `LB` on virtual
`LB`/FSD, an unexplained model fingerprint, missing or duplicate event, corruption/restart loop,
second writer, daemon-issued UPS command, stale output, or health that cannot explain the latest
outcome.

## Plain-language outcomes

- **Learned (plain report: Applied):** independent natural evidence supported a smaller `ir_k`;
  report before, estimate, after, evidence IDs, and that safety became no less conservative.
- **Recorded only:** the event is preserved and compared where possible, but no eligible multi-event
  cohort authorized a change. This is a normal successful outcome.
- **Comparison not attempted:** the trusted evaluated window was shorter than 180 seconds.
- **Rejected:** CAL, gap, corruption, capture damage, invalid telemetry, or another exact gate denied
  scientific use. Raw history remains preserved where safe.
- **Possible decline:** comparable raw evidence shows a trend, such as greater load sag. This is not
  SoH, a cause, or permission to apply an upward `ir_k`.

Partial events never report measured total runtime, capacity, SoH, Peukert, or a learned LUT.

## Rollback

Rollback is a controlled release operation, not evidence cleanup:

1. Restore physical power and confirm stable `OL` before stopping the candidate.
2. Record health, event inventory, current model hash/fingerprint, and service logs.
3. Stop the candidate and verify the writer lock is released.
4. Preserve all files under `~/.config/ups-battery-monitor/events/` and preserve
   `discharge-events-v1.jsonl` byte-for-byte. Do not reverse-import either.
5. If the reference transform or any IR commit occurred, restore the verified matching
   pre-transform or pre-commit `model.json` snapshot before starting Release A.
6. Restore the pinned Release A binary and configuration, then start it.
7. Verify fresh physical and virtual safety output, modeled runtime, status tokens, and writer
   identity. Keep the candidate evidence for later read-only diagnosis.

If a backup, artifact identity, fingerprint, lock state, or physical safety condition is uncertain,
stop and recover the safety path under the previously reviewed Release A procedure. Never guess or
force a model conversion.

### Installer transaction boundary

`scripts/install.sh` renders the service and virtual-driver units into a temporary staging tree and
runs `systemd-analyze verify` before changing production paths. Before the first mutation it takes
exact backups or absent markers for the monitor unit, exact virtual-driver fragment, legacy
driver drop-in, both target/monitor wants links, `/etc/nut/ups.conf`, and `/etc/nut/upsmon.conf`.
It also records whether
`nut-server`, `nut-monitor`, `ups-battery-monitor`, and
`nut-driver@cyberpower-virtual` were active, plus the monitor's enabled/disabled state.

If a later installation step fails, rollback restores those bytes, reloads systemd, restores the
enabled state, and restarts only services that were active before the transaction in safe order.
Units that were inactive remain inactive. A partial restart failure is surfaced as an incomplete
rollback and requires inspection before retry. The temporary transaction directory is cleaned up
after success or rollback; it is not the operator's retained external-state backup. Keep the
pre-transform `model.json` backup, event JSONL, and Release A archive according to the retention
steps above. No live installer, `sudo`, or `systemctl` operation was run for this candidate.
