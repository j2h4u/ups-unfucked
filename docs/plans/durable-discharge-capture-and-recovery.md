# Durable Discharge Capture and 2026-08-14 Recovery Plan

## 1. Decision and scope

### Problem

The monitor's primary long-blackout path is incomplete. `DischargeCollector` keeps the
voltage/time/load trace in RAM, while system shutdown sends SIGTERM before mains can return.
The signal handler saves `BatteryModel` but does not persist or replay the active discharge.
Consequently, the 2026-08-14 blackout produced a safe host shutdown but no completed event,
capacity estimate, SoH update, Peukert update, or complete on-battery duration.

This is a production failure of the rare-event learning contract. With roughly two natural
blackouts per year, losing one event can lose half of the annual observational dataset.

### Approved direction

Use the Kaizen-led panel recommendation: **a small local append-only event journal plus a
narrow shutdown marker and idempotent boot replay**.

The journal is the durable source of truth for operational discharge observations. Grafana
Cloud remains an off-host forensic copy, not a runtime dependency. Scientific processing is
separated by evidence class so that a partial operational trace can improve runtime/trend
analysis without being mislabeled as an absolute capacity or SoH measurement.

### Blast radius

`host-level` and `data-integrity`:

- LB propagation and `upsmon` shutdown must remain independent and timely.
- A persistence failure must never prevent shutdown.
- A replay or classification error must never double-apply model updates.
- A scientifically incomplete event must never update authoritative capacity, SoH, or
  Peukert state.

### Goals

1. Preserve every sample already accepted by the monitor during OB, including across
   SIGTERM, crash, SIGKILL, reboot, and torn final writes.
2. Preserve event identity and provenance so replay is exactly once.
3. Continue an event after reboot when the UPS is still OB; otherwise close it as partial.
4. Keep raw operational evidence separate from derived battery-model state.
5. Recover the observable portion of the 2026-08-14 event from Grafana Cloud.
6. Make practical runtime-to-safe-shutdown learning possible from partial events.
7. Reserve absolute capacity/SoH/Peukert claims for evidence that satisfies an explicit,
   documented scientific gate.
8. Retain the existing NUT separation of concerns: the daemon publishes metrics/LB and
   `upsmon` performs host shutdown.

### Non-goals

- No SQLite, message queue, generic event bus, cloud ingestion framework, or event-service
  abstraction.
- No dependency on Grafana, Alloy, DNS, or WAN availability for local correctness or
  shutdown.
- No automatic deep-discharge test in this change.
- No claim that the 2026-08-14 event measured absolute capacity or SoH.
- No retrospective rewrite or deletion of the 13 existing LUT points from that event.
- No inference of unobserved UPS behavior between host shutdown and the next boot.
- No exact coulomb count from `load% * nominal power / voltage`; that remains a labeled model
  estimate only.
- No redesign of unrelated scheduler, replacement, UI, or alerting functionality.

## 2. Current evidence and recovery status

### Local evidence

- Blackout collection began at approximately `2026-08-14 15:11:10 +05`.
- Real-blackout classification stabilized one second later.
- Calibration batches reached 162 in-memory observations before shutdown.
- The virtual UPS crossed the five-minute LB threshold around `15:38`.
- `upsmon` initiated automatic power-fail shutdown.
- SIGTERM saved only `BatteryModel`; the raw buffer was lost.
- The next boot started around `17:38`.
- At incident diagnosis, `model.json` had `cycle_count=21`, but today's duration was absent from
  `cumulative_on_battery_sec`; `discharge_events=[]`; capacity sample count is zero; SoH is
  still the default 1.0; Peukert RLS sample count is zero.
- Thirteen deduplicated LUT points from this event remain. They are lossy, omit load and the
  full time series, and use SoC derived from the existing LUT. They are not an independent
  raw-event checkpoint.

### Grafana evidence already verified read-only

- Alloy scrapes `cyberpower-virtual` every 15 seconds and remote-writes to Grafana Cloud.
- The canonical Grafana API-token copy is
  `~/.secrets/grafana-cloud-api.env` as `GRAFANA_CLOUD_API_TOKEN`.
- The original readable raw-token copy remains at `~/.config/grafana-cloud/token`.
- The Alloy remote-write token is separate and stored root-only at
  `/etc/systemd/system/alloy.service.d/env.conf` as `GCLOUD_RW_API_KEY`.
- Direct Prometheus authentication with the Alloy token is the wrong query path. Queries must
  use the Grafana datasource proxy UID `grafanacloud-prom`.
