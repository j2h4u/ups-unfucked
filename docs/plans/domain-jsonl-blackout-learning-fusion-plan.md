# Fusion implementation plan: domain-first JSONL blackout learning

Status: implementation-ready candidate; implementation has not started.

This is the single authoritative plan for the next slice. It supersedes the active design direction in
`natural-blackout-learning-implementation.md` and `release-b-shadow-evaluation-implementation.md`; those
files remain historical planning evidence and are not implementation inputs after this plan is approved.

## 1. Product result

After this slice, the daemon runs unattended and every physical blackout automatically becomes one durable,
plain-language result:

1. the physical event and its one-second observations are preserved in its own JSONL file;
2. the event is assessed for data quality;
3. the model frozen at blackout start is compared with the observed portion of the curve;
4. independently observable load-step evidence is assessed for one named model parameter;
5. a safe, bounded parameter change is applied automatically when all scientific and safety gates pass;
6. otherwise the model is left byte-identical and the exact reason is reported;
7. comparable evidence is accumulated automatically to reveal possible battery decline over time.

No event waits for an operator or an agent. Human input remains limited to facts the UPS cannot reveal, such
as physical battery replacement. No additional instrument, current sensor, temperature sensor, bench
calibration, or manually entered reference measurement is assumed.

The slice is not complete if it only moves modules, archives telemetry, or produces shadow residuals. It must
prove the full physical-poll-to-outcome path and contain a real automatic commit path for evidence that passes
the gates below.

## 2. Non-negotiable invariants

- Physical NUT polling and virtual UPS publication remain one second.
- Safety publication happens before event persistence, assessment, comparison, or learning work.
- A raw physical `LB` token is a first-class diagnostic/evidence observation, not a shutdown command. It is
  durably recorded but never forces virtual LB/FSD. Virtual low-battery timing remains owned by the modeled
  shutdown threshold plus the existing unconditional two-minute hard floor, preserving Release A behavior.
- Learning may make low-battery timing earlier or unchanged, never later.
- Human/system logs and `index.jsonl` are diagnostics/projections, never scientific input.
- Raw UPS observations are the only truth input to parameter identification.
- Predicted SoC, predicted runtime, virtual LB, forward-model residuals, previous decisions, and previous model
  outputs never become labels for updating the same model.
- A grid-restored partial discharge is censored evidence. It is never reported as full runtime, measured
  capacity, SoH, or a completed battery discharge.
- No generic runtime multiplier, correction factor, ML model, or second compensating signal is introduced.
- Capacity, SoH, Peukert exponent, and the voltage/SoC LUT are not learned from a natural partial event. The
  only event-evidence-related LUT write in this slice is the one-shot, deploy-time, mathematically equivalent
  reference-frame transformation in section 10; baseline reset may create a fresh reference-0 default LUT.
  The transformation's expected fingerprint is registered before deployment.
- The daemon never issues a deep-test, battery-to-exhaustion, or forced ten-minute off-mains command.
- Every closed event reaches one terminal durable outcome: `learned`, `recorded_only`, or `rejected`.
- Any infrastructure retry is distinct from a scientific decision; there is no manual approval queue.
- Release A remains the live baseline until one complete release candidate passes all gates.
- Ruff, Tach, import-linter, CI lint configuration, and their existing findings are outside this slice. Existing
  parallel changes to their files are preserved and not edited or reverted.

## 3. Decisions and rejected alternatives

### 3.1 Architecture

Adopt the Claude Opus architecture as the baseline:

```text
physical adapters -> application use cases -> domain policies
                         |                    ^
                         v                    |
                   explicit ports -----------+

src/monitor.py = composition root + one-second safety loop only
```

Use plain Python frozen dataclasses, enums, functions, and Protocols. Do not introduce a DDD framework,
repository framework, event-sourcing framework, message bus, ORM, or database.

Rejected:

- preserving the current handler/collector/journal structure and adding more branches: retains the mixed
  ownership that caused scientific rules and writes to bypass invariants;
- a large generic domain framework: unnecessary for one daemon, one UPS, and one writer;
- a parallel old/new business path or long-running shadow daemon: creates two authorities and delays the
  actual cutover.

### 3.2 JSONL layout

Choose one flat event-specific JSONL file per blackout, one bounded atomic work registry, and one rebuildable
summary index:

```text
<model-data-dir>/events/                         mode 0700
  active.json                                   mode 0600, bounded atomic work registry
  index.jsonl                                   mode 0600, projection only
  evt-20260815T094107.123Z-<uuidhex>.jsonl       0600 open, 0400 sealed
```

This keeps the Opus per-event storage boundary but removes an unnecessary directory around a single file.
The canonical identity is a UUIDv4 lowercase hex value inside the records; the UTC filename timestamp is only
for sorting and human navigation.

Rejected layouts:

1. One global JSONL journal: unbounded replay cost and one corruption radius for all evidence.
2. Segmented global stream with cursor, isolation manifest, rebuild spool, and promotion: solves layout
   problems with more state machines and creates the forbidden second journal.
3. Per-event files plus a rich mutable JSON index: two apparent sources of truth and cross-file transaction
   complexity.
4. Daily/weekly JSONL buckets: events can cross buckets and corruption still affects unrelated events.

`index.jsonl` contains one bounded summary per terminal outcome. It may locate candidate blackout IDs for a
report or cohort, but a scientific cohort reopens at most 32 selected event files and revalidates their raw
records and hashes. Losing the index loses convenience, not evidence.

### 3.3 Identifiable parameter

The only automatically identifiable parameter in a natural partial event is the empirical within-battery
load-sag coefficient:

```text
ir_k: volts per UPS load percentage point (V/pp)
```

It is not resistance in ohms because `ups.load` is not measured battery current. Do not label it Ω or mΩ.

The physical estimate and the permission to apply it are separate decisions:

- the estimate is symmetric and is always stored exactly as measured after quality gates;
- the safety policy may refuse a scientifically valid estimate without modifying or clipping the recorded
  estimate;
- a refusal is visible and contributes to degradation reporting.

Rejected scientific alternatives:

- OL-to-OB sag: contaminated by charger removal and surface-charge collapse;
- capacity or SoH from a grid-restored partial: terminal capacity is not identified;
- capacity from nominal watts and `ups.load`: derived current/Ah is a proxy, not an independent measurement;
- external/manual load-to-current calibration: no such instruments or sensors exist and unattended operation
  may not depend on them;
- the Opus single global OLS `V = a + b*t + k*L` as the primary estimator: the physical sign is actually
  `V = a + b*t - k*L`, and one global time slope is confounded with load scheduling. The selected estimator
  still corrects unavoidable local discharge drift, but fits each stable plateau independently and
  extrapolates both to the same transition instant;
- the DeepSeek capacity commit path: it requires unavailable external current calibration and terminal
  evidence, so it is not a production path on this hardware.

## 4. Ubiquitous language and exact domain values

Use these terms consistently in code, JSONL, health output, CLI, MOTD, and documentation:

- `PhysicalObservation`: one raw UPS reading plus clocks; never derived.
- `Blackout`: one physical on-battery episode.
- `TerminationFact`: why observation stopped, not an assertion that the battery was exhausted.
- `FrozenModelSnapshot`: immutable scientific model captured at blackout start.
- `EvidenceAssessment`: pure judgment about provenance and data quality.
- `ForwardComparison`: observed minus predicted values over only the observed interval.
- `LoadStepEstimate`: one independent local estimate of `ir_k`.
- `IrCohortEstimate`: robust estimate from multiple qualifying steps and events.
- `LearningDecision`: separate decisions for history, comparison, decline evidence, and model commit.
- `ModelCommitReceipt`: durable before/after proof for one allowed model write.
- `TerminalOutcome`: `learned`, `recorded_only`, or `rejected`, with ordered reason codes.
- `ReserveCohortStatus`: bounded evidence of possible decline, never falsely named measured SoH.

All domain values are frozen dataclasses. The essential contracts are:

```python
PhysicalObservation(
    boot_id, monotonic_ns, wall_time_utc, raw_status,
    battery_voltage_raw, battery_voltage_v, voltage_token_quantum_v,
    load_percent, input_voltage_v: float | None,
)

FrozenModelSnapshot(
    schema_revision, evaluation_revision, battery_epoch_id, scientific_fingerprint,
    rated_capacity_ah, nominal_voltage_v, nominal_power_watts,
    soh, peukert_exponent,
    ir_k_v_per_pp, ir_reference_load_percent, lut,
)

EvidenceAssessment(
    evidence_class, duration_s, observation_count, coverage_ratio,
    max_gap_s, voltage_summary, load_summary, reasons,
)

ForwardComparison(
    mode, evaluation_origin, evaluated_duration_s, point_count,
    start_residual_v, end_residual_v, mean_residual_v, rmse_v,
    observed_slope_v_per_s, predicted_slope_v_per_s,
    delivered_ah_proxy, reasons,
)

LoadStepEstimate(
    step_id, blackout_id, segment_id, pre_sequences, post_sequences, transition_monotonic_ns,
    pre_slope_v_per_s, early_post_slope_v_per_s, late_post_slope_v_per_s,
    delta_load_pp, early_delta_voltage_at_transition_v,
    settled_delta_voltage_at_transition_v, voltage_quantum_v,
    k_transition_v_per_pp, k_settled_v_per_pp, quality, reasons,
)

IrCohortEstimate(
    battery_epoch_id, blackout_ids, step_count, up_step_count, down_step_count,
    median_k_v_per_pp, mad_ratio, reasons,
)

LearningDecision(
    record_history, compare_forward_model, record_decline_evidence,
    commit_ir_k,
)

ModelChange(parameter, value_before, measured_estimate, value_after,
            evidence_hashes, bound_applied)

ModelCommitReceipt(blackout_id, parameter, value_before, measured_estimate,
                   value_after, model_hash_before, model_hash_after,
                   scientific_fingerprint_before, scientific_fingerprint_after,
                   evidence_set_id, consumed_step_hashes,
                   reference_reparameterization, safety_oracle)

TerminalOutcome(disposition, assessment, comparison, cohort_estimate,
                learning_decision, commit_receipt, reasons)
```

Reason codes are typed and ordered, not free-form strings. Serialize at most eight reasons per decision plus
an overflow count. Unknown reason codes fail serialization. Infrastructure reasons and scientific reasons
are disjoint namespaces. Register `comparison_not_attempted` in the comparison-decision namespace as the
non-error outcome for an observed interval shorter than 180 evaluated seconds.

## 5. Domain decomposition and dependency direction

```text
src/domain/
  values.py                 frozen values and enums
  lifecycle.py              OL/OB/end/reboot state transitions
  readiness.py              rolling physical full-charge readiness policy
  timeline.py               one duration, coverage, and gap implementation
  evidence.py               provenance and quality gates
  forward_comparison.py     frozen forward-model comparison
  ir_identification.py      local step and cohort estimator
  learning.py               commit eligibility and bounds
  decline.py                conservative evidence-only trend policies
  reporting.py              plain-language sentences, no I/O

src/application/
  ports.py                  I/O Protocols
  capture_blackout.py       accept observations and lifecycle transitions
  close_blackout.py         assess -> compare -> identify -> decide -> commit -> outcome
  startup_recovery.py       bounded recovery of only the active event
  reporting.py              bounded health/event query orchestration

src/adapters/
  jsonl_event_store.py      files, hashes, fsync, work registry, index
  model_owner.py            immutable snapshot and sole transactional commit
  nut_telemetry.py          NUT reply -> PhysicalObservation
  report_writer.py          health JSON, CLI, MOTD integration

src/monitor.py              wiring, one-second read/EMA/safety publish, capture handoff
```

Dependency rules:

- `domain` imports only stdlib and existing pure `battery_math`/pure LUT functions;
- `application` imports domain and ports, never concrete adapters;
- adapters implement ports and contain no evidence or learning policy;
- `monitor.py` contains no scientific thresholds or model mutation;
- only `model_owner.py` may call the model's private atomic save;
- `reference_load_percent` has exactly one production source: the immutable model snapshot. The old config
  field/constant is removed after the cutover and a guard rejects a second source;
- the existing production safety API remains stable until differential tests prove any internal move.

Ports:

```python
class EventStorePort(Protocol):
    open(start: EventStart) -> EventHandle
    append(handle: EventHandle, record: EventRecord) -> EventHandle
    seal(handle: EventHandle, outcome: TerminalOutcome) -> SealedEventRef
    work_registry() -> WorkRegistry
    project(ref: EventRef) -> EventProjection
    index_tail(limit: int) -> tuple[EventSummary, ...]
    storage_health() -> StorageHealth

class BatteryModelPort(Protocol):
    current_snapshot() -> FrozenModelSnapshot
    commit(change: ModelChange) -> ModelCommitReceipt

class PhysicalTelemetryPort(Protocol):
    read() -> PhysicalObservation

class SafetyPublisherPort(Protocol):
    publish(result: SafetyPublication) -> None

class ReportSinkPort(Protocol):
    publish(report: PlainLanguageReport) -> None
```

`SafetyPublication` contains the virtual status token string, `lb: bool`, raw status, `raw_lb_observed: bool`,
event class, modeled runtime, and the virtual-LB source (`modeled_threshold`, `hard_floor`, or none). For every
event class, `virtual_lb = modeled_lb OR hard_floor_lb`; physical `LB` is carried separately and cannot enter
that expression. The safety class defaults to `BLACKOUT_REAL` until a vendor self-test is positively confirmed by
available `input_voltage_v >= 100 V` while raw status indicates battery operation
(`OB` or `CAL`). `CAL` alone is never confirmation: absent/low input voltage remains `BLACKOUT_REAL`. For a
positively confirmed test, existing test-mode FSD suppression remains. Once virtual LB has been published within a blackout, later classification cannot clear
it; only durable return to OL ends the sticky event-scoped floor.
`hard_floor` is the existing unconditional two-minute modeled-runtime floor and is reported separately from
the configurable shutdown threshold.

Supporting frozen port values are exact:

```text
EventStart(blackout_id, segment_id, first_observation, frozen_model,
           charge_readiness, evaluation_revision)
EventHandle(blackout_id, segment_id, path_token, next_seq)
EventRecord = Start | PhysicalObservation | Gap | End | DerivedRecord | TerminalOutcome
SealedEventRef(blackout_id, segment_ids, final_path_token, outcome_record_sha256)
PreparingCaptureRef(blackout_id, segment_id, path_token, canonical_start_record_utf8)
CapturingEventRef(blackout_id, segment_id, path_token)
ProcessingRef(blackout_id, segment_ids, final_path_token, frozen_stage, last_record_hash)
WorkRegistry(capture: PreparingCaptureRef | CapturingEventRef | None,
             pending_processing: tuple[ProcessingRef, ...])
EventProjection(start, observations, gaps, end, derived_records, outcome,
                trusted_prefixes, damaged_segment_hashes)
EventSummary(schema_version, blackout_id, segment_filename, started_utc, ended_utc,
             termination, evidence_class, disposition, duration_s, observation_count,
             battery_epoch_id, comparison_available, comparison_mode, ir_estimate_available,
             commit_receipt_id, damaged_segment_hashes, damaged_segment_overflow,
             outcome_record_sha256, event_file_sha256)
StorageHealth(capture_available, active_phase, queued_observations, durability_lag_s,
              index_available, rebuild_generation, rebuild_in_progress,
              rebuild_files_done, rebuild_files_target, rebuild_files_remaining,
              rebuild_last_progress_utc, rebuild_stalled, consumed_step_budget_remaining,
              event_count, total_bytes, free_bytes, alarm, bounded_error)
PlainLanguageReport(blackout_id, disposition, lines, generated_utc)
```

`close_blackout` is the only scientific completion path and is shared by live close and restart recovery.
`ModelOwner` serializes writes, exposes the current immutable safety snapshot by atomic reference swap, and
never exposes its mutable state. The poll uses one snapshot for the complete calculation; a first-OB capture
command carries that exact snapshot rather than asking a worker to read a possibly newer model later.

The model schema is intentionally revised with one exact policy-metadata object:

```json
"ir_learning_policy": {
  "battery_epoch_id": "uuidhex",
  "epoch_initial_k_v_per_pp": 0.015,
  "last_commit_utc": null,
  "consumed_step_hashes": []
}
```

The `0.015` value above is illustrative; the transform writes the actual live `k` at transform time as
`epoch_initial_k_v_per_pp`.
`consumed_step_hashes` is a unique canonical list capped at 256 SHA-256 values; reaching the cap refuses further
commits with `consumed_evidence_budget_exhausted` until an explicit physical battery replacement resets epoch.
Health and plain-language reporting expose the remaining slots before exhaustion.

It is initialized by the reference transform/reset and updated atomically with an IR commit. It is included
in the persisted model hash but excluded from the scientific fingerprint; only the applied `ir_k`, reference
load, and transformed LUT affect that fingerprint. Strict schema validation rejects missing/extra/mismatched
epoch metadata on every normal daemon load. The one-shot deploy transform is the sole exception: it uses a
separate transform-only source loader that accepts exactly the pinned Release-A schema, rejects every
missing/extra field, requires `reference_load_percent == 20`, and verifies the registered Release-A
fingerprint. It constructs the complete target state, including `ir_learning_policy`, in memory and validates
that state with the strict target schema before equivalence checks or any write. No runtime compatibility or
reverse migration is required.

The target schema delta is explicit. Remove `physics.rls_state` in full (`ir_k` and `peukert`); the transform
accepts only its exact pinned Release-A shape and drops it, while normal target loading rejects its presence.
Persisted LUT entries with `source == "measured"` are retained as read-only historical anchors and shifted by
the same `k_current * 20` as every other LUT entry so the reference transform stays equivalent. Delete
`_prune_lut`, measured-entry insertion, and every other LUT/RLS mutator during cutover; the canonical IR commit
does not edit the LUT. An import/call-site guard proves no scientific writer exists outside `ModelOwner.commit`.

