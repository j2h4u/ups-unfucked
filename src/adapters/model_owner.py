"""Strict target-model persistence and the sole transactional scientific writer."""

import copy
import math
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Never

from src.adapters import model_state_persistence as files
from src.adapters import model_state_schema as schema
from src.application.model_port import (
    ModelPolicyProjection,
    ModelPortConflict,
    ModelPortRefused,
    PreparedModelCommit,
)
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.battery_math.lut import FrozenLut, LutPoint
from src.domain.learning import evidence_set_id
from src.domain.values import FrozenModelSnapshot, ModelChange, ModelCommitReceipt

SafetyOracle = Callable[
    [FrozenModelSnapshot, FrozenModelSnapshot],
    tuple[bool, str],
]
EmptyWorkPredicate = Callable[[], bool]


class ModelOwnerError(RuntimeError):
    """Base error for target-model ownership failures."""


class ModelStateConflict(ModelOwnerError, ModelPortConflict):
    """Persisted state changed outside the owner's expected transaction."""


class ModelPersistenceError(ModelOwnerError):
    """A published model candidate could not be verified or reconciled."""


class ModelCommitRefused(ModelOwnerError, ModelPortRefused):
    """A scientific or safety gate refused a model mutation."""


class WorkRegistryNotEmpty(ModelOwnerError):
    """A battery reset was requested while capture or processing work exists."""