- A live datasource-proxy query succeeded.
- For `15:05-15:45 +05`, `timestamp(network_ups_tools_battery_voltage{...})` found 133
  distinct real scrape timestamps, a maximum real scrape gap of 15 seconds, and a final real
  scrape at `15:37:59 +05`.
- Prometheus range evaluation repeated the final value after scraping stopped. Recovery must
  use `timestamp(metric)` to distinguish real stored samples from lookback repetitions.
- The completed recovery tool trimmed the first contiguous observed OB segment to 108 distinct
  real scrape timestamps. The private bundle is stored at
  `~/.local/share/ups-battery-monitor/recovery/2026-08-14/`; its manifest verifies 44 artifacts
  by size and SHA-256. It has not been appended to the production journal or model.

### Credentials inventory

The server convention is simple duplication into `~/.secrets` as the central inventory. It
does not require moving consumers, replacing existing files, symlinking, or reconfiguring
services.

- `~/.secrets/grafana-cloud-api.env` is mode `0600` and contains the API token variable plus
  purpose comments.
- The Alloy remote-write environment remains root-only in its systemd drop-in. A plain copy
  may be kept under `~/.secrets`; changing Alloy's consumer path is outside this plan.

## 3. Alternatives considered

### Approach A: finalize the current RAM buffer on SIGTERM

**Strategy:** Call the existing completion pipeline during orderly shutdown.

**Advantages:** Small code delta; captures today's exact systemd path.

**Rejection:** It does not survive SIGKILL, crash, kernel panic, or storage/power failure. It
also risks promoting a shutdown-interrupted operational trace into a completed scientific
measurement. It is a narrow helper only, not the solution.

### Approach B: atomic full-buffer checkpoint

**Strategy:** Rewrite one complete active-event JSON file after each poll using temp file,
sync, rename, and parent-directory sync.

**Advantages:** Simple replay; one authoritative snapshot; bounded parsing.

**Disadvantages:** Rewrites the full growing trace every ten seconds and loses audit history
unless completed snapshots are retained separately. Correct directory durability is easy to
omit.

**Disposition:** Acceptable fallback, but not selected.

### Approach C: append-only JSONL event journal — selected

**Strategy:** Append `start`, `sample`, and `end` records with a stable `event_id`; sync every
accepted OB record; replay the small journal on startup.

**Advantages:** Minimal writes, natural audit trail, survives all synced samples, torn tail is
recoverable, no dependency, and event volume is negligible at roughly two events per year.

**Costs:** Requires schema validation, event projection, idempotent replay, and explicit
corruption handling.

### Approach D: Grafana as primary storage

**Strategy:** Reconstruct each event from remote metrics.

**Rejection:** WAN, Alloy, credentials, retention, scrape cadence, Prometheus lookback, and
derived metrics make this unsuitable as the source of truth. Keep it as independent off-host
forensics.

### Approach E: rely on scheduled deep tests

**Strategy:** Replace rare natural events with periodic `test.battery.start.deep` runs.

**Rejection for this change:** It does not fix data durability, stresses VRLA batteries, and
requires a controlled test protocol. A single supervised test may be planned only after
durable capture has shipped and passed safety validation.

## 4. Scientific evidence classes

Every event must carry an evidence classification independent from lifecycle completion.

| Evidence class | Definition | Allowed use | Forbidden use |
|---|---|---|---|
| `operational_partial` | Natural/test discharge without validated start and endpoint | Voltage/load trajectory, runtime-to-safe-threshold, lower-bound delivered energy, trends | Absolute capacity, absolute SoH, Peukert fit |
| `operational_gapped` | Partial event containing reboot or telemetry gap | Same as partial, with gap-aware confidence reduction | Integration across unknown gap; authoritative model updates |
| `operational_complete_to_safety_threshold` | Observed from known online baseline to configured LB/shutdown threshold | Practical runtime model to the safe operating endpoint | Claim of full battery capacity |
| `controlled_quick_test` | Vendor quick self-test | Switch-over and basic health check | Capacity/SoH/Peukert |
| `controlled_capacity_test` | Fully charged/preconditioned, defined load/current, temperature context, defined endpoint, complete observation | Capacity and SoH; Peukert only if load/rate evidence is sufficient | Extrapolation beyond protocol limits |

Lifecycle values are separate:

- `open`
- `closed_power_restored`
- `closed_shutdown_requested`
- `closed_restart_recovered`
- `closed_corrupt_tail`

`closed` does not mean scientifically complete.

The existing rule "duration >= 300 seconds and delta-SoC >=15%" is a useful operational
quality filter, not a sufficient capacity-test definition. It must not be the sole gate for
absolute capacity or SoH.

