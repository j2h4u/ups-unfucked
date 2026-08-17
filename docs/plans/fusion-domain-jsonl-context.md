# Fusion planning brief: domain architecture and JSONL blackout learning

## Decision already approved by the user

Plan one coherent, deliberately large next slice for `ups-battery-monitor`.

- Adopt a lightweight domain architecture before adding more blackout-learning behavior.
- Separate domain decisions, application orchestration, and persistence adapters.
- Use JSONL files, not SQLite or another database.
- Human/system logs are diagnostic output only and are never the source of scientific data.
- Do not work on Ruff, Tach, import-linter, CI lint settings, or their existing findings in this slice.
  Parallel-agent changes to those files are unrelated and must be preserved, not edited or reverted.
- There is no product requirement for backward-compatible state migration or reverse migration. Preserve
  the existing single journal byte-for-byte as a read-only archive; do not keep it in the active runtime
  path and do not import it unless an explicit product acceptance criterion requires that.
- Avoid a DDD framework, generic repository framework, event-sourcing framework, message bus, database,
  or speculative abstractions. This is one daemon, one UPS, one writer, and low event volume.
- Treat the physical UPS `LB` token as diagnostic/evidence only. It must be preserved in event JSONL but must
  not force virtual LB/FSD; shutdown timing remains owned by the model threshold and existing hard floor.

## Business goals: precedence over technical preferences

1. The daemon runs unattended 24/7. Every physical blackout is durably represented and automatically
   reaches one understandable outcome: used for an allowed purpose or rejected with exact reasons.
2. Every sufficiently good discharge automatically checks the existing voltage/chemistry model against
   the actually observed curve.
3. Partial discharge is censored evidence. It must never be called measured full runtime, absolute
   capacity, or SoH.
4. Never add a generic runtime correction factor or another signal that masks errors in the physical
   model.
5. Change only a specific battery-model parameter identified by evidence independent of the model
   output. Independence means physical/sensor provenance, not manual approval.
6. No positive feedback: predictions, virtual LB, model-derived SoC, or prior residuals are not truth
   labels for updating the same model.
7. Reliable evidence applies any allowed safe update automatically. Routine event learning never waits
   for a human or agent. Manual input is reserved for an externally unobservable fact such as physical
   battery replacement.
8. Repeated comparable long or terminal events must eventually reveal declining available reserve.
9. Learning must never weaken or delay conservative virtual LB and upsmon safe-shutdown behavior.
10. A release is complete only when the end-to-end user outcome is proven and reported in plain language,
    not when capture or unit tests alone are green.

## Product outcome required from this slice

The slice must deliver a real vertical path, not only module movement:

```text
physical NUT observations
  -> domain blackout lifecycle
  -> durable event-specific JSONL data
  -> automatic evidence assessment
  -> comparison with immutable start-time battery-model snapshot
  -> explicit per-purpose learning decision
  -> safe automatic model update only when an independently identifiable allowed parameter exists
  -> durable result and plain-language health/MOTD/CLI explanation
```

For every event the user must be able to answer:

- What happened and for how long?
- Which physical points were preserved?
- Were the data good enough, and if not, exactly why not?
- Where did the current physical model disagree with observations?
- What model parameter changed, if any, from which independent evidence and within what safety bound?
- If nothing changed, why was that scientifically correct?
- Did the safety/shutdown behavior remain unchanged?

Do not promise that every partial event changes the model. The plan must state exactly which event facts
identify which parameter. If ordinary partials can only validate or falsify the forward curve, say so.
If the slice includes an automatic parameter update, define its physical evidence, direction/bounds,
anti-feedback proof, rollback snapshot, and safety monotonicity test.

This project exists to build and maintain a progressively more accurate model of this specific physical
battery, not merely to archive telemetry or calculate residuals. The slice is not product-complete if it
only moves modules and records shadow comparisons. It must implement at least one real automatic
parameter-update path when independently identifying physical evidence is present, while automatically
refusing updates that the observed event cannot identify. It must also retain comparable-event evidence
needed to detect declining available reserve over time. The plan must name the parameter, proof, safety
bounds, minimum evidence, and user-visible before/after result; it may not invent a generic correction.

