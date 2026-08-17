# DDD/SOLID Remediation and Architecture Quality Plan

Status: pre-commit review reopened on 2026-08-17; no deployment is authorized by this document.

Current execution status is tracked in
`docs/plans/precommit-ddd-solid-product-remediation-2026-08-17.md`. The detailed unchecked decomposition in
this document is the original design specification, not a current task counter; its 639-test receipt is
historical and must not be used to approve the changed candidate.

This plan turns the independent DDD/SOLID panel findings into an executable checklist. It applies
to the current `feat/natural-blackout-learning` candidate and preserves the original product goal:
the service must protect the host, record natural blackouts, and use trustworthy evidence
automatically without operator intervention or a model feedback loop.

Source note: the panel review was delivered in the 2026-08-16 working session rather than as a
repository artifact. Section 7 is the normalized finding register. Before implementation, Cluster
0 must preserve the verbatim review as `docs/reviews/ddd-solid-panel-2026-08-16.md`, record its
SHA-256 and reviewed tree identity, and bind every stable finding ID below to that artifact.

### Implementation receipt — 2026-08-16

> Historical receipt for the earlier 639-test candidate. Subsequent expert review found
> additional product and architecture gaps and changed the tree. It is not a current GO receipt;
> the exact final counts, dispositions, and verdict will be recorded after the reopened panel and
> full release gate converge.

This receipt separates repository evidence from deployment evidence. The detailed checkboxes below remain
the acceptance specification; this summary is the current execution status for the exact dirty feature-tree
candidate reviewed before any deployment.

- [x] Clusters 0–10 are implemented in the repository and their deterministic release gate passes.
- [x] The original plan identity is preserved as
  `93a6501029b4bf6048a6b66e52552cab131e45c5f93d08e6d0861bcd300b5a28`.
- [x] The verbatim panel review is preserved at
  `docs/reviews/ddd-solid-panel-2026-08-16.md`, SHA-256
  `662f12774e66b037d2d87a5fc35d5a641bc86f0f3d54cd51aa5600aaf59c27d2`.
- [x] Candidate branch is `feat/natural-blackout-learning`; base HEAD is
  `77a84ca9775b1f110f1aa1b7f2de527dee5e5592`; the implementation is intentionally uncommitted pending
  final review, so this is not yet a deployable commit identity.
- [x] `just check` passes on the post-boundedness candidate: Ruff format/lint with upstream default
  complexity thresholds, zero mandatory complexity suppressions, module `<=800`, class `<=500`, Pyright,
  Vulture, Import Linter 6/6, Tach normal and exact, 639 tests, and CRAP maximum `29.89 <= 30`.
- [x] Coverage (`84%` in this run) is informational and used only as an input to CRAP; no coverage-percent
  acceptance gate exists.
- [x] Executable adversarial fixtures prove the configured Import Linter and Tach reject injected outward
  dependencies; the clean architecture passes both tools.
- [x] Installer/systemd fixtures pass 28 tests and the external NUT fixture passes 3 tests; `bash -n`
  and ShellCheck pass. These are isolated fixtures, not a claim about the installed live units.
- [x] No live service, UPS command, production state migration, deployment, or physical outage was performed
  while implementing Clusters 0–10. The external temporary state backup remains intentionally retained.
- [x] Cluster 11 final expert panel, premium Cross-AI convergence, and fresh-reader test pass on this
  exact candidate. The final premium receipt is preserved at
  `docs/reviews/cross-ai-ddd-final-accepted-2026-08-17/20260816T193941Z/claude-opus-5.md`.
- [ ] Cluster 12 deployment and bounded live UPS acceptance are pending separate execution under the runbook.
- [ ] Cluster 13 post-UAT adjudication and backup cleanup remain pending by design.

## 1. Current verdict

The refactor is materially better than the legacy runtime. The DDD layers are real rather than
cosmetic: domain policies are pure, application use cases depend on ports, adapters own NUT and
filesystem details, and model mutation has one owner. The repository candidate is **GO for RC
review**; deployment remains **NO-GO** until live acceptance is complete.

Panel summary:

- DDD and dependency inversion are strong.
- Scientific honesty and safety-before-learning are strong.
- SRP and ISP remain weak around persistence, recovery, and orchestration.
- Several operational safety and correctness paths are not yet represented by the current gates.
- The implementation must be improved incrementally; another big-bang rewrite is out of scope.

## 2. Why the 3,000-line modules passed the current gates

The existing gates are enabled, but they measure different things:

- Ruff `C901` and `PLR0911` through `PLR0917` primarily measure individual functions.
- Ruff `PLR0904` counts public methods, so a class with many private methods can still pass.
- CRAP is also calculated per function from cyclomatic complexity and test coverage.
- Import Linter and Tach check dependency direction, not module cohesion or responsibility count.
- There is no current hard gate for production module length, class span, private method count, or
  concentration of unrelated responsibilities.

