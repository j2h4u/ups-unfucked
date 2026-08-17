# Unified blackout and recharge evidence learning plan

**Status:** authoritative product-completion plan; not implemented; repository and deployment are **NO-GO**
until the applicable slices, deterministic gates, independent panel review, premium Cross-AI review, and
live UAT are complete.

**Kaizen objective:** every physical blackout is recorded automatically; every trustworthy fragment is
used automatically for the scientific question it can answer; no operator or agent is required; model
outputs never become their own evidence; safety publication remains the first responsibility of every poll.

This plan corrects the narrower Release-B decision that treated only grid-restored events as scientific and
permitted only load-sag learning. The existing DDD/JSONL implementation is the foundation, not completion of
the original product goal.

## Product authority and traceability

[`PRODUCT.md`](../../PRODUCT.md) is the stable authority for why this product exists, its lightweight Jobs to
Be Done, honest sensor limitations, success measures and decision priority. This plan is one delivery strategy
and may not redefine those values through a technical shortcut.

- **J1 — Protect:** Slices 0–4 preserve safety-first publication and prove storage/analysis cannot delay it.
- **J2 — Remember:** Slices 1, 2 and 4 deliver unified blackout/recharge evidence and exact v3 history.
- **J3 — Improve prediction:** Slices 1–3 generalize usable evidence and permit only independently proved,
  bounded model changes.
- **J4 — Warn:** Slices 1, 3, 4 and conditional Slice 5 deliver comparable discharge/recharge trends through
  the health reporting surface; honest `insufficient` remains a valid result.
- **J5 — Run unattended:** every slice owns its crash recovery, bounded loss, replay and no-operator tests.

No product job is intentionally deferred beyond this plan. Absolute capacity/SoH and additional writable
parameters are limitations rather than hidden promises until independently identifiable sensors exist.

## Panel convergence

Kaizen chaired three independent reviews. Product review required using all trustworthy fragments and
recharge history. Architecture review rejected another generic framework and recommended small typed slices
with consumer-specific assessments. Scientific review vetoed treating host shutdown as an empty-battery
endpoint: the daemon shuts the host down while reserve remains, and its boundary is partly produced by the
model under evaluation. The converged decision is therefore:

- capture every event and all bounded raw NUT fields;
- assess each typed fragment independently, including long prefixes from deep events;
- treat arbitrary start charge as context rather than global rejection;
- use raw LB only as a firmware marker and shutdown as censoring;
- retain `ir_k` as the only currently proved automatic model target;
- require a separate identifiability proof before any new runtime/capacity/LUT/Peukert target can write the
  model; if the available sensors cannot prove it, keep the result diagnostic rather than manufacture a
  calibration.

## 1. Business outcomes

- [ ] A user can ask how many blackouts occurred in a calendar period and receive a result derived from the
  durable event projection, including explicit counts of individually recorded and aggregate-loss events.
- [ ] Every available raw UPS observation during blackout, and every observation selected by the versioned
  recharge-subsampling policy, is durably captured or represented by an explicit bounded loss/rejection
  receipt. Deliberately unselected recharge polls are represented by the policy revision recorded at start;
  they are not silently treated as missing measurements.
- [ ] Short, partial, deep, safe-shutdown, restarted, and self-test events share one evidence pipeline; they
  are not separate ad-hoc learning systems.
- [ ] Evidence is admitted per scientific question and per fragment. One bad suffix does not erase a valid
  earlier fragment, and one valid fragment does not make the entire event trustworthy.
- [ ] Starting from less than a full charge does not globally disqualify an event. Start readiness and charge
  history affect which inferences are identifiable and their uncertainty.
- [ ] A deep natural blackout preserves substantially more trustworthy trajectory than a short one and can
  improve only those local estimators its raw fragments identify. Host shutdown is a censored safety
  boundary, not proof that the battery reached zero and not, by itself, capacity/runtime calibration.
- [ ] Recharge observations are linked to the preceding discharge cycle and used automatically where they add
  independent information; otherwise they remain honest diagnostics, never fake capacity evidence.
- [ ] Every accepted model update is bounded, idempotent, tied to immutable evidence hashes, checked by the
  safety oracle, and performed by the sole model owner.
- [ ] The service remains unattended 24/7 and never needs Grafana, cloud data, an operator decision, or a
  manually initiated full discharge for routine learning.

## 2. Non-negotiable constraints

- [ ] Use only variables actually exposed by this UPS. The currently implemented typed physical observation is
  status, battery voltage and original token/quantisation, UPS load percentage, input voltage, wall time,
  monotonic time, and boot identity. Before implementation, one read-only capability probe must register any
  additional stable raw fields, notably `battery.charge` or battery current. Preserve every returned NUT
  key and original token in a bounded forensic envelope even when it has no typed scientific meaning; only
  explicitly registered typed fields may enter an estimator. No temperature value may be invented: this UPS
  has no temperature sensor.
- [ ] Treat UPS `battery.charge` and `battery.runtime` as firmware/model projections unless independently
  validated. They may be stored as raw vendor claims and compared diagnostically, but cannot alone authorize
  a scientific model change.
- [ ] Do not claim measured amp-hours, returned energy, coulombic efficiency, absolute SoH, or a uniquely
  identified Peukert exponent without battery current (or another independently validated current/power
  measurement).
- [ ] Keep immutable per-event JSONL as scientific evidence. Journald is the human event log. Rebuildable
  indexes, reports, cohorts, and model candidates are projections. Do not add SQLite.
- [ ] Preserve safety-first ordering, the hard runtime floor, conservative unknown handling, sticky shutdown
  behavior, and the one-second physical polling loop.
- [ ] Do not command deep discharge or remove mains merely to create learning evidence. Built-in short tests
  remain operational/UAT evidence and never masquerade as natural blackouts.
- [ ] No backward compatibility or reverse migration is required for this single-host service. Preserve raw
  evidence and the pre-deployment state backup; replace derived schemas atomically. The v3 daemon contains no
  v2 reader. A one-shot transform-only quiescence tool is allowed solely at deployment and never imports v2
  evidence into v3 science.
- [ ] Preserve all returned raw fields locally with owner-only permissions, including device/UPS serial fields;
  this is an explicit single-host forensic decision, not an accidental disclosure surface. Raw envelopes and
  serial fields never enter journald, MOTD, health, virtual-UPS output, or human report projections.

## 3. Domain language and boundaries

### 3.1 Aggregates and values

- [ ] `PowerInterruption` is one physical loss-of-mains episode, from the earliest independently observed
  boundary through restoration, safe-shutdown handoff, or an explicit censored/lost boundary.
- [ ] `RechargeEpisode` is the online recovery interval following a `PowerInterruption`, ending when charge
  stabilization is proven, another blackout begins, the battery epoch changes, or capture is censored.
- [ ] `CycleWindow` is a read-only link between one interruption and its optional recharge episode; it is not
  an aggregate claiming that a complete electrochemical cycle was observed.