Primary references:

- IEEE 1188: maintenance, testing, and replacement guidance for stationary VRLA batteries.
- IEC 60896-21/-22: VRLA test methods and requirements.
- EnerSys VRLA operation manual: controlled current, endpoint, time, temperature, and recharge
  requirements for capacity testing.
- NUT `upscmd`: instant commands are device/driver-specific and must be enumerated before use.

## 5. Storage and schema design

### Location

Use the existing writable and backed-up model directory:

```text
~/.config/ups-battery-monitor/discharge-events-v1.jsonl
```

Rationale:

- The service already has write access under its systemd hardening profile.
- The directory is already part of the battery-state backup set.
- No new root-owned directory or service permission is required.
- With approximately two events per year, indefinite retention is simpler than rotation.

File mode must be `0600`; directory mode must not exceed `0700`. Reject symlinks and
non-regular files before opening.

### Record envelope

Every line is one UTF-8 JSON object with a bounded maximum line size.

```json
{
  "schema_version": 1,
  "record_type": "start|sample|end|applied",
  "event_id": "uuid",
  "seq": 0,
  "boot_id": "linux-boot-id",
  "wall_time_utc": "RFC3339",
  "monotonic_ns": 123,
  "payload": {}
}
```

Required invariants:

- `event_id` is generated once on the first OL-to-OB transition and reused after reboot.
- `seq` starts at zero and strictly increases within an event.
- UTC time is for cross-boot correlation; monotonic time is authoritative only within one
  `boot_id`.
- Duplicate `(event_id, seq)` records are rejected during projection.
- Unknown schema versions are not replayed into the model.
- Only a final unterminated/invalid line may be treated as a torn tail and ignored with a
  prominent warning. Corruption in the middle is a hard journal-health failure.
- Parsing has bounds for line length, record count, numeric ranges, and string lengths.

### Start payload

- raw NUT status and input voltage;
- raw battery voltage and raw UPS load;
- event classification (`BLACKOUT_REAL` or `BLACKOUT_TEST`);
- predicted runtime snapshot;
- pre-event online baseline if available;
- configured shutdown threshold;
- source UPS name and daemon version.

### Sample payload

Store both observation and model input; one must never be represented as the other:

- raw NUT battery voltage, load, input voltage, and status;
- EMA voltage and EMA load passed to `DischargeCollector`;
- current classification;
- derived SoC and runtime as explicitly derived fields;
- journal health/error state at acceptance time.

Do not require firmware battery charge/runtime for replay. They may be stored as provenance but
remain vendor-derived observations.

### End payload

- lifecycle close reason;
- last accepted sequence;
- observed same-boot duration;
- explicit reboot gaps;
- evidence class;
- power-restored/cooldown outcome;
- model-processing eligibility and the reasons for acceptance/rejection.

### Applied record

An `applied` record records the model commit outcome and model content hash. It is an audit
record, not the only deduplication mechanism.

For completed scientific processing, `event_id` must also be stored in the existing nested
`discharge_events` entry. Replay checks that nested ID before model mutation. Avoid adding an
unbounded top-level compatibility key to the strict model schema.

Partial/gapped operational events remain in the journal and do not enter authoritative
`discharge_events`.

## 6. Durability and ordering

### Append path

1. Validate the journal path without following symlinks.
2. On first creation, create with `O_CREAT|O_APPEND|O_WRONLY` and `0600`.
3. Serialize one bounded JSON object plus newline.
4. Append one record without interleaving.
5. Call `fdatasync()` after each OB record.
6. On file creation, sync the parent directory once so the directory entry is durable.
7. Update in-memory journal health only after sync succeeds.

The daemon polls every ten seconds only during rare events, so per-sample syncing is the
correct Kaizen trade-off: simple semantics and negligible annual write volume.

### Safety ordering

LB/shutdown output has priority over telemetry durability:

1. Poll and calculate current safety state.
2. Attempt durable sample append.
3. If append fails, set persistent health degradation and log an alert.
4. Continue virtual UPS/LB publication and watchdog handling.

Journal failure is fail-visible but fail-open with respect to life/safety of the host. It must
never suppress LB or delay shutdown beyond the existing poll budget.

### Signal handling

The Python signal handler sets a stop request and records the signal number. It must not run
the scientific completion pipeline. Normal loop/finally shutdown attempts an end marker and
sync, then saves already-valid model state. If that final marker fails, boot replay closes the
open event from the last synced sample.

### Boot replay