Therefore a 2,950-line adapter can pass when it consists of many individually small private
functions. This is a genuine quality-gate blind spot, not evidence that Ruff or CRAP are disabled.

## 3. Business goals that must remain visible in every slice

- [ ] Safety publication remains the highest-priority function of the service.
- [ ] No stale virtual `OL` can be treated indefinitely as current physical truth.
- [ ] Storage, assessment, reporting, or learning failure cannot silently disable host safety.
- [ ] Natural-blackout evidence is captured and processed automatically, 24/7.
- [ ] Self-tests and calibration never authorize scientific learning.
- [ ] Only independent raw observations can change a model parameter.
- [ ] Partial events never become measured capacity, SoH, Peukert, total runtime, or a learned LUT.
- [ ] Model changes remain conservative, bounded, idempotent, and owned by one writer.
- [ ] Per-event JSONL remains the durable evidence format; SQLite is not introduced.
- [ ] Normal operation and recovery require no operator or agent data handling.
- [ ] The system remains maintainable by one owner and does not acquire speculative frameworks.

## 4. Non-goals

- [ ] Do not redesign the battery chemistry model.
- [ ] Do not add temperature, current, capacity, or other sensors the UPS does not expose.
- [ ] Do not add a generic event bus, repository framework, ORM, service container, or plugin system.
- [ ] Do not force a real model commit during UPS acceptance testing.
- [ ] Do not preserve obsolete internal APIs merely for compatibility; this is a single-host service.
- [ ] Do not split files mechanically without first assigning one coherent responsibility per target.
- [ ] Do not weaken safety, CRAP, Ruff, typing, or architecture thresholds to make a gate green.

## 5. Target architecture

The intended dependency direction is:

```text
composition
    -> adapters
    -> application
        -> domain
            -> battery_math
```

The intended runtime flow is:

```text
NUT observation
    -> freshness and safety policy
    -> virtual UPS publication
    -> asynchronous capture handoff
    -> per-event JSONL
    -> assessment of sealed raw evidence
    -> optional prepared model change
    -> one model owner
    -> durable terminal outcome
    -> reconstructable operator report
```

The intended degraded flow is:

```text
scientific storage unavailable
    -> current safety publication still operates
    -> capture and learning are disabled
    -> explicit latched health alarm
    -> no false claim that evidence was recorded
```

## 6. Delivery strategy

Each cluster below must be code-complete and independently reviewable. Use targeted tests during a
cluster. Run the full gate only at cluster boundaries that change production semantics and for the
release candidate. Keep the temporary state backup until deployment and live acceptance finish.

No cluster may begin by moving files. First freeze behavior with tests, then change the smallest
coherent boundary, then update imports and gates.

---

## Cluster 0 — Freeze the candidate and the panel findings

Goal: make the remediation work auditable and prevent accidental scope drift.

- [ ] Record the current branch, commit, working-tree inventory, and authoritative plan SHA.
- [ ] Preserve the verbatim 2026-08-16 panel review at the declared path and record its SHA-256;
  do not rewrite the review to match later implementation.
- [ ] Record the exact current production file and class spans.
- [ ] Record all complexity-related `noqa` occurrences in `src/` and `scripts/`.
- [ ] Bind each stable finding ID to the preserved source, confirm its primary owner cluster, and
  replace each planned acceptance description with an exact test/fixture identifier.
- [ ] Preserve the current deterministic safety goldens before changing failure behavior.
- [ ] Preserve the temporary state backup and document that it is not part of the repository.
- [ ] Confirm no production service mutation, UPS command, state migration, or deployment occurs
  during clusters 0 through 11.
- [ ] Allow only official-documentation review, read-only installed-config inspection, and an
  isolated temporary NUT/dummy-ups/upsmon fixture with separate paths, sockets, process names, and
  unit names. Production service/UPS proof requires separate authorization in Cluster 12.

Exit criteria:

- [ ] Every Critical, High, Medium, and quality-gate finding has exactly one owner cluster.
- [ ] No recommendation is silently dropped or deferred without an explicit reason.

## Cluster 1 — Safety freshness and fail-closed publication

Goal: an old virtual `OL` must never masquerade indefinitely as fresh physical state.

### 1.1 Decide and encode the safety-liveness contract

- [ ] Inspect installed NUT/dummy-ups/upsmon configuration read-only and consult relevant official
  documentation; exercise behavior only in the isolated fixture defined by Cluster 0.
- [ ] Define a typed publication-freshness state: fresh, temporarily unavailable, and stale/failed.
- [ ] Define the maximum acceptable age of the last successful physical and virtual publication.
- [ ] Choose one fail-closed action for stale data only after proving its real upsmon behavior:
  publish a validated fail-safe status, or deliberately fail/restart in a way that cannot leave a
  permanently trusted stale file.