- [ ] `EvidenceAnchor` is a typed boundary with explicit provenance: physical transfer to battery,
  firmware low-battery flag, grid restoration or boot boundary; modeled safe-shutdown publication/intent;
  operational service-stop receipt or charge stabilization; or explicit gap/corruption. Consumers decide
  which provenance is independent enough for their question.
- [ ] Use small typed fragments rather than a generic evidence framework: `DischargeSlice`,
  `LoadStepObservation`, `EndpointAnchor`, and `RechargeSlice`, each referring to canonical raw sample hashes.
- [ ] Each consumer owns a typed assessment such as `LoadSagAssessment`, `CurveAssessment`,
  `FirmwareLbAssessment`, or `RechargeBehaviorAssessment`. Delete the global event-wide scientific yes/no
  authority and do not replace it with a generic capability bus.
- [ ] `LearningCandidate` names exactly one target parameter/projection, the raw fragment hashes, policy
  revision, estimate, uncertainty, before value, and safe allowed direction.
- [ ] `ModelChange` remains the only command accepted by `ModelOwner`; no domain evaluator writes storage.

### 3.2 DDD dependency direction

```text
NUT adapter -> application capture use cases -> immutable JSONL evidence
                                         |
                                         v
                              domain fragment profiler
                                         |
                  +----------------------+----------------------+
                  v                      v                      v
        discharge evaluators     recharge evaluator      history projection
                  |                      |
                  +----------> candidate arbitration <-----------+
                                         |
                                         v
                              ModelOwner + safety oracle
```

- [ ] Domain modules own classification, fragmentation, capability admission, estimators, uncertainty, and
  candidate arbitration as pure functions.
- [ ] Application modules own `CapturePowerInterruption`, `CaptureRechargeEpisode`, `ProfileEvidence`,
  `EvaluateLearningCandidates`, `CommitApprovedCandidate`, `RebuildHistoryProjection`, and
  `ReportBatteryEvidence` use cases. Existing recovery responsibilities remain explicit and tested:
  degraded/startup recovery (`degraded_startup.py`, `startup_recovery.py`), pending assessment replay
  (`assessment_worker.py`, `assessment_replay.py`), terminal/boundary recovery
  (`capture_terminal_retry.py`, `capture_boundary_recovery.py`), graceful stop
  (`monitor.py`, `capture_writer.py`), and bounded prestart loss (`prestart_loss.py`). The refactor may extract
  them but not dissolve them into composition.
- [ ] Adapters own NUT translation, JSONL codecs/durability, clocks, boot identity, model persistence, and
  human report sinks.
- [ ] `monitor.py` remains composition and poll ordering only. It may not contain scientific thresholds or
  persistence grammar.
- [ ] Import Linter and Tach exact mode encode the resulting graph; architecture tests reject domain imports
  of application/adapters and reject scientific policy in orchestration.

## 4. Capture model

### 4.1 Blackout capture

- [ ] Preserve the earliest available raw sample, all one-second battery observations, the complete bounded
  NUT key/token reply, vendor flags, gaps, boot changes, and physical restoration.
- [ ] Safety publication always happens first and never waits for storage. Immediately afterward, submit one
  bounded best-effort writer command that idempotently appends the decision poll's `discharge_sample` and then
  its `shutdown_boundary` in that strict order. Allow at most the existing five-second sticky-LB recovery
  window for durability; the monitor does not own or delay `upsmon` shutdown.
  The record names the raw observation hash, the exact per-poll model snapshot hash used by the published
  decision, monotonic/wall time, and threshold reason. A missing/failed append makes shutdown reconciliation
  censored and unhealthy but never weakens shutdown protection.
- [ ] The poll thread uses a bounded non-blocking `try_reserve` on the submission lane; it never waits for a
  writer-held lock or filesystem operation. Reservation failure records capture-unavailable health and a
  censored/missing boundary, while safety publication and `upsmon` proceed unchanged.
- [ ] Specify and test four boundary races: publication succeeds but append never starts; append is durable but
  publication fails; host dies during append; and publication/append succeed but shutdown is cancelled and
  the same boot later returns OL. The last case retains the boundary as a mid-event modeled-safety marker;
  continuous raw samples remain locally assessable but the marker is not a physical endpoint.
- [ ] Reserve/enqueue that combined boundary command under the capture submission lock before any subsequent
  poll can enqueue another sample. A deterministic paused-writer race proves no later sample can appear between
  the named decision sample and its anchor.
- [ ] On restart, reconcile the open event with the durable shutdown boundary and previous boot identity.
  Close it as `safe_shutdown_restarted` rather than generic `closed_restart_gap` when the receipt and raw tail
  agree: the anchor's named observation hash must equal the immediately preceding durable physical sample in
  JSONL chain order and its boot/sequence must match the open registry. Inverted anchor/sample order is an
  explicit refusal fixture. Missing or contradictory receipts remain explicit censored evidence.
- [ ] Preserve natural restoration, service stop, capture damage, and restart gap as distinct facts. Terminal
  type no longer globally decides whether all scientific use is allowed.
- [ ] A rapid second outage, storage failure, queue overflow, or delayed assessment must retain event
  boundaries or write the existing bounded loss receipt. No silent merge or clean suffix is allowed.
- [ ] Each v3 blackout aggregate/chain has an absolute 64 MiB ceiling and a separately named capture-append
  limit of 62 MiB, preserving at least 2 MiB for its terminal/derived tail. Before a capture-region append would
  cross the capture-append limit, close the aggregate as `aggregate_budget_exhausted` with
  `budget_kind=bytes` and immediately open a new aggregate with a new `blackout_id`, the same
  `physical_episode_id`, `continued_from` pointing to the old ID, and `continuation_kind=size_rollover`; the
  old `blackout_end` carries `continued_by`. There is no fixed limit on linked aggregate count, while every
  file/operation remains bounded; ordinary disk exhaustion uses the normal explicit loss path. Verified
  size-rollover links may concatenate adjacent raw samples. Reboot continuations use
  `continuation_kind=reboot_gap` and may never integrate across that gap.
- [ ] The capture-append limit applies exactly to START, ordinary discharge samples, nonterminal gaps, and raw
  anchors. Anchor records carry `anchor_role=intermediate|terminal`: `transfer_to_battery` and
  `raw_firmware_lb` are capture-region intermediate anchors; `modeled_safe_shutdown`, `power_restored`,
  `service_stop`, and `charge_stabilized` are terminal-reserve anchors; `boot_boundary`, `gap`, and
  `corruption` use the region selected by their explicit role. The reserved 2 MiB region is available only to terminal anchors,
  END/link, assessment, comparison, estimate, decision, receipt and outcome. No generic "physical record"
  predicate may accidentally reject a terminal anchor at 62 MiB.