1. Parse and validate the journal before normal learning begins.
2. Reconstruct the last event projection.
3. Poll current UPS state.
4. If the event is open and UPS is OB, continue the same `event_id` with a new `boot_id` and an
   explicit reboot gap.
5. If the event is open and UPS is OL, append `closed_restart_recovered`; end time is the last
   confirmed OB sample, not boot time.
6. Never integrate across an unobserved reboot gap.
7. Replay must be idempotent across unlimited repeated restarts.

### Model transaction

Current completion performs multiple mutations/saves. Refactor it so a scientifically eligible
event produces one complete derived result and one atomic `model.json` commit containing the
event entry with `event_id` and all accepted capacity/SoH/Peukert changes.

Crash ordering:

1. Build and validate the derived result without modifying persistent state.
2. Apply it to an in-memory copy.
3. Atomically persist the complete model.
4. Append the journal `applied` record.

If a crash occurs after step 3 but before step 4, replay sees `event_id` in the persisted event
and skips mutation, then repairs the missing audit marker.

Partial events are never applied to authoritative model state, avoiding exactly-once ambiguity
for incomplete evidence.

### Operational counters

Use the journal projection as the source for new transfer count and observed on-battery time.
The first journal event stores a one-time legacy baseline snapshot in its start payload. Do not
hardcode the incident-diagnosis values: the still-running old daemon may legitimately advance
them before deployment. For provenance, the incident snapshot was `cycle_count=21` and
`cumulative_on_battery_sec=490.8663840293884`; a later live check during implementation had
already advanced to `cycle_count=23` and approximately 495 seconds. The implementation must read
the actual current `BatteryModel` values when the first durable event starts.

New exported counters equal the immutable legacy baseline plus unique journal events/durations.
Do not increment counters again during replay. Do not count unknown reboot gaps as on-battery
time.

Older binaries will not understand new sidecar events after rollback; the raw journal remains
intact and can be replayed again after re-upgrade. Document that legacy counters may appear
frozen during rollback.

## 7. Cooldown and event-boundary correction

The current collector starts a 60-second power-restoration cooldown but monitor transition
handling can immediately process and clear the buffer on the first OB-to-OL poll. This can
split `OB -> OL -> OB` flicker into multiple processed events.

Required correction:

- `DischargeCollector` owns the single decision that an event is closed.
- First OL begins cooldown but does not invoke scientific completion.
- OB during cooldown cancels closure and continues the same `event_id`.
- Stable OL through cooldown appends the end record and emits one completion request.
- Monitor processes only that explicit completion request.
- `BLACKOUT_TEST -> BLACKOUT_REAL` inside one collection retains one event and records both
  classifications in provenance.

## 8. Component plan

### New: `src/discharge_journal.py`

Responsibilities:

- typed record/envelope definitions;
- safe path open and file-mode validation;
- append + fdatasync + directory sync;
- bounded parsing and torn-tail recovery;
- event projection and boot replay decision;
- journal health snapshot;
- no battery mathematics and no NUT commands.

Suggested interfaces:

```python
@dataclass(frozen=True)
class JournalHealth:
    healthy: bool
    open_event_id: str | None
    last_synced_seq: int | None
    last_error: str | None

class DischargeJournal:
    def start_event(self, sample: JournalStart) -> EventCursor: ...
    def append_sample(self, cursor: EventCursor, sample: JournalSample) -> EventCursor: ...
    def close_event(self, cursor: EventCursor, end: JournalEnd) -> None: ...
    def mark_applied(self, event_id: str, model_hash: str) -> None: ...
    def replay(self) -> JournalProjection: ...
```

### Modify: `src/discharge_collector.py`

- Inject `DischargeJournal`.
- Start journal event before/with the first accepted OB sample.
- Append every accepted sample after all raw/model-input fields are known.
- Retain `event_id` across test-to-real transition and cooldown.
- Return an explicit completed-event object only after stable OL cooldown.
- Stop directly incrementing counters that are now journal-derived.
- Keep LUT writes as existing behavior initially, but associate future writes with event
  provenance where possible; do not replay recovered partial events into LUT.

### Modify: `src/monitor.py`

- Create and replay journal during startup before learning.
- Separate safety calculation from persistence error handling.
- Ensure LB/virtual output runs despite journal errors.
- Replace immediate OB-to-OL completion with collector completion signal.
- Make signal handler lightweight; close/flush in normal shutdown/finally path.
- Expose journal health in health snapshot.

### Modify: `src/discharge_handler.py`

- Accept an immutable completed-event input carrying `event_id`, lifecycle, evidence class,
  samples, and quality reasons.
