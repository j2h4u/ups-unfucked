"""Read-only blackout assessment and deterministic close-plan preparation."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from src.application.assessment_codec import (
    ProjectionInputError,
    mapping,
    parse_utc,
    project_observations,
    project_snapshot,
    required_float,
    step_hash,
)
from src.application.assessment_replay import (
    DecisionBasis as _DecisionBasis,
)
from src.application.assessment_replay import (
    DurableDerivedStages as _DurableDerivedStages,
)
from src.application.assessment_replay import (
    durable_close,
    durable_derived_stages,
    new_derived_records,
)
from src.application.model_port import (
    AssessmentModelPort,
    ModelPortConflict,
    ModelPortRefused,
    PreparedModelCommit,
)
from src.application.ports import AssessmentQueryEventStorePort
from src.application.storage_values import (
    EventHandle,
    EventProjection,
    EventRecord,
    EventRef,
    EventSummary,
    ProcessingRef,
)
from src.domain.decline_policy import decline_evidence_eligible
from src.domain.evidence import (
    EvidenceContext,
    TerminalSciencePolicy,
    assess_evidence,
    terminal_science_policy,
)
from src.domain.forward_comparison import compare_forward_model
from src.domain.ir_identification import (
    CohortStep,
    IrCohortContext,
    IrCohortSelection,
    IrRawObservation,
    identify_load_steps,
    select_ir_cohort,
    selected_current_event_step_count,
)
from src.domain.learning import (
    IrLearningContext,
    ObservedLoadSagIncrease,
    evaluate_ir_learning,
    make_learning_decision,
    model_commit_refusal_reason,
)
from src.domain.lifecycle import classify_physical_observation
from src.domain.reasons import (
    EvidenceReason,
    LearningReason,
    OrderedReasons,
    order_reasons,
)
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    BlackoutKind,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    FrozenModelSnapshot,
    IrCohortEstimate,
    IrLearningPolicy,
    LearningDecision,
    ModelCommitReceipt,
    PhysicalObservation,
    TerminationFact,
)

MAX_PENDING_ASSESSMENTS = 8
HISTORICAL_COHORT_LIMIT = 31


def _historical_summary_is_eligible(summary: EventSummary) -> bool:
    return (
        summary.evidence_class == EvidenceClass.QUALIFYING.value
        and terminal_science_policy(summary.termination).authorizes_science
        and not summary.damaged_segment_hashes
        and summary.damaged_segment_overflow == 0
    )


@dataclass(frozen=True, slots=True)
class CloseRequest:
    processing: ProcessingRef


@dataclass(frozen=True, slots=True)
class PreparedClose:
    request: CloseRequest
    handle: EventHandle
    projection: EventProjection
    assessment: EvidenceAssessment
    comparison: ForwardComparison
    cohort_estimate: IrCohortEstimate
    learning_decision: LearningDecision
    outcome_reasons: OrderedReasons
    derived_records: tuple[EventRecord, ...]
    prepared_commit: PreparedModelCommit | None
    existing_receipt: ModelCommitReceipt | None
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY
    observed_load_sag_increase: ObservedLoadSagIncrease | None = None


@dataclass(frozen=True, slots=True)
class _AssessedProjection:
    observations: tuple[PhysicalObservation, ...]
    snapshot: FrozenModelSnapshot
    start_utc: datetime
    projection_available: bool
    assessment: EvidenceAssessment
    comparison: ForwardComparison


@dataclass(frozen=True, slots=True)
class _CohortSelectionInput:
    blackout_id: str
    snapshot: FrozenModelSnapshot
    current_steps: tuple[CohortStep, ...]
    projection_available: bool
    consumed_step_hashes: frozenset[str]
    durable: IrCohortSelection | None
    learning_policy: IrLearningPolicy


class AssessmentWorker:
    """Bounded read-only lane activated only after the first safety publication."""

    def __init__(self, store: AssessmentQueryEventStorePort, model: AssessmentModelPort) -> None:
        self._store = store
        self._model = model
        self._ready = False
        self._pending: deque[CloseRequest] = deque(maxlen=MAX_PENDING_ASSESSMENTS)

    @property
    def ready(self) -> bool:
        return self._ready

    def defer(self, request: CloseRequest) -> bool:
        if len(self._pending) == MAX_PENDING_ASSESSMENTS:
            return False
        self._pending.append(request)
        return True

    def after_first_safety_publication(self) -> None:
        self._ready = True

    def peek_pending(self) -> CloseRequest | None:
        """Return the next request without consuming it before preparation succeeds."""
        if not self._ready or not self._pending:
            return None
        return self._pending[0]

    def discard_pending(self, request: CloseRequest) -> None:
        """Consume exactly the request whose close or rejection was accepted."""
        if not self._pending or self._pending[0] is not request:
            raise RuntimeError("assessment queue changed during read-only preparation")
        self._pending.popleft()

    def prepare(self, request: CloseRequest) -> PreparedClose:
        """Project and calculate without appending records or mutating the model."""
        if not self._ready:
            raise RuntimeError("assessment_deferred_until_first_safety_publication")
        projection = self._store.project(
            EventRef(request.processing.blackout_id, request.processing.final_path_token)
        )
        handle = _processing_handle(request.processing, projection)
        terminal_policy = _terminal_policy(projection)
        durable = durable_close(projection)
        if durable is not None:
            current_policy = self._model.policy_projection().learning_policy
            if durable.learning_policy != current_policy:
                raise ProjectionInputError("durable learning policy does not match current policy")
            assessment = _apply_terminal_policy(durable.assessment, terminal_policy)
            outcome_reasons = order_reasons((*durable.reasons.values, *assessment.reasons.values))
            learning_decision = durable.decision
            prepared_commit = durable.prepared
            existing_receipt = durable.receipt
            if not terminal_policy.authorizes_science:
                learning_decision = replace(
                    learning_decision,
                    compare_forward_model=False,
                    record_decline_evidence=False,
                    commit_ir_k=False,
                )
                prepared_commit = None
                existing_receipt = None
            return PreparedClose(
                request=request,
                handle=handle,
                projection=projection,
                assessment=assessment,
                comparison=durable.comparison,
                cohort_estimate=durable.cohort,
                learning_decision=learning_decision,
                outcome_reasons=outcome_reasons,
                derived_records=(),
                prepared_commit=prepared_commit,
                existing_receipt=existing_receipt,
                learning_policy=durable.learning_policy,
                observed_load_sag_increase=durable.observed_load_sag_increase,
            )
        return self._prepare_new(request, handle, projection)

    def _prepare_new(
        self,
        request: CloseRequest,
        handle: EventHandle,
        projection: EventProjection,
    ) -> PreparedClose:
        policy = self._model.policy_projection()
        current_snapshot = policy.snapshot
        durable_stages = durable_derived_stages(projection)
        if (
            durable_stages.learning_policy is not None
            and durable_stages.learning_policy != policy.learning_policy
        ):
            raise ProjectionInputError("durable assessment learning policy mismatch")
        assessed = _assess_projection(projection, current_snapshot, durable_stages)
        current_steps, current_projection_available = _current_cohort_steps(
            request.processing.blackout_id,
            projection,
            assessed,
        )
        selection = self._select_cohort(
            _CohortSelectionInput(
                request.processing.blackout_id,
                assessed.snapshot,
                current_steps,
                current_projection_available,
                policy.consumed_step_hashes,
                durable_stages.cohort,
                policy.learning_policy,
            )
        )
        commit_time = _event_time(projection)
        learning_result = evaluate_ir_learning(
            selection.estimate,
            IrLearningContext(
                current_blackout_id=request.processing.blackout_id,
                # Cohort selection consumes only the first two positions from
                # the current event.  A third detected/ignored step is not
                # evidence and must not make an otherwise valid cohort fail.
                current_blackout_step_count=selected_current_event_step_count(
                    current_steps,
                    request.processing.blackout_id,
                    current_snapshot.battery_epoch_id,
                ),
                battery_epoch_id=current_snapshot.battery_epoch_id,
                current_k_v_per_pp=current_snapshot.ir_k_v_per_pp,
                epoch_initial_k_v_per_pp=policy.epoch_initial_k_v_per_pp,
                reference_load_percent=current_snapshot.ir_reference_load_percent,
                current_utc=commit_time,
                previous_commit_utc=policy.previous_commit_utc,
                candidate_step_hashes=selection.consumed_step_hashes,
                consumed_step_hashes=policy.consumed_step_hashes,
                learning_policy=policy.learning_policy,
            ),
        )
        decision = make_learning_decision(
            assessed.assessment,
            assessed.comparison,
            learning_result,
            decline_evidence_eligible=decline_evidence_eligible(
                assessed.assessment,
                assessed.observations,
                current_steps,
                _ready_at_start(projection),
            ),
        )
        prepared_commit = None
        learning_reasons = learning_result.reasons
        if decision.commit_ir_k and learning_result.change is not None:
            try:
                prepared_commit = self._model.prepare_commit(
                    learning_result.change,
                    blackout_id=request.processing.blackout_id,
                    committed_at=commit_time,
                )
                if prepared_commit.learning_policy != policy.learning_policy:
                    raise ModelPortRefused("learning_policy_mismatch")
            except ModelPortConflict:
                learning_reasons = order_reasons(
                    (*learning_reasons.values, LearningReason.MODEL_STATE_CONFLICT)
                )
                decision = replace(decision, commit_ir_k=False)
            except ModelPortRefused as exc:
                learning_reasons = order_reasons(
                    (*learning_reasons.values, _model_refusal_reason(exc))
                )
                decision = replace(decision, commit_ir_k=False)
        reasons = _combined_reasons(
            assessed.assessment,
            assessed.comparison,
            selection.estimate,
            learning_reasons,
        )
        records = new_derived_records(
            projection,
            _DecisionBasis(
                assessed.assessment,
                assessed.comparison,
                current_steps,
                selection.estimate,
                selection.consumed_step_hashes,
                decision,
                reasons,
                learning_reasons,
                learning_result.observed_load_sag_increase,
                prepared_commit,
                policy.learning_policy,
            ),
        )
        return PreparedClose(
            request=request,
            handle=handle,
            projection=projection,
            assessment=assessed.assessment,
            comparison=assessed.comparison,
            cohort_estimate=selection.estimate,
            learning_decision=decision,
            outcome_reasons=reasons,
            derived_records=records,
            prepared_commit=prepared_commit,
            existing_receipt=None,
            learning_policy=policy.learning_policy,
            observed_load_sag_increase=learning_result.observed_load_sag_increase,
        )

    def _select_cohort(
        self,
        selection_input: _CohortSelectionInput,
    ) -> IrCohortSelection:
        if selection_input.durable is not None:
            return selection_input.durable
        historical, overflow, history_available = self._historical_candidates(
            selection_input.blackout_id,
            selection_input.snapshot.battery_epoch_id,
        )
        return select_ir_cohort(
            (*selection_input.current_steps, *historical),
            IrCohortContext(
                current_blackout_id=selection_input.blackout_id,
                battery_epoch_id=selection_input.snapshot.battery_epoch_id,
                evaluation_revision=selection_input.snapshot.evaluation_revision,
                consumed_step_hashes=selection_input.consumed_step_hashes,
                projection_available=selection_input.projection_available and history_available,
                candidate_event_overflow=overflow,
                learning_policy=selection_input.learning_policy,
            ),
        )

    def _historical_candidates(
        self,
        current_blackout_id: str,
        battery_epoch_id: str,
    ) -> tuple[tuple[CohortStep, ...], int, bool]:
        overflow = 0
        available = False
        candidates: list[CohortStep] = []
        try:
            tail = self._store.index_tail_for_epoch(
                battery_epoch_id,
                HISTORICAL_COHORT_LIMIT,
            )
            overflow = tail.overflow_count
            eligible_summaries = tuple(
                summary for summary in tail.summaries if _historical_summary_is_eligible(summary)
            )
            available = tail.scan_complete and not any(
                summary.battery_epoch_id != battery_epoch_id
                or summary.blackout_id == current_blackout_id
                for summary in eligible_summaries
            )
            if available:
                for summary in eligible_summaries:
                    projection = self._store.project(
                        EventRef(summary.blackout_id, summary.segment_filename)
                    )
                    snapshot = project_snapshot(projection)
                    if (
                        snapshot.battery_epoch_id != battery_epoch_id
                        or snapshot.battery_epoch_id != summary.battery_epoch_id
                        or not _terminal_policy(projection).authorizes_science
                    ):
                        available = False
                        break
                    candidates.extend(
                        _cohort_steps(
                            summary.blackout_id,
                            projection,
                            snapshot,
                            _event_started_utc(projection),
                        )
                    )
        except Exception:
            available = False
        return (tuple(candidates) if available else ()), overflow, available


def _assess_projection(
    projection: EventProjection,
    current_snapshot: FrozenModelSnapshot,
    durable: _DurableDerivedStages,
) -> _AssessedProjection:
    projection_available = True
    try:
        observations = project_observations(projection)
        snapshot = project_snapshot(projection)
        started_utc = _event_started_utc(projection)
    except ProjectionInputError:
        observations = ()
        snapshot = current_snapshot
        started_utc = _event_time(projection)
        projection_available = False

    start_payload = projection.start.payload if projection.start is not None else {}
    terminal_policy = _terminal_policy(projection)
    capture_damaged = bool(
        projection.damaged_segment_hashes
        or projection.damaged_segment_overflow
        or projection.gaps
        or _capture_damaged_termination(projection)
        or not projection_available
    )
    if durable.assessment is None:
        assessment = assess_evidence(
            observations,
            EvidenceContext(
                blackout_kind=_blackout_kind(observations),
                frozen_snapshot_supported=(
                    projection_available
                    and snapshot.schema_revision == current_snapshot.schema_revision
                    and snapshot.evaluation_revision == current_snapshot.evaluation_revision
                ),
                current_battery_epoch_id=current_snapshot.battery_epoch_id,
                frozen_battery_epoch_id=snapshot.battery_epoch_id,
                capture_damaged=capture_damaged,
                snapshot_within_budget=(
                    projection_available
                    and start_payload.get("snapshot_budget_exceeded") is not True
                ),
                terminal_policy=terminal_policy,
            ),
        )
        gap_reasons = tuple(
            EvidenceReason.REBOOT_GAP
            if record.payload.get("reason") in {"boot_changed", "reboot_gap"}
            else EvidenceReason.RAW_GAP_TOO_LARGE
            for record in projection.gaps
        )
        if gap_reasons:
            assessment = replace(
                assessment,
                reasons=order_reasons((*assessment.reasons.values, *gap_reasons)),
            )
    else:
        assessment = durable.assessment
        assessment = _apply_terminal_policy(assessment, terminal_policy)

    comparison = durable.comparison
    if comparison is None:
        comparison = compare_forward_model(observations, snapshot, assessment)
    return _AssessedProjection(
        observations,
        snapshot,
        started_utc,
        projection_available,
        assessment,
        comparison,
    )


def _current_cohort_steps(
    blackout_id: str,
    projection: EventProjection,
    assessed: _AssessedProjection,
) -> tuple[tuple[CohortStep, ...], bool]:
    if not assessed.projection_available:
        return (), False
    try:
        return (
            _cohort_steps(
                blackout_id,
                projection,
                assessed.snapshot,
                assessed.start_utc,
            ),
            True,
        )
    except ProjectionInputError:
        return (), False


def _cohort_steps(
    blackout_id: str,
    projection: EventProjection,
    snapshot: FrozenModelSnapshot,
    started_utc: datetime,
) -> tuple[CohortStep, ...]:
    observations = project_observations(projection)
    raw = tuple(
        IrRawObservation(
            sequence=index,
            boot_id=item.boot_id,
            monotonic_ns=item.monotonic_ns,
            raw_status=item.raw_status,
            battery_voltage_v=required_float(item.battery_voltage_v, "battery_voltage_v"),
            voltage_token_quantum_v=required_float(
                item.voltage_token_quantum_v,
                "voltage_token_quantum_v",
            ),
            load_percent=required_float(item.load_percent, "load_percent"),
        )
        for index, item in enumerate(observations)
    )
    segment_id = projection.start.segment_id if projection.start is not None else ""
    estimates = identify_load_steps(blackout_id, segment_id, raw)
    return tuple(
        CohortStep(
            estimate=estimate,
            battery_epoch_id=snapshot.battery_epoch_id,
            evaluation_revision=snapshot.evaluation_revision,
            event_started_utc=started_utc,
            step_record_hash=step_hash(estimate),
            learning_policy=snapshot.learning_policy,
        )
        for estimate in estimates
    )


def _processing_handle(processing: ProcessingRef, projection: EventProjection) -> EventHandle:
    if projection.outcome is not None:
        outcome = projection.outcome
        if outcome.prev_record_sha256 is None:
            raise ProjectionInputError("outcome is missing predecessor hash")
        return EventHandle(
            processing.blackout_id,
            outcome.segment_id,
            processing.final_path_token,
            outcome.seq,
            outcome.prev_record_sha256,
        )
    if not projection.records:
        raise ProjectionInputError("event projection is empty")
    last = projection.records[-1]
    return EventHandle(
        processing.blackout_id,
        last.segment_id,
        processing.final_path_token,
        last.seq + 1,
        last.record_sha256,
    )


def _combined_reasons(
    assessment: EvidenceAssessment,
    comparison: ForwardComparison,
    cohort: IrCohortEstimate,
    learning: OrderedReasons,
) -> OrderedReasons:
    return order_reasons(
        (
            *assessment.reasons.values,
            *comparison.reasons.values,
            *cohort.reasons.values,
            *learning.values,
        )
    )


def _event_started_utc(projection: EventProjection) -> datetime:
    if projection.start is None:
        raise ProjectionInputError("missing_start")
    return parse_utc(projection.start.wall_time_utc)


def _event_time(projection: EventProjection) -> datetime:
    final = _event_anchor(projection)
    if final is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parse_utc(final.wall_time_utc)


def _event_anchor(projection: EventProjection):
    if projection.end is not None:
        return projection.end
    if projection.start is not None:
        return projection.start
    return projection.records[-1] if projection.records else None


def _capture_damaged_termination(projection: EventProjection) -> bool:
    return _terminal_policy(projection).termination is TerminationFact.CAPTURE_DAMAGED


def _terminal_policy(projection: EventProjection) -> TerminalSciencePolicy:
    if projection.end is None:
        return terminal_science_policy(None)
    payload = projection.end.payload
    raw = payload.get("termination") if isinstance(payload, Mapping) else None
    return terminal_science_policy(raw)


def _apply_terminal_policy(
    assessment: EvidenceAssessment,
    terminal_policy: TerminalSciencePolicy,
) -> EvidenceAssessment:
    if terminal_policy.authorizes_science:
        return assessment
    if terminal_policy.termination is TerminationFact.CAPTURE_DAMAGED:
        target = EvidenceClass.REJECTED
        reason = EvidenceReason.CAPTURE_DAMAGED
    else:
        target = EvidenceClass.OPERATIONAL_ONLY
        reason = EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED
    reasons = (
        assessment.reasons
        if reason in assessment.reasons.values
        else order_reasons((*assessment.reasons.values, reason))
    )
    evidence_class = (
        target
        if assessment.evidence_class == EvidenceClass.QUALIFYING
        else assessment.evidence_class
    )
    if evidence_class == assessment.evidence_class and reasons == assessment.reasons:
        return assessment
    return replace(assessment, evidence_class=evidence_class, reasons=reasons)


def _ready_at_start(projection: EventProjection) -> bool:
    if projection.start is None:
        return False
    payload = mapping(projection.start.payload, "start.payload")
    readiness = payload.get("charge_readiness")
    return isinstance(readiness, Mapping) and readiness.get("ready") is True


def _blackout_kind(observations: tuple[PhysicalObservation, ...]) -> BlackoutKind:
    if not observations:
        return BlackoutKind.UNKNOWN
    kinds = frozenset(classify_physical_observation(item) for item in observations)
    for kind in (
        BlackoutKind.BLACKOUT_TEST,
        BlackoutKind.BLACKOUT_REAL,
        BlackoutKind.ONLINE,
    ):
        if kind in kinds:
            return kind
    return BlackoutKind.UNKNOWN


def _model_refusal_reason(exc: ModelPortRefused) -> LearningReason:
    return model_commit_refusal_reason(str(exc))