- [ ] The 2 MiB terminal reserve covers the maximum canonical sum of every post-capture record. Before Slice 1,
  freeze `DerivedTailBudget` with the single construction inequality:
  `derived_total + terminal_link_receipt_outcome_total <= 2 MiB`. `derived_total` is at most 128 records times
  8 KiB = 1 MiB and includes every descriptor batch; at most 256 descriptors of at most 256 canonical bytes
  each may appear inside those 128 records. The independently summed, individually bounded terminal/link/
  receipt/outcome records therefore receive the remaining budget, never an additional 2 MiB. Profiling still consumes every eligible raw sample; it does not create one unbounded derived record
  per sample/fragment. Construction and append guards reject an invalid policy before capture starts, and
  boundary fixtures fill every allowed record at its maximum encoded size.
- [ ] The existing maximum of 64 physical segment references inside one aggregate is the second rollover
  trigger, with reference 64 reserved exclusively for a terminal continuation. When 63 references exist and
  recovery would need another, use reference 64 to terminalize and perform the same aggregate rollover with
  `termination=aggregate_budget_exhausted` and `budget_kind=segment_refs`; do not turn a reachable bounded
  resource limit into capture damage or an unhandled exception.
- [ ] In v3, segment references are created only by torn/corrupt/damage recovery that quarantines the old
  segment and continues the same aggregate; ordinary byte rollover creates a new aggregate and does not add a
  reference to the old aggregate's manifest. This makes `budget_kind=segment_refs` a reachable corruption-
  recovery bound rather than a second representation of byte rollover.
- [ ] Rollover is one recoverable writer-lane transaction. First durably reserve the old/new IDs and exact
  linkage in the work registry, then durably create the successor `blackout_start`, then append the carrier
  `blackout_end` with `continued_by`, and finally atomically move the active registry reference to the
  successor. The successor is not capture-active until that final registry swap. No later physical sample may
  pass the reserved transaction. Restart reconciliation completes the same identities and bytes. If successor
  creation fails, the old aggregate remains open/censored and the normal durable loss path is used; the
  carrier may never be closed with a dangling `continued_by`. If persistent failure follows durable successor
  START but precedes carrier END, history folds the registry-reserved pair as one open physical episode with
  no final boundary; it never reports two events or a fabricated terminal.
- [ ] Freeze a maximum canonical raw-envelope size of 16 KiB and maximum physical-record size of 20 KiB in
  `DischargeFragmentPolicy`/codec fixtures. Oversize replies produce an explicit gap/loss record; they are not
  partially truncated and presented as complete.

### 4.2 Recharge capture

- [ ] Start a recharge episode after any individually recorded discharge returns online, including restart
  reconciliation of `safe_shutdown_restarted` and other censored/reboot-gap terminals. If online recovery is
  observed after only aggregate/prestart blackout loss, write an unlinked censored recharge episode carrying
  the recharge-loss receipt; do not silently omit it or invent a blackout link.
- [ ] Capture an immediate dense window so voltage recovery and charger transition are visible; then reduce
  frequency adaptively when change becomes slow. Return to dense sampling on status/voltage/charge-slope
  changes. This is persistence subsampling of the uninterrupted one-second safety poll, never a change to NUT
  polling. Exact numeric policy is versioned and derived from observed quantisation/noise, not an arbitrary
  wall-clock duration.
- [ ] Persist a uniform versioned backbone interval throughout the episode plus separately tagged
  event-triggered enrichment samples. Recharge curve/slope/trend evaluators use only the uniform backbone
  unless their preregistered estimator explicitly corrects selection bias; enrichment is transition detail,
  not an exchangeable sample. §7.1 proof must bound selection bias for any future writable recharge target.
- [ ] Store the same raw physical fields plus any capability-probed vendor charge/current fields with their
  original tokens. Missing fields remain absent.
- [ ] End on proven charge stabilization, a new blackout, battery-epoch reset, bounded maximum observation
  budget, service stop, explicit gap, or capture damage/corruption. A long float-charge period must not grow a
  file without bound.
- [ ] Before Slice 4 implementation, freeze `RechargeSamplingPolicy` with these required fields:
  `revision`, `backbone_interval_s`, dense/sparse enrichment intervals, slope-window sample count, stable
  voltage band in raw quantisation units, required consecutive stable windows, optional vendor-charge flat
  band, maximum duration, maximum samples, and the closed episode-provenance fields
  `observation_origin` (`natural`, `self_test`, or `uat`) plus nullable `uat_intent_id`. The provenance and
  intent ID are durably repeated in `recharge_start` and `recharge_end`; linked UAT/self-test provenance is
  therefore a wire invariant, not a runbook convention. Stabilization is an operational file-closure rule,
  not proof of full charge or science; reaching a budget closes as censored rather than stable.
- [ ] The policy must prove its bounds arithmetically against the same storage limits. The capture region
  invariant is `START + max_samples * max_physical_record_size + max_gap_records * max_gap_record_size <=
  62 MiB`; `max_gap_records` is a required finite policy field. The independently computed full derived and
  terminal tail must be `<= 2 MiB`, giving a total `<= 64 MiB` without double-counting. At 63 segment
  references, reference 64 is reserved to close the recharge episode as
  `episode_budget_exhausted/budget_kind=segment_refs` rather than
  attempting another continuation. These checks are construction-time policy invariants and boundary tests,
  so a long float-charge episode cannot reach an adapter capacity exception. Before another gap would exceed
  `max_gap_records`, close with `episode_budget_exhausted/budget_kind=gap_records`.
- [ ] `vendor_charge_flat_band` is disabled unless the live capability manifest registered a stable typed
  `battery.charge`; absence never falls back to a model-derived charge value.
- [ ] The same policy freezes gap handling: a gap stays mid-chain only when exact episode identity and chain
  continuity can be reattached; otherwise append `recharge_end.termination=gap`. Neither path authorizes
  science across the gap. A detected torn/corrupt chain closes as `capture_damaged`; `gap` means loss/absence
  with an otherwise intact chain.
- [ ] Link recharge to interruption by immutable event identity and battery epoch, never by mutable filename
  inference.
- [ ] Exactly one recharge episode may be created per physical OL restoration, keyed idempotently by the OL
  observation hash/boot/sequence. Across a reboot-continuous outage, link it to the newest interruption that
  physically receives OL; older censored pieces carry `continued_by` links and are traversed read-only by the
  `CycleWindow`, never each spawning recharge. Startup recovery that links a continuing OB episode across a
  reboot preserves the same `physical_episode_id` but uses `continuation_kind=reboot_gap`.
- [ ] A recharge superseded by a new blackout uses one registry-reserved cross-episode transaction: reserve
  the new blackout identity, durably create its START with the first OB sample, then append recharge END with
  that exact superseding ID, and finally activate the blackout reference. If START creation fails, recharge
  remains open/censored and no dangling superseding ID is written; restart replays the exact reserved IDs.
- [ ] Add `RecoverRechargeCapture` with explicit preparing/capturing/processing states in the same bounded work
  registry. Restart attaches the exact episode identity; any host-off charging prefix is represented by a
  `recharge_gap` before new samples. Retry is idempotent and cannot duplicate the blackout link.
