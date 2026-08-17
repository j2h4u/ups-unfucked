"""Read-only model projection and prepared-commit boundary for application use cases."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    FrozenModelSnapshot,
    IrLearningPolicy,
    ModelChange,
    ModelCommitReceipt,
)


class ModelPortError(RuntimeError):
    """Base application-visible failure of model ownership."""


class ModelPortConflict(ModelPortError):
    """The durable prepared intent does not match the current model bytes."""


class ModelPortRefused(ModelPortError):
    """The model owner refused an unsafe or non-canonical prepared change."""


@dataclass(frozen=True, slots=True)
class ModelPolicyProjection:
    """Immutable policy inputs needed to make one learning decision."""

    snapshot: FrozenModelSnapshot
    persisted_hash: str
    epoch_initial_k_v_per_pp: float
    previous_commit_utc: datetime | None
    consumed_step_hashes: frozenset[str]
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY


@dataclass(frozen=True, slots=True)
class PreparedModelCommit:
    """Exact, durable-before-write model transaction intent."""

    blackout_id: str
    change: ModelChange
    committed_at: datetime
    model_hash_before: str
    expected_model_hash_after: str
    expected_scientific_fingerprint_after: str
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY


class ModelSnapshotPort(Protocol):
    """Read-only safety snapshot projection."""

    def current_snapshot(self) -> FrozenModelSnapshot: ...


class ModelPolicyPort(Protocol):
    """Read-only learning-policy projection."""

    def policy_projection(self) -> ModelPolicyProjection: ...


class ModelPreparationPort(Protocol):
    """Prepare a durable model intent without executing its commit."""

    def prepare_commit(
        self,
        change: ModelChange,
        *,
        blackout_id: str,
        committed_at: datetime,
    ) -> PreparedModelCommit: ...


class AssessmentModelPort(ModelPolicyPort, ModelPreparationPort, Protocol):
    """Assessment capability: project policy and prepare, never commit."""


class ModelCommitPort(Protocol):
    """The sole application capability that may execute a prepared commit."""

    def commit_prepared(self, prepared: PreparedModelCommit) -> ModelCommitReceipt: ...