- [ ] Document why the chosen action is safer than both stale `OL` and unconditional premature
  shutdown.
- [ ] Make publication age and failure reason visible in the bounded health projection.

### 1.2 Correct the runtime failure boundary

- [ ] Handle `SafetyPublicationError` explicitly in the main loop.
- [ ] Stop sending a healthy watchdog signal when the safety path no longer meets the freshness
  contract.
- [ ] Ensure persistent NUT failure cannot exhaust restart attempts and leave the safety unit dead
  while upsmon continues trusting old output.
- [ ] Bound virtual publication latency; a stuck `fdatasync` or directory `fsync` must not silently
  stall the one-second safety loop and watchdog.
- [ ] Keep raw firmware `LB` diagnostic-only; do not use this cluster to reintroduce it into the
  modeled decision.

Tests:

- [ ] One missed physical poll remains observable and follows the selected grace policy.
- [ ] Persistent physical poll failure cannot leave stale `OL` trusted as fresh.
- [ ] Virtual output `EIO`, `ENOSPC`, and permission failure follow the same explicit policy.
- [ ] A blocked publication exceeds the deadline and is detected.
- [ ] The systemd/upsmon fixture proves the externally observed result, not only Python state.
- [ ] Recovery to fresh `OL` clears only the intended failure state.

Exit criteria:

- [ ] No stale publication beyond the defined age can be reported as healthy.
- [ ] Watchdog, process exit/restart behavior, virtual file state, and upsmon observation agree.

## Cluster 2 — Safety-first degraded startup

Goal: corruption of scientific event storage must not prevent the first current safety publication.

- [ ] Separate the lightweight process/writer exclusivity decision from scientific store recovery.
- [ ] Keep a conflicting active writer lock fatal; never create two writers.
- [ ] Construct telemetry, model snapshot access, and safety publisher before expensive event
  recovery.
- [ ] Publish one current validated safety state before assessment, index rebuild, or corruption
  repair.
- [ ] Introduce the smallest no-capture application implementation needed for degraded operation;
  do not create a generic storage framework.
- [ ] If JSONL storage cannot open or recover, run safety with capture/learning disabled and a
  latched, operator-visible alarm.
- [ ] Make later bounded recovery possible without blocking the one-second poll.
- [ ] Ensure a corrupt or missing model remains a separate explicit startup decision; do not hide it
  under the event-storage degraded path.

Tests:

- [ ] Corrupt registry: current safety publishes, capture is unavailable, alarm is explicit.
- [ ] Event-directory permission error: same behavior.
- [ ] Store lock conflict: startup refuses without creating a second writer.
- [ ] Slow recovery cannot delay first safety publication.
- [ ] After recovery succeeds, capture activation never invents or duplicates an event.

Exit criteria:

- [ ] Event storage failure can disable science but cannot silently disable safety.

## Cluster 3 — Durable outcome and reconstructable report

Goal: a completed event must remain explainable after any crash or restart.

- [ ] Define the terminal outcome as the durable source for the human report.
- [ ] Persist every bounded fact required to reconstruct the report: disposition, ordered reasons,
  comparison result/mode, model before/estimate/after where applicable, evidence identifiers, and
  decline eligibility.
- [ ] Reconstruct the latest report on startup from the sealed event/outcome instead of relying on
  an in-memory `_CloseNotice`.
- [ ] Keep journald delivery best-effort unless a real requirement demands delivery receipts.
- [ ] Do not add a durable message queue merely to publish one local report.
- [ ] Ensure the health projection can explain the latest terminal outcome after restart.
- [ ] Separate poll, capture, storage, report, and background error channels; a successful poll must
  not erase an unrelated latched failure.
- [ ] Correct `durability_lag_s`: zero or null with no accepted-undurable work; otherwise age of the
  oldest accepted observation that is not yet durable.

Tests:

- [ ] Crash after terminal outcome but before in-memory notice.
- [ ] Crash after index summary but before report publication.
- [ ] Restart reconstructs byte-equivalent bounded report content.
- [ ] A successful virtual publication does not clear a capture/report/storage failure.
- [ ] Idle sealed storage reports no growing durability lag.

Exit criteria:

- [ ] Every sealed event remains understandable without RAM state or manual JSONL inspection.

## Cluster 4 — Learning correctness and single policy authority

Goal: close the identified learning bugs without changing the scientific scope.

### 4.1 Current-event step accounting

- [ ] Count only the selected first two current-event cohort positions.
- [ ] Do not pass the count of every detected step to `IrLearningContext`.
- [ ] Add a regression: two valid selected steps plus a third ignored step remains eligible when all
  other cohort requirements pass.
- [ ] Preserve the rule that the third step cannot replace a consumed first or second position.

### 4.2 Exact epoch overflow

- [ ] Replace `EpochIndexTail.has_more: bool` with a bounded exact `overflow_count`.
- [ ] Scan only within the existing byte/event bounds; fail closed if the exact count cannot be
  proven within the bound.