class ModelOwner:
    """Own one strict model state and atomically publish immutable snapshots."""

    @classmethod
    def open_runtime(
        cls,
        model_path: str | Path,
        *,
        rated_capacity_ah: float = RATED_CAPACITY_AH,
        evaluation_revision: str = schema.DEFAULT_EVALUATION_REVISION,
        safety_oracle: SafetyOracle,
        create_if_missing: bool = False,
    ) -> "ModelOwner":
        """Acquire the runtime writer lock before loading and owning the model."""
        path = Path(model_path)
        lock_fd = files.acquire_writer_lock(path.parent / "monitor.lock")
        try:
            owner = cls(
                path,
                rated_capacity_ah=rated_capacity_ah,
                evaluation_revision=evaluation_revision,
                safety_oracle=safety_oracle,
                create_if_missing=create_if_missing,
            )
            owner.adopt_writer_lock(lock_fd)
            return owner
        except BaseException:
            files.release_writer_lock(lock_fd)
            raise

    def __init__(
        self,
        model_path: str | Path,
        *,
        rated_capacity_ah: float = RATED_CAPACITY_AH,
        evaluation_revision: str = schema.DEFAULT_EVALUATION_REVISION,
        safety_oracle: SafetyOracle,
        create_if_missing: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.precommit_path = self.model_path.with_name("model.precommit.json")
        self.rated_capacity_ah = float(rated_capacity_ah)
        self.evaluation_revision = evaluation_revision
        self._safety_oracle = safety_oracle
        self._writer_lock_fd: int | None = None
        self._lock = threading.RLock()
        if self.model_path.is_symlink():
            raise schema.TargetModelStateError(f"target model path is a symlink: {self.model_path}")
        if self.model_path.exists():
            state, raw = schema.load_target_state(self.model_path)
        elif create_if_missing:
            state = schema.fresh_target_state(rated_capacity_ah=self.rated_capacity_ah)
            raw = schema.canonical_json(state).encode("utf-8")
            files.atomic_write_model(self.model_path, raw.decode("utf-8"))
        else:
            raise schema.TargetModelStateError(f"target model does not exist: {self.model_path}")
        self._state = state
        self._raw_bytes = raw
        self._persisted_hash = files.persisted_hash(raw)
        self._snapshot = self._snapshot_from_state(state)

    @property
    def writer_lock_fd(self) -> int | None:
        """Descriptor for the lock owned by this model, if composition supplied one."""
        return self._writer_lock_fd

    def adopt_writer_lock(self, writer_lock_fd: int) -> None:
        """Transfer an already-held composition lock into model ownership."""
        if self._writer_lock_fd is not None:
            raise RuntimeError("model owner already owns a writer lock")
        self._writer_lock_fd = writer_lock_fd

    def close(self) -> None:
        """Release the composition-owned writer lock exactly once."""
        if self._writer_lock_fd is None:
            return
        fd = self._writer_lock_fd
        self._writer_lock_fd = None
        files.release_writer_lock(fd)

    def current_snapshot(self) -> FrozenModelSnapshot:
        """Return the atomically published immutable safety snapshot."""
        return self._snapshot

    def policy_projection(self) -> ModelPolicyProjection:
        """Return immutable inputs for a read-only learning decision."""
        with self._lock:
            policy = self._state["ir_learning_policy"]
            learning_policy = schema._learning_policy_from_state(policy)
            return ModelPolicyProjection(
                snapshot=self._snapshot,
                persisted_hash=self._persisted_hash,
                epoch_initial_k_v_per_pp=float(policy["epoch_initial_k_v_per_pp"]),
                previous_commit_utc=schema._parse_commit_time(
                    policy["last_commit_utc"],
                    allow_none=True,
                ),
                consumed_step_hashes=frozenset(policy["consumed_step_hashes"]),
                learning_policy=learning_policy,
            )

    def prepare_commit(
        self,
        change: ModelChange,
        *,
        blackout_id: str,
        committed_at: datetime,
    ) -> PreparedModelCommit:
        """Validate and freeze exact commit intent without changing persisted bytes."""
        with self._lock:
            disk_raw = files.read_model_file(
                self.model_path, error_type=schema.TargetModelStateError
            )
            disk_hash = files.persisted_hash(disk_raw)
            if disk_hash != self._persisted_hash:
                raise ModelStateConflict("model_state_conflict")
            evidence = _canonical_evidence_hashes(change.evidence_hashes)
            consumed = frozenset(self._state["ir_learning_policy"]["consumed_step_hashes"])
            if consumed.intersection(evidence):
                raise ModelCommitRefused("overlapping_evidence_already_consumed")
            commit_time = _require_commit_utc(committed_at)
            candidate, after_snapshot, _ = self._prepare_candidate(
                change,
                evidence=evidence,
                committed_at=commit_time,
            )
            expected_after = files.persisted_hash(schema.canonical_json(candidate).encode("utf-8"))
            return PreparedModelCommit(
                blackout_id=blackout_id,
                change=change,
                committed_at=commit_time,
                model_hash_before=disk_hash,
                expected_model_hash_after=expected_after,
                expected_scientific_fingerprint_after=after_snapshot.scientific_fingerprint,
                learning_policy=schema._learning_policy_from_state(
                    self._state["ir_learning_policy"]
                ),
            )

    def commit_prepared(self, prepared: PreparedModelCommit) -> ModelCommitReceipt:
        """Apply or reconstruct one previously durable exact commit intent."""
        with self._lock:
            current_policy = schema._learning_policy_from_state(self._state["ir_learning_policy"])
            if prepared.learning_policy != current_policy:
                raise ModelCommitRefused("learning_policy_mismatch")
            disk_raw = files.read_model_file(
                self.model_path,
                error_type=schema.TargetModelStateError,
            )
            disk_hash = files.persisted_hash(disk_raw)
            if disk_hash != self._persisted_hash:
                self._adopt_prepared_publication(prepared, disk_raw, disk_hash)
            if disk_hash == prepared.model_hash_before:
                verified = self.prepare_commit(
                    prepared.change,
                    blackout_id=prepared.blackout_id,
                    committed_at=prepared.committed_at,
                )
                if verified != prepared:
                    raise ModelStateConflict("model_state_conflict")
            elif disk_hash == prepared.expected_model_hash_after:
                if (
                    self._snapshot.scientific_fingerprint
                    != prepared.expected_scientific_fingerprint_after
                ):
                    raise ModelStateConflict("model_state_conflict")
            else:
                raise ModelStateConflict("model_state_conflict")
            receipt = self.commit(
                prepared.change,
                blackout_id=prepared.blackout_id,
                expected_model_hash=prepared.model_hash_before,
                committed_at=prepared.committed_at,
            )
            if (
                receipt.model_hash_after != prepared.expected_model_hash_after
                or receipt.scientific_fingerprint_after
                != prepared.expected_scientific_fingerprint_after
            ):
                raise ModelStateConflict("model_state_conflict")
            return receipt

    def _adopt_prepared_publication(
        self,
        prepared: PreparedModelCommit,
        disk_raw: bytes,
        disk_hash: str,
    ) -> None:
        """Reconcile an exact candidate left durable after rollback failure."""
        if not _adoption_hashes_match(self._persisted_hash, disk_hash, prepared):
            raise ModelStateConflict("model_state_conflict")
        state, verified_raw = schema.load_target_state(self.model_path)
        if verified_raw != disk_raw:
            raise ModelStateConflict("model_state_conflict")
        snapshot = self._snapshot_from_state(state)
        if not _adoption_science_matches(snapshot, state, prepared):
            raise ModelStateConflict("model_state_conflict")
        if not _adoption_evidence_is_consumed(state, prepared):
            raise ModelStateConflict("model_state_conflict")
        self._state = state
        self._raw_bytes = verified_raw
        self._persisted_hash = disk_hash
        self._snapshot = snapshot

    def commit(
        self,
        change: ModelChange,
        *,
        blackout_id: str,
        expected_model_hash: str | None = None,
        committed_at: datetime | None = None,
    ) -> ModelCommitReceipt:
        """Commit one bounded IR decrease or reconstruct its idempotent receipt."""
        with self._lock:
            disk_raw = files.read_model_file(
                self.model_path, error_type=schema.TargetModelStateError
            )
            disk_hash = files.persisted_hash(disk_raw)
            if disk_hash != self._persisted_hash:
                raise ModelStateConflict("model_state_conflict")
            evidence = _canonical_evidence_hashes(change.evidence_hashes)
            consumed = tuple(self._state["ir_learning_policy"]["consumed_step_hashes"])
            overlap = frozenset(evidence).intersection(consumed)
            if overlap:
                return self._reconstruct_idempotent_receipt(
                    change,
                    blackout_id=blackout_id,
                    evidence=evidence,
                    expected_model_hash=expected_model_hash,
                )
            if expected_model_hash is not None and expected_model_hash != disk_hash:
                raise ModelStateConflict("model_state_conflict")
            commit_time = _require_commit_utc(committed_at or datetime.now(timezone.utc))
            candidate, after_snapshot, oracle_description = self._prepare_candidate(
                change,
                evidence=evidence,
                committed_at=commit_time,
            )
            before_snapshot = self._snapshot

            # The exact before bytes become durable before the live model changes.
            files.atomic_write_model(
                self.precommit_path,
                self._raw_bytes.decode("utf-8"),
                mode=0o600,
            )
            if (
                files.read_model_file(self.precommit_path, error_type=schema.TargetModelStateError)
                != self._raw_bytes
            ):
                raise ModelStateConflict("precommit_backup_verification_failed")

            serialized = schema.canonical_json(candidate)
            try:
                after_hash = files.atomic_write_model(self.model_path, serialized)
                written = files.read_model_file(
                    self.model_path, error_type=schema.TargetModelStateError
                )
                if files.persisted_hash(written) != after_hash:
                    raise ModelPersistenceError("post_commit_model_hash_mismatch")
            except BaseException as failure:
                self._reconcile_failed_publication(failure)

            before_hash = self._persisted_hash
            self._state = candidate
            self._raw_bytes = written
            self._persisted_hash = after_hash
            self._snapshot = after_snapshot
            return ModelCommitReceipt(
                blackout_id=blackout_id,
                parameter=schema.IR_PARAMETER,
                value_before=change.value_before,
                measured_estimate=change.measured_estimate,
                value_after=change.value_after,
                model_hash_before=before_hash,
                model_hash_after=after_hash,
                scientific_fingerprint_before=before_snapshot.scientific_fingerprint,
                scientific_fingerprint_after=after_snapshot.scientific_fingerprint,
                evidence_set_id=evidence_set_id(evidence),
                consumed_step_hashes=evidence,
                reference_reparameterization=False,
                safety_oracle=oracle_description,
            )

    def _reconcile_failed_publication(self, failure: BaseException) -> Never:
        """Restore the verified precommit bytes before exposing persistence failure."""
        try:
            files.restore_exact_source(
                self.model_path,
                self._raw_bytes,
                backup=self.precommit_path,
                failure=failure,
            )
        except files.ModelStateFileError as reconciliation_error:
            raise ModelPersistenceError(str(reconciliation_error)) from reconciliation_error
        raise AssertionError("restore_exact_source must raise a persistence result")

    def _prepare_candidate(
        self,
        change: ModelChange,
        *,
        evidence: tuple[str, ...],
        committed_at: datetime,
    ) -> tuple[dict[str, Any], FrozenModelSnapshot, str]:
        self._validate_change(change, evidence=evidence, committed_at=committed_at)
        consumed = tuple(self._state["ir_learning_policy"]["consumed_step_hashes"])
        candidate = copy.deepcopy(self._state)
        candidate["physics"]["ir_compensation"]["k_volts_per_percent"] = change.value_after
        policy = candidate["ir_learning_policy"]
        policy["last_commit_utc"] = schema.format_utc(committed_at)
        policy["consumed_step_hashes"] = sorted((*consumed, *evidence))
        schema.validate_target_state(candidate, source="IR commit candidate")
        after_snapshot = self._snapshot_from_state(candidate)
        oracle_ok, oracle_description = self._safety_oracle(self._snapshot, after_snapshot)
        if not oracle_ok or not oracle_description.strip():
            raise ModelCommitRefused("safety_oracle_failed")
        return candidate, after_snapshot, oracle_description

    def reset_baseline(
        self,
        *,
        work_is_empty: EmptyWorkPredicate,
        install_date: str | None = None,
    ) -> FrozenModelSnapshot:
        """Reset a physically replaced battery only after an explicit empty-work check."""
        with self._lock:
            if not work_is_empty():
                raise WorkRegistryNotEmpty(
                    "battery reset requires empty capture and processing work"
                )
            disk_raw = files.read_model_file(
                self.model_path, error_type=schema.TargetModelStateError
            )
            if files.persisted_hash(disk_raw) != self._persisted_hash:
                raise ModelStateConflict("model_state_conflict")
            old = self._state
            last_upscmd = (
                old["last_upscmd_timestamp"],
                old["last_upscmd_type"],
                old["last_upscmd_status"],
            )
            candidate = schema.fresh_target_state(
                rated_capacity_ah=self.rated_capacity_ah,
                install_date=install_date,
                last_upscmd=last_upscmd,
            )
            files.atomic_write_model(
                self.precommit_path,
                self._raw_bytes.decode("utf-8"),
                mode=0o600,
            )
            if (
                files.read_model_file(
                    self.precommit_path,
                    error_type=schema.TargetModelStateError,
                )
                != self._raw_bytes
            ):
                raise ModelStateConflict("precommit_backup_verification_failed")
            serialized = schema.canonical_json(candidate)
            try:
                new_hash = files.atomic_write_model(self.model_path, serialized)
                written = files.read_model_file(
                    self.model_path, error_type=schema.TargetModelStateError
                )
                if files.persisted_hash(written) != new_hash:
                    raise ModelPersistenceError("post_reset_model_hash_mismatch")
            except BaseException as failure:
                self._reconcile_failed_publication(failure)
            self._state = candidate
            self._raw_bytes = written
            self._persisted_hash = new_hash
            self._snapshot = self._snapshot_from_state(candidate)
            return self._snapshot

    def _validate_change(
        self,
        change: ModelChange,
        *,
        evidence: tuple[str, ...],
        committed_at: datetime | None,
    ) -> None:
        learning_policy = schema._learning_policy_from_state(self._state["ir_learning_policy"])
        _validate_change_identity(change, evidence)
        current = self._snapshot.ir_k_v_per_pp
        _validate_change_values(change, current, learning_policy)
        policy = self._state["ir_learning_policy"]
        _validate_change_epoch(change, evidence, policy, learning_policy)
        _validate_commit_window(policy, learning_policy, committed_at)

    def _reconstruct_idempotent_receipt(
        self,
        change: ModelChange,
        *,
        blackout_id: str,
        evidence: tuple[str, ...],
        expected_model_hash: str | None,
    ) -> ModelCommitReceipt:
        consumed = frozenset(self._state["ir_learning_policy"]["consumed_step_hashes"])
        if not frozenset(evidence).issubset(consumed):
            raise ModelCommitRefused("overlapping_evidence_already_consumed")
        if not math.isclose(
            self._snapshot.ir_k_v_per_pp,
            change.value_after,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ModelCommitRefused("overlapping_evidence_already_consumed")
        before_state, before_raw = schema.load_target_state(self.precommit_path)
        before_snapshot = self._snapshot_from_state(before_state)
        before_hash = files.persisted_hash(before_raw)
        if expected_model_hash is not None and expected_model_hash != before_hash:
            raise ModelStateConflict("model_state_conflict")
        if not math.isclose(
            before_snapshot.ir_k_v_per_pp,
            change.value_before,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ModelStateConflict("model_state_conflict")
        oracle_ok, oracle_description = self._safety_oracle(before_snapshot, self._snapshot)
        if not oracle_ok or not oracle_description.strip():
            raise ModelCommitRefused("safety_oracle_failed")
        return ModelCommitReceipt(
            blackout_id=blackout_id,
            parameter=schema.IR_PARAMETER,
            value_before=change.value_before,
            measured_estimate=change.measured_estimate,
            value_after=change.value_after,
            model_hash_before=before_hash,
            model_hash_after=self._persisted_hash,
            scientific_fingerprint_before=before_snapshot.scientific_fingerprint,
            scientific_fingerprint_after=self._snapshot.scientific_fingerprint,
            evidence_set_id=evidence_set_id(evidence),
            consumed_step_hashes=evidence,
            reference_reparameterization=False,
            safety_oracle=oracle_description,
        )

    def _snapshot_from_state(self, state: Mapping[str, Any]) -> FrozenModelSnapshot:
        physics = state["physics"]
        ir = physics["ir_compensation"]
        lut: FrozenLut = tuple(
            LutPoint(
                voltage_v=float(entry["v"]),
                soc=float(entry["soc"]),
                source=str(entry["source"]),
            )
            for entry in state["lut"]
        )
        return FrozenModelSnapshot(
            schema_revision=schema.TARGET_SCHEMA_REVISION,
            evaluation_revision=self.evaluation_revision,
            battery_epoch_id=str(state["battery_epoch_id"]),
            scientific_fingerprint=schema.scientific_fingerprint(state),
            rated_capacity_ah=self.rated_capacity_ah,
            nominal_voltage_v=NOMINAL_VOLTAGE,
            nominal_power_watts=NOMINAL_POWER_WATTS,
            soh=float(state["soh"]),
            peukert_exponent=float(physics["peukert_exponent"]),
            ir_k_v_per_pp=float(ir["k_volts_per_percent"]),
            ir_reference_load_percent=float(ir["reference_load_percent"]),
            lut=lut,
            learning_policy=schema._learning_policy_from_state(state["ir_learning_policy"]),
        )


def _canonical_evidence_hashes(hashes: tuple[str, ...]) -> tuple[str, ...]:
    if any(
        not isinstance(value, str) or schema.SHA256_RE.fullmatch(value) is None for value in hashes
    ):
        raise ModelCommitRefused("invalid_evidence_hash")
    canonical = tuple(sorted(hashes))
    if len(canonical) != len(set(canonical)):
        raise ModelCommitRefused("duplicate_evidence_hash")
    return canonical


def _validate_change_identity(change: ModelChange, evidence: tuple[str, ...]) -> None:
    if change.parameter != schema.IR_PARAMETER:
        raise ModelCommitRefused("unsupported_model_parameter")
    if not evidence:
        raise ModelCommitRefused("missing_commit_evidence")


def _validate_change_values(
    change: ModelChange,
    current: float,
    learning_policy: Any,
) -> None:
    for value, name in (
        (change.value_before, "value_before"),
        (change.measured_estimate, "measured_estimate"),
        (change.value_after, "value_after"),
    ):
        _require_commit_positive_finite(value, name)
    if not math.isclose(change.value_before, current, rel_tol=0.0, abs_tol=1e-12):
        raise ModelStateConflict("model_state_conflict")
    expected_after = max(
        learning_policy.min_k_v_per_pp,
        change.measured_estimate,
        (1.0 - learning_policy.max_single_commit_fraction) * current,
    )
    if not math.isclose(change.value_after, expected_after, rel_tol=0.0, abs_tol=1e-12):
        raise ModelCommitRefused("ir_commit_not_canonical")
    if change.value_after > current - learning_policy.deadband_v_per_pp:
        raise ModelCommitRefused("ir_change_below_noise_floor")
    expected_bound = not math.isclose(
        change.value_after,
        change.measured_estimate,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    if change.bound_applied is not expected_bound:
        raise ModelCommitRefused("ir_bound_flag_mismatch")


def _validate_change_epoch(
    change: ModelChange,
    evidence: tuple[str, ...],
    policy: Mapping[str, Any],
    learning_policy: Any,
) -> None:
    epoch_floor = float(policy["epoch_initial_k_v_per_pp"]) * (
        1.0 - learning_policy.max_epoch_decrease_fraction
    )
    if change.value_after < epoch_floor:
        raise ModelCommitRefused("epoch_cumulative_decrease_exceeded")
    if (
        len(policy["consumed_step_hashes"]) + len(evidence)
        > learning_policy.max_consumed_step_hashes
    ):
        raise ModelCommitRefused("consumed_evidence_budget_exhausted")


def _adoption_hashes_match(
    persisted_hash: str,
    disk_hash: str,
    prepared: PreparedModelCommit,
) -> bool:
    return (
        persisted_hash == prepared.model_hash_before
        and disk_hash == prepared.expected_model_hash_after
    )


def _adoption_science_matches(
    snapshot: FrozenModelSnapshot,
    state: Mapping[str, Any],
    prepared: PreparedModelCommit,
) -> bool:
    return (
        snapshot.scientific_fingerprint == prepared.expected_scientific_fingerprint_after
        and schema._learning_policy_from_state(state["ir_learning_policy"])
        == prepared.learning_policy
    )


def _adoption_evidence_is_consumed(
    state: Mapping[str, Any],
    prepared: PreparedModelCommit,
) -> bool:
    evidence = frozenset(prepared.change.evidence_hashes)
    consumed = frozenset(state["ir_learning_policy"]["consumed_step_hashes"])
    return bool(evidence) and evidence.issubset(consumed)


def _validate_commit_window(
    policy: Mapping[str, Any], learning_policy: Any, committed_at: datetime | None
) -> None:
    now = _require_commit_utc(committed_at or datetime.now(timezone.utc))
    previous = schema._parse_commit_time(policy["last_commit_utc"], allow_none=True)
    if previous is None:
        return
    if now < previous:
        raise ModelCommitRefused("commit_rate_window_indeterminate")
    if now - previous < learning_policy.min_commit_interval:
        raise ModelCommitRefused("commit_rate_limited")


def _require_commit_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ModelCommitRefused("commit time must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _require_commit_positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ModelCommitRefused(f"{name} must be a finite number")
    if value <= 0.0:
        raise ModelCommitRefused(f"{name} must be positive")
    return float(value)