- Reject partial/gapped/quick events before capacity, SoH, or Peukert mutation.
- Calculate a complete derived result before persistent mutation.
- Commit all accepted derived state once.
- Include `event_id` and evidence provenance in nested discharge event metadata.

### Modify: `src/capacity_estimator.py`

- Keep operational duration/DoD checks as signal-quality gates.
- Require a separate controlled-capacity-test eligibility result before producing an
  authoritative capacity estimate.
- Make assumptions explicit: no measured current means estimated delivered energy, not a
  coulomb-counted capacity measurement.

### Modify: `src/model.py`

- Support event-level atomic commit without intermediate saves.
- Deduplicate completed event application by nested `event_id`.
- Preserve strict top-level schema validation.
- Sync the parent directory after atomic rename to provide the promised power-loss durability.
- Keep journal storage outside `model.json`.

### Modify: `src/virtual_ups_exporter.py` and `src/monitor_config.py`

- Export journal-derived cycle/on-battery counters.
- Add health fields:
  - `journal_healthy`
  - `active_event_id`
  - `journal_last_synced_seq`
  - `journal_last_error`
  - `pending_replay`
  - `recovered_partial_events`
- Bound/sanitize errors so health JSON does not leak paths or unbounded exception text.

### Modify: `scripts/battery-health.py` and `src/motd_status.py`

- Distinguish authoritative capacity/SoH samples from operational partial events.
- Show count/date of partial and recovered events without presenting them as capacity samples.
- Display degraded journal health prominently.

### New: `scripts/recover_grafana_discharge.py`

Offline-only tool; never imported by daemon.

- Read `GRAFANA_CLOUD_API_TOKEN` from the canonical
  `~/.secrets/grafana-cloud-api.env`, never CLI arguments.
- Query only through the Grafana datasource proxy.
- Fetch both values and `timestamp(metric)`.
- Remove Prometheus lookback repetitions by underlying scrape timestamp.
- Verify labels, cadence, units, gaps, duplicates, and cross-series timestamp alignment.
- Merge local journald transition/LB evidence.
- Emit a versioned recovery artifact and a journal-compatible partial event.
- Default to dry-run; never mutate `model.json`.
- Refuse any `capacity`, `SoH`, or Peukert import mode.
- Redact tokens and Authorization headers from logs/errors.

### Modify: `scripts/install.sh`

- Ensure journal file/directory ownership and mode are correct without following symlinks.
- Preserve an existing journal on reinstall.
- Do not add new NUT privileges or commands.

### Documentation updates

- `README.md`: truthful durability and evidence-class claims.
- `docs/USER-SCENARIOS.md`: interrupted shutdown, recovery, controlled-test prerequisites.
- `docs/GLOSSARY.md`: lifecycle vs evidence class; operational vs authoritative capacity.
- `docs/internal/CONTEXT.md`: journal ownership, replay, safety ordering.
- New ADR: reverse the old "timestamp dedup handles restart" decision and cite the 2026-08-14
  production incident.
- Incident report: exact timeline, lost/preserved data, Grafana recovery, corrective actions.
- Grafana-config docs: document that recovery queries must use API token + proxy and
  `timestamp(metric)`.

## 9. Recovery plan for 2026-08-14

### Preserve before interpretation

Create a private recovery directory outside Git:

```text
~/.local/share/ups-battery-monitor/recovery/2026-08-14/
```

Required artifacts, mode `0600`:

- unmodified `model.json` snapshot;
- prior-boot monitor journal export;
- prior-boot `nut-monitor` journal export;
- raw Grafana value query response;
- raw Grafana `timestamp(metric)` query response;
- normalized partial-event JSON;
- manifest with command versions, query strings, UTC/local window, file sizes, and SHA-256
  checksums.

Do not store tokens, Authorization headers, or expanded credentials in artifacts or shell
history.

### Grafana query contract

Endpoint:

```text
https://j2h4u.grafana.net/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query_range
```

Window with margin:

```text
2026-08-14 15:05:00 +05 through 15:45:00 +05
```

Metrics:

- `network_ups_tools_battery_voltage`
- `network_ups_tools_battery_charge`
- `network_ups_tools_battery_runtime`
- `network_ups_tools_ups_load`
- `network_ups_tools_ups_status`

Required source labels:

```text
instance="localhost:9199"
job="prometheus.scrape.nut_metrics"
```

For every metric, fetch `timestamp(metric)` and retain only distinct underlying scrape
timestamps. Range evaluation timestamps alone are not evidence of a fresh scrape.

### Validation

