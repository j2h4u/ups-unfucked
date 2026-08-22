"""Runtime owner for one validated, immutable model snapshot."""

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.adapters import model_state_persistence as files
from src.adapters import model_state_schema as schema
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.battery_math.lut import FrozenLut, LutPoint
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