Move the existing pure `soc_from_voltage(voltage, frozen_lut)` into `battery_math/lut.py`, add
`inverse_lut_voltage(soc, frozen_lut)` beside it as a pure clamped inverse, and rewire every production caller
to that single module. Cluster 1 and the frame-equivalence oracle cover both directions and every clamp edge;
the inverse is not assumed to exist already and domain code never imports `soc_predictor`.

## 6. Blackout lifecycle and one-second capture

Lifecycle states:

```text
idle -> preparing -> capturing -> processing -> sealed
                                  \-> capture_damaged -> sealed
```

- First physical OB creates a UUID, frozen model snapshot, readiness snapshot, and `start` record. A CAL-only
  episode also creates an event for operational history, but its terminal disposition can only be
  `recorded_only` or `rejected`; it never supplies natural-learning evidence.
- Every successful physical read while OB becomes a raw `observation` at one-second cadence.
- Status changes, raw LB/CAL, last OB before OL, gap, and end are immediate records.
- Return to OL creates `end(power_restored)`; it never implies battery exhaustion.
- A service stop while OB creates `end(service_stop)` after the last durable observation.
- Reboot while OB adds a cross-boot `gap` and continues the same event for history, but automatically denies
  scientific use across the gap.
- If startup finds the UPS OL, the stranded event closes at its last durable observation as
  `closed_restart_gap`.
- `end` without `outcome` schedules exactly one close use case after the first new safety publication.
- `input.voltage` is optional because a valid physical reply may omit it. Raw OB status remains authoritative
  for starting a real blackout; missing input voltage adds `input_voltage_unavailable` to provenance but does
  not turn OB into OL or synthetic evidence.
- `nut_telemetry.py` retains the original NUT battery-voltage decimal token before numeric conversion so
  `voltage_token_quantum_v` is reproducible; the current parser's lossy numeric-only behavior is replaced at
  this adapter boundary.
- `boot_id` is the Linux kernel boot ID read once from `/proc/sys/kernel/random/boot_id` at daemon start, not a
  per-process UUID; every record in that daemon process carries the frozen value.

Safety ordering for every successful poll:

```text
read physical UPS
  -> validate and update EMA
  -> classify physical state
  -> calculate current safety result
  -> publish physical/virtual status and LB
  -> hand immutable observation to capture
```

Persistence must not stall the next one-second safety publication. Use two deliberately small local lanes,
not a general message bus:

- `CaptureWriter` is the sole event/model writer. It drains a bounded priority deque, always serving
  `start`, `end`, `gap`, segment-open, and recovery/commit receipts before ordinary observations. Eight
  reserved lifecycle slots, sized from the enumerated lifecycle records plus one recovery receipt, cannot be
  consumed by observation backlog; the observation capacity is 120 seconds.
- `AssessmentWorker` is read-only. It processes sealed/processing evidence, bounded cohort reads, forward
  comparison, and dense safety oracles, then returns frozen canonical derived records or a commit request to
  `CaptureWriter`. It never holds the capture writer or model-owner lock while calculating.

Queue overflow or worker failure sets `capture_available=false`, creates a durable gap/outcome when any
writable segment exists, and disables scientific use; safety keeps running. A new OB is accepted even while
assessment or recovery is running. Tests prove deterministic ordering, reserved lifecycle capacity, no
duplicate sequence numbers, and visible loss rather than silent loss.

The durability guarantee begins when the sole writer can accept the first command. If the process or host is
hard-killed before that command becomes durable while event storage is unwritable, the RAM-only first sample
cannot be reconstructed from the available sensors. Adding a second synchronous journal solely to disguise
that physical boundary is out of scope. Graceful service stop instead waits boundedly for lifecycle capacity,
persists the retained start with an explicit gap and terminal outcome, and only then drains the writer.
Permanent storage failure remains visible in health even when no event bytes can be created.

One-second durable writes are deliberate: blackouts are rare, the volume is small, unexpected load steps
need both pre/post samples, and short outages must have a useful history. Do not revert to ten-second storage
or adaptive burst capture in this slice.

NUT may refresh an unchanged value only about every two seconds. Repeated raw values remain separate timed
observations and are valid for plateau stability; hash/sequence deduplication removes only duplicate delivery
of the same record, not equal sensor values at different monotonic times.

## 7. JSONL schema, durability, and recovery

Every record uses schema version 2. A stable `blackout_id` groups the physical episode; a `segment_id`
identifies a writable file segment created after corruption or an unavoidable capture split:

```json
{
  "schema_version": 2,
  "record_type": "observation",
  "provenance": "physical",
  "blackout_id": "uuidhex",
  "segment_id": "uuidhex",
  "seq": 1,
  "boot_id": "...",
  "wall_time_utc": "...",
  "monotonic_ns": 123,
  "prev_record_sha256": "...",
  "payload": {},
  "record_sha256": "..."
}
```

Record types:

- physical: `start`, `observation`, `end`;
- system fact: `gap`;
- derived: `assessment`, `comparison`, `ir_estimate`, `learning_decision`, `model_commit`, `outcome`.

`start` includes the first raw OB observation, charge readiness, battery epoch, evaluation revision, and the
frozen model snapshot. Derived EMA values, if useful for diagnostics, live under an explicitly derived field
and never replace raw values. `outcome` is mandatory. After outcome, chmod the event file to `0400`.

Canonical serialization is UTF-8 `json.dumps(record_without_record_sha256, sort_keys=True,
separators=(",", ":"), ensure_ascii=True, allow_nan=False)`. Finite floats use Python's shortest
round-trip representation. `record_sha256` hashes those exact bytes without a newline and without the hash
field itself; the stored line is the same canonical object with `record_sha256` added, followed by exactly
one `\n`. A golden-byte fixture locks this representation to the supported Python version.

Hard bounds: encoded line at most 128 KiB; frozen snapshot at most 64 KiB; work registry at most 128 KiB;
reason code at most 64 ASCII bytes; diagnostic error at most 512 bytes; at most eight reasons plus overflow.
Snapshot/line overflow preserves raw history but sets `snapshot_budget_exceeded` and authorizes neither
comparison nor commit.

For any single-boot evaluation interval, `coverage_ratio = sum(accepted_edge_dt_s) /
(last_monotonic_s - first_monotonic_s)`, where an accepted edge is finite, strictly increasing, and no longer
than the purpose-specific maximum gap. Zero/non-positive span has zero coverage. Timeline boundary fixtures pin
the numerator, denominator, duplicate timestamps, and gaps; callers still apply their separate max-gap gate.

The fixed-shape `index.jsonl` line is at most 4 KiB and contains only: schema version, blackout ID, segment
filename, start/end UTC, termination, evidence class, disposition, duration, raw observation count, battery
epoch, comparison availability, comparison mode (`full`, `short_window`, or `none`), IR-estimate availability,
commit receipt ID, at most 16 damaged-segment hashes, damaged-segment overflow count, outcome-record SHA-256,
and event-file SHA-256. It contains no raw series and no authoritative
estimator inputs. The outcome hash remains inside the 4-KiB bound.

Durability rules:

- reuse the existing process-wide non-blocking writer lock;
- reject symlinks and non-regular files; use `O_NOFOLLOW|O_CREAT|O_EXCL` for new event files;
- write one complete newline-terminated record and `fdatasync` before acknowledging it as durable;
- fsync the parent directory after create, rename, and atomic work-registry replacement;
- `active.json` contains exactly one optional `capture` slot and a FIFO `pending_processing` of at most eight
  small `ProcessingRef` values. It changes only on phase/file transitions; sequence/hash comes from a bounded
  event tail, so the registry is not rewritten every second;
- during `preparing`, registry-first order is mandatory: atomically write a `PreparingCaptureRef` containing
  exactly one newline-terminated, bounded, strict-schema/canonical-hash-validated start record and fsync the
  registry parent before creating and append-syncing the event file. If recovery finds that ref but no event
  file, recreate the file from those exact frozen bytes idempotently by `blackout_id`. Only after create,
  byte-identical start append+sync, and event-parent-directory fsync succeed may the writer atomically replace
  it with the smaller `CapturingEventRef`; it never synthesizes a second start. This
  closes the start/registry crash window without becoming a second observation journal;
- durable `end` atomically moves the capture reference into `pending_processing` and clears the capture slot in
  one replace, immediately freeing capture for a new OB. The processing reference remains in the FIFO until
  its event has both a durable sealed outcome and an idempotently durable summary in the healthy index or
  active rebuild delta. If the FIFO is full, the writer appends and seals a durable
  `rejected(processing_backlog_full)` outcome in that event instead of silently dropping it;