Short blackouts remain useful even when they cannot identify capacity: every one contributes to the
plain-language event history (frequency, duration, physical voltage/load trajectory, gaps) and, when
quality permits, validates the actually observed upper portion of the frozen forward model. This value
must be automatic and visible, not a manual forensic workflow.

The existing runtime estimate is already more useful than the UPS firmware. The architecture replacement
must preserve or improve its current behavior and may not regress the physical-to-virtual NUT output,
one-second polling, conservative LB timing, or upsmon shutdown path while pursuing learning.

## Required architecture boundary

Use ubiquitous project language and prefer a small number of explicit components. The plan must settle
exact names and contracts for at least these responsibilities:

- domain values for a blackout, observation, lifecycle, evidence assessment, frozen model snapshot,
  model comparison/residuals, and per-purpose learning decision;
- pure domain policies for lifecycle transition, evidence classification, quality gates, forward-model
  comparison, and allowed model-change decision;
- application use cases that coordinate capture, close/process, startup recovery, and reporting;
- ports for event storage, battery-model snapshots/commits, physical telemetry, and safety publication;
- JSONL adapter(s), model-file adapter, NUT adapter, and user-facing report adapter;
- a thin composition root/poll loop that contains no scientific decision rules.

The domain imports no filesystem, JSON, NUT, systemd, logger, CLI, `BatteryModel`, or mutable persistence
object. Persistence does not decide evidence quality or model eligibility. Orchestration does not reach
into private handler/model fields.

## JSONL constraints and questions the plan must settle

- Prefer one immutable event-specific JSONL file per blackout plus the smallest necessary atomic active
  pointer/index, rather than one indefinitely replayed global event store. Compare at least three JSONL
  layouts and justify the selected one.
- Each event file contains the original physical start, observations, termination fact, frozen start-time
  model/readiness snapshot, assessment/comparison, learning decision, and model-commit receipt when one
  exists. Derived records must be distinguishable from raw physical observations.
- Define exact filename/event-ID rules, schema/version, permissions (`0700` directory, `0600` files),
  symlink/non-regular rejection, append/fsync/atomic-rename rules, torn-tail handling, middle corruption,
  process restart, reboot during OB, duplicate delivery, and one-writer ownership.
- Startup must inspect only the active pointer/current event and bounded metadata. It must not replay all
  historical event files before the first physical poll or virtual safety publication.
- A blackout during recovery must still be captured. Do not invent a second general-purpose journal to
  compensate for a flawed primary layout.
- Historical files are opened only for an explicit event query, reporting, bounded trend calculation, or
  planned evidence cohort; never on every one-second safety poll.
- The plan must choose the durable observation cadence explicitly. Physical polling remains one second;
  preserve immediate start/end and enough raw points for short-event history and model comparison without
  inventing unavailable temperature data.
- Retention is indefinite at current low volume, but disk usage/capture failure must be visible and may
  never silently delete unprocessed evidence.

## Physical evidence limitations and test policy

- This CyberPower UPS exposes no battery-temperature sensor. Temperature must remain explicitly
  unavailable; no synthetic/default temperature may be stored or used as evidence.
- The UPS does not provide an independently measured battery current. Voltage, raw NUT status/input
  voltage, and load percentage are observed; any current/Ah derived from nominal watts/voltage and load
  percentage is a proxy and must be named as such. It cannot silently become an authoritative measured
  capacity label.
- A natural partial event may validate/falsify only its observed curve. Return to OL is a censored end,
  and model-derived virtual LB is not independent terminal evidence.
- No automatic deep discharge, ten-minute forced off-mains run, or battery-to-exhaustion test belongs in
  this slice. A vendor short self-test may be used during separately authorized integration debugging,
  possibly more than once, but it is not capacity/SoH evidence and is never an unattended learning
  prerequisite.
- Release A remains the live safety/capture baseline throughout implementation. The new slice is deployed
  only as one verified code-complete candidate; no half-migrated domain/storage path reaches production.

