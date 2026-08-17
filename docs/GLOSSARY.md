# Glossary

Terms used in the codebase and documentation, with project-specific meaning.

## Battery and electrical terms

### SoC — State of Charge

The estimated fraction of charge remaining now, from 0% to 100%. The UPS firmware value is not
trusted as a precise measurement. The monitor estimates SoC from normalized battery voltage and a
lookup table.

### SoH — State of Health

A model value describing battery health relative to new. It affects predicted runtime, but it is
not measured by a natural partial blackout. This UPS has no independent battery-current or
temperature sensor, so automatic blackout learning never updates SoH.

### VRLA — Valve-Regulated Lead-Acid

The sealed lead-acid battery type used by the CyberPower UT850EG. Its delivered energy depends on
load, age, temperature, and discharge rate.

### Battery voltage (`battery.voltage`)

Terminal voltage in volts. It depends on both remaining charge and current load. The adapter keeps
the original decimal token so the estimator can state the available voltage-quantisation bound.
That decimal precision is not proof of the physical ADC resolution.

### Load (`ups.load`)

UPS output load as a percentage of nominal power. It is useful for comparison and load-step
identification, but it is not a measurement of battery current.

### Peukert exponent

A parameter describing how usable lead-acid capacity changes with discharge rate. Natural partial
events do not identify or update it because the monitor lacks independent current evidence.

### Temperature

Battery behaviour changes with temperature, but this UPS exposes no temperature sensor. The
monitor therefore does not claim temperature compensation or temperature-normalized decline.

## Normalization and prediction

### EMA — Exponential Moving Average

A time-based filter that smooths noisy voltage and load values. The safety model uses a
120-second time constant with one-second polling. Estimator windows use raw durable observations,
not the safety EMA as substitute evidence.

### `ir_k` — load-sag coefficient

The coefficient in:

```text
V_norm = V + ir_k * (load - reference_load)
```

Its unit is volts per load-percentage point (`V/pp`). It compensates for the apparent voltage drop
caused by load. Natural blackout learning may estimate `ir_k` only from independent stable load
steps. A strict multi-event cohort may apply a downward change. An upward estimate is reported as
possible decline but is never applied to the safety model.

### Reference load

The load frame used by IR compensation. The domain-JSONL candidate uses a one-time equivalent
transform from Release A's 20% reference to 0%. The LUT is shifted by the same amount so predictions
remain equivalent before learning begins.

### Normalized voltage (`V_norm`)

Battery voltage after load-sag compensation. It is used for LUT lookup and forward comparison.

### LUT — Lookup Table

The voltage-to-SoC curve. Forward and inverse interpolation are pure battery-math functions. The
natural-blackout path never learns, inserts, prunes, or otherwise changes LUT points.

### Modeled runtime

Runtime calculated from the frozen model snapshot and current observation. It drives virtual safety
policy. A partial event's observed duration is not measured total runtime.

### Safety poll and publication freshness

The daemon's physical poll interval is fixed at one second. A successful poll calculates safety from
one immutable model snapshot and publishes the virtual UPS before it queues evidence work. A failed
telemetry read has a default 30-second freshness grace derived from NUT transport timing. After that grace the exporter writes an explicit
`OB DISCHRG LB` fail-safe with zero runtime/charge. A failure while writing the safety file instead
invalidates the previous output immediately and stops the safety loop; it never leaves stale `OL`
data trusted. On cold start without a proven physical observation, it does not synthesise `LB`:
the output remains unavailable and then fails closed after the finite startup grace.

The accepted derived-telemetry policy separately uses a default 30-second grace for a routine
`upsd`/NUT restart. With the default five-minute modeled threshold and two-minute hard floor, the default grace
is 30 seconds and its upper bound is `5m - 2m = 3m`; a larger transport budget cannot consume the
hard-floor reserve. It avoids false shutdown during a normal telemetry-server restart, but is not
fresh physical evidence; simultaneous blackout and NUT loss are indistinguishable with the
available sensors and remain a live-UAT risk.

