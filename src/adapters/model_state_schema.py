"""Target-model schema codec and validation."""

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from src.adapters import model_state_persistence as files
from src.domain.time import utc_second

DEFAULT_IR_K_V_PER_PP = 0.015
DEFAULT_PEUKERT_EXPONENT = 1.2
TARGET_STATE_KEYS = frozenset({"soh", "physics", "lut"})
FEEDBACK_STATE_KEY = "last_feedback"


IrCompensationState = TypedDict("IrCompensationState", {"k_volts_per_percent": float})
PhysicsState = TypedDict(
    "PhysicsState",
    {"peukert_exponent": float, "ir_compensation": IrCompensationState},
)
LutStatePoint = TypedDict("LutStatePoint", {"v": float, "soc": float})


FeedbackChangeState = TypedDict(
    "FeedbackChangeState",
    {
        # Persisted names intentionally match the human-readable JSON receipt.
        "from": float,
        "to": float,
        "delta": float,
        "evidence_at": NotRequired[str],
        "reason": NotRequired[str],
    },
)


FeedbackState = TypedDict(
    "FeedbackState",
    {
        "event_at": str,
        "evidence_at": str,
        "changes": dict[str, FeedbackChangeState],
        "reason": str,
    },
)

# Complete persisted model.json contract.  Scientific units are documented by
# the named nested schemas above and validated at the JSON boundary below.
TargetModelState = TypedDict(
    "TargetModelState",
    {
        "soh": float,
        "physics": PhysicsState,
        "lut": list[LutStatePoint],
        "last_feedback": NotRequired[FeedbackState],
    },
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


def validate_target_state(state: Mapping[str, Any], *, source: str = "model.json") -> None:
    """Validate the exact post-transform schema without mutating it."""
    _validate_target_object(state, source=source)
    _validate_target_structure(state, source=source)
    soh = _require_finite_in_range(state["soh"], f"{source}.soh", minimum=0.0, maximum=1.0)
    if soh <= 0.0:
        raise TargetModelStateError(f"{source}.soh must be > 0")
    _validate_target_physics(state["physics"], source=source)
    _validate_lut(state["lut"], source=source)
    if FEEDBACK_STATE_KEY in state:
        _validate_last_feedback(state[FEEDBACK_STATE_KEY], source=source)


def _validate_target_object(state: object, *, source: str) -> None:
    if not isinstance(state, Mapping):
        raise TargetModelStateError(f"{source} must contain a JSON object")
    actual_keys = frozenset(state)
    allowed_keys = TARGET_STATE_KEYS | {FEEDBACK_STATE_KEY}
    if actual_keys not in {TARGET_STATE_KEYS, allowed_keys}:
        missing = sorted(TARGET_STATE_KEYS - actual_keys)
        extra = sorted(actual_keys - allowed_keys)
        raise TargetModelStateError(
            f"{source} has invalid target keys (missing={missing}, extra={extra})"
        )


def _validate_last_feedback(value: object, *, source: str) -> None:
    required = {"event_at", "evidence_at", "changes", "reason"}
    if not isinstance(value, Mapping) or frozenset(value) != required:
        raise TargetModelStateError(f"{source}.last_feedback has invalid keys")
    _validate_feedback_timestamp(value["event_at"], f"{source}.last_feedback.event_at")
    _validate_feedback_timestamp(value["evidence_at"], f"{source}.last_feedback.evidence_at")
    if not isinstance(value["reason"], str) or not value["reason"]:
        raise TargetModelStateError(f"{source}.last_feedback.reason must be non-empty text")
    _validate_feedback_changes(value["changes"], source=source)


def _validate_feedback_timestamp(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TargetModelStateError(f"{name} must be a timestamp")
    try:
        canonical = utc_second(value)
    except (TypeError, ValueError) as exc:
        raise TargetModelStateError(f"{name} must be a timezone-aware timestamp") from exc
    if value != canonical:
        raise TargetModelStateError(f"{name} must use canonical UTC seconds")


def _validate_feedback_changes(value: object, *, source: str) -> None:
    if not isinstance(value, Mapping) or not value:
        raise TargetModelStateError(f"{source}.last_feedback.changes must be non-empty")
    for field, change in value.items():
        _validate_feedback_change(field, change, source=source)


def _validate_feedback_change(field: object, change: object, *, source: str) -> None:
    if not isinstance(field, str) or not isinstance(change, Mapping):
        raise TargetModelStateError(f"{source}.last_feedback.changes is malformed")
    allowed = {"from", "to", "delta", "evidence_at", "reason"}
    if not set(change) <= allowed or not {"from", "to", "delta"} <= set(change):
        raise TargetModelStateError(f"{source}.last_feedback.changes[{field}] is malformed")
    _require_finite(change["from"], f"{source}.last_feedback.changes[{field}].from")
    _require_finite(change["to"], f"{source}.last_feedback.changes[{field}].to")
    _require_finite(change["delta"], f"{source}.last_feedback.changes[{field}].delta")
    if "evidence_at" in change:
        _validate_feedback_timestamp(
            change["evidence_at"], f"{source}.last_feedback.changes[{field}].evidence_at"
        )
    if "reason" in change and (not isinstance(change["reason"], str) or not change["reason"]):
        raise TargetModelStateError(
            f"{source}.last_feedback.changes[{field}].reason must be non-empty text"
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
    if not isinstance(value, Mapping) or frozenset(value) != {"k_volts_per_percent"}:
        raise TargetModelStateError(f"{source}.physics.ir_compensation has invalid keys")
    _require_finite(value["k_volts_per_percent"], f"{source}.physics.ir_compensation.k")


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
    if not isinstance(value, dict) or set(value) != {"v", "soc"}:
        raise TargetModelStateError(f"{source} must contain exactly v and soc")
    _require_finite(value["v"], f"{source}.v")
    _require_finite_in_range(value["soc"], f"{source}.soc", minimum=0.0, maximum=1.0)


def _validate_lut_order(value: list[object], *, source: str) -> None:
    for previous, current in zip(value, value[1:], strict=False):
        assert isinstance(previous, dict) and isinstance(current, dict)
        if float(previous["v"]) <= float(current["v"]):
            raise TargetModelStateError(f"{source}.lut voltages must be strictly descending")
        if float(previous["soc"]) < float(current["soc"]):
            raise TargetModelStateError(f"{source}.lut SoC must be non-increasing")


def load_target_state(path: Path) -> tuple[TargetModelState, bytes]:
    """Read and validate one target model without changing it."""
    raw = files.read_model_file(path, error_type=TargetModelStateError)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetModelStateError(f"malformed target model {path}: {exc}") from exc
    validate_target_state(decoded, source=str(path))
    assert isinstance(decoded, dict), "validated JSON model must be a dict"
    return cast(TargetModelState, decoded), raw


def fresh_target_state() -> TargetModelState:
    """Create a predictor equivalent to the current Release-A defaults."""
    offset = DEFAULT_IR_K_V_PER_PP * 20.0
    lut = [
        LutStatePoint(v=voltage + offset, soc=soc)
        for voltage, soc in (
            (13.4, 1.00),
            (12.8, 0.85),
            (12.4, 0.64),
            (12.1, 0.40),
            (11.6, 0.18),
            (11.0, 0.06),
            (10.5, 0.00),
        )
    ]
    state: TargetModelState = {
        "soh": 1.0,
        "physics": {
            "peukert_exponent": DEFAULT_PEUKERT_EXPONENT,
            "ir_compensation": {
                "k_volts_per_percent": DEFAULT_IR_K_V_PER_PP,
            },
        },
        "lut": lut,
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