- a byte-identical duplicate `(blackout_id, segment_id, seq)` is idempotent success and is not appended when
  detected. If an ambiguous crash leaves duplicate lines, readers keep the first and ignore later
  byte-identical copies for counts/coverage; differing bytes are a conflict and fail science closed;
- truncate only trailing bytes after the last newline-terminated, strict-JSON, canonical-hash-valid record
  when those trailing bytes either fail strict parse/hash validation or lack a newline. A newline-terminated
  invalid line or any invalid record followed by more bytes is middle corruption and is never truncated;
- preserve a corrupt file as `corrupt-<sha>-evt-...jsonl` and, if still OB, open a new segment under the same
  `blackout_id`. The final writable segment contains the aggregate `capture_damaged` outcome, the first 16
  damaged-segment hashes in canonical segment order, and `damaged_segment_overflow` for the remainder. Every
  corrupt segment remains separately preserved and discoverable by filename; the index projects the same
  bounded fields. Its disposition is `rejected`,
  history remains available from trusted segments, and comparison/identification/commit are denied with
  `capture_damaged`;
- event-file failure does not corrupt another event;
- index append failure makes reports/cohorts unavailable but does not invalidate the event file;
- index repair is automatic bounded lazy maintenance after the first safety publication: at most 32 event
  files or 4 MiB per 60-second reporting tick. `index-rebuild.cursor.json` (0600, atomic replace plus parent
  fsync) records generation ID, the lexicographic target-last filename captured at generation start, last
  projected filename and SHA-256, rebuild output offset/hash, files done, and target count. Summaries append+sync to
  `index.rebuild.in-progress.jsonl`; every tick resumes strictly after the validated cursor. Restart validates
  cursor against the rebuild tail and resumes; mismatch discards only that projection attempt and starts a new
  generation. While rebuild is active, `CaptureWriter` appends+syncs each newly sealed fixed-shape summary to
  `index-rebuild.delta.jsonl`, idempotently keyed by `(blackout_id, outcome_record_sha256)`, instead of the unavailable
  main index. On reaching the scan target, an AssessmentWorker promotion request is serialized through
  `CaptureWriter`; the writer drains and deduplicates the complete delta into the rebuild file, syncs it,
  atomically renames the rebuild over `index.jsonl`, fsyncs the directory, and removes cursor/delta. Since the
  same writer serializes outcome summary append and promotion, no new event can fall between delta drain and
  rename. Restart resumes both cursor and delta without loss or duplication. Capture/history continue while repair runs;
  cohort science reports `cohort_projection_unavailable` until the target generation is promoted. Health
  exposes generation, files done/target/remaining, last-progress UTC, and stalled status. This never runs on
  startup or the safety poll and never requires an operator;
- startup reads only the bounded registry and at most the capture slot's event tail before the first safety
  poll. It does not open pending-processing event files until after that publication.

Seal/projection order is exact: append+sync `outcome`, `fchmod(0400)`+sync the event, append+sync its fixed-shape
summary to healthy `index.jsonl` or the active rebuild delta idempotently by
`(blackout_id, outcome_record_sha256)`, and
only then atomically remove its processing reference from `active.json`. Projection failure retains the
processing reference and exposes `projection_unavailable`; it never invalidates or reopens the sealed event.
Recovery first verifies the sealed outcome, then ensures the matching summary exists in the currently active
projection destination, and only after that removes the reference. The same key plus byte-identical canonical
summary bytes is idempotent success; the same key with different bytes is `projection_conflict`, retains the
processing reference, and denies reporting/cohort science. Fault injection covers every boundary. Model
commit order:

- crash before durable outcome: `active=processing`; recovery resumes the frozen close stage;
- crash after outcome but before chmod: recovery verifies outcome and seals, without recomputing science;
- crash after chmod but before summary append: recovery retains the reference and idempotently appends the
  summary to the healthy index or active rebuild delta;
- crash after summary append but before registry removal: recovery verifies the matching
  `(blackout_id, outcome_record_sha256)` projection and removes the reference without duplicating the summary;
- there is no state in which the only processing reference is removed before a durable summary exists;
- no crash window reopens a sealed file for different bytes.

```text
durable learning decision and exact intended bytes
  -> fsynced pre-commit model snapshot
  -> atomic model save
  -> durable model_commit receipt
  -> durable outcome
```

The durable decision contains canonical `ModelChange` bytes, `model_hash_before`, and precomputed
`expected_model_hash_after`. Recovery is total: current hash equals `hash_before` -> apply exactly the
canonical change; it equals `expected_hash_after` -> reconstruct only the receipt; otherwise emit
`model_state_conflict` and perform zero writes. The idempotency key is
`(blackout_id, parameter, evidence_set_id)`. Recovery never recomputes a candidate from a mutable live model.

The current `discharge-events-v1.jsonl` remains byte-identical as a read-only archive. New runtime code never
opens or imports it. No backward or reverse migration is required.

## 8. Evidence assessment and forward comparison

All duration/integration uses same-boot monotonic edges. Wall time is display metadata only.

Forward comparison common gates:

- natural physical OB; no CAL;
- supported frozen snapshot from the current battery epoch;
- coverage at least 0.90;
- maximum raw gap at most 5 seconds and no reboot gap;
- finite battery voltage in 8--15 V, matching the production validator, and valid load in 0--100%.

After common gates, select exactly one mode in this precedence order:

1. `full`: `evaluated_duration_s >= 300` and normalized endpoint movement
   `abs(Vnorm_observed_end - Vnorm_observed_start) >= 0.20 V` over the evaluated interval, using
   `Vnorm = V + frozen_k * (load - frozen_reference_load)` from the start snapshot.
2. `short_window`: `evaluated_duration_s >= 180` and at least one qualifying stable comparison segment wholly
   inside the evaluated interval of at least 120 contiguous same-boot seconds, load population standard
   deviation at most 2 pp, no raw gap above 2.5 seconds, and finite valid voltage/load throughout.

If both qualify, `full` wins. If neither qualifies, comparison is refused with an exact duration/movement or
stable-segment reason. An accepted short branch carries typed reason `short_window_comparison` in the derived
JSONL comparison, `comparison_mode=short_window` in the index, and an explicit short-window label in CLI/MOTD.
It validates or falsifies only the observed upper curve. The comparison result is never an identification
input and by itself authorizes zero model writes; it is never eligible for a decline cohort, whose independent
section 11 gates remain unchanged.

The first 60 seconds after OL->OB remain history-only because charger removal and surface-charge collapse
contaminate them. Evaluation origin is the midpoint of the earliest complete 31-observation window beginning
at least 60 seconds after OB with load standard deviation at most 2 pp, no gap above 2.5 seconds, and valid
raw voltage/load. `V_0` and `load_0` are the respective window medians; comparison/integration begins at that
midpoint. If no origin exists, comparison is refused with `no_stable_post_transfer_origin`.

Use the existing pure battery-math kernel and the frozen start snapshot to predict only the evaluated observed
interval. The algorithm uses configured rated capacity and SoH exactly once:

```text
Vnorm_0 = V_0 + k * (load_0 - reference_load)
soc_0 = soc_from_voltage(Vnorm_0)

for each accepted same-boot monotonic edge i -> i+1:
    load_mid = (load_i + load_i+1) / 2
    T_full_healthy_h = peukert_runtime_hours(
        load_mid, rated_capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts,
    )
    T_full_effective_s = 3600 * T_full_healthy_h * soh
    soc_i+1 = clamp(soc_i - delta_t_s / T_full_effective_s, 0, 1)
    Vnorm_pred_i+1 = inverse_lut_voltage(soc_i+1)
    Vpred_i+1 = Vnorm_pred_i+1 - k * (load_i+1 - reference_load)
```

Zero/invalid runtime yields deterministic comparison refusal. Do not pass an already SoH-adjusted capacity
and multiply by SoH again. Every signed residual is:

```text
observed - predicted
```

Store start/end/mean/RMSE voltage residual, observed/predicted slope, and delivered-Ah proxy. The proxy must
always include `proxy` in its code and user-facing name. Comparison failure creates a deterministic reason and
zero model writes.

The legacy LUT was calibrated from raw voltage near 20% load and documents up to 5% SoC frame uncertainty.
After the equivalent frame shift, all LUT consumers use normalized voltage in the new frame. Measured-LUT
writes and raw-voltage DoD/`days_since_deep` writes are removed with the old handler; baseline reset creates a
single-frame LUT. For reporting, convert `soc_pred +/- 0.05` through the inverse LUT into a local voltage
uncertainty interval. A residual inside it is reported as `within_known_lut_frame_uncertainty`; a residual
outside it is disagreement beyond that known floor. No causal battery claim is made from the former. The
dense equivalence oracle enumerates every remaining LUT call site and a guard forbids future raw-voltage LUT
writes.

Shorter events still record history and may contribute a valid load-step estimate if the step-specific gates
pass; they are not falsely rejected as useless.

## 9. Independent `ir_k` identification

### Step detection and independent windows

Analysis scans raw observations after event close. A candidate transition at sequence `t0` is the first
sample after re-arm for which:

```text
pre_load = median(load[t0-15 .. t0-1])
post_load = median(load[t0+10 .. t0+25])
abs(post_load - pre_load) >= 15 pp
abs(load[t0] - pre_load) >= 10 pp
```

All referenced sequences must exist. The transition must reach the new plateau within five seconds and stay
within +/-2 pp of `post_load` through `t0+120`; otherwise it is not a settled step. A contiguous run of
qualifying candidate instants represents one transition; choose its earliest `t0`. Accepted windows never
overlap, accepted transitions are at least 180 seconds apart, no observation sequence participates in two
accepted steps, and at most two steps per event enter a commit cohort. Re-arm occurs only after the complete
late window plus 30 consecutive seconds on a stable plateau. The stable ID is SHA-256 of the canonical UTF-8
JSON array `[blackout_id, segment_id, last_pre_seq, first_post_seq]` using the section 7 encoding.

### Drift-corrected estimate

Fit raw voltage separately against monotonic seconds centered on `t0` for the pre-step plateau, the early
post-step plateau, and a late quasi-steady plateau:

```text
V_pre(t)   = a_pre   + b_pre   * (t - t0)    over t0-15 .. t0-1
V_early(t) = a_early + b_early * (t - t0)    over t0+10 .. t0+25
V_late(t)  = a_late  + b_late  * (t - t0)    over t0+60 .. t0+120

delta_load = median(load_post) - median(load_pre)              [pp]
delta_voltage_early_at_t0 = a_early - a_pre                     [V]
delta_voltage_settled_at_t0 = a_late - a_pre                    [V]
k_transition = -delta_voltage_early_at_t0 / delta_load          [V/pp]
k_settled = -delta_voltage_settled_at_t0 / delta_load           [V/pp]
```

All voltage plateaus are compared at the same inferred transition time, so monotonic discharge drift between
windows is not silently attributed to load. `k_transition` is stored as diagnostic evidence. The identified
quantity matching the EMA-domain safety model is `k_settled`; only it may enter a cohort. Require
`abs(k_settled - k_transition) / k_transition <= 0.15`; otherwise store the observation with
`sag_not_settled` and refuse cohort use. These are local nuisance-slope fits, not the rejected single global
load/time regression.

Inputs are only raw battery voltage, raw load percentage, monotonic time, boot ID, and raw status. The
function signature cannot accept a model snapshot, SoC, runtime, LUT, residual, or previous `ir_k`.
Signature-level, import-boundary, and bounded call-graph tests are the primary anti-feedback enforcement; an
AST check is supplemental.

All step gates are inclusive:

- raw status contains OB and contains neither CAL nor LB;
- one boot and no gap greater than 2.5 seconds in any window;
- pre-window contains 15 raw one-second points before the detected step;
- early post-window uses 16 raw points from 10 through 25 seconds after the step;
- late post-window uses all 61 raw points from 60 through 120 seconds after the step;
- absolute load change at least 15 percentage points;
- both plateau loads greater than 0% and at most 50%;
- load standard deviation at most 2 pp in every plateau;
- each absolute fitted plateau slope is at most 0.002 V/s;
- `max(abs(b_pre), abs(b_early)) * 25 <= 0.10 * abs(delta_voltage_early_at_t0)`;
- `max(abs(b_pre), abs(b_late)) * 120 <= 0.10 * abs(delta_voltage_settled_at_t0)`;
- voltage token quantum `q` is derived from the retained raw decimal token; absolute extrapolated voltage
  change in both early and settled estimates is at least `max(0.15 V, 3*q)`;
- voltage and load move in opposite directions;
- all voltages finite and in 8--15 V;
- both `k_transition` and `k_settled` in 0.005--0.040 V/pp;
- transition/settled relative disagreement at most 0.15.

Commit cohort gates:

The candidate universe is deterministic: the current blackout plus the immediately preceding at most 31
terminal events from the same battery epoch in canonical `(started_utc, blackout_id)` order. Reopen and hash-
validate every selected event file. Recompute every candidate from raw records with the current estimator
revision; stored derived `ir_estimate` records are diagnostic-only and never estimator inputs. For each event,
select the first two settled step positions in monotonic transition order before applying consumed or
qualifying filters, then drop consumed and non-qualifying members. An event whose two selected positions are
consumed contributes nothing later; a third step never moves into the cohort. Never search older events or
drop an inconvenient qualifying step to improve dispersion. `candidate_event_overflow` reports how many older
same-epoch events were outside the fixed window. If the projection is unavailable/incomplete, commit is denied
with `cohort_projection_unavailable` rather than scanning history or cherry-picking.

- at least four unconsumed qualifying steps total: at least one from the current blackout plus at least three
  other steps;
- at least two different blackout events;
- exactly one battery epoch;
- exactly one current `evaluation_revision` across all recomputed cohort members; mixed revisions refuse with
  `mixed_evaluation_revision`;
- at least one upward and one downward load step;
- robust dispersion `MAD / median <= 0.25`.

Gate evaluation first proves `k_transition` and `k_settled` finite and inside the positive physical bounds,
then evaluates their relative disagreement, so the ratio cannot divide by zero. The cohort estimate is the
median of qualifying `k_settled` values. `index.jsonl` may identify the event files,
but the estimator reads and validates raw event records and their hashes. The estimate is stored unchanged
even if the safety policy refuses it.

## 10. Safe automatic model update

The current formula is:

```text
V_norm = V + k * (load - reference_load)
```

At the current 20% reference load, any change in `k` is conservative on one side of 20% and less conservative
on the other. Therefore Claude's replay of only the current event cannot prove future-load safety.

Before enabling automatic IR commits, perform one atomic equivalent reparameterization:

```text
reference_load_old = 20
reference_load_new = 0
lut_voltage_new[i] = lut_voltage_old[i] + k_current * 20
```

With the existing `k`, this shifts normalized voltage and every LUT voltage by the same amount, so SoC,
runtime, virtual status, and LB must remain equivalent across the complete input grid. The release candidate
must abort transformation unless the source reference is exactly 20, the model schema/fingerprint is expected,
the pre-transform snapshot is durable, and the dense pre/post oracle is equivalent within declared numeric
tolerance.

Equivalence tolerances are `abs(delta_soc) <= 0.005`, `abs(delta_runtime) <= 1.0 second`, identical non-LB
status tokens, and exactly identical LB transitions. The grid includes load 0--100%, all LUT breakpoints,
both clamp edges and values immediately above/below them, valid raw voltage 8--15 V, SoH/Peukert boundaries,
and every remaining LUT caller. The expected post-transform fingerprint is computed from the exact canonical
state bytes before production write.

Execution is a dedicated one-shot `scripts/reparameterize-ir-reference` command. It acquires the existing
writer lock non-blockingly, invokes a transform-only Release-A loader rather than normal `ModelOwner.load`,
and is the only deploy-time process allowed to call the transformation save. That loader validates exactly the
pinned Release-A source schema, `reference_load_percent == 20`, and registered source fingerprint; it then
builds the complete target state (including fresh `ir_learning_policy`) in memory, passes it through the same
strict target validator used by `ModelOwner`, and runs the dense equivalence oracle before atomic save. It
rejects missing/extra source fields, a target-validation failure, and reruns against already transformed state
without changing bytes. It never runs inside daemon startup. Production order is exact: stop Release A,
verify it released the writer lock, acquire that lock in the one-shot command, verify/retain the pre-transform
backup, apply the canonical transformation, verify the actual fingerprint equals the pre-registered expected
fingerprint, release the lock, then start the candidate. The command aborts without writes if any process holds
the lock. Candidate startup refuses to become ready with `ir_reference_frame_not_transformed` when persisted
reference load is not zero; it never transforms state implicitly.

After reparameterization, for all non-negative loads, decreasing `k` cannot increase normalized voltage,
SoC, or runtime. Commit policy:

```text
deadband = 0.001 V/pp

if measured_k >= current_k - deadband:
    no commit
else:
    value_after = max(0.005, measured_k, 0.80 * current_k)
```

After calculation, require `value_after <= current_k - deadband`; otherwise record
`ir_change_below_noise_floor` and perform no commit.

Thus one commit changes at most -20%. Run a dense numerical oracle over load 0--100%, LUT breakpoints, the
valid OB voltage range, runtime, and LB transitions. Require runtime after to be no larger than before within
numeric epsilon and LB to be identical or earlier everywhere. Failure refuses the commit; it never adjusts
the physical estimate to manufacture a pass.

An estimated increase is stored as `observed_load_sag_increase` with before/estimate/evidence and reason
`unsafe_upward_ir_change_not_applied`. This is a possible battery-degradation signal, not an applied model
change and not measured SoH. This asymmetry is deliberate: the estimator remains unbiased, while the active
safety model cannot become less conservative.

