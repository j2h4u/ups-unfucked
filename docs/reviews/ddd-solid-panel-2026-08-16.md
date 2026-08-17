# DDD/SOLID panel — 2026-08-16

Status: preserved Cluster 0 review artifact; deployment verdict: **NO-GO**.

This artifact records the DDD/SOLID panel delivered in the 2026-08-16 working
session. The session review was not written to a repository file, so this is a
faithful, normalized preservation of the panel observations and finding
register that were carried into
[`ddd-solid-remediation-and-quality-gates.md`](../plans/ddd-solid-remediation-and-quality-gates.md).
The finding IDs below are stable. This file is not rewritten to make later
implementation appear to have been part of the review.

## Provenance and review boundary

| Item | Snapshot |
|---|---|
| Review date | 2026-08-16 |
| Branch | `feat/natural-blackout-learning` |
| Reviewed HEAD at preservation | `77a84ca9775b1f110f1aa1b7f2de527dee5e5592` |
| Authoritative plan SHA-256 | `93a6501029b4bf6048a6b66e52552cab131e45c5f93d08e6d0861bcd300b5a28` |
| Working tree | Dirty candidate; pre-existing/concurrent edits were preserved and not reverted |
| Production source roots measured | `src/` and `scripts/`; tests excluded from module budgets |
| Deployment/service/UPS/state action | None in this review or Cluster 0 tooling |
| Temporary state backup | Preserved outside the repository according to the operations runbook; not copied, moved, or deleted |

The artifact SHA-256 is recorded in the Cluster 0 handoff after this file is
written. The plan SHA above is the input identity used for this preservation.

## Panel verdict

The candidate has a real domain/application/adapter boundary and is materially
safer than the legacy runtime. Safety publication, scientific honesty, and the
single model-writer intent are strong. The candidate remains **NO-GO** because
freshness/fail-closed safety behavior, degraded startup, restart-reconstructable
outcomes, learning edge cases, responsibility concentration, and quality-gate
blind spots are not all closed on the reviewed tree.

The panel's ordering for conflicts is: safety, correctness, reliability,
simplicity, effort, elegance. A green function-complexity score does not prove
that a large adapter has one responsibility, and a passing dependency graph
does not prove bounded writer-lane behavior.

## Non-negotiable product contracts

- Safety publication is the highest-priority function.
- A stale virtual `OL` cannot remain trusted as current physical truth.
- Storage, assessment, reporting, and learning failures cannot silently disable host safety.
- Natural-blackout evidence is captured and processed automatically, 24/7.
- Self-tests and calibration do not authorize scientific learning.
- Only independent raw observations can change a model parameter.
- Partial events never become measured capacity, SoH, Peukert, total runtime, or a learned LUT.
- Model changes are conservative, bounded, idempotent, and owned by one writer.
- Per-event JSONL remains the durable evidence format; SQLite and a generic persistence framework are out of scope.
- Normal operation and recovery require no operator or agent data handling.

## Findings and traceability

The owner is the primary remediation cluster. The acceptance identifier is the
exact test or fixture name to be created or retained by that cluster; it is not
an assertion that a future production behavior has already passed.