- Confirm a maximum real scrape gap of 15 seconds in the observed host-up interval.
- Confirm first OB and first LB against local journald within one scrape interval.
- Confirm last real scrape near `15:37:59 +05` and reject later repeated lookback values.
- Verify voltage/load are passthrough observations from the physical NUT input in current
  exporter code.
- Mark charge/runtime/status as model-derived or overridden where applicable.
- Keep the reboot-to-power-return interval explicitly unobserved.

### Permitted recovered conclusions

- Observed voltage/load trajectory while the host was alive.
- Runtime from observed blackout start to safe LB/shutdown threshold.
- Lower bound on delivered energy using a clearly labeled current/load model.
- Presence and timing of OB/LB transitions.
- Evidence for practical runtime-to-safety-threshold calibration and future trend comparison.

### Forbidden recovered conclusions

- Absolute battery capacity or SoH.
- Exact SoC from derived cloud charge.
- Exact Peukert exponent.
- Full blackout duration or battery behavior after host shutdown.
- Exact 10-second EMA buffer from 15-second Grafana data.
- Exact Ah without measured current.
- Internal resistance without a controlled current step and temperature context.

### Import policy

The first recovery run produces only a reviewed artifact. A separate explicit approval is
required before appending the partial event to the production journal. It must never directly
edit `model.json` or the current LUT.

## 10. Implementation phases and gates

### Phase 0: evidence preservation and security hygiene

1. Let the UPS reach and maintain full charge; do not run quick/deep/calibration commands.
2. Export and checksum all recovery artifacts.
3. Validate actual scrape timestamps and gaps.
4. Verify the canonical `~/.secrets` API-token copy exists with mode `0600`.
5. Record the incident and reverse the obsolete checkpointing decision.

**Gate:** Recovery artifact is complete, checksummed, private, and scientifically labeled.

### Phase 1: capture-only durable journal

1. Implement typed journal records, safe file handling, append, sync, replay projection, and
   health state.
2. Integrate start/sample/end capture without enabling recovered-event model learning.
3. Fix cooldown ownership and stable-OL completion signal.
4. Add boot continuation/closure behavior.
5. Export journal health.

**Gate:** All synced samples survive SIGTERM/SIGKILL simulations and LB still propagates under
all persistence failures.

Phase 1 may deploy independently because preserving the next observation has immediate value
even while scientific processing remains conservative.

### Phase 2: evidence classification and atomic model application

1. Add lifecycle/evidence classification.
2. Refactor completion into validate/calculate/one-commit.
3. Add event-ID deduplication and applied audit record.
4. Separate operational learning from authoritative capacity/SoH/Peukert gates.
5. Move exported new counters to the journal projection with the legacy baseline.

**Gate:** Replaying the same journal repeatedly leaves model hash and counters unchanged after
the first eligible commit; partial/gapped events leave authoritative model fields unchanged.

### Phase 3: offline recovery and review

1. Implement the Grafana recovery tool.
2. Produce the 2026-08-14 normalized partial event.
3. Review values, timestamps, gaps, provenance, and allowed conclusions.
4. With explicit approval, append it once to the operational journal.
5. Surface practical runtime-to-safety-threshold evidence without changing absolute capacity,
   SoH, Peukert, or LUT.

**Gate:** Production `model.json` authoritative fields are byte/value unchanged by recovery;
the operational event is queryable and auditable.

### Phase 4: supervised capacity-test design

Do not wait for another natural blackout, but also do not improvise a deep test.

1. Verify full charge and adequate post-charge rest.
2. Verify actual UT850EG commands with `upscmd -l cyberpower`.
3. Verify `test.battery.stop` behavior before considering deep test.
4. Define load/current, endpoint, temperature context, abort conditions, post-test recharge,
   and independent observation through the endpoint.
5. Decide whether the host can observe the required endpoint without compromising safe
   shutdown; if not, use an independent collector or do not claim a full capacity test.
6. Run a bounded rehearsal with virtual NUT only.
7. Obtain explicit user approval for the hardware deep test.

**Gate:** Written test protocol, abort path, recovery plan, and evidence gate approved. No real
UPS command is part of automated tests.

## 11. Test plan

### Unit tests: new `tests/test_discharge_journal.py`

- Create/open with correct ownership/mode and regular-file validation.
- Reject symlink and non-regular targets.
- Append start/sample/end and replay projection.
- Monotonic sequence and duplicate rejection.
- Unknown schema version rejection.
- Bounded line/field/numeric validation.
- Torn final line ignored with warning.
- Corrupt middle record degrades journal and prevents model replay.
- `fdatasync` and parent-directory sync invocation.
- EIO/ENOSPC/EROFS at open/write/sync.
- Event continuation across boot IDs and explicit gap representation.