- [ ] Recharge-side loss is a `recharge_gap` with system provenance and bounded reason, count, first/last
  boot/monotonic boundaries and nullable wall UTCs, failed-command/error fields, and source blackout/intent IDs
  where known. It uses
  the same in-flight/residual reservation and exact durable-proof acknowledgement semantics as blackout loss.
  It rides its episode's own chain; if no linkable episode exists, it creates the unlinked censored recharge
  chain described above.

### 4.3 JSONL v3 wire and ports

- [ ] Cut over to fresh `events-v3/blackouts/` and `events-v3/recharges/` authorities under the same sole
  writer lock. Freeze `SCHEMA_VERSION=3`; v3 physical record names are the exact closed enums below rather
  than v2 `start|observation|end`. Each logical aggregate remains its own hash-linked bounded JSONL chain.
- [ ] Blackout physical record types are `blackout_start`, `discharge_sample`, `endpoint_anchor`,
  `discharge_gap`, and `blackout_end`; recharge types are `recharge_start`, `recharge_sample`, `recharge_gap`,
  and `recharge_end`. Consumer assessments and the terminal outcome are derived records and may never alter
  physical history.
- [ ] Every `blackout_start` and `blackout_end` carries the same closed `observation_origin`
  (`natural`, `self_test`, or `uat`) and nullable `uat_intent_id`. Rollover and reboot-continuation STARTs copy
  those exact immutable values; a continuation can never reclassify operational data as natural. Assessment
  reads only this durable stamp, never the current presence of a UAT-intent file.
- [ ] `shutdown_boundary` is wire-encoded as `endpoint_anchor` with
  `anchor_kind=modeled_safe_shutdown`; it is not a separate record type. `safe_shutdown_restarted` is a v3
  `blackout_end.termination` value alongside the other closed termination values below.
- [ ] `blackout_end.termination` is closed to `power_restored`, `service_stop`, `closed_restart_gap`,
  `safe_shutdown_restarted`, `capture_damaged`, and `aggregate_budget_exhausted`; the last value requires
  `budget_kind=bytes|segment_refs`. Physical-episode final-boundary counts exclude the technical carrier and
  use the final linked aggregate. A separate episode-level safety-anchor namespace reports whether any linked
  piece reached `safe_shutdown_restarted`, raw firmware LB, or modeled safe shutdown. Thus a reboot-continuous
  episode that eventually restores reports both `final_boundary=power_restored` and
  `safe_shutdown_restarted=true`; folding never erases the operationally strongest anchor.
- [ ] `recharge_end.termination` is closed to `charge_stabilized`, `superseded_by_blackout`,
  `battery_epoch_reset`, `episode_budget_exhausted`, `service_stop`, `gap`, and `capture_damaged`.
  `episode_budget_exhausted` requires `budget_kind=bytes|segment_refs|gap_records|samples|duration`; reaching a storage
  bound is never mislabeled as corruption. This enum is part of the mandatory pre-Slice-4
  `RechargeSamplingPolicy`/codec freeze.
- [ ] Every `recharge_end` stores episode ID, linked blackout ID or explicit null, final raw/hash reference,
  wall/monotonic/boot boundary, provenance, and termination-specific data: superseding blackout ID, new battery
  epoch ID, consumed/maximum sample counts, gap/damage reason and damaged hashes as applicable. Epoch reset and
  budget exhaustion carry operational provenance directly in `recharge_end`; they require no fake anchor.
- [ ] The v3 `anchor_kind` enum is closed to `transfer_to_battery`, `raw_firmware_lb`,
  `modeled_safe_shutdown`, `power_restored`, `service_stop`, `boot_boundary`, `charge_stabilized`, `gap`, and
  `corruption`; every kind also carries explicit physical, firmware, modeled, or operational provenance.
- [ ] Blackout-side prestart/overflow loss is a `discharge_gap` with system provenance and exact bounded
  payload: reason, count, first/last boot IDs, monotonic times and nullable wall UTCs, failed command/error type
  where known, plus optional `loss_terminal_boundary_kind` and nullable wall UTC when the accumulated physical
  episode ended after capture stopped. The boundary kind is closed to `power_restored`,
  `modeled_safe_shutdown`, `service_stop`, and `boot_boundary`; null means still active or unknown. It
  rides the next retained blackout chain, is projected into a separate aggregate-loss history count, and is
  acknowledged only after exact payload/hash proof; later loss accumulates in a separate in-flight receipt.
- [ ] Every prestart/in-flight/residual/aggregate-loss receipt also carries immutable
  `observation_origin`/`uat_intent_id`. A recovered blackout START and any unlinked censored recharge created
  from that receipt copy the stamp. No missing START makes UAT provenance vacuously satisfied.
- [ ] The durable `discharge_gap` emitted from any such receipt repeats the exact immutable
  `observation_origin`/`uat_intent_id`; aggregate-loss history and deferred replay never need the deleted
  active-intent file to distinguish induced loss from natural history.
- [ ] `continued_by` is carried by the older interruption's `blackout_end` payload and names the exact newer
  blackout ID. The newer `blackout_start` carries the exact older ID in `continued_from`; both sides carry the
  closed `continuation_kind` (`size_rollover` or `reboot_gap`) and the same `physical_episode_id`.
- [ ] Aggregate loss is attributed to a UTC query by its first lost-boundary wall UTC; receipts whose wall time
  is unavailable are reported separately as range-unattributable, never assigned to the carrier.
- [ ] Every physical sample contains typed safety/science fields plus a bounded canonical map of every raw NUT
  key/token returned in that poll. Oversize or malformed replies retain an explicit loss reason rather than a
  truncated map presented as complete.
- [ ] Consecutive invalid replies are coalesced into one bounded durable loss receipt regardless of alternating
  oversize/malformed subreason. It contains total count, bounded per-subreason counts, and first/last
  boot/monotonic/wall boundaries; it does not append one gap per poll.
  Canonical envelopes `<=16 KiB` are accepted; complete NUT replies in `(16,64] KiB` become `codec_oversize`;
  replies beyond the client's 64 KiB receive limit or incomplete/malformed protocol replies become
  `telemetry_reply_lost` without pretending their unavailable bytes were preserved. Both subreasons feed the
  same coalesced invalid-reply receipt and explicit capture-health state.
  Recovery or terminalization emits one exact gap from that receipt and acknowledges it only after durable
  hash proof. The 16/20 KiB policy is frozen only after comparing it with Slice-0 live reply sizes.
- [ ] Manifest reservation and continuation creation enforce the reference budget before filesystem creation:
  a 65th reference is refused before any manifest entry or file bytes exist. Reference 64 is accepted only for
  the typed terminal continuation reserved by the registry transaction; all readers remain able to project
  the preceding 63 references after a refused attempt.
- [ ] Expose least-authority ports rather than a generic repository: project one blackout/recharge, page
  blackouts by UTC cursor, page recharge links, append capture records, close one aggregate, and rebuild
  projections. Scientific consumers reopen selected JSONL and verify raw hashes; summaries are never evidence.