### Sticky `LB`

An in-process safety latch used while durable recovery is still pending. It is bounded to five
seconds. If recovery has not become safe by then, the next fresh physical `OL` is published, while
capture and learning become unhealthy and the monitor requests a controlled restart. A restart
starts with a clear latch and requires a current physical observation again.

## Evidence and lifecycle

### Natural blackout

A physical `OB` episode caused by loss of input power, not by a self-test. Processing is fully
automatic: capture, assessment, comparison, load-step identification, decision, and reporting need
no operator data handling.

### Partial or censored event

An outage that ends because grid power returns. It proves only what was observed before return. It
does not reveal total runtime, capacity, SoH, Peukert, or a complete discharge curve.

### Evidence class

A scientific quality label separate from lifecycle closure. A natural partial may support bounded
forward comparison and independent load-step IR evidence when all gates pass. CAL, reboot gaps,
capture damage, corruption, or invalid telemetry refuse the affected scientific use.

### Blackout ID and segment ID

A `blackout_id` groups one physical episode. A `segment_id` identifies one append-only writable
file segment. Recovery may preserve a damaged segment and continue history in a new segment under
the same blackout ID; the aggregate event is then rejected for science.

### Evaluation origin

The fixed start of a forward-comparison interval. It follows the on-battery transient and is based
on the median voltage and load of an exact 31-point stable window. Duration gates are measured from
this origin, not from the first physical `OB` sample.

### Short-window and full comparison

Short-window mode needs at least 180 evaluated seconds and checks RMSE, bias, slope, and residual
trend. Full mode has precedence when at least 300 evaluated seconds and sufficient normalized
endpoint movement are available; it also checks endpoint error. An event shorter than the short
window reports `comparison_not_attempted`.

### Load step

An independent change of at least 15 load-percentage points with stable pre- and post-step plateaus,
a bounded transition, and a 30-second stable re-arm before another accepted step. The estimator
compares raw voltage/load medians; it does not learn from its own model prediction.

### Anti-feedback

The rule that evidence used to estimate a parameter must be independent of that parameter's current
model output. Forward comparison can evaluate a model but cannot authorize learning by itself.
Self-tests and previously consumed load-step hashes cannot authorize a new natural IR commit.

### Battery epoch

The identity of one physical battery installation. Cohorts never cross epochs. Physical replacement
is an explicit operator fact that starts a new epoch; it is not inferred from a partial outage.

### Terminal outcome

The one durable final disposition for an event: `learned`, `recorded_only`, or `rejected`, with
bounded exact reasons. The plain-language report may render `learned` as **Applied**;
`recorded_only` is often the scientifically correct successful result. Every outcome is derived
from that event's durable raw evidence and frozen start snapshot.

### Possible decline

A trend recomputed from comparable sealed raw evidence, such as higher load sag or less reserve at
firmware LB. It is a warning, not a cause, measured SoH, or permission to weaken safety.

## Persistence and recovery

### Per-event JSONL

The authoritative scientific evidence under
`~/.config/ups-battery-monitor/events/`. Each record is canonical, hash-linked, newline-terminated,
and synchronized before it is acknowledged as durable. There is no SQLite database.

### `ModelOwner`

The single `src/adapters/model_owner.py` authority allowed to write the scientific model during
normal daemon operation. Safety polls and assessment decisions receive immutable projections or
prepared commit intents; no other runtime module writes `model.json`. The separately reviewed,
one-shot install/reference-transform tools are deployment operations, not a second live writer. A
runtime commit is accepted only for the bounded downward `ir_k` change and only after evidence,
epoch, rate, idempotency, and no-later-shutdown checks.

### Event-store writer lock

`src/adapters/jsonl_event_store.py` takes the exclusive
`~/.config/ups-battery-monitor/monitor.lock`. A normal storage-open failure degrades capture and
learning while safety continues. A conflict with another writer is fatal after the first safety
publication; operators must stop the competing process rather than remove the lock manually.