## Current code facts that the plan must address

- `src/monitor.py` is a 1,353-line orchestrator and directly coordinates model, journal, collector,
  handler, scheduler, safety output, replay, and reporting.
- `src/model.py` is 1,072 lines and mixes persisted state, scientific model access, mutations, and atomic
  file writing.
- `src/discharge_collector.py` owns lifecycle but imports/holds mutable `BatteryModel`, handler, EMA,
  config, and journal.
- `src/discharge_handler.py` mixes scientific calculations, eligibility, direct state/physics mutation,
  persistence, RLS/capacity estimators, alerts, and reporting side effects.
- `src/discharge_types.py` contains only thin `CompletedDischarge` and `ModelApplicationResult` contracts
  with free-form strings and one broad eligibility boolean.
- `src/discharge_journal.py` is currently a single-file append/replay store and some public operations call
  full `replay()` internally. This active architecture is superseded by this fusion decision.
- `src/battery_math/` is already a useful pure kernel and should be reused rather than rewritten.
- Release A capture-only safety behavior is deployed and must stay operational until the replacement
  slice passes its release gate. No live service, NUT, or production state changes occur during planning.

## Planning requirements

Produce an implementation-ready plan with:

1. Product invariants and ubiquitous language.
2. At least three architecture/storage approaches inside the JSONL constraint, with explicit rejection.
3. Selected lightweight domain decomposition and import/dependency direction.
4. Exact domain types, ports, use cases, JSONL schema/layout, state transitions, and failure semantics.
5. Exact old-module decomposition/removal map; do not leave parallel legacy and new business paths.
6. Goal-backward implementation clusters that each leave code complete; the final cluster proves the full
   physical-poll-to-user-report path.
7. Tests for pure domain policies, adapter durability/crash recovery, anti-feedback, zero model writes on
   insufficient evidence, safe bounded automatic update where applicable, one-second safety isolation,
   restart/reboot, and plain-language acceptance.
8. Release/rollback strategy that preserves the old production state archive but does not require reverse
   migration. No live deployment in this planning task.
9. Explicit non-goals, including all linter/tooling improvement work.
10. A recommendation on whether this slice may safely change a model parameter or should stop at automatic
    shadow comparison, justified from identifiable physical evidence and the business goals rather than
    schedule convenience.

No implementation, no test execution, no live operations, and no edits outside planning artifacts.

## Post-implementation live acceptance required by the user

The implementation is accepted only after deterministic automated tests and a staged real-UPS UAT.
Planning must specify the runbook and exact observable pass/fail evidence, but must not execute it now.

1. Read-only preflight: physical and virtual UPS are healthy/OL, battery is sufficiently charged, load is
   safe, no shutdown is pending, event storage/model fingerprints are captured, the daemon is single-writer,
   and a verified rollback artifact exists.
2. Optional vendor short self-test may first validate event detection, classification, JSONL durability,
   and reporting. It is not equivalent to a natural blackout and cannot authorize capacity/SoH learning.
3. For the product acceptance, the user physically removes UPS input power for a bounded interval and
   later restores it. Software does not issue a deep-test command or force a full discharge.
4. During the live event, one-second physical polling, virtual UPS publication, conservative LB/upsmon
   behavior, durable event-specific JSONL writes, and human event history must continue.
5. After OL returns, the system automatically closes and assesses the event without operator data handling.
   It reports preserved points, quality, frozen-model comparison, per-purpose decision, any exact parameter
   update with before/after/proof, or the scientific reason no parameter changed.
6. A bounded partial blackout is allowed to finish with no model mutation when it cannot identify a
   parameter; that is a valid scientific result, not a pipeline failure. The same UAT must still prove the
   useful event history and model-comparison product path.
7. Pass requires unchanged or more conservative safety timing, no UPS command from the daemon, no event
   loss/duplication, no manual approval queue, and plain-language output. Any stale virtual output, later LB,
   unexplained model mutation, storage corruption, duplicate event, or restart loop is a rollback trigger.