- [ ] Add a bounded streaming evidence port over `jsonl_large_event_cursor.py`; fragment profiling, assessment,
  comparison and decline consumers may not materialize a maximum-size aggregate or linked physical episode as
  `tuple[all records]`. `DischargeFragmentPolicy` freezes page/window bytes and maximum resident evidence
  state (at most 4 MiB per worker). Streaming sufficient statistics and bounded candidate sets consume every
  eligible sample while safety publication remains responsive. A 64 MiB aggregate and multi-aggregate episode
  fixture proves bounded RSS/state, deterministic hashes/results and continued one-second safety publication.
- [ ] The transform-only cutover preflight requires the v2 work registry to prove zero
  open/preparing/processing captures.
  It also requires every prestart/overflow in-flight and residual receipt slot to be empty. Otherwise
  deployment aborts and the old binary must recover/terminalize its own event/receipt before retry; v3 never
  strands or fabricates a v2 terminal outcome.
- [ ] The transform-only preflight also requires the v2 report outbox to be empty/acknowledged; otherwise abort and let the old binary
  deliver it. No pending v2 report is silently abandoned.

### 4.4 One-shot v2 quiescence handshake and cohort reset

- [ ] A transform-only `scripts/attest-v2-quiescence` owns all v2 knowledge. With the old daemon stopped and
  the sole writer lock held, it verifies the registry, prestart/in-flight/residual receipt slots and report
  outbox are empty, then atomically writes owner-only `v2-quiescence-v1.json` and fsyncs its directory.
- [ ] The closed handshake contains schema/tool revision, creation UTC, state-directory device/inode, exact
  relative paths, byte sizes and hashes of every v2 registry/receipt/outbox input, explicit empty
  booleans/counts, and the baseline source SHA.
  Any missing/nonempty/mutated input aborts. The v3 installer validates this handshake and unchanged input
  hashes without decoding v2 grammar; the v3 daemon never opens v2. The temporary handshake is removed only
  after successful cutover and recorded in the deployment receipt; rollback continues to use the untouched v2
  archive/old binary.
- [ ] V2 raw events are not imported or reprojected into v3 scientific cohorts. Existing learned model values
  and the entire IR safety-budget bookkeeping remain unchanged: battery epoch, `epoch_initial_k_v_per_pp`,
  current k, previous/last commit UTC, cumulative floor, and consumed step hashes are never re-baselined or
  cleared at cutover. Only raw-event IR and decline cohorts restart empty. The first new IR commit cannot occur
  before the current policy again observes at least four qualifying steps, at least three qualifying steps not
  from the current event, and at least two distinct blackout IDs; it may take months or never occur. The
  existing 30-day limit and 50% battery-epoch decrease floor continue from pre-cutover state. Each decline
  metric remains `insufficient_comparable_evidence` until six qualifying v3 samples exist. This clean reset is
  an explicit product cost, needs no operator decision, and avoids compatibility archaeology or double
  consumption.

## 5. Information-content gates

No global minimum number of minutes exists. Each evaluator uses physical information content:

- [ ] Proven natural/test provenance for the fragment and explicit exclusion of model-derived inputs.
- [ ] Same battery epoch and supported policy/evaluator revision.
- [ ] Monotonic ordering within each boot; explicit handling of boot boundaries.
- [ ] Coverage and maximum gap appropriate to the evaluator interval.
- [ ] Battery-voltage span greater than registered quantisation plus noise/deadband.
- [ ] Sufficient load stability for discharge-curve fragments or sufficient load excitation for sag/load
  dependence.
- [ ] A trusted independent anchor where the inference depends on remaining reserve. Raw firmware LB is a
  hardware/firmware marker but not empty capacity; host safe shutdown is model-derived censoring and cannot
  authorize capacity/runtime calibration.
- [ ] Overlap with existing independently observed fragments when absolute position is not anchored.
- [ ] No double consumption of the same canonical raw record/hash for the same model target.
- [ ] Sampling selection is independent of the estimated quantity, uses the uniform registered backbone, or
  has a preregistered correction and bounded residual selection bias.
- [ ] Start readiness is a feature of the fragment: it strengthens a known-full origin but is not mandatory
  when another anchor/overlap identifies the interval.
- [ ] Gaps reject only dependent intervals. For example, damage after a trusted firmware-LB prefix cannot
  invalidate that prefix, while damage before its selected LB does.
- [ ] Before Slice 1 implementation, freeze `DischargeFragmentPolicy`: revision, normal and load-step gap
  bounds, minimum occupied voltage quanta/span, slope/noise windows, stable-load band/window, natural/test
  provenance rules, the closed `observation_origin` and nullable `uat_intent_id` wire fields, continuation-copy
  invariant, same-boot rules, and per-fragment sample/event budgets. These are consumer prerequisites, not a
  new event-wide decision.

## 6. What each event may teach

| Evidence | Automatic scientific use | Must not claim |
|---|---|---|
| Seconds-long natural outage with a clean load step | Load-sag coefficient | Capacity, total runtime, SoH |
| Partial outage with meaningful voltage span and stable load | Local discharge-curve/runtime residual fragment; forward-model score | Unobserved tail or full capacity |
| Partial outage starting below full charge | Same local fragment; overlap alignment with other events | A false full-charge origin |
| Natural event reaching firmware LB | Trusted time/trajectory-to-firmware-LB proxy and curve fragment through LB | Empty capacity or firmware LB as direct shutdown authority |
| Natural event reaching durable safe shutdown then restart | Long trusted pre-shutdown trajectory, operational boundary and censored lower-bound behavior | Physical empty endpoint, amp-hours, total runtime, capacity or SoH |
| Several events with overlapping voltage/load regions | Joint curve/load-dependence constraints and uncertainty reduction | Filling uncovered regions by interpolation presented as measurement |
| Self-test/CAL | Capture/durability/operational diagnostics | Natural-learning authorization |
| Recharge after a known discharge | Charge recovery/stabilization trend and corroborative aging signal | Returned energy or capacity from time alone |

### 6.1 Model targets and safe rollout order

1. [ ] Keep bounded downward load-sag learning as the first already-proven target.
2. [ ] Shadow an `ObservedDischargeSurface` candidate: raw-supported local residuals indexed by voltage region
   and load band, with uncertainty and evidence hashes. It remains diagnostic until an identifiability review
   proves a named model parameter can improve held-out prediction without turning the old model into evidence.
3. [ ] Keep safe-shutdown events censored. Use their longer trusted prefixes for comparisons and decline
   trends, but make no endpoint/runtime/capacity commit from the shutdown boundary.
4. [ ] Keep Peukert, absolute capacity, SoH and discharge-LUT writes disabled with the currently available
   sensors. Reconsider only if a future capability probe supplies independent current and temperature plus a
   defined physical endpoint and comparable load regimes.
5. [ ] Add degradation reporting from comparable current-policy evidence. Automatic downward safety-model
   changes require a separate bounded policy and oracle; upward changes remain report-only.
6. [ ] Keep recharge response diagnostic/corroborative initially. Promote a recharge-derived parameter only
   after a preregistered identifiability review proves it independent using the fields this UPS actually
   exposes.

