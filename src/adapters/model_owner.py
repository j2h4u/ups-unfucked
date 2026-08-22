"""Runtime owner for one validated model and its immutable published snapshot."""

import copy
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.adapters import model_state_persistence as files
from src.adapters import model_state_schema as schema
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.battery_math.lut import FrozenLut, LutPoint
from src.domain.time import utc_second
from src.domain.values import FrozenModelSnapshot


class ModelOwner:
    """Load and hold the model while owning the runtime writer lock."""

    @classmethod
    def open_runtime(
        cls,
        model_path: str | Path,
        *,
        rated_capacity_ah: float = RATED_CAPACITY_AH,
        create_if_missing: bool = False,
    ) -> "ModelOwner":
        path = Path(model_path)
        lock_fd = files.acquire_writer_lock(path.parent / "monitor.lock")
        try:
            owner = cls(
                path,
                rated_capacity_ah=rated_capacity_ah,
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
        create_if_missing: bool = False,
    ) -> None:
        self.model_path = Path(model_path)
        self.rated_capacity_ah = float(rated_capacity_ah)
        self._writer_lock_fd: int | None = None
        self._lock = threading.RLock()
        if self.model_path.is_symlink():
            raise schema.TargetModelStateError(f"target model path is a symlink: {self.model_path}")
        if self.model_path.exists():
            state, _ = schema.load_target_state(self.model_path)
        elif create_if_missing:
            state = schema.fresh_target_state()
            files.atomic_write_model(self.model_path, schema.canonical_json(state))
        else:
            raise schema.TargetModelStateError(f"target model does not exist: {self.model_path}")
        self._state = copy.deepcopy(state)
        self._snapshot = self._snapshot_from_state(state)

    def adopt_writer_lock(self, writer_lock_fd: int) -> None:
        if self._writer_lock_fd is not None:
            raise RuntimeError("model owner already owns a writer lock")
        self._writer_lock_fd = writer_lock_fd

    def close(self) -> None:
        if self._writer_lock_fd is None:
            return
        fd = self._writer_lock_fd
        self._writer_lock_fd = None
        files.release_writer_lock(fd)

    def current_snapshot(self) -> FrozenModelSnapshot:
        with self._lock:
            return self._snapshot

    def apply_feedback(
        self,
        *,
        ir_k: float | None = None,
        soh: float | None = None,
        event_receipt: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, tuple[float, float]]:
        """Atomically apply feedback and, when supplied, its one event receipt.

        ``event_receipt`` contains ``event_at`` and may contain
        ``evidence_at``, ``reason`` and optional per-field ``field_metadata``.
        The resulting receipt is persisted inside the same atomic model write.
        """
        with self._lock:
            self._require_writer_lock()
            if event_receipt is not None and receipt is not None:
                raise ValueError("provide only one of event_receipt and receipt")
            receipt_data = _receipt_data(event_receipt if event_receipt is not None else receipt)
            event_at = receipt_data.get("event_at") if receipt_data is not None else None
            if event_at is not None and _same_event(self._state, event_at):
                return {}
            candidate = copy.deepcopy(self._state)
            physics = candidate["physics"]
            assert isinstance(physics, dict)
            ir = physics["ir_compensation"]
            assert isinstance(ir, dict)
            before_ir_k = float(ir["k_volts_per_percent"])
            before_soh = float(candidate["soh"])
            if ir_k is not None:
                ir["k_volts_per_percent"] = ir_k
            if soh is not None:
                candidate["soh"] = soh
            schema.validate_target_state(candidate, source=str(self.model_path))
            after_ir_k = float(ir["k_volts_per_percent"])
            after_soh = float(candidate["soh"])
            changes: dict[str, tuple[float, float]] = {}
            if ir_k is not None and before_ir_k != after_ir_k:
                changes["physics.ir_compensation.k_volts_per_percent"] = (
                    before_ir_k,
                    after_ir_k,
                )
            if soh is not None and before_soh != after_soh:
                changes["soh"] = (before_soh, after_soh)
            if not changes:
                return changes
            if receipt_data is not None:
                candidate[schema.FEEDBACK_STATE_KEY] = _feedback_receipt(
                    event_at=event_at,
                    changes=changes,
                    receipt_data=receipt_data,
                )
            files.atomic_write_model(self.model_path, schema.canonical_json(candidate))
            persisted, _ = schema.load_target_state(self.model_path)
            self._state = copy.deepcopy(persisted)
            self._snapshot = self._snapshot_from_state(persisted)
            return changes

    def last_feedback(self) -> dict[str, Any] | None:
        """Return the durable receipt needed to recover a missing history row."""
        with self._lock:
            receipt = self._state.get(schema.FEEDBACK_STATE_KEY)
            return copy.deepcopy(receipt) if isinstance(receipt, dict) else None

    def _require_writer_lock(self) -> None:
        if self._writer_lock_fd is None:
            raise files.ModelStateFileError("model changes require ownership of the writer lock")

    def _snapshot_from_state(self, state: Mapping[str, Any]) -> FrozenModelSnapshot:
        physics = state["physics"]
        assert isinstance(physics, Mapping)
        ir = physics["ir_compensation"]
        assert isinstance(ir, Mapping)
        lut = _snapshot_lut(state["lut"])
        return FrozenModelSnapshot(
            rated_capacity_ah=self.rated_capacity_ah,
            nominal_voltage_v=NOMINAL_VOLTAGE,
            nominal_power_watts=NOMINAL_POWER_WATTS,
            soh=float(state["soh"]),
            peukert_exponent=float(physics["peukert_exponent"]),
            ir_k_v_per_pp=float(ir["k_volts_per_percent"]),
            ir_reference_load_percent=0.0,
            lut=lut,
        )