- [ ] Pass the exact count through application and domain records.
- [ ] Add 0/1/many and truncated-scan boundary tests.

### 4.3 One versioned learning policy

- [ ] Define bounds, deadband, rate limit, and cumulative decrease limits once as an immutable,
  versioned domain policy value.
- [ ] Freeze the policy revision and its scientific values into the event assessment/prepared-commit
  record before any model write.
- [ ] Make restart/replay use the recorded revision; an unknown revision, mixed revisions, or a
  revision/value mismatch fails closed without changing model bytes.
- [ ] Keep independent adapter validation, but consume the same policy values rather than duplicate
  numeric constants.
- [ ] Prove that application decision and `ModelOwner` refusal cannot drift.
- [ ] Preserve the final dense safety oracle as an independent write-time check.
- [ ] Add old-known, unknown, mixed, mismatch, and crash/restart replay fixtures.

Exit criteria:

- [ ] A valid cohort is neither rejected because of ignored observations nor accepted with
  ambiguous history.
- [ ] Domain and adapter policy values have one source of truth.

## Cluster 5 — Honest code and module complexity gates

Goal: control function complexity and concentration of responsibility independently.

### 5.1 Preserve the existing gates

- [ ] Keep CRAP as a per-function hard gate at `<= 30`.
- [ ] Keep Ruff `C901`, `PLR0904`, `PLR0911` through `PLR0917`, and `PLR1702` enabled for
  `src`, `tests`, and `scripts`.
- [ ] Keep Pyright, Ruff format, import-linter, Tach, and architecture tests in `just check` and CI.
- [ ] Keep repository-wide coverage percentage informational only.

### 5.2 Eliminate gate bypasses

- [ ] Remove every complexity-related `noqa` from production and scripts.
- [ ] Add a small static check that fails on `noqa` for mandatory complexity codes.
- [ ] Do not add threshold overrides, per-file ignores, generated baselines, or exception lists.
- [ ] Refactor each currently suppressed natural seam: strict schema validation, durable-stage
  grammar, JSON conversion, physics inputs, and dense-oracle iteration.

### 5.3 Add fixed module/class source-span concentration ceilings

Ruff's upstream complexity defaults remain authoritative for functions and methods. Ruff does not
provide an upstream default for total module length, and `PLR0904` counts public methods rather than
the full span of a class. The repository therefore needs one separate, fixed source-span ceiling.
It is a guardrail, not a measurement of cohesion or SRP, and must report source path, class or
module span, the violated limit, and remediation guidance.

- [ ] Add a simple AST/line-based checker for production Python modules and classes.
- [ ] Hard gate: no production Python module may exceed 800 physical source lines.
- [ ] Hard gate: no production class may span more than 500 source lines.
- [ ] Apply the same thresholds repository-wide: no grandfathered files, moving baseline, ratchet,
  per-file budget, or exception list.
- [ ] Target budget for newly split modules: 500 lines or fewer; exceeding the target is a review
  flag, not a reason to split a coherent algorithm mechanically.
- [ ] Exclude tests from the module-size gate; test quality remains behavioral and CRAP-driven.
- [ ] Do not count blank/comment-only lines as a second competing metric; keep one deterministic
  implementation and document it.
- [ ] Add review checks against statement packing, arbitrary helper/mixin sharding, and
  forwarding-only modules; the responsibility map and review establish cohesion, not line count.
- [ ] Build and self-test the checker before extraction, but add its hard invocation to `just check`
  and CI only after Cluster 6 has brought the current tree under the fixed limits.
- [ ] Add self-tests for boundary values and nested classes.

Exit criteria for Cluster 5 design and tooling:

- [ ] The checker reports every current violation deterministically without becoming a hard gate
  before Cluster 6 completes.
- [ ] A coherent mathematical module is not fragmented solely to chase a vanity metric.

## Cluster 6 — Split the concentrated responsibilities

Goal: reduce change radius while retaining one store facade, one writer, and one model owner.

### 6.1 JSONL adapter

Keep `JsonlEventStore` as the transactional facade. Extract internal collaborators, not public
repositories or a generic persistence framework.

- [ ] Extract canonical record and summary codec.
- [ ] Extract strict event-stream append/read/torn-tail handling.
- [ ] Extract work-registry serialization and transition persistence.
- [ ] Extract sealed index projection and bounded rebuild maintenance.
- [ ] Extract narrow filesystem primitives only where reused by these components.
- [ ] Keep writer lock ownership and transaction ordering in one obvious facade.
- [ ] Preserve file bytes, hashes, permissions, fsync order, corruption evidence, and recovery
  behavior with golden/fault-injection tests.
- [ ] Make event-index lookup bounded; do not rescan a growing full index for each seal forever.
- [ ] Make storage health inventory bounded and resumable.