### Unit tests: collector/handler/model

- One event ID across `BLACKOUT_TEST -> BLACKOUT_REAL`.
- One event across `OB -> OL -> OB` within cooldown.
- Stable OL emits one completion request.
- Partial/gapped/quick events do not mutate capacity/SoH/Peukert.
- Controlled eligible event yields one derived result and one save.
- Nested event-ID dedup prevents repeated application.
- Parent-directory sync after atomic model rename.
- Operational gates do not masquerade as controlled-test eligibility.

### Integration tests

- `OL -> OB -> samples -> SIGTERM -> boot OL` creates one partial event.
- Same path with boot still OB continues same event ID.
- SIGKILL after event open, after append, after sync, before/after model commit.
- Repeat boot/replay two or three times; no duplicates or counter drift.
- Torn tail and corrupt journal startup behavior.
- Persistence fault while runtime crosses five minutes; virtual UPS still emits LB and
  `upsmon` shutdown path is unaffected.
- Normal `OL -> OB -> stable OL` regression.
- Cooldown/flicker storm regression.
- Clock jumps forward/back; monotonic same-boot duration remains correct.
- Grafana, network, and Alloy absent; local correctness unchanged.

### Recovery-tool tests

- Token never appears in argv, stdout, stderr, or artifacts.
- Prometheus lookback repetitions removed via underlying timestamps.
- Gap, duplicate, stale, unit, and label validation.
- Cross-series alignment and status flags.
- Dry-run default and hard refusal to write `model.json`.
- Fixture based on the 2026-08-14 response shape.

### Real scenario smokes

During development use a fake NUT source and temporary model directory. Run targeted tests in
code-complete clusters. Do not run full gates in each small loop.

Before release candidate:

```bash
uv run pytest tests/test_discharge_journal.py
uv run pytest tests/test_monitor_integration.py tests/test_monitor.py tests/test_model.py
uv run pytest tests/test_capacity_estimator.py tests/test_discharge_handler.py
just check
```

Before deployment, run a virtual-UPS E2E scenario that proves an injected persistence failure
does not block LB. A green unit/build gate is not runtime proof; inspect real status propagation,
journal contents, replay outcome, and model hash.

## 12. Security and failure model

| Threat/failure | Impact | Required mitigation |
|---|---|---|
| Symlink replacement | Write outside intended state path | Refuse symlinks; verify regular file and ownership; restrictive modes |
| Torn tail/power loss | Last sample partial | One-line records, sync, ignore only final torn line |
| Middle corruption | Incorrect replay | Fail journal health; preserve file; no scientific application |
| ENOSPC/EIO/ROFS | Evidence loss | Loud health/log failure; never block LB/shutdown |
| Replay duplication | Double counters/model mutation | Stable event ID, sequence, atomic commit, nested event-ID dedup |
| Crash mid-model processing | Half-applied science | Calculate fully, one atomic model commit, then applied marker |
| Clock jump | Wrong duration | Same-boot monotonic time; UTC only for correlation |
| Reboot gap | Invented runtime/energy | Explicit gap; never integrate unknown interval |
| Malicious/huge journal | Memory/CPU exhaustion | Bounds on line, fields, records, values; streaming parse |
| Secret exposure | Grafana compromise | Token file only, `0600`, no argv/log/artifact token |
| Cloud outage | Recovery unavailable | Cloud is secondary only |
| Slow sync | Late LB | Safety ordering, latency measurement, E2E fault smoke |

The new journal contains no credentials or personal data. No new network listener, NUT command
permission, sudo rule, or external dependency is required.

## 13. Deployment and rollback

### Pre-deployment

- Preserve unrelated dirty `pyproject.toml`; do not overwrite it.
- Back up `model.json`, current config, and the new journal if it exists.
- Validate free space, ownership, mode, non-symlink paths, NUT reachability, active data root,
  and current service binary/commit.
- Ensure the UPS is online and charged; do not deploy during active discharge.

### Deployment

- Install code through the existing installer.
- Restart once while UPS is OL.
- Verify daemon active process and working directory, not only build/tests.
- Verify journal health endpoint, NUT physical/virtual values, watchdog, and `upsmon` primary.
- Run a bounded virtual event/restart replay smoke without issuing real hardware commands.

### Rollback

- Stop the new daemon while UPS is OL.
- Preserve, do not delete, `discharge-events-v1.jsonl` and recovery artifacts.
- Restore previous code/service and pre-deployment `model.json` only if required.
- Document that the old daemon will not project new journal-derived counters while rolled back.
- Re-upgrade can replay the retained journal; do not manually merge it into `model.json`.

