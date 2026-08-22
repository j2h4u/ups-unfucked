"""Target-model schema codec, validation, and scientific identity helpers.

This module owns the JSON representation of the current model.  It performs
no persistence or mutation of a live model owner; callers receive ordinary
detached dictionaries that are validated before use.
"""

import copy
import json
import math
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.adapters import model_state_persistence as files
from src.battery_math.constants import RATED_CAPACITY_AH
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
)

DEFAULT_EVALUATION_REVISION = "domain-jsonl-v2"
DEFAULT_IR_K_V_PER_PP = 0.015
DEFAULT_PEUKERT_EXPONENT = 1.2
IR_LEARNING_POLICY = DEFAULT_IR_LEARNING_POLICY
LEGACY_STATE_KEYS = frozenset(
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
TARGET_STATE_KEYS = frozenset((*LEGACY_STATE_KEYS, "ir_learning_policy"))
SCIENTIFIC_FINGERPRINT_FIELDS = (
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
POLICY_KEYS = frozenset(
    {
        "revision",
        "deadband_v_per_pp",
        "min_k_v_per_pp",
        "max_k_v_per_pp",
        "max_single_commit_fraction",
        "max_epoch_decrease_fraction",
        "min_commit_interval_days",
        "max_consumed_step_hashes",
        "battery_epoch_id",
        "epoch_initial_k_v_per_pp",
        "last_commit_utc",
        "consumed_step_hashes",
    }
)


class TargetModelStateError(RuntimeError):
    """The target model is missing, malformed, or violates its strict schema."""


def canonical_json(state: Mapping[str, Any]) -> str:
    """Return the one persisted target-model JSON representation."""
    return json.dumps(
        state,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    )


def scientific_fingerprint(state: Mapping[str, Any]) -> str:
    """Fingerprint scientific fields while excluding policy bookkeeping."""
    scientific = {key: copy.deepcopy(state[key]) for key in SCIENTIFIC_FINGERPRINT_FIELDS}
    canonical = json.dumps(
        scientific,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return files.persisted_hash(canonical)


def validate_target_state(state: Mapping[str, Any], *, source: str = "model.json") -> None:
    """Validate the exact post-transform schema without mutating it."""
    _validate_target_object(state, source=source)
    _validate_target_structure(state, source=source)
    _validate_target_policy(state, source=source)
    soh = _require_finite_in_range(state["soh"], f"{source}.soh", minimum=0.0, maximum=1.0)
    if soh <= 0.0:
        raise TargetModelStateError(f"{source}.soh must be > 0")
    _validate_target_physics(state["physics"], source=source)
    _validate_lut(state["lut"], source=source)


def _validate_target_object(state: object, *, source: str) -> None:
    if not isinstance(state, Mapping):
        raise TargetModelStateError(f"{source} must contain a JSON object")
    actual_keys = frozenset(state)
    if actual_keys != TARGET_STATE_KEYS:
        missing = sorted(TARGET_STATE_KEYS - actual_keys)
        extra = sorted(actual_keys - TARGET_STATE_KEYS)
        raise TargetModelStateError(
            f"{source} has invalid target keys (missing={missing}, extra={extra})"
        )


def _validate_target_structure(state: Mapping[str, Any], *, source: str) -> None:
    physics = state["physics"]
    if not isinstance(physics, Mapping) or frozenset(physics) != {
        "peukert_exponent",
        "ir_compensation",
    }:
        raise TargetModelStateError(
            f"{source}.physics must contain exactly peukert_exponent and ir_compensation"
        )
    _validate_ir_compensation(physics["ir_compensation"], source=source)


def _validate_ir_compensation(value: object, *, source: str) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != {
        "k_volts_per_percent",
        "reference_load_percent",
    }:
        raise TargetModelStateError(f"{source}.physics.ir_compensation has invalid keys")
    _require_finite(value["k_volts_per_percent"], f"{source}.physics.ir_compensation.k")
    _require_finite(
        value["reference_load_percent"],
        f"{source}.physics.ir_compensation.reference_load_percent",
    )
    if float(value["reference_load_percent"]) != 0.0:
        raise TargetModelStateError("ir_reference_frame_not_transformed")


def _validate_target_policy(state: Mapping[str, Any], *, source: str) -> None:
    policy = state["ir_learning_policy"]
    if not isinstance(policy, Mapping) or frozenset(policy) != POLICY_KEYS:
        raise TargetModelStateError(f"{source}.ir_learning_policy has invalid keys")


def _validate_target_physics(physics: Mapping[str, Any], *, source: str) -> None:
    _require_finite_in_range(
        physics["peukert_exponent"],
        f"{source}.physics.peukert_exponent",
        minimum=1.0,
        maximum=1.5,
    )
    _require_finite(
        physics["ir_compensation"]["k_volts_per_percent"],
        f"{source}.physics.ir_compensation.k_volts_per_percent",
    )


def _validate_lut(value: object, *, source: str) -> None:
    if not isinstance(value, list) or len(value) < 2:
        raise TargetModelStateError(f"{source}.lut must be a list with at least two entries")
    for index, entry in enumerate(value):
        _validate_lut_entry(entry, source=f"{source}.lut[{index}]")
    _validate_lut_order(value, source=source)


def _validate_lut_entry(value: object, *, source: str) -> None:
    if not isinstance(value, dict):
        raise TargetModelStateError(f"{source} must be an object")
    _validate_lut_fields(value, source=source)
    entry_source = value["source"]
    allowed = _validate_lut_source(value, entry_source, source=source)
    if set(value) != allowed:
        raise TargetModelStateError(f"{source} has invalid keys for source {entry_source}")


def _validate_lut_fields(value: dict[str, object], *, source: str) -> None:
    if not {"v", "soc", "source"}.issubset(value):
        raise TargetModelStateError(f"{source} must contain v, soc, source")
    _require_finite(value["v"], f"{source}.v")
    _require_finite_in_range(value["soc"], f"{source}.soc", minimum=0.0, maximum=1.0)


def _validate_lut_source(
    value: dict[str, object],
    entry_source: object,
    *,
    source: str,
) -> set[str]:
    if not isinstance(entry_source, str) or entry_source not in {"standard", "anchor", "measured"}:
        raise TargetModelStateError(f"{source}.source is invalid")
    if entry_source == "measured":
        _validate_measured_lut_entry(value, source=source)
        return {"v", "soc", "source", "timestamp"}
    return {"v", "soc", "source"}


def _validate_measured_lut_entry(value: dict[str, object], *, source: str) -> None:
    if "timestamp" not in value:
        raise TargetModelStateError(f"{source}.timestamp is required for measured LUT entries")
    _require_finite(value["timestamp"], f"{source}.timestamp")


def _validate_lut_order(value: list[object], *, source: str) -> None:
    for previous, current in zip(value, value[1:], strict=False):
        assert isinstance(previous, dict) and isinstance(current, dict)
        if float(previous["v"]) <= float(current["v"]):
            raise TargetModelStateError(f"{source}.lut voltages must be strictly descending")
        if float(previous["soc"]) < float(current["soc"]):
            raise TargetModelStateError(f"{source}.lut SoC must be non-increasing")


def load_target_state(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read and validate one target model without changing it."""
    raw = files.read_model_file(path, error_type=TargetModelStateError)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetModelStateError(f"malformed target model {path}: {exc}") from exc
    validate_target_state(decoded, source=str(path))
    assert isinstance(decoded, dict), "validated JSON model must be a dict"
    return decoded, raw


def fresh_target_state(
    *,
    rated_capacity_ah: float = RATED_CAPACITY_AH,
    install_date: str | None = None,
    epoch_id: str | None = None,
    last_upscmd: tuple[str | None, str | None, str | None] = (None, None, None),
) -> dict[str, Any]:
    """Create a new reference-0 state equivalent to Release A defaults."""
    _require_positive_finite(rated_capacity_ah, "rated_capacity_ah")
    epoch = epoch_id or uuid.uuid4().hex
    _require_uuid(epoch, "battery_epoch_id")
    offset = DEFAULT_IR_K_V_PER_PP * 20.0
    lut = [
        {"v": voltage + offset, "soc": soc, "source": source}
        for voltage, soc, source in (
            (13.4, 1.00, "standard"),
            (12.8, 0.85, "standard"),
            (12.4, 0.64, "standard"),
            (12.1, 0.40, "standard"),
            (11.6, 0.18, "standard"),
            (11.0, 0.06, "standard"),
            (10.5, 0.00, "anchor"),
        )
    ]
    history_date = install_date or datetime.now(timezone.utc).date().isoformat()
    state: dict[str, Any] = {
        "soh": 1.0,
        "physics": {
            "peukert_exponent": DEFAULT_PEUKERT_EXPONENT,
            "ir_compensation": {
                "k_volts_per_percent": DEFAULT_IR_K_V_PER_PP,
                "reference_load_percent": 0.0,
            },
        },
        "lut": lut,
        "soh_history": [{"date": history_date, "soh": 1.0, "capacity_ah_ref": rated_capacity_ah}],
        "capacity_estimates": [],
        "capacity_ah_measured": None,
        "r_internal_history": [],
        "battery_install_date": install_date,
        "cycle_count": 0,
        "cumulative_on_battery_sec": 0.0,
        "battery_epoch_id": epoch,
        "new_battery_detected": False,
        "new_battery_detected_timestamp": None,
        "discharge_events": [],
        "last_upscmd_timestamp": last_upscmd[0],
        "last_upscmd_type": last_upscmd[1],
        "last_upscmd_status": last_upscmd[2],
        "ir_learning_policy": {
            "revision": IR_LEARNING_POLICY.revision,
            "deadband_v_per_pp": IR_LEARNING_POLICY.deadband_v_per_pp,
            "min_k_v_per_pp": IR_LEARNING_POLICY.min_k_v_per_pp,
            "max_k_v_per_pp": IR_LEARNING_POLICY.max_k_v_per_pp,
            "max_single_commit_fraction": IR_LEARNING_POLICY.max_single_commit_fraction,
            "max_epoch_decrease_fraction": IR_LEARNING_POLICY.max_epoch_decrease_fraction,
            "min_commit_interval_days": IR_LEARNING_POLICY.min_commit_interval_days,
            "max_consumed_step_hashes": IR_LEARNING_POLICY.max_consumed_step_hashes,
            "battery_epoch_id": epoch,
            "epoch_initial_k_v_per_pp": DEFAULT_IR_K_V_PER_PP,
            "last_commit_utc": None,
            "consumed_step_hashes": [],
        },
    }
    validate_target_state(state, source="fresh target state")
    return state


def _require_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetModelStateError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise TargetModelStateError(f"{name} must be a finite number")
    return float(value)


def _require_finite_in_range(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    number = _require_finite(value, name)
    if minimum is not None and number < minimum:
        raise TargetModelStateError(f"{name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise TargetModelStateError(f"{name} must be <= {maximum}")
    return number


def _require_positive_finite(value: object, name: str) -> float:
    number = _require_finite(value, name)
    if number <= 0.0:
        raise TargetModelStateError(f"{name} must be positive")
    return number


def _require_uuid(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TargetModelStateError(f"{name} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise TargetModelStateError(f"{name} must be a UUID string") from exc
    if value not in {str(parsed), parsed.hex}:
        raise TargetModelStateError(f"{name} must be canonical lowercase UUID text")
    return value