## 7. Anti-feedback and candidate arbitration

- [ ] Raw physical values and durable hardware/status anchors are the only scientific inputs.
- [ ] Frozen model snapshots are used solely for forward prediction and residual comparison; predicted SoC,
  runtime, charge readiness, or previous derived residuals cannot become observations.
- [ ] Each estimator emits a candidate plus uncertainty/refusal reasons; it never mutates the model.
- [ ] Candidate arbitration checks independence, conflicts between targets, evidence reuse, rate limits,
  cumulative bounds, battery epoch, and safety direction.
- [ ] A model commit is prepared from an exact before hash, persisted through the existing one-writer lane,
  verified, receipted into the originating evidence chain, and idempotently recoverable after every crash
  boundary.
- [ ] Safety oracle compares the old and candidate model over the registered voltage/load grid and the exact
  contributing raw trajectories. No candidate may delay low-battery shutdown outside the approved bounded
  policy.

### 7.1 Required identifiability preregistration for any new writable target

Before code may enable a model target other than the existing `ir_k`, a target-specific ADR must freeze:

- [ ] the single named parameter and causal role in the runtime equation;
- [ ] the exact raw sensor fields and proof that none is produced by the candidate model;
- [ ] nuisance/confounded parameters held fixed and the physical conditions under which separation is valid;
- [ ] cohort selection, minimum independent blackout IDs, load/voltage diversity, sample-hash deduplication,
  and exclusion reasons;
- [ ] a leave-one-blackout-out comparison against the frozen production model, primary error metric, minimum
  practically meaningful improvement greater than raw quantisation/noise, and one-sided 95% uncertainty bound;
- [ ] direction, per-commit and battery-epoch bounds, rate limit, consumed-evidence budget, rollback, and exact
  no-later-shutdown oracle;
- [ ] shadow duration and deterministic fixtures. Failure to fill or pass any field means diagnostic-only; an
  expert opinion cannot waive the preregistration.

## 8. History and reporting

- [ ] Extend the rebuildable summary projection with physical episode kind, start/end UTC, duration, terminal
  anchor, aggregate-loss count, battery epoch, linked recharge ID, `physical_episode_id`, `blackout_id`,
  `continued_from`, `continued_by`, and `continuation_kind`. Do not place raw scientific values in the summary.
- [ ] Bounded history scans fold verified adjacent summaries by `physical_episode_id`: one physical episode
  count, one final physical-boundary count, separate technical segment/rollover counts, and nonexclusive
  episode-level safety-anchor counts. Reboot-gap pieces share the episode count but remain separate scientific
  fragments. A final restoration can therefore coexist with an earlier safe-shutdown/restart fact.
- [ ] Provide a pure `BlackoutHistoryQuery` over bounded index scans: count by calendar range, total observed
  on-battery duration, terminal-type counts, and explicitly unknown/aggregate-loss counts.
- [ ] Expose it through `scripts/battery-health history --from <UTC> --to <UTC>` and
  `scripts/battery-health history --year <YYYY>`. Ranges are half-open UTC intervals selected by physical
  event start time; open events and aggregate-loss receipts are reported in separate counts and never hidden.
- [ ] Blackout and recharge terminal-type counts are separate namespaces. Recharge counts use the closed
  `recharge_end.termination` enum and never inflate blackout counts.
- [ ] Exact query history begins at the v3 cutover. Release-A/v2 archives remain separately labelled forensic
  history and are not silently mixed into v3 counts.
- [ ] Human output distinguishes: recorded, usable fragments, model target changed, diagnostic trend, and
  rejected/lost evidence. Never say “capacity measured” when only runtime/voltage behavior was calibrated.
- [ ] Reporting failure cannot block capture, safety, assessment, or later report reconstruction.

## 9. Delivery slices

### Slice 0 — correct authority and freeze current safety

- [ ] Mark the previous pre-commit RC receipt historical/superseded for product completeness.
- [ ] Write ADR 0004 for unified fragment capabilities and recharge linkage; explicitly supersede ADR 0003's
  `POWER_RESTORED`-only science and IR-only product boundary while retaining its storage/safety decisions.
- [ ] Before implementation edits, create one baseline commit on the feature branch and record its SHA plus an
  exact fixture manifest. Freeze safety/capture/model-owner/JSONL goldens and existing qualifying IR outputs;
  explicitly mark global terminal dispositions as intentionally superseded. The historical 873-test receipt
  is evidence about its old dirty tree, not a substitute for this baseline. Its missing premium verdict stays
  recorded as missing and is not inherited.
- [ ] Register exact available NUT fields from the daemon's ordinary read-only physical NUT reply and a saved
  fixture; never issue a UPS command. Unregistered returned fields remain preserved raw-only, while a
  registered field that is absent disables only dependent assessments.
- [ ] Implement the named read-only Slice-0 producer
  `scripts/record-telemetry-capability-baseline`. It connects only to the configured physical NUT data socket,
  issues no UPS command, records exactly 60 consecutive complete replies plus UPS model/serial/firmware and
  NUT driver identity/version, and atomically writes owner-only
  `telemetry-capability-baseline-v1.json`. It refuses concurrent runs, incomplete replies, identity changes,
  unsafe file ownership/mode, and output replacement without an explicit no-clobber destination. Its codec,
  permissions, restart, and absent/unstable-field behavior are deterministic Slice-0 tests. The artifact
  records every observed `ups.status` and scopes each optional capability to the states in which it was
  observed. An OL-only run may register recharge-visible fields but cannot claim OB availability; any later
  expansion requires another reviewed policy revision from read-only observations. This tool is the producer
  required by the next manifest step; no deployed-v2 code change is needed.
- [ ] V3 deployment preflight requires a present, owner-only, schema-valid baseline whose recorded live
  identity matches the physical UPS/NUT endpoint; missing, corrupt or mismatched first-install input aborts
  activation and instructs the operator to run the named read-only producer. It never silently seeds initial
  scientific capabilities from daemon auto-collection.
- [ ] Persist the versioned capability manifest and its observed raw-key set as derived configuration, not
  scientific evidence. Before policy freezes, the existing read-only NUT path records 60 consecutive ordinary
  physical replies into owner-only `telemetry-capability-baseline-v1.json`; that live-host artifact, not a test
  fixture, is the authority for optional fields in v3 policies. The v3 manifest is seeded only when UPS
  model/serial/firmware and NUT driver identity/version match the baseline. A hardware/driver identity change
  disables dependent typed capabilities while the v3 daemon automatically collects a new state-scoped
  60-reply candidate baseline through ordinary read-only polling; it issues no UPS command and never blocks
  safety/capture. Before Slice 1, freeze `CapabilityIdentityPolicy`: exact hardware model/serial/firmware and
  NUT driver name/version identity fields; status-scoped raw-key/token/parse/missing signatures; and one rule
  that automatic re-enablement is permitted only for an already reviewed field whose new 60-reply signature
  exactly matches its prior registered signature in each state observed by the new window. A previously known
  but not-yet-reobserved state remains capability-unavailable, not mismatched; background read-only collection
  continues and re-enables that state only after its own matching 60-reply window. Any hardware identity
  change, semantic/signature mismatch or newly seen field stays raw-only until a future reviewed policy revision.
  A routine package upgrade therefore needs no operator merely to resume raw observation, and matching
  previously approved capabilities recover automatically without broadening scientific authority.
  Ordinary disappearance is field-level unavailability. Saved fixtures are regression inputs only and never
  populate the live manifest.

