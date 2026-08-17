"""Offline-only Release-A to reference-0 model transformation."""

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Never

from src.adapters import model_state_persistence as files
from src.adapters import model_state_release_a as release_a
from src.adapters import model_state_schema as target
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.battery_math.lut import FrozenLut, LutPoint, soc_from_voltage
from src.battery_math.peukert import PeukertParameters, runtime_minutes
from src.domain.safety_policy import decide_unlatched_safety_status
from src.domain.values import BlackoutKind

SOURCE_REFERENCE_LOAD_PERCENT = release_a.SOURCE_REFERENCE_LOAD_PERCENT
TARGET_REFERENCE_LOAD_PERCENT = release_a.TARGET_REFERENCE_LOAD_PERCENT
SOC_EQUIVALENCE_TOLERANCE = 0.005
RUNTIME_EQUIVALENCE_TOLERANCE_SECONDS = 1.0
RELEASE_A_HARD_FLOOR_MINUTES = 2.0
ModelTransformError = release_a.ModelTransformError


class WriterLockHeld(ModelTransformError):
    """Another process owns the daemon writer lock."""


@dataclass(frozen=True, slots=True)
class ReleaseASource:
    state: dict[str, Any]
    raw_bytes: bytes
    model_hash: str
    scientific_fingerprint: str


@dataclass(frozen=True, slots=True)
class EquivalenceReport:
    case_count: int
    shutdown_threshold_minutes: float
    max_soc_delta: float
    max_runtime_delta_seconds: float
    lb_transitions_identical: bool
    non_lb_status_identical: bool


@dataclass(frozen=True, slots=True)
class TransformReceipt:
    source_model_hash: str
    target_model_hash: str
    source_scientific_fingerprint: str
    target_scientific_fingerprint: str
    backup_path: Path
    equivalence: EquivalenceReport


@dataclass(frozen=True, slots=True)
class TransformSettings:
    shutdown_threshold_minutes: float
    rated_capacity_ah: float = RATED_CAPACITY_AH


@dataclass(frozen=True, slots=True)
class _DenseOracleGrid:
    """Frozen inputs shared by every point in the transform equivalence grid."""

    source_lut: FrozenLut
    target_lut: FrozenLut
    source_k: float
    target_k: float
    source_reference_load: float
    target_reference_load: float
    voltage_grid: frozenset[float]
    soh_grid: frozenset[float]
    peukert_grid: frozenset[float]
    rated_capacity_ah: float


@dataclass(frozen=True, slots=True)
class _DenseCaseReport:
    """Equivalence measurements for one raw-voltage/load grid point."""

    soc_delta: float
    max_runtime_delta_seconds: float
    case_count: int
    lb_identical: bool
    non_lb_identical: bool


def load_release_a_source(
    model_path: str | Path,
    *,
    registered_source_fingerprint: str,
) -> ReleaseASource:
    """Load only the exact pinned Release-A schema and registered state."""
    path = Path(model_path)
    if not _is_sha256(registered_source_fingerprint):
        raise ModelTransformError("registered source fingerprint must be lowercase SHA-256")
    if not path.exists():
        raise ModelTransformError(f"Release-A model does not exist: {path}")
    raw_before = files.read_model_file(path, error_type=ModelTransformError)
    state = release_a.decode_release_a_state(raw_before, source=str(path))
    raw_after = files.read_model_file(path, error_type=ModelTransformError)
    if raw_after != raw_before:
        raise ModelTransformError("Release-A source changed during validation")
    source_fingerprint = release_a.release_a_scientific_fingerprint(state)
    if source_fingerprint != registered_source_fingerprint:
        raise ModelTransformError("registered Release-A fingerprint mismatch")
    return ReleaseASource(
        state=state,
        raw_bytes=raw_before,
        model_hash=files.persisted_hash(raw_before),
        scientific_fingerprint=source_fingerprint,
    )


def dense_equivalence_oracle(
    source_state: dict[str, Any],
    target_state: dict[str, Any],
    *,
    shutdown_threshold_minutes: float,
    rated_capacity_ah: float = RATED_CAPACITY_AH,
) -> EquivalenceReport:
    """Prove the reference transform preserves SoC, runtime, status, and LB."""
    _validate_shutdown_threshold(shutdown_threshold_minutes)
    grid = _build_dense_oracle_grid(source_state, target_state, rated_capacity_ah)
    if grid.target_k != grid.source_k:
        raise ModelTransformError("reference transform changed ir_k")
    max_soc_delta = 0.0
    max_runtime_delta_seconds = 0.0
    lb_identical = True
    non_lb_identical = True
    cases = 0
    for load in range(101):
        for raw_voltage in grid.voltage_grid:
            case = _evaluate_dense_case(
                grid,
                raw_voltage,
                float(load),
                shutdown_threshold_minutes,
            )
            max_soc_delta = max(max_soc_delta, case.soc_delta)
            max_runtime_delta_seconds = max(
                max_runtime_delta_seconds,
                case.max_runtime_delta_seconds,
            )
            lb_identical = lb_identical and case.lb_identical
            non_lb_identical = non_lb_identical and case.non_lb_identical
            cases += case.case_count
    return EquivalenceReport(
        case_count=cases,
        shutdown_threshold_minutes=shutdown_threshold_minutes,
        max_soc_delta=max_soc_delta,
        max_runtime_delta_seconds=max_runtime_delta_seconds,
        lb_transitions_identical=lb_identical,
        non_lb_status_identical=non_lb_identical,
    )