Each cohort has `evidence_set_id = sha256(sorted(step_record_hashes))`. A step hash present in any prior
`ModelCommitReceipt.consumed_step_hashes` in the current battery epoch is excluded from cohort membership and
from the median entirely. A commit requires at least four unconsumed qualifying steps total: one or more from
the current blackout plus at least three other steps, spanning at least two events. A receipt stores the
evidence-set ID and all consumed hashes; the same or overlapping physical evidence can never commit twice.
Further movement toward the same measured value requires an entirely new disjoint cohort.

Allow at most one commit per 30 days and at most a 50% cumulative decrease from the battery epoch's initial
post-transform `k`; crossing either bound records an exact refusal. The receipt stores commit UTC. If current
wall time is earlier than the previous commit UTC, rate-window eligibility fails closed with
`commit_rate_window_indeterminate`; monotonic clocks remain authoritative for all within-event science.
Battery replacement starts a new epoch and bounds.

Exactly one model writer and one transactional commit method exist. Every refusal leaves the model's
scientific fingerprint byte-identical.

Before a permitted commit, atomically replace `<model-data-dir>/model.precommit.json` (0600 plus parent fsync)
with the exact validated before-state and retain it through post-commit verification and rollback eligibility;
only the next permitted commit may replace it after preserving the then-current before-state. It is never a
second live model authority.

`reset_baseline` for a physically replaced battery refuses unless both work-registry capture and processing
queue are empty, then creates a new epoch directly in the reference-0 frame: the fresh default LUT is expressed
in that frame and the initial `ir_k` is recorded as the new epoch baseline.
Every commit rechecks `reference_load_percent == 0`; any other value yields
`ir_reference_frame_not_transformed` and zero writes. A replacement/reset followed by a qualifying decrease
must pass the same dense no-later-LB oracle.

## 11. Decline detection without false precision

Charge readiness is a frozen start-time fact, not model output. It is ready only after continuous physical OL
for at least 12 hours on one boot, no CAL, every raw voltage in 13.0--14.5 V, trailing 30-minute voltage span
at most 0.30 V, and no acquisition gap above 25 seconds. Reboot, CAL, OB, boot change, excessive gap, missing
voltage, or voltage/span violation resets readiness and produces an ordered reason.
`domain/readiness.py` owns this pure rolling state machine; the application observation flow feeds it each raw
physical sample and snapshots its immutable result into `EventStart`. It never accretes into `monitor.py`.

This slice reports evidence, not a fabricated health percentage:

1. `load_sag_trend`: take the latest six valid settled steps in canonical event-start/transition order, at most
   two per event, from at least four events in one epoch; do not select for result. Baseline is the first three
   and recent is the last three, with each group spanning at least two events. Recent median above baseline by
   `max(0.003 V/pp, 0.20 * baseline_median)` becomes `possible_load_sag_degradation`.
2. `firmware_lb_reserve_proxy`: take the latest six otherwise qualifying events containing raw physical LB in canonical
   event-start order. For this metric only, use the single-boot trusted prefix from `evaluation_origin` through
   the first raw-LB observation; evaluate coverage and maximum-gap gates only on that prefix. Raw LB does not
   end the event or request shutdown; later observations remain history but are not integrated into this proxy. Then require
   comparability across all six: readiness, one epoch,
   `max(start_voltage)-min(start_voltage) <= 0.10 V`, `max(mean_load)-min(mean_load) <= 5 pp`, each load
   standard deviation at most 2 pp, coverage at least 0.90, and max gap 5 s. For accepted monotonic edges only,
   `reserve_proxy_pp_s = sum(((load_i + load_i+1) / 2) * delta_t_s)` from evaluation origin through the first
   raw-LB observation. Baseline is median of the first three, recent is median of the last three;
   `recent <= 0.80 * baseline` becomes `possible_reserve_decline`. Otherwise evidence is insufficient.
3. `long_partial_curve`: take the latest six otherwise qualifying events in canonical event-start order, each
   at least 650 seconds with readiness, coverage at least 0.90 and max gap 5 s. Across all six require one epoch,
   start-voltage range at most 0.10 V, mean-load range at most 3 pp, and each load standard deviation at most
   2 pp. `V@600s` is linear interpolation between nearest bracketing raw samples, each within 2 seconds of the
   horizon. Baseline is median of the first three and recent median of the last three;
   `baseline - recent >= 0.20 V` becomes `possible_reserve_decline`. No event is dropped to improve a verdict.

No temperature correction is invented. None of these statuses directly changes capacity, SoH, Peukert, or
LUT. User output says `insufficient comparable evidence`, `possible decline`, or `stable within observed
evidence`; it never claims a causal diagnosis from one event.

## 12. Existing-module decomposition and removal

| Existing module | Final disposition |
|---|---|
| `src/monitor.py` | Keep wiring, one-second read/EMA/safety publish, and bounded capture handoff. Remove journal replay, scientific decisions, and model mutation. Target a small composition root, not a numeric line-count gate. |
| `src/model.py` | Sole owner of scientific state. Privatize mutable state/save; expose atomically published immutable safety snapshot, equivalent reference transformation, one canonical IR commit, baseline reset, and read-only projections. Remove `physics.rls_state`, RLS accessors, `_prune_lut`, measured-LUT insertion, and every unused scientific setter after callers are cut over; retained measured LUT rows and legacy `soh_history`, `capacity_estimates`, `r_internal_history`, and `discharge_events` are read-only history projections, never learning inputs or mutation paths. |
| `src/discharge_collector.py` | Replace with lifecycle domain policy and `capture_blackout` use case; delete. |
| `src/discharge_handler.py` | Replace with pure evidence/comparison/learning policies and one close use case; delete. |
| `src/discharge_journal.py` | Replace with per-event adapter; delete from runtime. Preserve the old on-disk journal as archive. |
| `src/discharge_types.py` | Replace with explicit domain values and per-purpose decisions; delete. |
| `src/event_classifier.py` | Move pure classification/lifecycle rules into domain; delete stateful wrapper after cutover. |
| `src/sag_tracker.py` | Retain only useful diagnostic observation using scalar inputs; remove mutable `BatteryModel`/RLS coupling. Live safety reads `k` and reference load only from the same immutable `ModelOwner` snapshot used by that poll. Delete the tracker if no diagnostic responsibility remains. |
| `src/capacity_estimator.py`, `src/soh_calculator.py` | Remove after proving no production callers; move any still-useful pure helper to `battery_math`. |
| `src/soc_predictor.py`, `src/runtime_calculator.py`, `src/battery_math/` | Move the existing forward LUT function and all callers to `battery_math/lut.py`, add the inverse beside it, and use only a thin intra-cluster re-export while callers move; remove that re-export and the old implementation before Cluster 1 completes. Enumerate every LUT call site in the frame-equivalence test and forbid raw-voltage LUT writes. No state/backward compatibility layer and no gratuitous rewrite. |
| `src/monitor_config.py` | Remove the duplicate production `reference_load_percent`; keep unrelated validated configuration. |
| `src/scheduler_manager.py` | Replace journal access with bounded reporting port. The daemon is built/configured proposal-only; execute dispatch is unreachable from the daemon. Any optional vendor self-test is explicitly operator-issued outside it and remains audited. |
| exporter, MOTD, CLI | Add event outcome, exact reasons, storage health, physical/model/safety status source, and decline evidence recomputed from current sealed evidence rather than frozen into `EventSummary`. Retire misleading `battery.internal.resistance` in Ω; expose `battery.load_sag.coefficient_v_per_load_percent` together with `battery.load_sag.reference_load_percent`, plus the last apparent OL-to-OB sag as diagnostic-only, never identification evidence. |

No parallel legacy and new business path remains after the cutover cluster. Delete obsolete tests only when
equivalent behavior is covered through the new public/domain contracts.

## 13. Implementation clusters and development cadence

Each cluster is code-complete and gets targeted checks. Do not run heavyweight full gates inside every small
loop; reserve them for the final release candidate and reviews.

### Cluster 0: freeze safety and state transformation

- Capture current SoC/runtime/status/LB behavior across voltage, load 0--100%, status, EMA stabilization,
  raw physical LB, and shutdown thresholds.
- Implement the transform-only strict Release-A source loader and reference-load transformation against
  copies of realistic model files; prove valid source success, missing/extra source-field refusal, complete
  target policy initialization, and byte-identical rerun refusal.
- Prove exact/equivalent pre/post results and failure without mutation for every invalid precondition.
- Produce and verify the state backup/rollback operation.
- Prove the one-shot transform aborts byte-identically while the writer lock is held and candidate startup
  refuses a nonzero reference frame rather than transforming it.
- Add the raw-LB separation contract: firmware LB with modeled runtime above threshold is durably recorded but
  leaves virtual LB/FSD unchanged; only the modeled threshold or two-minute hard floor publishes virtual LB.
- Prove reset-baseline creates reference-0 state and a subsequent safe decrease cannot delay LB.

Targeted checks: safety golden tests, transformation boundaries, atomic-save fault injection.

### Cluster 1: pure domain