### `active.json`

A bounded durable registry for at most one active capture and a bounded FIFO of events awaiting
processing. It is recovery metadata, not a second observation journal.

### `index.jsonl`

A bounded, rebuildable projection of sealed event summaries. Reports and cohorts fail closed while
the projection is unavailable; raw event JSONL remains authoritative.

### Release A global journal

`~/.config/ups-battery-monitor/discharge-events-v1.jsonl` is the historical journal selected by ADR
0002. ADR 0003 supersedes it for new runtime capture. The file remains byte-for-byte unchanged as a
read-only archive and is never imported by the new runtime.

### Journald

The human and operational event log. It is useful for alerts and diagnosis, but it is not
scientific evidence and cannot replace missing JSONL records.

### Installer transaction

The bounded install step that verifies rendered systemd units before mutation and snapshots the
monitor unit, exact virtual-driver fragment, legacy drop-in, target/monitor wants links, NUT and
`upsmon` files, plus pre-install active/enabled state. On failure it restores files and runtime
state; inactive units stay inactive. Temporary transaction backups are removed afterward.
Operator-retained model and evidence backups follow the runbook.

### Gap and capture damage

A gap explicitly marks time that was not safely observed. Capture damage covers queue loss, writer
failure, or preserved corruption. Either remains visible and prevents affected records from
authorizing science; safety publication continues.

## Safety and NUT terms

### NUT — Network UPS Tools

The physical telemetry and shutdown ecosystem used by the monitor. The daemon reads NUT but does
not replace it or issue UPS test/power commands during automatic capture and learning. The current
read-only adapter connects to `localhost:3493` with a two-second socket timeout and uses the
`cyberpower` device by default.

### Virtual UPS

The monitor's atomic NUT-compatible safety projection. It publishes modeled runtime and status for
`upsmon`. Every successful poll publishes safety before handing the observation to persistence. Its
freshness limit is 30 seconds by default, derived from NUT transport timing; a telemetry outage beyond that limit is published as explicit
fail-safe `OB DISCHRG LB`, while a publication write failure invalidates the old file immediately.
On cold start with no physical evidence, the projection is unavailable rather than synthetic `LB`.
The existence of `/run/ups-battery-monitor/ups-virtual.dev` outside the managed lifecycle is not
freshness proof. The monitor removes it before start and after stop, publishes after a current
physical observation, and only then becomes ready for the ordered exact-instance driver. This is a
candidate contract, not a deployment claim.

### Virtual-driver lifecycle

The candidate exact-instance `nut-driver@cyberpower-virtual.service` is a projection of the monitor,
not an independent source. It uses `BindsTo=` and `PartOf=` on `ups-battery-monitor.service`, is
ordered after monitor readiness, and is not a member of `nut-driver.target`. The monitor lifecycle
unlinks the file before start and after stop. Fatal monitor starts are limited to three attempts in
five minutes with `RestartSec=10`; telemetry loss retries in process. A maintenance stop leaves this
projection unavailable and unmonitored until a fresh publication; it is not transparent.

### External NUT fixture

An isolated, temporary NUT 2.8+ test setup. The observed local receipt was NUT 2.8.1: unlinking
`.dev` alone while `dummy-ups` was alive retained stale `OL`, while stopping the driver made
`upsc` unavailable. It is test evidence only and does not prove live UPS deployment.

### Raw firmware LB

The physical UPS `LB` flag. It is stored as a diagnostic and may contribute to a separately gated
reserve trend. It does not directly set virtual `LB`, request FSD, end capture, or authorize
learning.

### CAL and self-test

`CAL` identifies a UPS calibration/self-test episode. It may prove operational detection,
durability, and reporting only. It is never natural-blackout comparison or learning evidence.

| NUT token | Meaning |
|---|---|
| `OL` | Online, powered by mains |
| `OB` | On battery |
| `DISCHRG` | Battery is discharging |
| `LB` | Physical firmware low-battery flag |
| `FSD` | Forced shutdown request |