def _snapshot_lut(raw_lut: object) -> FrozenLut:
    assert isinstance(raw_lut, list)
    return tuple(LutPoint(float(entry["v"]), float(entry["soc"])) for entry in raw_lut)


def _receipt_data(receipt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    allowed = {"event_at", "evidence_at", "reason", "field_metadata"}
    if set(receipt) - allowed or "event_at" not in receipt:
        raise ValueError("event_receipt must contain event_at and only receipt metadata")
    event_at = utc_second(str(receipt["event_at"]))
    evidence_at = utc_second(str(receipt.get("evidence_at", event_at)))
    reason = receipt.get("reason", "model feedback")
    if not isinstance(reason, str) or not reason:
        raise ValueError("event_receipt.reason must be non-empty text")
    field_metadata = receipt.get("field_metadata", {})
    if not isinstance(field_metadata, Mapping):
        raise ValueError("event_receipt field_metadata must be an object")
    return {
        "event_at": event_at,
        "evidence_at": evidence_at,
        "reason": reason,
        "field_metadata": _canonical_field_metadata(field_metadata),
    }


def _same_event(state: Mapping[str, Any], event_at: str) -> bool:
    receipt = state.get(schema.FEEDBACK_STATE_KEY)
    return isinstance(receipt, Mapping) and receipt.get("event_at") == event_at


def _feedback_receipt(
    *,
    event_at: str | None,
    changes: Mapping[str, tuple[float, float]],
    receipt_data: Mapping[str, Any],
) -> dict[str, Any]:
    assert event_at is not None
    rendered: dict[str, dict[str, Any]] = {}
    for field, (before, after) in changes.items():
        entry: dict[str, Any] = {
            "from": before,
            "to": after,
            "delta": round(after - before, 12),
        }
        metadata = receipt_data["field_metadata"].get(field, {})
        if "evidence_at" in metadata:
            entry["evidence_at"] = metadata["evidence_at"]
        if "reason" in metadata:
            field_reason = metadata["reason"]
            if not isinstance(field_reason, str) or not field_reason:
                raise ValueError(f"event_receipt field reason for {field} must be non-empty text")
            entry["reason"] = field_reason
        rendered[field] = entry
    return {
        "event_at": event_at,
        "evidence_at": receipt_data["evidence_at"],
        "changes": rendered,
        "reason": receipt_data["reason"],
    }


def _canonical_field_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field, raw in metadata.items():
        if not isinstance(raw, Mapping) or set(raw) - {"evidence_at", "reason"}:
            raise ValueError(f"event_receipt field metadata for {field} is malformed")
        entry: dict[str, Any] = {}
        if "evidence_at" in raw:
            entry["evidence_at"] = utc_second(str(raw["evidence_at"]))
        if "reason" in raw:
            if not isinstance(raw["reason"], str) or not raw["reason"]:
                raise ValueError(f"event_receipt field reason for {field} is malformed")
            entry["reason"] = raw["reason"]
        result[str(field)] = entry
    return result