- Add values, lifecycle, readiness, timeline, evidence, forward comparison, colocated forward/inverse LUT, IR identification,
  learning, decline, and reporting policies.
- Test every threshold below/equal/above, sign convention, reason ordering, zero/one/two-point totality,
  frozen-snapshot determinism, step-estimator recovery, cohort selection, and anti-feedback.
- Validate the new forward predictor against a synthetic battery with known parameters, zero expected
  residuals, known injected residuals, correct slopes, LUT inversion, and clamp behavior.
- No runtime orchestration or behavior changes. Cluster 1 may mechanically repoint LUT imports to
  `battery_math/lut.py` and remove the temporary intra-cluster re-export; this import-only cutover must remain
  covered by the frame-equivalence oracle and introduces no parallel runtime path.

### Cluster 2: per-event JSONL adapter

- Implement schema, hashes, one-writer semantics, pointer states, one-second append, seal, summary index,
  bounded query, automatic lazy index repair, and storage health.
- Fault-inject every create/write/sync/rename/pointer/index stage.
- Prove torn-tail repair, middle-corruption containment, duplicate/conflict behavior, reboot gap, symlink
  rejection, permissions, disk-full visibility, and constant startup work with 0/10/100 historical events.
- With 100 sealed events and a deleted index, prove bounded ticks plus a mid-rebuild restart and a newly sealed
  event complete without loss/duplication, promote the full projection, and clear
  `cohort_projection_unavailable`. Fault injection includes registry-first prepare before/after replacement,
  event creation, start append, and recovery that recreates a missing event file from the frozen start bytes.

### Cluster 3: model ownership

- Privatize mutable model access and atomic save.
- Add immutable snapshot, equivalent reference transformation, and sole idempotent IR commit.
- Rewire read-only consumers.
- Add AST/call-site guards proving no other scientific writer exists.

### Cluster 4: application use cases and safety-first capture

- Implement priority `CaptureWriter`, read-only `AssessmentWorker`, close/process, startup recovery, and
  reporting orchestration.
- Rewire `monitor.py` so virtual UPS publication precedes capture work.
- Prove lifecycle-slot reservation, queue ordering, visible overflow, new-OB capture during assessment/recovery,
  safety independence from a blocked/failing store, first publication before deferred recovery, and one
  shared live/recovery science path.
- Prove `A processing -> B starts OB -> crash -> restart` preserves B capture and gives both blackouts exactly
  one outcome from the bounded registry.
- Prove a model commit is visible in the next safety publication and no poll observes a mixed old/new snapshot.

### Cluster 5: single cutover and legacy removal

- Cut runtime to the domain/use-case/adapters path in one coherent change.
- Remove legacy collector, handler, global journal, thin types, unreachable estimator/stateful classifier code,
  and direct mutable-model calls.
- Rewire scheduler, exporter, health, CLI, MOTD, and alerts.
- Preserve the legacy journal byte-for-byte outside the runtime path.

### Cluster 6: full vertical proof and documentation

- Raw fake-NUT stream -> safety output -> real serializer/store -> close/assessment -> frozen comparison ->
  IR estimate/cohort -> bounded commit or exact refusal -> sealed outcome -> restart -> CLI/MOTD report.
- Include both a fixture that produces exactly one real safe commit and the same data with a missing gate that
  produces zero writes and an exact reason.
- Include realistic short, ten-minute partial, raw-LB marker, CAL/self-test, reboot, gap, disk failure,
  corrupt record, duplicate, second-writer, and post-start live-model-change fixtures.
- Update ADR, glossary, user scenarios, operations runbook, and plain-language examples.

### Release-candidate gate

- Treat the existing project RC gate as an external prerequisite owned by the tooling workstream. This slice
  neither changes its configuration nor investigates/remediates unrelated tooling findings; it runs the
  resulting gate only on the release candidate.
- Differentially compare Release A and the candidate for every safety fixture. Apart from one pre-registered
  fail-closed classification change, non-LB status tokens and initial virtual-LB timing are identical:
  missing `input.voltage` during battery-status operation classifies as real
  rather than retaining Release A's stale class. Fixtures cover `CAL`/`OB` with missing input voltage and name
  the intentional difference in the report instead of failing the gate as unexplained.
- Verify model writes occur only with a receipt and never on any refusal/retry path.
- Run bounded startup, storage crash matrix, restart idempotency, and end-to-end scenarios.
- Cross-AI review the implemented release candidate; resolve all blockers before deployment.

## 14. Mandatory automated test oracles

1. Every domain threshold: below, equal, above.
2. Raw/derived provenance cannot be confused or deserialized into the wrong type.
3. Wall-clock jumps never affect duration or integration.
4. Reboot/gap never integrates and never authorizes science.
5. Short blackout is durably useful but makes no capacity/SoH claim.
6. Ten-minute partial compares only the observed interval.
   Short-window boundaries at 179/180/181 evaluated seconds prove exact refusal/admission when the existing stable-segment,
   coverage, and gap gates pass; the accepted comparison is labeled `short_window_comparison`, enters no decline
   cohort, and causes zero model writes on its own. Full-mode precedence and 299/300/301 evaluated-second
   boundaries with normalized evaluated-interval endpoint movement exactly below/equal/above 0.20 V are also pinned.
7. Raw physical LB is durably preserved as diagnostic/evidence but never changes virtual LB/FSD. A fixture with
   raw LB and modeled runtime above threshold remains non-LB virtually; modeled-threshold and hard-floor
   fixtures still publish LB. Missing input voltage fails closed as a real blackout without changing this rule.
8. CAL/vendor self-test cannot authorize natural-learning decisions.
9. Frozen comparison is identical if the live model changes after event start.
   Changing live nominal voltage/power constants after start also leaves comparison and Ah proxy byte-identical
   because both come from the frozen snapshot.
10. Residuals and predicted values cannot enter `ir_identification` by type/import/call graph.
11. Known raw step with discharge drift yields the correct positive settled
    `k = -(a_late_at_t0-a_pre_at_t0)/delta_load`; up/down synthetic estimates agree.
12. A synthetic battery with ohmic plus polarization sag proves committed `k_settled` reproduces steady-state
    sag within 10%; a 10--25-second-only estimate that disagrees by more than 15% is stored but refused with
    `sag_not_settled`. Time trend without a stable step, excessive plateau slope, or drift contribution above
    10% is also refused.
13. Sliding candidates around one transition deduplicate to one step; overlapping windows, transition bounce,
    less than 180-second separation, or more than two steps per event cannot inflate a cohort.
14. Fewer than four unconsumed steps total, no current-event step, one event only, one direction only, high dispersion, noisy plateau,
    high load, insufficient voltage movement relative to token quantum, gap, CAL, and LB all produce zero
    model writes with exact reasons.
15. A valid decrease commits once, is rate-limited at 20%, has before/estimate/after, and cannot delay LB.
    Reusing the same evidence set, including any previously consumed step in a new cohort, a new event with no
    new step, a second commit inside 30 days, backward wall time, and crossing the epoch cumulative bound all
    produce zero writes with the exact typed reason.
    Canonical cohort selection uses all qualifying steps from exactly the current-plus-31 event universe;
    reordering inputs, inconvenient dispersion, and older attractive candidates cannot change the evidence set.
    The first two step positions are selected before filters: consuming one while a third exists never promotes
    the third. Raw records are recomputed under one current evaluator revision; mixed revisions refuse exactly.
16. A valid increase is stored unchanged as possible degradation evidence and is not committed.
17. Reprocessing the same event or crashing before save/after save/before receipt cannot double-apply; unexpected
    model hash yields `model_state_conflict` and zero writes.
18. Every refusing scenario leaves the scientific fingerprint byte-identical.
19. Reference reparameterization is equivalent across every LUT caller and the dense safety domain or aborts
    atomically; the expected transformed fingerprint is pre-registered. The transform-only loader accepts a
    valid pinned Release-A source, rejects missing/extra fields and an already transformed rerun byte-identically,
    initializes the complete target policy, drops exactly shaped legacy `physics.rls_state`, shifts retained
    measured LUT entries with all others, and normal `ModelOwner` loading never accepts the old schema or any
    reappearing `rls_state`. An unexpected legacy RLS shape refuses before write, and call-site guards prove
    `_prune_lut`, measured-LUT insertion, and RLS mutators are absent.
    Forward/inverse LUT golden tests also pin the existing `soc_from_voltage` +/-0.01 V exact-match band on
    both sides of every boundary, not only clamp edges.
20. Constant-battery/varying-load comparison reports residuals inside the explicit known LUT-frame uncertainty
    instead of attributing them to battery decline.
21. Store failure at every stage cannot block safety and is visible; a new start/end uses reserved capacity.
22. Torn tail affects only the incomplete suffix; middle corruption is preserved, grouped by `blackout_id`,
    and the durable aggregate outcome lives in a writable segment rather than only the index.
    Crash injection before/after summary append and before registry removal proves both healthy-index and
    active-rebuild-delta paths retain a processing reference until the summary is durable, then converge
    without loss or duplication. Both paths assert serialized `outcome_record_sha256`; a same-key/different-byte
    summary yields `projection_conflict` and retains the reference.
    Events with 15/16/17 damaged segments keep at most 16 canonical hashes, set the exact overflow count, and
    remain projectable inside the 4-KiB index bound while every corrupt file stays discoverable.