No migration may destroy or rewrite the original event journal. Format evolution uses a new
schema version and explicit migration output.

## 14. Acceptance criteria

The change is complete only when all are true:

1. Every successfully synced OB sample survives process death and reboot byte/value-equivalent.
2. `OL -> OB -> SIGTERM -> boot` yields exactly one event with the same event ID.
3. Duration ends at the last confirmed OB sample; boot time and unknown gaps are excluded.
4. Repeated replay does not change model hash or counters after the first eligible commit.
5. Cycle count is not duplicated across restart or cooldown continuation.
6. Partial/gapped events never update absolute capacity, SoH, Peukert, or recovered LUT.
7. Controlled eligible events apply all accepted derived state in one atomic model commit.
8. Journal write/sync failures are visible but do not prevent timely LB/shutdown.
9. Stable-OL cooldown creates one completion; flicker continuation creates no split event.
10. Grafana recovery removes lookback repetitions and preserves the raw response and checksums.
11. The 2026-08-14 event is represented only as a partial operational trace.
12. No token appears in Git, argv, logs, tests, or recovery artifacts.
13. Targeted tests and `just check` pass at release-candidate cadence.
14. A real virtual-UPS runtime smoke demonstrates capture, replay, and LB independence.
15. Documentation no longer claims that timestamp dedup is discharge-buffer persistence.

## 15. Expert recommendation traceability

| Recommendation | Design location | Verification |
|---|---|---|
| Kaizen leads; avoid gold-plating | Sections 1, 3, 5 | No DB/queue/dependency; one JSONL sidecar |
| Durable per-sample capture | Sections 5-6 | SIGTERM/SIGKILL/torn-tail tests |
| Narrow SIGTERM behavior | Section 6 | Signal and boot-replay tests |
| Continue same event if boot is OB | Section 6 | Boot-OB integration test |
| Close partial if boot is OL | Section 6 | Boot-OL integration test |
| Stable event identity | Sections 5-6 | Replay and duplicate tests |
| Exactly-once model mutation | Section 6 | Repeated replay/model-hash test |
| Atomic all-stage model commit | Sections 6, 8 | Crash-before/after-commit tests |
| Partial is not scientific completion | Section 4 | Eligibility rejection tests |
| Separate lifecycle/evidence class | Section 4 | Classification matrix tests |
| Raw and model-input values both retained | Section 5 | Record round-trip tests |
| Grafana is secondary only | Sections 1, 3, 9 | Network-off integration test |
| Recover today's trace from Grafana | Section 9 | Checksummed artifact + review gate |
| Detect Prometheus lookback | Sections 2, 9 | Timestamp fixture test |
| Do not import recovery into model/LUT | Sections 3, 9 | Hard refusal test |
| Add only confirmed observed duration | Sections 6, 9 | Gap exclusion and counter tests |
| Preserve journal indefinitely | Section 5 | No retention/rotation feature |
| Journal failure never blocks LB | Sections 6, 12 | EIO/ENOSPC + LB E2E smoke |
| Torn tail recovery | Sections 5, 11 | Final-line corruption test |
| Middle corruption is not silently skipped | Sections 5, 12 | Corrupt-middle test |
| Parent directory durability | Sections 6, 8 | Directory-sync unit test |
| Fix cooldown/immediate completion conflict | Section 7 | Flicker/cooldown integration test |
| Counter replay cannot double count | Section 6 | Multi-restart test |
| No deep test before durable capture | Sections 3, 10 | Phase gate and runbook review |
| One supervised test after capture | Section 10 | Separate explicit approval gate |
| Controlled endpoint needed for capacity | Sections 4, 10 | Scientific eligibility tests |
| No exact current/Ah claim from load percent | Sections 1, 4, 9 | Output-label assertion |
| Expose journal observability | Section 8 | Health/MOTD/report tests |
| Keep secrets out of artifacts | Sections 9, 12 | Redaction tests |
| Keep a canonical API-token copy under `~/.secrets` | Sections 2, 10 | File-mode and value-presence verification |
| Preserve reversibility and raw evidence | Sections 9, 13 | Checksums and rollback smoke |

## 16. User decisions and approval gates

The user has approved producing this detailed plan based on the panel's selected JSONL
approach. Implementation still requires explicit approval.

Additional approvals required later:

1. Append the reviewed 2026-08-14 recovered partial event to the production journal.
2. Run any real UPS quick/deep/stop command.
3. Conduct a supervised hardware capacity test.

No such mutation or UPS command is authorized by this planning document.
