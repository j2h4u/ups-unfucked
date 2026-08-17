"""Strict Release-A source codec and deterministic target-state builder."""

import copy
import hashlib
import json
import math
import uuid
from decimal import Decimal
from typing import Any

from src.adapters import model_state_schema as target
from src.domain.values import DEFAULT_IR_LEARNING_POLICY

SOURCE_REFERENCE_LOAD_PERCENT = 20.0
TARGET_REFERENCE_LOAD_PERCENT = 0.0
RELEASE_A_STATE_KEYS = frozenset(
    {
        "soh",
        "soh_history",
        "capacity_estimates",
        "capacity_ah_measured",
        "physics",
        "lut",
        "r_internal_history",
        "battery_install_date",
        "battery_epoch_id",
        "cycle_count",
        "cumulative_on_battery_sec",
        "new_battery_detected",
        "new_battery_detected_timestamp",
        "discharge_events",
        "last_upscmd_timestamp",
        "last_upscmd_type",
        "last_upscmd_status",
    }
)
RELEASE_A_SCIENTIFIC_FINGERPRINT_FIELDS = (
    "soh",
    "soh_history",
    "capacity_estimates",
    "capacity_ah_measured",
    "physics",
    "lut",
    "r_internal_history",
    "battery_install_date",
    "battery_epoch_id",
    "new_battery_detected",
    "new_battery_detected_timestamp",
)
RELEASE_A_PHYSICS_KEYS = frozenset({"peukert_exponent", "ir_compensation", "rls_state"})
RELEASE_A_IR_KEYS = frozenset({"k_volts_per_percent", "reference_load_percent"})
RELEASE_A_RLS_NAMES = ("ir_k", "peukert")
RELEASE_A_RLS_PARAMETER_KEYS = frozenset({"theta", "P", "sample_count", "forgetting_factor"})


class ModelTransformError(RuntimeError):
    """The offline transformation refused or failed before a valid cutover."""


def decode_release_a_state(raw: bytes, *, source: str) -> dict[str, Any]:
    """Decode and validate exactly the pinned Release-A JSON representation."""
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelTransformError(
            f"strict Release-A source validation failed for {source}: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ModelTransformError(
            f"strict Release-A source validation failed for {source}: expected JSON object"
        )
    state: dict[str, Any] = copy.deepcopy(decoded)
    try:
        _validate_release_a_state(state, source=source)
    except ModelTransformError as exc:
        raise ModelTransformError(
            f"strict Release-A source validation failed for {source}: {exc}"
        ) from exc
    return state