| ID | Severity | Finding and source evidence | Owner | Acceptance identifier | Disposition |
|---|---|---|---:|---|---|
| SAF-01 | Critical | Stale virtual `OL` plus healthy watchdog after telemetry loss (`src/monitor.py`, `src/virtual_ups_exporter.py`) | 1 | `fixture.c1_persistent_poll_loss_external` | open |
| SAF-02 | Critical | Publication failure/restart can leave trusted stale output (`src/virtual_ups_exporter.py`, systemd unit) | 1 | `fixture.c1_publication_failure_eio_enospc_deadline` | open |
| BOOT-01 | High | Scientific store recovery precedes first current safety publication (`src/monitor.py`, `src/adapters/jsonl_event_store.py`) | 2 | `fixture.c2_corrupt_store_degraded_start` | open |
| REP-01 | High | Terminal report can disappear after seal/restart (`src/monitor.py`, storage projection) | 3 | `test_c3_crash_after_seal_reconstructs_report` | open |
| OPS-01 | Medium | Shared error channel and idle durability lag can misstate health | 3 | `test_c3_channel_latches_and_idle_lag_is_zero` | open |
| SCI-01 | Correctness blocker | Third ignored current-event step blocks an otherwise valid cohort (`src/application/assessment_worker.py`, `src/domain/ir_identification.py`) | 4 | `test_c4_selected_two_plus_third_ignored_step` | open |
| SCI-02 | Medium | Candidate-event overflow is reduced to a boolean (`EpochIndexTail`) | 4 | `test_c4_epoch_overflow_zero_one_many_and_bound` | open |
| SCI-03 | Medium | Learning thresholds have two sources of truth (`src/domain/learning.py`, `src/adapters/model_owner.py`) | 4 | `test_c4_policy_revision_value_replay_matrix` | open |
| QLT-01 | Quality blocker | Complexity suppressions bypass mandatory Ruff policy | 5 | `test_quality_gates.py::test_suppression_scanner_only_rejects_mandatory_complexity_rules` | open; scanner now reports 12 current suppressions |
| QLT-02 | Quality gap | No fixed module/class source-span ceiling; current large modules are 2,950, 1,134, and 1,033 lines | 5 | `test_quality_gates.py::test_source_span_checker_enforces_strict_module_boundary` and `test_source_span_checker_includes_nested_class_boundaries` | open; report-only until Cluster 6 |
| ARC-01 | Medium | JSONL adapter concentrates codec, stream, registry, index, and filesystem responsibilities | 6 | `test_c6_jsonl_facade_fault_goldens` | open |
| ARC-02 | Medium | Broad `EventStorePort` violates least authority (`src/application/ports.py`) | 7 | `test_c7_consumer_owned_event_ports` | open |
| ARC-03 | Medium | Assessment mixes orchestration and wire codec (`src/application/assessment_worker.py`) | 6 | `test_c6_assessment_codec_differential_replay` | open |
| ARC-04 | Medium | Decline policy leaks into application (`src/application/decline_reporting.py`) | 6 | `test_c6_decline_policy_domain_boundary` | open |
| ARC-05 | Medium | Model owner mixes schema, persistence, and transaction mechanics (`src/adapters/model_owner.py`) | 6 | `test_c6_model_owner_byte_refusal_receipt_differential` | open |
| ARC-06 | Medium | Domain lifecycle is not runtime authority (`src/domain/lifecycle.py`, capture path) | 6 | `test_c6_runtime_uses_one_lifecycle_transition_table` | open |
| ARC-07 | Low/Medium | Composition root owns background application orchestration (`src/monitor.py`) | 6 | `test_c6_monitor_composition_responsibility` | open |
| ARC-08 | Low/Medium | Ports expose more authority than consumers need (`model_port.py`, NUT contracts) | 7 | `test_c7_structural_least_authority` | open |
| ARC-09 | Low | Architecture tests overfit private symbol call counts | 7 | `test_c7_semantic_ownership_boundaries` | open |
| DEP-01 | Quality gap | Tach/Import Linter previously described broad/transitional layers and Tach dependencies were not explicit | 8 | `tests/test_quality_gates.py::test_forbidden_edge_fixtures_pin_the_architecture_contract`, `tach check`, `tach check --exact` | tooling active; transitional map handoff remains |
| RUN-01 | Medium | Writer lane can be monopolized by model/index work | 9 | `fixture.c9_slow_maintenance_then_ob_sla` | open |
| RUN-02 | Medium | Growing index/health work is not fully bounded | 9 | `fixture.c9_large_history_bounded_work` | open |

No recommendation from the normalized register is silently dropped. Open
findings remain release blockers at their stated severity; a Low may only be
accepted or scheduled after the independent final review.

## Evidence snapshot

### Source concentration

The deterministic report-only checker uses physical source lines (including
blank and comment lines) and AST class start/end lines. At this preservation
snapshot it reported:

| Path | Kind | Exact span | Limit |
|---|---|---:|---:|
| `src/adapters/jsonl_event_store.py` | module | 2,950 lines | 800 |
| `src/adapters/jsonl_event_store.py` | `JsonlEventStore` class, lines 214–1,954 | 1,741 lines | 500 |
| `src/adapters/model_owner.py` | module | 1,033 lines | 800 |
| `src/application/assessment_worker.py` | module | 1,134 lines | 800 |