Target shape:

```text
src/adapters/jsonl_event_store.py      transactional facade
src/adapters/jsonl_record_codec.py     canonical bytes and strict decoding
src/adapters/jsonl_event_stream.py     append/read/tail repair
src/adapters/jsonl_work_registry.py    durable work state
src/adapters/jsonl_index.py            summaries and bounded rebuild
```

Names may change to match the final ubiquitous language; generic `utils` or `helpers` are forbidden.

### 6.2 Assessment application service

- [ ] Keep `AssessmentWorker` responsible for one use case: prepare a deterministic close plan from
  sealed evidence.
- [ ] Extract strict event/snapshot/derived-record decoding into one application boundary codec.
- [ ] Extract durable-stage replay grammar from orchestration.
- [ ] Move decline sample construction and thresholds into a pure domain service.
- [ ] Remove duplicated observation/JSON conversion from capture, assessment, and decline paths.
- [ ] Keep raw evidence decoding fail-closed and bounded.

### 6.3 Model adapter

- [ ] Keep `ModelOwner` as the only mutable authority and public facade.
- [ ] Extract target-state schema codec and validation.
- [ ] Extract atomic file/precommit persistence mechanics.
- [ ] Keep commit policy and snapshot publication visible in the owner transaction.
- [ ] Preserve byte-identical refusal, lock, backup, receipt replay, and dense-oracle tests.

### 6.4 Composition and lifecycle

- [ ] Move `BackgroundCoordinator` from `monitor.py` into the application layer.
- [ ] Leave `monitor.py` with composition, signal lifecycle, and the one-second safety loop.
- [ ] Make the domain lifecycle state machine the production authority for blackout transitions.
- [ ] Remove the parallel boolean state machine once the tagged lifecycle states cover its behavior.
- [ ] If a domain lifecycle type remains unused after the migration, delete it rather than retain a
  paper architecture.

Exit criteria:

- [ ] Every extracted module has one sentence describing its responsibility.
- [ ] No new wrapper exists solely to satisfy a line-count limit.
- [ ] JSONL format and model bytes remain compatible with the reviewed candidate artifacts.
- [ ] All production modules are at most 800 physical lines and all production classes span at most
  500 lines, with no exception or baseline.
- [ ] Enable the already-tested source-span checker as a hard `just check` and CI gate only now,
  after the current violations are zero.
- [ ] A 3,000-line god adapter can no longer pass merely because each private function is simple.

## Cluster 7 — Consumer-owned ports and SOLID cleanup

Goal: narrow authority without creating interface ceremony.

- [ ] Split the current broad `EventStorePort` by actual consumer:
  capture append, assessment query/recovery, reporting health/index, and maintenance.
- [ ] Let one concrete `JsonlEventStore` structurally implement the narrow protocols; do not add
  forwarding adapter classes.
- [ ] Split model snapshot/projection, commit preparation, and commit execution authority where the
  consumers differ.
- [ ] Give `AssessmentWorker` no commit execution capability.
- [ ] Give the commit lane no unrelated reporting/maintenance authority.
- [ ] Define a read-only NUT telemetry protocol that cannot expose `send_instcmd` to the daemon.
- [ ] Merge or remove the unused `SafetyPublisherPort` and duplicate `PollPublisher` contract.
- [ ] Replace private-symbol call-count architecture tests with semantic ownership tests where
  possible; retain byte-level and crash behavior in adapter tests.
- [ ] Confirm no second model writer, event writer, scheduler, or UPS command path exists.

Exit criteria:

- [ ] Each application service receives only the operations it needs.
- [ ] Narrowing ports reduces authority and test setup rather than increasing wrapper count.

## Cluster 8 — Rebuild Tach and Import Linter from the final architecture

Goal: make the actual post-refactor dependency graph executable and exact.

Do this after module extraction and port narrowing, not before. Otherwise the configuration merely
describes temporary paths and immediately becomes stale.

### 8.1 Required dependency matrix

| Layer | May depend on | Must not depend on |
|---|---|---|
| `battery_math` | standard library | domain, application, adapters, composition |
| `domain` | battery_math, standard library | application, adapters, composition |
| `application` | domain, battery_math, application-owned contracts | concrete adapters, composition |
| `adapters` | application contracts, domain, battery_math, approved low-level client | composition |
| `composition` | all inward layers | no reverse imports from lower layers |

Normative module-to-layer map before Cluster 6 extraction:

| Layer | Modules |
|---|---|
| `battery_math` | `src.battery_math.*`; move `src.ema_filter` here only if it remains pure math |
| `domain` | `src.domain.*` |
| `application` | `src.application.*`; final background coordinator; final scheduler use case |
| `adapters` | `src.adapters.*`, `src.alerter`, `src.motd_status`, `src.virtual_ups_exporter`; `src.nut_client` is the explicitly approved low-level NUT client |
| `composition` | `src.monitor`, `src.monitor_config`; any top-level runtime entry point |