### Slice 1 — generalized domain assessment, no new model writes

- [ ] Introduce anchors, typed fragments, consumer-specific assessments, typed reasons, and policy revision.
- [ ] Replace `TerminalSciencePolicy.authorizes_science` and event-wide `EvidenceAssessment` gating at all
  cohort/report call sites with consumer-specific admission. The mechanical inventory includes
  `assessment_worker.py`, `forward_comparison.py`, `learning.py`, `decline_policy.py`, `close_blackout.py`,
  `decline_reporting.py`, `report_reconstruction.py`, `storage_values.py`, `jsonl_work_registry.py`,
  `jsonl_event_stream.py`, `assessment_codec.py`, `jsonl_record_codec.py`, and summary/index codecs.
  Infrastructure terminal disposition may remain rejected, but it cannot act as an event-wide science gate.
  Architecture tests enforce this at assessment and report reconstruction boundaries.
- [ ] Keep current IR result byte-for-byte equivalent for fixtures whose entire current/historical cohort uses
  only `power_restored` events. Fixtures containing service-stop/restart-gap terminals receive explicit new
  fragment-level expected values and recorded rationale; they are not called byte-equivalent.
- [ ] Persist versioned fragment/profile derived records and reconstruct them idempotently.
- [ ] Cut over atomically to fresh v3 blackout/recharge JSONL directories and indexes. V2 and Release-A files
  remain immutable operational archives and are never silently re-evaluated as v3 science; no compatibility
  reader or reverse migration is added.

### Slice 2 — safe-shutdown/restart evidence

- [ ] Add safety-first, bounded best-effort shutdown-boundary capture and exact restart/same-boot
  reconciliation without delaying `upsmon`.
- [ ] Profile partial-start and full-start deep events without claiming amp-hours.
- [ ] Shadow-evaluate long trusted-prefix curve residuals and raw-firmware-LB proxies; host shutdown remains
  censored and no model writes occur in this slice.
- [ ] Prove crash convergence before/after boundary, shutdown, boot, profile, candidate, receipt, and outcome.

### Slice 3 — discharge-surface shadow evaluation

- [ ] Define a rebuildable diagnostic projection for local constraints/uncertainty and evidence identities;
  do not put an unidentifiable surface into safety state.
- [ ] Backtest named candidate parameters on leave-one-event-out raw trajectories and enforce conservative
  safety-oracle behavior.
- [ ] Enable a bounded automatic commit only through a separate reviewed decision proving that parameter is
  identifiable from independent raw fields. If no parameter passes, `ir_k` remains the sole automatic target.

### Slice 4 — recharge evidence

- [ ] Add linked recharge lifecycle, adaptive sampling, bounded JSONL, recovery, and reporting.
- [ ] Establish repeatability baselines by comparable start/end anchors and load history.
- [ ] Keep findings diagnostic until the registered sensors identify a safe independent parameter.
- [ ] No live v3 deployment is permitted before Slices 1–4 ship together; this prevents an interim v3 blackout
  from losing its required recharge episode/receipt. Slice boundaries are development checkpoints, not
  separately deployable releases.

### Slice 5 — richer load dependence and degradation, conditional on sensors

- [ ] Do not promote Peukert/load-dependence without measured battery current, temperature, independent
  endpoint cohorts, and parameter-correlation tests.
- [ ] Compare discharge and recharge trends across battery epoch for early degradation reporting.
- [ ] Any new automatic target gets its own direction/bound/rate/evidence budget and safety oracle.

## 10. Required tests and UAT

### Deterministic tests

- [ ] 5–20 second natural blackout: recorded; history count increments; only independently supported sag
  fragment can learn.
- [ ] Ten-minute partial blackout: local curve fragment retained; unobserved capacity/runtime rejected.
- [ ] Partial-start deep event (for example vendor 60–80%) reaching safe shutdown: all trusted local fragments
  are assessed; full-charge readiness is not required; no empty-capacity/total-runtime claim is made.
- [ ] Full-start deep event reaching safe shutdown and restart: exactly one logical event, boundary receipt,
  fragment assessments and terminal report; no commit is authorized solely by the shutdown boundary.
- [ ] Several overlapping partial/deep events reconstruct only covered voltage/load regions.
- [ ] Same trajectory with insufficient voltage span/quantisation, gap, pre-anchor reboot, or corrupt segment
  refuses only affected capabilities.
- [ ] CAL/self-test and high input voltage remain operational-only for natural estimators.
- [ ] Model-derived runtime/charge/SoC substituted for raw input is structurally impossible or rejected.
- [ ] Recharge sampling is dense at transitions, sparse when stable, bounded indefinitely, and resumes after
  restart without duplicating cycle identity.
- [ ] Every recharge episode contains the configured uniform backbone across its full observed duration;
  evaluators ignore tagged enrichment for unbiased slope/trend statistics unless a registered correction is
  under test.
- [ ] Recharge tail corruption/recovery closes once as `capture_damaged`, emits no scientific assessment, and
  replays idempotently without stranding the registry.
- [ ] Recharge ending at battery-epoch reset records the old/new epoch boundary and cannot link evidence across
  epochs; observation-budget exhaustion closes censored with exact consumed/maximum counts.
- [ ] Reboot-continuous outage split into two interruption records then one OL creates exactly one recharge,
  linked to the newest interruption; the older carries `continued_by`, and replay creates no duplicate.
- [ ] Faster apparent recharge alone cannot update capacity/SoH; missing current and temperature remain
  explicit limitations.
- [ ] Plain-language outcome fixtures independently cover all five product states: recorded, scientifically
  usable, model changed, diagnostic-only trend, and refused/lost with an exact reason. Restart reconstruction
  yields the same user-facing result through `scripts/battery-health.py` and the health/MOTD projection.
- [ ] Per supported degradation signal, five comparable natural v3 samples remain
  `insufficient comparable evidence`; the sixth produces the deterministic current-policy stable or possible-
  worsening result. Mixed invalid/test evidence is skipped without poisoning six later valid samples, and the
  same bounded result is visible through `scripts/battery-health.py` plus health/MOTD reporting.