def release_a_scientific_fingerprint(state: dict[str, Any]) -> str:
    """Reproduce the registered Release-A scientific fingerprint."""
    _validate_release_a_state(state, source="Release-A fingerprint input")
    scientific = {key: copy.deepcopy(state[key]) for key in RELEASE_A_SCIENTIFIC_FINGERPRINT_FIELDS}
    canonical = json.dumps(
        scientific,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_release_a_state(state: dict[str, Any], *, source: str) -> None:
    _require_exact_keys(state, RELEASE_A_STATE_KEYS, source)
    _require_uuid(state["battery_epoch_id"], f"{source}.battery_epoch_id")
    _require_finite_number(state["soh"], f"{source}.soh", minimum=0.0, maximum=1.0)
    if state["soh"] <= 0.0:
        raise ModelTransformError(f"{source}.soh must be > 0")
    for key in ("soh_history", "capacity_estimates", "r_internal_history", "discharge_events"):
        _require_object_list(state[key], f"{source}.{key}")
    _validate_release_a_soh_history(state["soh_history"], source=source)
    _validate_release_a_capacity_estimates(state["capacity_estimates"], source=source)
    _validate_release_a_internal_history(state["r_internal_history"], source=source)
    for key in (
        "battery_install_date",
        "new_battery_detected_timestamp",
        "last_upscmd_timestamp",
        "last_upscmd_type",
        "last_upscmd_status",
    ):
        value = state[key]
        if value is not None and not isinstance(value, str):
            raise ModelTransformError(f"{source}.{key} must be a string or null")
    if state["capacity_ah_measured"] is not None:
        _require_positive_finite(state["capacity_ah_measured"], f"{source}.capacity_ah_measured")
    cycle_count = state["cycle_count"]
    if isinstance(cycle_count, bool) or not isinstance(cycle_count, int) or cycle_count < 0:
        raise ModelTransformError(f"{source}.cycle_count must be a nonnegative integer")
    _require_finite_number(
        state["cumulative_on_battery_sec"],
        f"{source}.cumulative_on_battery_sec",
        minimum=0.0,
    )
    if not isinstance(state["new_battery_detected"], bool):
        raise ModelTransformError(f"{source}.new_battery_detected must be a boolean")
    _validate_release_a_physics(state["physics"], source=source)
    _validate_release_a_lut(state["lut"], source=source)


def _validate_release_a_physics(value: object, *, source: str) -> None:
    physics = _require_object(value, f"{source}.physics")
    _require_exact_keys(physics, RELEASE_A_PHYSICS_KEYS, f"{source}.physics")
    _require_finite_number(
        physics["peukert_exponent"],
        f"{source}.physics.peukert_exponent",
        minimum=1.0,
        maximum=1.5,
    )
    ir = _require_object(physics["ir_compensation"], f"{source}.physics.ir_compensation")
    _require_exact_keys(ir, RELEASE_A_IR_KEYS, f"{source}.physics.ir_compensation")
    _require_finite_number(ir["k_volts_per_percent"], f"{source}.physics.ir_compensation.k")
    _require_finite_number(
        ir["reference_load_percent"],
        f"{source}.physics.ir_compensation.reference_load_percent",
        minimum=0.0,
    )
    if ir["reference_load_percent"] != SOURCE_REFERENCE_LOAD_PERCENT:
        raise ModelTransformError("Release-A reference load must be exactly 20")
    _validate_release_a_rls(physics["rls_state"], source=source)


def _validate_release_a_soh_history(value: object, *, source: str) -> None:
    entries = _require_object_list(value, f"{source}.soh_history")
    for index, entry in enumerate(entries):
        path = f"{source}.soh_history[{index}]"
        _require_exact_keys(entry, frozenset({"date", "soh", "capacity_ah_ref"}), path)
        if not isinstance(entry["date"], str):
            raise ModelTransformError(f"{path}.date must be a string")
        _require_finite_number(entry["soh"], f"{path}.soh", minimum=0.0, maximum=1.0)
        if entry["soh"] <= 0.0:
            raise ModelTransformError(f"{path}.soh must be > 0")
        _require_positive_finite(entry["capacity_ah_ref"], f"{path}.capacity_ah_ref")


def _validate_release_a_capacity_estimates(value: object, *, source: str) -> None:
    entries = _require_object_list(value, f"{source}.capacity_estimates")
    expected = frozenset({"timestamp", "ah_estimate", "confidence", "metadata"})
    for index, entry in enumerate(entries):
        path = f"{source}.capacity_estimates[{index}]"
        _require_exact_keys(entry, expected, path)
        if not isinstance(entry["timestamp"], str):
            raise ModelTransformError(f"{path}.timestamp must be a string")
        _require_positive_finite(entry["ah_estimate"], f"{path}.ah_estimate")
        _require_finite_number(entry["confidence"], f"{path}.confidence", minimum=0.0, maximum=1.0)
        if not isinstance(entry["metadata"], dict):
            raise ModelTransformError(f"{path}.metadata must be an object")


def _validate_release_a_internal_history(value: object, *, source: str) -> None:
    entries = _require_object_list(value, f"{source}.r_internal_history")
    expected = frozenset({"date", "r_ohm", "v_before", "v_sag", "load_percent", "event"})
    for index, entry in enumerate(entries):
        path = f"{source}.r_internal_history[{index}]"
        _require_exact_keys(entry, expected, path)
        if not isinstance(entry["date"], str) or not isinstance(entry["event"], str):
            raise ModelTransformError(f"{path}.date and event must be strings")
        for key in ("r_ohm", "v_before", "v_sag", "load_percent"):
            _require_finite_number(entry[key], f"{path}.{key}", minimum=0.0)


def _validate_release_a_rls(value: object, *, source: str) -> None:
    rls = _require_object(value, f"{source}.physics.rls_state")
    _require_exact_keys(rls, frozenset(RELEASE_A_RLS_NAMES), f"{source}.physics.rls_state")
    for name in RELEASE_A_RLS_NAMES:
        path = f"{source}.physics.rls_state.{name}"
        parameters = _require_object(rls[name], path)
        _require_exact_keys(parameters, RELEASE_A_RLS_PARAMETER_KEYS, path)
        _require_finite_number(parameters["theta"], f"{path}.theta")
        _require_finite_number(parameters["P"], f"{path}.P", minimum=0.0)
        sample_count = parameters["sample_count"]
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
            raise ModelTransformError(f"{path}.sample_count must be a nonnegative integer")
        _require_finite_number(
            parameters["forgetting_factor"], f"{path}.forgetting_factor", minimum=0.0
        )


def _validate_release_a_lut(value: object, *, source: str) -> None:
    if not isinstance(value, list) or len(value) < 2:
        raise ModelTransformError(f"{source}.lut must be a list with at least two entries")
    for index, entry in enumerate(value):
        path = f"{source}.lut[{index}]"
        if not isinstance(entry, dict):
            raise ModelTransformError(f"{path} must be an object")
        required = frozenset({"v", "soc", "source"})
        if not required.issubset(entry):
            raise ModelTransformError(f"{path} must contain v, soc, source")
        _require_finite_number(entry["v"], f"{path}.v")
        _require_finite_number(entry["soc"], f"{path}.soc", minimum=0.0, maximum=1.0)
        entry_source = entry["source"]
        if not isinstance(entry_source, str) or entry_source not in {
            "standard",
            "anchor",
            "measured",
        }:
            raise ModelTransformError(f"{path}.source is invalid")
        allowed = set(required)
        if entry_source == "measured":
            allowed.add("timestamp")
            if "timestamp" not in entry:
                raise ModelTransformError(f"{path}.timestamp is required for measured LUT entries")
            _require_finite_number(entry["timestamp"], f"{path}.timestamp")
        if set(entry) != allowed:
            raise ModelTransformError(f"{path} has invalid keys for source {entry_source}")


def _require_object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelTransformError(f"{path} must be an object")
    return value


def _require_object_list(value: object, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ModelTransformError(f"{path} must be a list")
    if any(not isinstance(entry, dict) for entry in value):
        raise ModelTransformError(f"{path} entries must be objects")
    return value


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ModelTransformError(f"{path} has invalid keys: missing={missing}, extra={extra}")


def _require_finite_number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ModelTransformError(f"{path} must be a finite number")
    if minimum is not None and value < minimum:
        raise ModelTransformError(f"{path} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ModelTransformError(f"{path} must be <= {maximum}")
    return float(value)


def _require_positive_finite(value: object, path: str) -> None:
    numeric = _require_finite_number(value, path, minimum=0.0)
    if numeric <= 0.0:
        raise ModelTransformError(f"{path} must be > 0")


def _require_uuid(value: object, path: str) -> None:
    if not isinstance(value, str) or not value:
        raise ModelTransformError(f"{path} must be a UUID string")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ModelTransformError(f"{path} must be a UUID string") from exc


def build_target_state(source_state: dict[str, Any]) -> dict[str, Any]:
    """Build the complete target state in memory without touching its source."""
    source = copy.deepcopy(source_state)
    physics = source["physics"]
    ir = physics["ir_compensation"]
    if ir["reference_load_percent"] != SOURCE_REFERENCE_LOAD_PERCENT:
        raise ModelTransformError("Release-A reference load must be exactly 20")
    current_k = float(ir["k_volts_per_percent"])
    offset = Decimal(str(current_k)) * Decimal("20")
    source["lut"] = [
        {**copy.deepcopy(entry), "v": float(Decimal(str(entry["v"])) + offset)}
        for entry in source["lut"]
    ]
    epoch_id = source["battery_epoch_id"]
    source["physics"] = {
        "peukert_exponent": physics["peukert_exponent"],
        "ir_compensation": {
            "k_volts_per_percent": current_k,
            "reference_load_percent": TARGET_REFERENCE_LOAD_PERCENT,
        },
    }
    source["ir_learning_policy"] = {
        "revision": DEFAULT_IR_LEARNING_POLICY.revision,
        "deadband_v_per_pp": DEFAULT_IR_LEARNING_POLICY.deadband_v_per_pp,
        "min_k_v_per_pp": DEFAULT_IR_LEARNING_POLICY.min_k_v_per_pp,
        "max_k_v_per_pp": DEFAULT_IR_LEARNING_POLICY.max_k_v_per_pp,
        "max_single_commit_fraction": DEFAULT_IR_LEARNING_POLICY.max_single_commit_fraction,
        "max_epoch_decrease_fraction": DEFAULT_IR_LEARNING_POLICY.max_epoch_decrease_fraction,
        "min_commit_interval_days": DEFAULT_IR_LEARNING_POLICY.min_commit_interval_days,
        "max_consumed_step_hashes": DEFAULT_IR_LEARNING_POLICY.max_consumed_step_hashes,
        "battery_epoch_id": epoch_id,
        "epoch_initial_k_v_per_pp": current_k,
        "last_commit_utc": None,
        "consumed_step_hashes": [],
    }
    try:
        target.validate_target_state(source, source="reference transform candidate")
    except target.TargetModelStateError as exc:
        raise ModelTransformError(str(exc)) from exc
    return source