`src.scheduler_manager` and `src.ema_filter` are transitional names: Cluster 6 must either move them
to the stated final layer or record why their final responsibility belongs elsewhere. Every new
module extracted by Cluster 6 must be added to the normative map before Cluster 8 changes either
tool. The final map must assign every `src/**/*.py` exactly once; the observed import graph is
evidence to compare with the policy, never the source of the policy itself.

Checklist:

- [ ] Complete the normative module-to-layer map, including every extracted module and approved
  low-level client, then inventory the actual final import graph independently.
- [ ] Resolve every actual edge that contradicts the normative map before configuring allowances.
- [ ] Update Import Linter layers to the final packages and composition modules.
- [ ] Add explicit forbidden contracts: domain/application to concrete adapters; battery_math to
  every higher layer; domain to application/composition.
- [ ] Add independence contracts where sibling adapters must not import each other.
- [ ] Keep approved low-level NUT client access explicit rather than hiding it in a broad exception.
- [ ] Rebuild `tach.toml` module boundaries and `depends_on` declarations from the same matrix.
- [ ] Enable `layers_explicit_depends_on = true`, or the verified equivalent for the pinned Tach
  version, so an undeclared actual edge fails the ordinary dependency check.
- [ ] Run both `tach check` and `tach check --exact`; the former rejects illegal/undeclared actual
  edges and the latter also rejects stale declared dependencies.
- [ ] Add fixture tests proving one forbidden import per boundary is rejected.
- [ ] Remove stale module names and temporary compatibility allowances.
- [ ] Document which tool owns which guarantee to avoid duplicate, contradictory configuration.

Tool responsibilities:

- [ ] Ruff: local syntax, style, function/class structural complexity.
- [ ] CRAP: risky function complexity relative to tests.
- [ ] Source-span checker: fixed module/class concentration ceilings and forbidden suppressions;
  it does not claim to measure cohesion.
- [ ] Import Linter: human-readable architectural dependency contracts.
- [ ] Tach: exact module dependency declarations and drift detection.
- [ ] AST ownership tests: semantic invariants not expressible as imports, such as sole model writer.

Exit criteria:

- [ ] `lint-imports`, `tach check`, and `tach check --exact` all pass on the same architecture.
- [ ] A deliberately injected outward dependency is rejected by both the intended tool and its test.
- [ ] No config entry names a deleted or transitional module.

## Cluster 9 — Writer-lane boundedness and long-running behavior

Goal: preserve one-second capture when background maintenance or model work is slow.

- [ ] Do not start model commit or index maintenance while a capture is active or accepted capture
  commands are queued.
- [ ] Split maintenance into bounded cooperative chunks with a measured wall-time budget.
- [ ] Export maximum writer-lane busy time and queue age.
- [ ] Make seal idempotency lookup bounded as the number of events grows.
- [ ] Make health/index inventory resumable instead of a full unbounded scan.
- [ ] Test a slow maintenance chunk immediately followed by physical `OB`.
- [ ] Prove START and observations meet the durability SLA without overflow.
- [ ] Preserve lifecycle priority and same-event FIFO ordering.

Exit criteria:

- [ ] Background work cannot monopolize the sole writer across a blackout transition.
- [ ] Event count growth does not create unbounded per-poll or per-seal work.

## Cluster 10 — Documentation and deterministic release candidate

- [ ] Update ADR 0003 with the final safety freshness and degraded-startup decisions.
- [ ] Update the operations runbook with freshness, failure, and rollback observations.
- [ ] Update glossary and user scenarios using plain language.
- [ ] Remove or clearly archive stale legacy architecture documentation.
- [ ] Update diagrams to the actual final modules and ports.
- [ ] Run focused tests after each cluster and the complete `just check` for the RC.
- [ ] Require zero complexity suppressions, zero module/class budget violations, CRAP `<= 30`, and
  green Ruff, Pyright, Import Linter, Tach, AST ownership tests, and full pytest.
- [ ] Run fault fixtures for telemetry loss, publication failure, storage corruption, report crash,
  model commit replay, and slow maintenance.
- [ ] Record the exact RC commit, tree hash, plan hash, test receipts, and migration fingerprints.
- [ ] Confirm no live UPS/state/deployment action occurred during implementation.

## Cluster 11 — Independent final review and convergence

The final review must compare this checklist with the live codebase, not merely read implementation
summaries.

### 11.1 Expert panel

- [x] Convene Kaizen Master as panel lead.
- [x] Include an independent DDD/SOLID architect.
- [x] Include an independent SRE/QA adversarial reviewer.
- [x] Give each reviewer the business goals, this plan, the final dependency graph, and repository
  access read-only.