def _build_dense_oracle_grid(
    source_state: dict[str, Any],
    target_state: dict[str, Any],
    rated_capacity_ah: float,
) -> _DenseOracleGrid:
    source_physics = source_state["physics"]
    target_physics = target_state["physics"]
    source_ir = source_physics["ir_compensation"]
    target_ir = target_physics["ir_compensation"]
    voltage_grid = {float(Decimal("8") + Decimal(index) * Decimal("0.05")) for index in range(141)}
    for entry in source_state["lut"]:
        voltage = float(entry["v"])
        voltage_grid.update(
            (voltage - 0.011, voltage - 0.01, voltage, voltage + 0.01, voltage + 0.011)
        )
    return _DenseOracleGrid(
        source_lut=_frozen_lut(source_state["lut"]),
        target_lut=_frozen_lut(target_state["lut"]),
        source_k=float(source_ir["k_volts_per_percent"]),
        target_k=float(target_ir["k_volts_per_percent"]),
        source_reference_load=float(source_ir["reference_load_percent"]),
        target_reference_load=float(target_ir["reference_load_percent"]),
        voltage_grid=frozenset(voltage_grid),
        soh_grid=frozenset({0.5, 0.8, 1.0, float(source_state["soh"])}),
        peukert_grid=frozenset({1.0, 1.2, 1.5, float(source_physics["peukert_exponent"])}),
        rated_capacity_ah=rated_capacity_ah,
    )


def _evaluate_dense_case(
    grid: _DenseOracleGrid,
    raw_voltage: float,
    load: float,
    shutdown_threshold_minutes: float,
) -> _DenseCaseReport:
    source_normalized = raw_voltage + grid.source_k * (load - grid.source_reference_load)
    target_normalized = raw_voltage + grid.target_k * (load - grid.target_reference_load)
    source_soc = soc_from_voltage(source_normalized, grid.source_lut)
    target_soc = soc_from_voltage(target_normalized, grid.target_lut)
    soc_delta = abs(target_soc - source_soc)
    if soc_delta > SOC_EQUIVALENCE_TOLERANCE:
        raise ModelTransformError(
            f"SoC equivalence failed at voltage={raw_voltage}, load={load}: {soc_delta}"
        )

    max_runtime_delta_seconds = 0.0
    lb_identical = True
    non_lb_identical = True
    cases = 0
    for soh in grid.soh_grid:
        for peukert in grid.peukert_grid:
            source_runtime, target_runtime = _runtime_pair(
                grid,
                (source_soc, target_soc),
                load,
                soh,
                peukert,
            )
            runtime_delta_seconds = abs(target_runtime - source_runtime) * 60.0
            max_runtime_delta_seconds = max(
                max_runtime_delta_seconds,
                runtime_delta_seconds,
            )
            if runtime_delta_seconds > RUNTIME_EQUIVALENCE_TOLERANCE_SECONDS:
                raise ModelTransformError(
                    "runtime equivalence failed at "
                    f"voltage={raw_voltage}, load={load}: {runtime_delta_seconds}s"
                )
            case_lb_identical, case_non_lb_identical = _status_equivalence(
                source_runtime,
                target_runtime,
                shutdown_threshold_minutes,
            )
            lb_identical = lb_identical and case_lb_identical
            non_lb_identical = non_lb_identical and case_non_lb_identical
            cases += 1
    return _DenseCaseReport(
        soc_delta=soc_delta,
        max_runtime_delta_seconds=max_runtime_delta_seconds,
        case_count=cases,
        lb_identical=lb_identical,
        non_lb_identical=non_lb_identical,
    )


def _runtime_pair(
    grid: _DenseOracleGrid,
    soc_pair: tuple[float, float],
    load: float,
    soh: float,
    peukert: float,
) -> tuple[float, float]:
    parameters = PeukertParameters(
        capacity_ah=grid.rated_capacity_ah,
        soh=soh,
        peukert_exponent=peukert,
        nominal_voltage=NOMINAL_VOLTAGE,
        nominal_power_watts=NOMINAL_POWER_WATTS,
    )
    return (
        runtime_minutes(soc_pair[0], load, parameters),
        runtime_minutes(soc_pair[1], load, parameters),
    )