- [ ] Calendar query returns exact individual events plus separately labelled aggregate overflow counts.
- [ ] A blackout approaching the 62 MiB capture-append limit rolls before consuming the reserved terminal
  headroom, whose worst-case full derived/terminal tail is proven to fit; the same occurs when 63 references
  exist and the next recovery needs the terminal-reserved 64th reference. The recoverable transaction proves successor
  START durable before carrier END/link, never emits a dangling `continued_by`, and loses no accepted sample.
  Later restoration yields one physical episode/history count, one final physical-boundary count, one
  technical rollover count, and preserves every earlier safety-anchor flag. Reboot continuation uses the same
  episode ID but `reboot_gap` and refuses cross-gap science. Fault injection at every registry/start/end/swap
  boundary either resumes the exact IDs/bytes or leaves the old aggregate open with an exact bounded-loss
  receipt; it never fabricates continuity or erases a safe-shutdown anchor.
- [ ] Recharge policy construction rejects any sample/duration/record-size/gap-count combination whose
  capture-region bytes exceed 62 MiB or whose independently bounded terminal tail exceeds 2 MiB. A
  63-reference damaged recharge uses only the reserved terminal reference and closes with
  `episode_budget_exhausted/budget_kind=segment_refs`; neither byte nor reference exhaustion escapes as an
  adapter error.
- [ ] Fault matrix covers every append/fsync/replace/readback/receipt boundary and converges after restart.
- [ ] A paused writer proves the combined decision-sample/anchor command remains adjacent ahead of later poll
  samples; inverted order refuses exact shutdown reconciliation.
- [ ] A held submission lock makes `try_reserve` fail within its bound, leaves safety publication fresh, and
  produces censored/unhealthy capture state without waiting for filesystem I/O.
- [ ] The v2 quiescence tool refuses each nonempty registry/receipt/outbox state and every post-attestation
  mutation; exact empty-state handshake permits one cutover and is never opened by the v3 daemon.
- [ ] A natural outage occurring before the planned manual UAT run remains operational-only but does not disarm
  the active intent; the later manual run and both recharge episodes are also operational-only until explicit
  close/expiry, after which a new natural event is eligible normally.
- [ ] UAT intent removal from the active slot is acknowledged only after the capture/prestart/residual
  registries prove that every boundary inside the closed/expired window is either terminalized or represented
  by a durable provenance-stamped receipt, every durable blackout START is stamped, and every linked or
  receipt-derived unlinked recharge has copied it. The active file remains in a closed, nonmatching state
  while any such boundary is unresolved; only then may it be deleted. Deferred assessment after deletion,
  missing-START recovery and aggregate rollover retain the same operational-only origin; exact replay tests
  prove no induced data can be laundered into natural cohorts.
- [ ] Existing quality gates remain mandatory: CRAP <=30 per production function; Ruff structural complexity
  with no suppressions; source modules <=800 and classes <=500; Pyright; Vulture; Import Linter; Tach exact;
  architecture guards; full `just check` only at coherent RC boundaries.

### Live UAT after implementation and reviews

- [ ] Read-only preflight: physical UPS online, battery not already low, virtual UPS fresh, model/events backups
  verified, no active capture/test, rollback command proven.
- [ ] Built-in short self-test validates detection, one-second capture, JSONL closure, recharge linkage, report,
  and zero natural-learning authorization.
- [ ] If a controlled approximately ten-minute mains-removal run is used, first create a durable, expiring
  `uat_intent` in the state directory with creation UTC, operator-selected expiry capped at one hour, random
  intent ID, and purpose. It survives restart and marks every event whose start falls inside the active window
  permanently operational-only; creation after a first raw sample can never relabel that event. Matching an
  event records its ID but does not disarm the window. The operator explicitly closes/cancels the intent after
  all planned runs, or expiry closes it automatically; removal follows durable acknowledgement. The runbook
  requires confirming an active intent immediately before every induced mains removal. A coincident natural
  event inside the window is conservatively excluded; this accepted loss is preferable to admitting induced
  data. This run validates plumbing only.
- [ ] An active-window UAT/self-test provenance is inherited by every linked recharge episode, which is also permanently
  operational-only, and exact `observation_origin`/`uat_intent_id` values are asserted in both recharge START
  and END codec/restart tests. The runbook forbids inducing mains loss after intent expiry without first creating a fresh
  intent. Software cannot distinguish a later unmarked manual unplug from a natural outage; violating this
  procedure is an explicitly accepted operator error that may misclassify the event, not a guarantee the
  daemon can enforce.
- [ ] Natural long-blackout observation is post-deployment monitoring, not a random release gate. Deterministic
  shutdown/restart/fault tests are release authority. A natural event, whenever it later occurs, must
  automatically produce long-prefix assessments and either the independently supported bounded change or a
  precise refusal.
- [ ] Validate history queries and recharge episode closure after the event.
- [ ] Remove temporary review/UAT artifacts and the explicitly temporary state backup only after acceptance;
  never delete raw event evidence.

## 11. Review and release gates

- [ ] Kaizen-led expert panel verifies business coverage, scientific honesty, DDD/SOLID/DRY boundaries,
  unattended recovery, and minimality after each slice.
- [ ] Standard Cross-AI review closes all Critical/High/Medium findings on the plan and implementation.
- [ ] Premium Cross-AI/Claude Opus reviews the exact plan before implementation and the exact tested tree
  before RC. A quota/session error is not a verdict.
- [ ] `just check` passes on the exact reviewed tree and the receipt records the tree identity and test count.
- [ ] No deployment claim is made before the user-run privileged deployment and runbook UAT.

## 12. Scientific interpretation of recharge

A worn lead-acid battery can appear to reach the charger's voltage/percentage target sooner because it has
less usable capacity, but charge time is not monotonic health truth: increased internal resistance and poor
charge acceptance can alter or slow the curve, and the UPS charger controls an unobserved current profile.
Published lead-acid work supports using charge-curve features as SoH correlates, while manufacturer charge
characteristics show that voltage/current/time jointly define the stages. With this UPS's current sensor set,
recharge therefore begins as linked corroborative evidence, not an automatic capacity measurement.

References:

- MDPI Electronics, *State of Health Estimation of Lead-Acid Batteries Based on the Charging Curve*:
  https://www.mdpi.com/2079-9292/12/21/4552
- GS Yuasa, NP-series charge characteristics and application data:
  https://www.gs-yuasa.com/en/products/pdf/NP_PE_PX_PXL_PWL_Japan.pdf

## 13. Definition of done

Expected marginal result is explicit: history, fragment-level diagnostics, deep-prefix comparisons, and linked
recharge trends become materially richer immediately; the only guaranteed automatic model learner remains
the existing `ir_k`. With the present sensors, no second writable parameter may pass identifiability, so the
automatic prediction gain beyond better IR evidence may honestly be zero. Product completion means the system
automatically extracts and evaluates all available information without making false claims, not that it forces
an unidentifiable parameter to change.

This product correction is complete only when all raw blackout/recharge capture and history requirements are
live, deep safe-shutdown evidence is automatically profiled, every automatic model target has independent
identifiability and safety gates, all deterministic and applicable live acceptance gates pass, the final expert panel
finds no Critical/High/Medium defect, premium Cross-AI returns an actual GO, and no routine path needs an
operator or agent to decide whether evidence should be used.