- [x] Require reviewers to inspect production code before tests.
- [x] Require explicit closure status for every checkbox and every prior panel finding.
- [x] Require a fresh architecture score and GO / GO_WITH_FOLLOWUPS / NO_GO verdict.
- [x] Ask the panel what complexity was removed, what was merely relocated, and what should be the
  next smallest improvement after deployment.
- [x] Preserve dissent; resolve conflicts in the order safety, correctness, reliability, simplicity,
  effort, elegance.

### 11.2 Cross-AI review

- [x] After the panel reaches no Critical/High/Medium blocker, run premium Cross-AI through Claude
  Opus against the final code and this plan.
- [x] Fix material findings and repeat deterministic gates and review until GO.
- [x] Do not treat API limits, invalid output, or an old GO from an earlier tree as final approval.

### 11.3 Reader test

- [x] Give this plan and final architecture document to a fresh-context reviewer.
- [x] Verify it can answer: what protects the host, what may change the model, what happens when
  storage fails, who owns each write, what every gate proves, and what remains for live UAT.
- [x] Fix ambiguous or contradictory documentation before deployment.

Exit criteria:

- [x] Panel verdict has no Critical, High, or Medium release blocker.
- [x] Premium Cross-AI verdict is GO on the exact final tree.
- [x] Every remaining Low has an explicit disposition: fixed, deliberately accepted, or scheduled
  as the next Kaizen improvement.

## Cluster 12 — Deployment and live acceptance

- [ ] Follow the reviewed operations runbook and exact RC artifact.
- [ ] Re-run read-only preflight and verify physical/virtual `OL`, charge, reserve, writer identity,
  model fingerprint, publication freshness, capture health, and rollback assets.
- [ ] Perform the one-shot model transformation with the exact configured shutdown threshold.
- [ ] Start the candidate and require a fresh safety publication before scientific recovery.
- [ ] Verify degraded-mode indicators without injecting destructive production faults.
- [ ] Optionally run the separately authorized short UPS self-test; confirm it cannot learn.
- [ ] Perform the separately authorized bounded 360-second natural outage with all abort conditions.
- [ ] Require unattended close, durable outcome, reconstructable report, and no unexplained model
  change.
- [ ] Accept `recorded_only` as a correct outcome when no multi-event cohort qualifies.
- [ ] Observe restart/recovery and normal reporting without manual event processing.
- [ ] Retain the temporary state backup through the acceptance and rollback window.
- [ ] Preserve deployed commit/tree/config identity, transform receipt, model/event hashes, fresh
  safety publication, service state, terminal outcome, and reconstructed report for adjudication.

## Cluster 13 — Independent post-UAT adjudication and cleanup

Goal: distinguish a green repository from a proven deployed system.

- [ ] Give an independent read-only verifier the exact RC identity, deployed identity, reviewed
  configuration, Cluster 12 receipts, and rollback/backup inventory.
- [ ] Verify deployed commit and tree match the approved artifact and the expected final Tach/Import
  Linter architecture configuration is installed.
- [ ] Verify the transform receipt and model/event hashes; explain every model fingerprint change.
- [ ] Verify a fresh virtual safety publication, service state, observed physical/virtual status,
  terminal outcome, and restart-reconstructable report from the live receipts.
- [ ] Confirm no UAT observation contradicts the safety freshness, degraded startup, writer SLA, or
  automatic-learning contracts.
- [ ] Require an independent GO / GO_WITH_FOLLOWUPS / NO_GO verdict and record follow-ups with
  severity and owner; only GO completes this plan.
- [ ] Treat GO_WITH_FOLLOWUPS as unfinished: close the follow-ups and repeat the relevant checks
  until the independent verifier returns GO.
- [ ] Delete the temporary backup only after GO and explicit confirmation that rollback no longer
  requires it; inventory the exact path and size before deletion and verify cleanup.

Exit criteria:

- [ ] Repository gates, pre-deployment panel review, deployment receipts, and post-UAT verification
  all refer to the same explainable release lineage.
- [ ] No temporary fixture, process, socket, unit, file, or backup remains without an explicit reason.

## 7. Traceability matrix

Cluster 0 binds each row to the preserved panel source and replaces the planned acceptance
description with an exact test/fixture ID. Cluster 11 and Cluster 13 update `open` to `fixed`,
`accepted`, or `scheduled`, with only non-blocking Low findings eligible for the latter two. Each
finding has one primary owner; a prerequisite is not a second owner.

