"""Target-model schema codec and validation."""

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.adapters import model_state_persistence as files

DEFAULT_IR_K_V_PER_PP = 0.015
DEFAULT_PEUKERT_EXPONENT = 1.2
TARGET_STATE_KEYS = frozenset({"soh", "physics", "lut"})


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


def fresh_target_state() -> dict[str, Any]:
    """Create a predictor equivalent to the current Release-A defaults."""
    offset = DEFAULT_IR_K_V_PER_PP * 20.0
    lut = [
        {"v": voltage + offset, "soc": soc}
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
    state: dict[str, Any] = {
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