The largest under-budget responsibility spans were retained as review context:
`BlackoutCapture` lines 33–473 (441 lines), `ModelOwner` lines 508–898 (391
lines), and `AssessmentWorker` lines 163–394 (232 lines). The checker reports
all violations in stable path/start/name order; it does not use a baseline,
ratchet, per-file budget, exception list, or test-file input.

### Complexity suppression inventory

The mandatory Ruff set remains upstream-default `C901`, `PLR0904`,
`PLR0911`–`PLR0917`, and `PLR1702`. The tokenizer-based scanner found these
12 current suppressions (line numbers are from this snapshot):

```text
src/adapters/model_owner.py:202      C901
src/adapters/model_owner.py:767      C901
src/adapters/model_transform.py:422  C901, PLR0914, PLR1702
src/adapters/model_transform.py:454  PLR1702
src/application/assessment_worker.py:206  PLR0914
src/application/assessment_worker.py:322  PLR0913
src/application/assessment_worker.py:352  PLR0911
src/application/assessment_worker.py:488  PLR0911
src/application/assessment_worker.py:647  C901
src/application/capture_blackout.py:482  PLR0911
src/battery_math/lut.py:23  PLR0911
src/battery_math/peukert.py:35  PLR0913, PLR0917
```

The suppression check is now a hard lint step and therefore correctly fails
until Cluster 5/6 removes these comments and refactors their natural seams.
No threshold override or suppression was added to make the gate pass.

### Architecture gate receipt

The normative pre-extraction map is encoded in both `pyproject.toml` and
`tach.toml`:

```text
composition -> adapters -> application -> domain -> battery_math
```

The current map assigns `src.monitor`, `src.monitor_config`,
`src.scheduler_manager`, and `src.ema_filter` to transitional composition;
`src.adapters.*`, `src.alerter`, `src.motd_status`, `src.nut_client`, and
`src.virtual_ups_exporter` to adapters; `src.application.*` to application;
`src.domain.*` to domain; and `src.battery_math.*` to battery math. The
explicit low-level NUT client allowance is `src.nut_client` from adapters.

At this snapshot, the following read-only checks passed:

```text
uv run lint-imports --no-cache     4 contracts kept, 0 broken
uv run tach check                 ✅ All modules validated!
uv run tach check --exact         ✅ All modules validated!
```

`layers_explicit_depends_on = true` is enabled. The exact invocation is kept
separate from normal mode so stale declared dependencies cannot hide behind a
successful ordinary check. Three forbidden-edge fixtures pin the intended
rejection boundaries; they are under `tests/fixtures/architecture/` and are
not production imports.

## Deterministic safety and backup evidence

The existing safety golden remains
`tests/application/test_safety.py::test_virtual_status_matches_pinned_release_a_golden`.
The model transformation tests retain exact-backup and refusal behavior in
`tests/test_model_transform.py`. Cluster 0 did not mutate model bytes, state
directories, service units, sockets, UPS configuration, or deployment state.
The temporary pre-transform state backup remains an external rollback asset,
not a repository artifact; its exact path and hash belong in the deployment
receipt, not in this review file.

## Activation handoff and prerequisites

1. Keep the source-span command report-only while any module/class violation remains. Cluster 6 must split only real cohesive responsibilities, rerun the command, and activate it in `just check` and CI only after zero violations.
2. Keep the mandatory-complexity suppression step hard. Cluster 5/6 must remove all 12 comments through refactoring; do not replace them with thresholds, per-file ignores, baselines, or exceptions.
3. Keep `uv run tach check` and `uv run tach check --exact` in `justfile` and CI. When Cluster 6 moves transitional modules, update the normative map and every explicit dependency together; do not widen a layer to make an edge disappear.
4. Turn the forbidden-edge fixtures into an actual temporary `src` fixture run when the architecture harness is extended; a deliberately injected outward edge must fail both the intended Import Linter contract and the intended Tach module declaration.
5. Run the full `just check` only at a cluster boundary or release candidate. The current candidate also has unrelated concurrent test/CRAP failures; those are not silently attributed to this evidence/tooling slice.

## Review closeout

The panel preserves dissent and keeps the candidate NO-GO. Cluster 0 is
complete only as evidence capture and tooling foundation; production extraction,
safety behavior changes, live UPS acceptance, deployment, and post-UAT
adjudication remain unauthorized by this artifact.