def _status_equivalence(
    source_runtime: float,
    target_runtime: float,
    shutdown_threshold_minutes: float,
) -> tuple[bool, bool]:
    source_status = _release_a_real_blackout_status(source_runtime, shutdown_threshold_minutes)
    target_status = decide_unlatched_safety_status(
        BlackoutKind.BLACKOUT_REAL,
        target_runtime,
        shutdown_threshold_minutes,
    ).virtual_status
    source_lb = source_status.endswith(" LB")
    target_lb = target_status.endswith(" LB")
    if source_lb != target_lb:
        raise ModelTransformError("LB transition equivalence failed")
    if not source_lb and not target_lb and source_status != target_status:
        raise ModelTransformError("non-LB status equivalence failed")
    return source_lb == target_lb, source_status == target_status


def transform_model_file(
    model_path: str | Path,
    *,
    backup_path: str | Path,
    registered_source_fingerprint: str,
    registered_target_fingerprint: str,
    settings: TransformSettings,
) -> TransformReceipt:
    """Lock, back up, transform, verify, and atomically replace one model."""
    model = Path(model_path)
    backup = Path(backup_path)
    _validate_shutdown_threshold(settings.shutdown_threshold_minutes)
    if not _is_sha256(registered_target_fingerprint):
        raise ModelTransformError("registered target fingerprint must be lowercase SHA-256")
    if backup == model or backup.parent != model.parent:
        raise ModelTransformError("backup must be a distinct sibling of model.json")
    lock_fd = _acquire_writer_lock(model.parent / "monitor.lock")
    try:
        source = load_release_a_source(
            model,
            registered_source_fingerprint=registered_source_fingerprint,
        )
        target_state = release_a.build_target_state(source.state)
        target_fingerprint = target.scientific_fingerprint(target_state)
        if target_fingerprint != registered_target_fingerprint:
            raise ModelTransformError("registered target fingerprint mismatch")
        equivalence = dense_equivalence_oracle(
            source.state,
            target_state,
            shutdown_threshold_minutes=settings.shutdown_threshold_minutes,
            rated_capacity_ah=settings.rated_capacity_ah,
        )
        try:
            files.ensure_verified_backup(backup, source.raw_bytes)
        except files.ModelStateFileError as exc:
            raise ModelTransformError(str(exc)) from exc
        target_text = target.canonical_json(target_state)
        try:
            target_hash = files.atomic_write_model(model, target_text)
            actual_fingerprint = _verify_written_target(
                model,
                expected_hash=target_hash,
                registered_target_fingerprint=registered_target_fingerprint,
            )
        except BaseException as exc:
            _restore_exact_source(model, source.raw_bytes, backup=backup, failure=exc)
        return TransformReceipt(
            source_model_hash=source.model_hash,
            target_model_hash=target_hash,
            source_scientific_fingerprint=source.scientific_fingerprint,
            target_scientific_fingerprint=actual_fingerprint,
            backup_path=backup,
            equivalence=equivalence,
        )
    finally:
        _release_writer_lock(lock_fd)


def _frozen_lut(entries: list[dict[str, Any]]) -> FrozenLut:
    return tuple(
        LutPoint(float(entry["v"]), float(entry["soc"]), str(entry["source"])) for entry in entries
    )


def _release_a_real_blackout_status(runtime: float, threshold: float) -> str:
    """Evaluate the frozen historical source policy, not the current target policy."""
    if runtime < RELEASE_A_HARD_FLOOR_MINUTES or runtime < threshold:
        return "OB DISCHRG LB"
    return "OB DISCHRG"


def _validate_shutdown_threshold(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0.0:
        raise ModelTransformError("shutdown threshold minutes must be finite and positive")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _verify_written_target(
    path: Path,
    *,
    expected_hash: str,
    registered_target_fingerprint: str,
) -> str:
    reloaded_raw = files.read_model_file(path, error_type=ModelTransformError)
    try:
        reloaded = json.loads(reloaded_raw)
        target.validate_target_state(reloaded, source=str(path))
    except (UnicodeDecodeError, json.JSONDecodeError, target.TargetModelStateError) as exc:
        raise ModelTransformError(f"written target verification failed: {exc}") from exc
    actual_fingerprint = target.scientific_fingerprint(reloaded)
    if actual_fingerprint != registered_target_fingerprint:
        raise ModelTransformError("actual target fingerprint differs from registration")
    if files.persisted_hash(reloaded_raw) != expected_hash:
        raise ModelTransformError("actual target model hash differs from atomic-write receipt")
    return actual_fingerprint


def _restore_exact_source(
    path: Path,
    source_raw: bytes,
    *,
    backup: Path,
    failure: BaseException,
) -> Never:
    try:
        files.restore_exact_source(path, source_raw, backup=backup, failure=failure)
    except files.ModelStateFileError as exc:
        raise ModelTransformError(str(exc)) from failure


def _acquire_writer_lock(lock_path: Path) -> int:
    try:
        return files.acquire_writer_lock(lock_path)
    except files.ModelStateLockHeld as exc:
        raise WriterLockHeld(str(exc)) from exc
    except files.ModelStateFileError as exc:
        raise ModelTransformError(str(exc)) from exc


def _release_writer_lock(fd: int) -> None:
    files.release_writer_lock(fd)