23. Readers deduplicate byte-identical sequence records before counts, coverage, medians, and integration.
24. Startup reads bounded active metadata independent of history size; automatic index repair runs only later.
25. No runtime code opens the legacy global journal.
26. No daemon path calls a UPS command during capture, assessment, learning, recovery, or UAT fixtures.
27. Health output is bounded and says in plain language why the model did or did not change.
    It reports remaining consumed-evidence hash budget. Release-A/candidate safety fixtures pin both missing
    modeled runtime (`None`) and exact `0.0` to the existing fail-closed hard-floor LB behavior.
28. Decline boundaries are exact: terminal recent/baseline ratios 0.799/0.800/0.801, long-partial voltage
    differences 0.199/0.200/0.201 V, and all-six comparability ranges below/equal/above their limits.
29. An event containing raw LB remains eligible for a firmware-LB reserve proxy when its same-boot trusted prefix
    through first LB passes coverage/gap gates. Raw LB does not end the event or cause FSD; later duration is
    retained as observed post-LB reserve evidence but is not integrated into the proxy.

## 15. Release, rollback, and live UPS acceptance

### Deployment preflight

- physical and virtual UPS both report OL, neither reports LB/FSD, NUT replies are complete enough for the
  production safety validator, and virtual publication is fresh within two polling intervals;
- reported battery charge is at least 90%, modeled reserve is at least 18 minutes at current load, and the
  configured/current load is within the UPS and test runbook's safe range;
- no shutdown is pending;
- the daemon is the sole writer;
- capture/storage health is green;
- code artifact, model fingerprint, event inventory, boot ID, and NUT command audit are recorded;
- the Release A binary/config and the pre-transformation state backup are verified offline;
- the exact expected post-transform scientific fingerprint is calculated on a copy and registered beside the
  pre-transform fingerprint, so the authorized one-shot change cannot masquerade as an unexplained mutation;
- the candidate has passed all automated and Cross-AI gates.

Deploy only the complete candidate, using the section 10 stop/lock/transform/verify/start sequence. The
transformation command is forbidden while Release A or any other writer holds the lock, and candidate startup
never transforms state. Do not expose a half-migrated domain/storage path. Preserve new event
files and the old journal on rollback; do not reverse-import either. If the reference transformation or IR
commit occurred, restore the verified pre-transform/pre-commit model snapshot before restoring Release A.

Rollback triggers: later modeled LB, any raw physical LB influence on virtual LB/FSD, stale virtual output, unexplained model fingerprint,
missing/duplicate event, corrupt-loop, restart loop, second writer, daemon-issued UPS command, or a health
state that cannot explain the last outcome.

### Staged live UAT

1. Run the read-only preflight and start concurrent observation of physical status, virtual status, safety
   source, model fingerprint, active event, storage health, and command audit.
2. Optionally run the vendor's short self-test. It proves detection, CAL classification, JSONL durability,
   and reporting only. A separate monitor-only restart fixture/test proves recovery. The self-test cannot prove
   natural-blackout learning, capacity, SoH, or IR learning.
3. Product acceptance target is 360 durable seconds of a physical outage. Preflight requires physical OL,
   reported charge at least 90%, predicted reserve at least 18 minutes at current load, no raw/virtual LB,
   capture queue empty, and storage healthy. The user removes UPS input power; the daemon issues no UPS test
   or power command. Restore power at 360 seconds or immediately on virtual LB, modeled runtime at or
   below 10 minutes, capture degradation/overflow, stale virtual output, lost SSH/control path, or any operator
   concern, whichever comes first.
4. During the event, prove one-second physical polling and virtual publication, raw-LB diagnostic separation,
   one-second durable observations, no stale output, and no storage error hidden from health. If raw LB appears,
   record modeled runtime at first LB and the subsequently observed on-battery duration; raw LB alone does not
   abort the UAT or request shutdown.
5. After OL, require automatic `end -> assessment -> comparison -> identification -> decision -> outcome`
   with no operator data handling.
6. Report duration, preserved point count, gaps, comparison, exact parameter estimate, before/after if applied,
   or the scientific reason nothing changed; state explicitly that shutdown rules stayed unchanged or became
   more conservative.
7. Pass numbers: at least 300 durable seconds, coverage at least 0.90, at least 270 deduplicated raw
   observations, maximum raw gap 5 seconds, durability lag no more than 2 seconds at steady state and never
   above 5 seconds, zero queue overflow, and one terminal outcome. If a safety abort ends the outage before
   300 physical seconds, operational capture/safety UAT may pass. Comparison acceptance is independent: a
   qualifying event needs at least 180 evaluated seconds after `evaluation_origin` (roughly 255 physical seconds)
   for short-window acceptance, or at least 300 evaluated seconds (roughly 375 physical seconds) plus normalized
   endpoint movement for full acceptance. The 360-second product UAT targets short-window acceptance; full mode
   is optional only when the safety abort thresholds leave sufficient reserve. Otherwise comparison acceptance
   must be repeated only after the abort cause is understood and a later preflight is safe. Any event ending
   before 180 evaluated seconds reports `comparison_not_attempted` (insufficient observed window), not a model
   refusal or failed learning decision.
8. An optional host-load change of at least 15 pp while total load remains at most 50% may exercise step
   detection. It is not required for a pass and one event normally cannot satisfy the multi-event commit cohort.
9. A scientifically correct `recorded_only` result passes the live UAT if the entire automatic path and
   refusal explanation work. A real commit path is proven deterministically in automated tests and will apply
   automatically in production once a future natural cohort qualifies.

The user may choose physical unplugging or an authorized short built-in test for operational validation, but
they are not scientifically equivalent. The physical bounded outage is the final product acceptance event.

## 16. Honest limitations after this slice

- Partial outages do not reveal true total capacity, full runtime, or SoH.
- `ups.load` does not reveal true battery current; integrated Ah remains a proxy.
- No temperature effect can be measured because the UPS exposes no temperature sensor.
- Until natural events establish the relationship between firmware LB, modeled runtime, and subsequently
  observed reserve on this exact UPS, raw LB remains diagnostic-only. Enabling any future raw-LB safety floor
  requires a separate reviewed product decision and evidence-backed acceptance gate; it is not latent config.
- Decimal-token precision is only the best available lower bound on voltage quantization, not proof of ADC
  resolution; the independent 0.15 V movement floor and dispersion/agreement gates remain mandatory.
- Natural load steps and multi-event qualifying cohorts may be rare.
- The planning workspace contains no verified production load distribution, so this plan makes no claim about
  how often the commit path will fire. After release, unattended health output reports qualifying-step counts
  and observed eligibility rate; absence of steps is a plain physical limitation, not an operator task.
- The likely aging direction is an increase in load sag. It is detected and reported but not allowed to delay
  shutdown by changing the active safety model.
- One live UAT cannot prove long-term degradation detection or a multi-event commit.
- Capacity/SoH automatic update remains impossible on this hardware, including at a terminal event, because
  there is no independent current/charge measurement. Terminal events provide only comparable reserve/load-time
  proxy evidence; this is a physical limitation, not an operator task.
- Applying upward `ir_k` estimates would require explicitly separating the best scientific estimate from a
  conservative safety envelope. That additional state is intentionally deferred to avoid hiding complexity
  or adding another runtime signal in this slice.
- The epoch's LUT frame stays anchored to the initial reference transform after later downward `ir_k` commits;
  this can add conservative frame offset up to the bounded cumulative change. It is disclosed inside the known
  LUT-frame uncertainty and is not silently presented as a more accurate physical LUT.
- A capture-writer stall beyond the 120-second observation queue sacrifices that blackout's scientific
  eligibility but never safety; the durable/health outcome is explicit and the operations runbook treats it as
  storage degradation, not successful learning.
- A brand-new post-cutover model is created directly at reference load 0; its default LUT is the legacy default
  LUT shifted by `default_k * 20`, so first install has the same baseline predictions without invoking migration.

## 17. Completion definition

The slice is done only when:

- the old spaghetti business path is absent from runtime;
- every blackout automatically reaches a durable, explainable outcome;
- per-event JSONL is the scientific evidence store and logs remain logs;
- short and partial events provide automatic history/comparison value;
- the independent IR estimator and safe commit path are real, tested, and unattended;
- possible decline becomes visible without false SoH claims;
- safety is identical or more conservative across deterministic and live evidence;
- the implementation passes normal tests, the release-candidate full gate, and the staged real-UPS acceptance
  runbook.

After deterministic completion, Cross-AI implementation review remains a separate mandatory release-process
gate: all Critical/High blockers must be resolved before deployment, but model opinion is not itself a
deterministic product oracle.