| ID | Finding and source evidence | Severity | Primary owner | Acceptance proof | Final disposition |
|---|---|---:|---:|---|---|
| SAF-01 | Stale virtual `OL` plus healthy watchdog after telemetry loss (`monitor.py`, `virtual_ups_exporter.py`) | Critical | 1 | persistent-poll-loss external fixture | fixed in repository; live UAT pending |
| SAF-02 | Publication failure/restart can leave trusted stale output (`virtual_ups_exporter.py`, systemd unit) | Critical | 1 | EIO/ENOSPC/deadline plus restart fixture | fixed in repository; live UAT pending |
| BOOT-01 | Scientific store construction/recovery precedes first current safety publication (`monitor.py`, `jsonl_event_store.py`) | High | 2 | corrupt/unwritable-store degraded-start fixture | fixed in repository |
| REP-01 | Terminal plain-language report can disappear after seal/restart (`monitor.py`, `storage_values.py`) | High | 3 | crash-after-seal restart reconstruction fixture | fixed in repository |
| OPS-01 | Shared error channel and idle durability lag misstate health (`virtual_ups_exporter.py`, `jsonl_event_store.py`) | Medium | 3 | channel-clear and accepted-not-durable age fixtures | fixed in repository |
| SCI-01 | Third ignored current-event step blocks an otherwise valid cohort (`assessment_worker.py`, `ir_identification.py`) | Correctness blocker | 4 | selected-two-plus-third regression | fixed in repository |
| SCI-02 | Candidate-event overflow is reduced to a boolean (`storage_values.py`, assessment path) | Medium | 4 | exact 0/1/many/bounded-failure fixtures | fixed in repository |
| SCI-03 | Learning thresholds have two sources of truth (`domain/learning.py`, `adapters/model_owner.py`) | Medium | 4 | revision/value/replay matrix and drift test | fixed in repository |
| QLT-01 | Complexity suppressions bypass mandatory policy (production `noqa` inventory) | Quality blocker | 5 | suppression scanner plus Ruff defaults | fixed in repository |
| QLT-02 | No fixed module/class source-span ceiling (2,950/1,093/972-line files) | Quality gap | 5 | checker self-tests; hard activation in Cluster 6 | fixed in repository |
| ARC-01 | JSONL adapter concentrates multiple persistence responsibilities (`jsonl_event_store.py`) | Medium | 6 | facade fault goldens plus responsibility map | fixed in repository |
| ARC-02 | Broad `EventStorePort` violates least authority (`application/ports.py`) | Medium | 7 | consumer-owned protocol type/architecture tests | fixed in repository |
| ARC-03 | Assessment mixes orchestration and wire codec (`assessment_worker.py`) | Medium | 6 | extracted-codec differential/replay tests | fixed in repository |
| ARC-04 | Decline policy leaks into application (`decline_reporting.py`, `assessment_worker.py`) | Medium | 6 | pure-domain policy boundary tests | fixed in repository |
| ARC-05 | Model owner mixes schema, persistence, and transaction mechanics (`model_owner.py`) | Medium | 6 | byte/refusal/receipt/lock differential tests | fixed in repository |
| ARC-06 | Domain lifecycle is not runtime authority (`domain/lifecycle.py`, `capture_blackout.py`) | Medium | 6 | one transition table used by production tests | fixed in repository |
| ARC-07 | Composition root owns background application orchestration (`monitor.py`) | Low/Medium | 6 | composition import/responsibility test | fixed in repository |
| ARC-08 | Ports expose more authority than consumers need (`model_port.py`, NUT contracts) | Low/Medium | 7 | structural least-authority tests | fixed in repository |
| ARC-09 | Architecture tests overfit private symbol call counts (`test_architecture_boundaries.py`) | Low | 7 | semantic ownership plus behavioral writer tests | fixed in repository |
| DEP-01 | Tach/Import Linter describe broad/transitional layers and Tach dependencies are not explicit | Quality gap | 8 | forbidden-edge fixtures plus normal/exact Tach | fixed in repository |
| RUN-01 | Writer lane can be monopolized by model/index work (`capture_writer.py`, scheduler path) | Medium | 9 | slow-maintenance-to-OB SLA fixture | fixed in repository |
| RUN-02 | Growing index/health work is not fully bounded (`jsonl_event_store.py`) | Medium | 9 | large-history bounded-work benchmark/test | fixed in repository |

## 8. Final definition of done

- [ ] All release-blocking checklist items are complete.
- [ ] Every business goal in section 3 has a production path and a test or live acceptance proof.
- [ ] No production module exceeds 800 lines and no production class exceeds 500 lines.
- [ ] No mandatory complexity rule is suppressed.
- [ ] CRAP, Ruff complexity, fixed source-span ceilings, Pyright, Import Linter, normal/exact Tach,
  ownership tests, and full pytest are green.
- [ ] Safety remains operational or explicitly fail-closed during telemetry, publication, storage,
  and background failures.
- [ ] The latest event outcome and report survive restart.
- [ ] Natural evidence processing remains fully automatic and scientifically independent.
- [x] Expert panel and premium Cross-AI approve the exact final tree.
- [ ] Deployment and bounded live acceptance pass under the reviewed runbook, followed by an
  independent post-UAT GO.
- [ ] Temporary work and review processes are cleaned up; the state backup is handled according to
  the explicit rollback decision.
