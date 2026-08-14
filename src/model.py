"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import copy
import dataclasses
import hashlib
import json
import logging
import math
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict, cast

from src import replacement_predictor
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.capacity_estimator import compute_cov


class ModelLoadError(Exception):
    """Raised when an existing model.json is unreadable, malformed, or invalid."""


class ModelState(TypedDict):
    """Typed schema for the persisted model.json state dict.

    This is the complete current persisted state. Every key is emitted by fresh
    defaults and every existing file must contain exactly this set of keys.
    Runtime does not fill in missing keys or preserve retired state.
    """

    # Capacity & SoH
    soh: float
    soh_history: List[Dict[str, Any]]
    capacity_estimates: List[Dict[str, Any]]
    capacity_ah_measured: Optional[float]
    # Physics / LUT
    physics: Dict[str, Any]
    lut: List[Dict[str, Any]]
    r_internal_history: List[Dict[str, Any]]
    # Lifecycle
    battery_install_date: Optional[str]
    battery_epoch_id: str
    cycle_count: int
    cumulative_on_battery_sec: float
    new_battery_detected: bool
    new_battery_detected_timestamp: Optional[str]
    # Discharge log
    discharge_events: List[Dict[str, Any]]
    # Diagnostic test scheduling (category ③ — learned/persisted: last upscmd result only)
    # scheduled_test_timestamp / scheduled_test_reason / test_block_reason are NOT persisted:
    # they are scheduler outputs (health.json-only), not learned state.
    last_upscmd_timestamp: Optional[str]
    last_upscmd_type: Optional[str]
    last_upscmd_status: Optional[str]


# Derived from the schema so the two never drift. Used by load() to fail-fast on
# unknown keys instead of letting them silently survive save()'s round-trip.
KNOWN_STATE_KEYS = frozenset(ModelState.__annotations__)

# Release A deliberately has no runtime migration path.  The one-time production
# conversion is performed before deployment; a running binary accepts only this
# schema and therefore never needs a legacy sentinel or fallback mapping.
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


def latest_capacity_ah_ref(soh_history: List[Dict[str, Any]]) -> Optional[float]:
    """Return the capacity_ah_ref of the most recent soh_history entry, or None if empty.

    The SINGLE shared baseline selector used by both BatteryModel.compute_replacement_due()
    and scripts/battery-health.py, so both filter the replacement regression on the SAME
    baseline (preventing mixed-baseline divergence). Every entry is tagged at write time by
    add_soh_history_entry, so a non-empty history always has the field — the index access
    fails fast if that invariant is ever broken. None means only "no history yet".
    """
    if not soh_history:
        return None
    return soh_history[-1]["capacity_ah_ref"]


def is_capacity_converged(estimates: List[Dict[str, Any]]) -> bool:
    """Single shared convergence predicate: >=3 capacity samples AND CoV < 0.10.

    Used by BatteryModel.get_convergence_status() and scripts/battery-health.py so both
    select samples and apply the CoV gate from ONE definition — they cannot drift. Entries
    missing 'ah_estimate' are skipped rather than raising KeyError, so a partial/corrupt
    estimate can never make the daemon and the CLI disagree on `converged`.
    """
    ah = [e["ah_estimate"] for e in estimates if "ah_estimate" in e]
    return len(ah) >= 3 and compute_cov(ah) < 0.10


# RLS estimator defaults — single source of truth for _sync_physics_from_state,
# _default_vrla_lut, and PhysicsParams dataclass defaults.
DEFAULT_IR_K_THETA = 0.015
DEFAULT_PEUKERT_EXPONENT = 1.2


@dataclass(frozen=True)
class ConvergenceStatus:
    """Immutable return type of BatteryModel.get_convergence_status()."""

    sample_count: int
    confidence_percent: float
    latest_ah: Optional[float]
    rated_ah: float
    converged: bool
    capacity_ah_measured: Optional[float]
    cov: float


@dataclass
class IRCompensation:
    """IR voltage compensation parameters."""

    k_volts_per_percent: float = DEFAULT_IR_K_THETA
    reference_load_percent: float = 20.0


@dataclass
class RLSParams:
    """Scalar RLS estimator state for a single parameter."""

    theta: float = 0.0
    P: float = 1.0
    sample_count: int = 0
    forgetting_factor: float = 0.97


@dataclass
class PhysicsParams:
    """Typed view of the physics sub-dict in model.json.

    Only the three learned keys are stored here: peukert_exponent, ir_compensation,
    rls_state. nominal_voltage and nominal_power_watts are sourced from constants.py
    at runtime and are not persisted.
    """

    peukert_exponent: float = DEFAULT_PEUKERT_EXPONENT
    ir_compensation: IRCompensation = field(default_factory=IRCompensation)
    rls_state: Dict[str, RLSParams] = field(
        default_factory=lambda: {
            "ir_k": RLSParams(theta=DEFAULT_IR_K_THETA),
            "peukert": RLSParams(theta=DEFAULT_PEUKERT_EXPONENT),
        }
    )


logger = logging.getLogger("ups-battery-monitor")


def _sync_parent_directory(parent: Path) -> None:
    """Durably persist a completed atomic rename in the parent directory."""
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write(filepath, content: str, mode: int = 0o600) -> str:
    """
    Safely write string content to filepath with atomic guarantees.

    Uses tempfile + fdatasync + os.replace pattern to prevent corruption
    on power loss or crash during write.

    fdatasync (data-only sync) is used instead of fsync because file
    metadata (atime, ctime) is not critical for reading. This reduces I/O
    latency by ~50% by skipping unnecessary inode syncs.

    Args:
        filepath: Target file path (str or Path)
        content: String content to write
        mode: File permission bits (default 0o600). Callers writing
              files read by other users/services should pass 0o644.

    Returns:
        SHA-256 digest of the exact UTF-8 content successfully renamed into place.

    Raises:
        IOError: If write or fdatasync fails
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()

    # Same directory ensures same filesystem for atomic rename
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(filepath.parent), delete=False, suffix=".tmp"
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fdatasync(tmp.fileno())
            os.fchmod(tmp.fileno(), mode)

        tmp_path.replace(filepath)  # atomic on POSIX (unlink + link)
        _sync_parent_directory(filepath.parent)
        logger.debug(f"Atomically wrote {filepath}")
        return content_hash

    except Exception as e:
        # Clean up temp file on write-phase or rename-phase error
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as cleanup_err:
                logger.warning(
                    "Failed to clean up temp file %s: %s",
                    tmp_path,
                    cleanup_err,
                    extra={"event_type": "atomic_write_cleanup_failed"},
                )
        logger.error(
            f"Atomic write failed: {e}", exc_info=True, extra={"event_type": "atomic_write_failed"}
        )
        raise


def atomic_write_json(filepath, data, mode: int = 0o600) -> str:
    """Atomically write dict as JSON. Thin wrapper around atomic_write."""
    return atomic_write(filepath, json.dumps(data, indent=2), mode=mode)


class BatteryModel:
    """
    Battery model persistence and VRLA LUT management.

    Stores:
    - LUT: voltage → SoC lookup table with source tracking
    - SoH history: list of (date, SoH) points for degradation tracking
    - Metadata: capacity, current SoH estimate
    """

    def __init__(
        self,
        model_path=None,
        capacity_ah: float = RATED_CAPACITY_AH,
        soh_threshold: float = 0.80,
    ):
        """
        Initialize battery model from file or create default.

        Args:
            model_path: Path to model.json (str or Path)
                       If None, defaults to ~/.config/ups-battery-monitor/model.json
            capacity_ah: Rated reference capacity in Ah. Sourced from config at
                        runtime (not persisted). Default is RATED_CAPACITY_AH (7.2
                        for CyberPower UT850EG).
            soh_threshold: SoH fraction at which replacement is recommended. Sourced
                          from config.soh_alert_threshold at runtime (not persisted).
                          Default 0.80 matches the config default so standalone callers
                          (motd_status.py) reproduce the prior value without a config object.
        """
        if model_path is None:
            model_path = Path.home() / ".config" / "ups-battery-monitor" / "model.json"
        else:
            model_path = Path(model_path)

        self.model_path = model_path
        self.capacity_ah = capacity_ah
        self.soh_threshold = soh_threshold
        self.state: ModelState = self._default_vrla_lut()
        self.load()

    def load(self):
        """
        Load model.json from disk or initialize with standard VRLA curve.

        If file exists: parse JSON and validate the complete current schema
        If missing: create default VRLA curve
        If malformed JSON: raise ModelLoadError without changing the file
        If unreadable (permission/IO error): raise ModelLoadError to prevent
            silent fallback that would overwrite good data on next save()

        Raises:
            ModelLoadError: If an existing file cannot be read, parsed, or validated.
        """
        if self.model_path.exists():
            try:
                with open(self.model_path, "r") as f:
                    loaded = json.load(f)
            except json.JSONDecodeError as e:
                logger.error(
                    "Malformed model.json: %s; refusing startup without changing it",
                    e,
                    extra={"event_type": "model_corrupt", "model_path": str(self.model_path)},
                )
                raise ModelLoadError(f"Malformed model {self.model_path}: {e}") from e
            except UnicodeDecodeError as e:
                logger.error(
                    "Unreadable model.json encoding: %s; refusing startup without changing it",
                    e,
                    extra={"event_type": "model_corrupt", "model_path": str(self.model_path)},
                )
                raise ModelLoadError(f"Cannot decode model {self.model_path}: {e}") from e
            except OSError as e:
                raise ModelLoadError(f"Cannot read {self.model_path}: {e}") from e
            if not isinstance(loaded, dict):
                raise ModelLoadError(
                    f"{self.model_path} must contain a JSON object, got {type(loaded).__name__}"
                )
            self.state = cast(ModelState, loaded)
            logger.info(
                "Loaded model from %s",
                self.model_path,
                extra={"event_type": "model_loaded", "model_path": str(self.model_path)},
            )
        else:
            logger.info(
                "Model file not found; initializing with standard VRLA curve",
                extra={"event_type": "model_init_default", "model_path": str(self.model_path)},
            )
            self.state = cast(ModelState, self._default_vrla_lut())

        self._require_current_schema()
        self._validate_state_fields()
        self._sync_physics_from_state()
        self._validate_lut()

    def _sync_physics_from_state(self):
        """Populate self.physics from self.state['physics'] dict."""
        physics = self.state["physics"]
        ir = physics["ir_compensation"]
        rls_data = physics["rls_state"]

        rls_state = {}
        for name in ("ir_k", "peukert"):
            stored_params = rls_data[name]
            rls_state[name] = RLSParams(
                theta=stored_params["theta"],
                P=stored_params["P"],
                sample_count=stored_params["sample_count"],
                forgetting_factor=stored_params["forgetting_factor"],
            )

        self.physics = PhysicsParams(
            peukert_exponent=physics["peukert_exponent"],
            ir_compensation=IRCompensation(
                k_volts_per_percent=ir["k_volts_per_percent"],
                reference_load_percent=ir["reference_load_percent"],
            ),
            rls_state=rls_state,
        )

    def _sync_physics_to_state(self):
        """Write self.physics back to self.state['physics'] for JSON serialization.

        Only the three learned keys persist: peukert_exponent, ir_compensation, rls_state.
        nominal_voltage and nominal_power_watts are sourced from constants.py at runtime.
        """
        self.state["physics"] = {
            "peukert_exponent": self.physics.peukert_exponent,
            "ir_compensation": {
                "k_volts_per_percent": self.physics.ir_compensation.k_volts_per_percent,
                "reference_load_percent": self.physics.ir_compensation.reference_load_percent,
            },
            "rls_state": {
                name: dataclasses.asdict(rls) for name, rls in self.physics.rls_state.items()
            },
        }

    def _require_current_schema(self) -> None:
        """Require exactly the complete current top-level schema and a UUID epoch."""
        if not isinstance(self.state, dict):
            raise ModelLoadError(f"{self.model_path} must contain a JSON object")
        actual = set(self.state)
        missing = KNOWN_STATE_KEYS - actual
        unknown = actual - KNOWN_STATE_KEYS
        if missing or unknown:
            details = []
            if missing:
                details.append(f"missing key(s): {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown key(s): {', '.join(sorted(unknown))}")
            raise ModelLoadError(
                f"{self.model_path} has invalid top-level schema ({'; '.join(details)})"
            )
        epoch = self.state["battery_epoch_id"]
        if not isinstance(epoch, str) or not epoch:
            raise ModelLoadError(f"{self.model_path} has invalid battery_epoch_id")
        try:
            uuid.UUID(epoch)
        except (ValueError, AttributeError):
            raise ModelLoadError(f"{self.model_path} has invalid battery_epoch_id")

    @staticmethod
    def _require_finite_number(
        value: Any, path: str, *, minimum: float | None = None, maximum: float | None = None
    ) -> None:
        """Require a real finite number, optionally within an inclusive range."""
        try:
            finite = math.isfinite(value) if isinstance(value, (int, float)) else False
        except (OverflowError, TypeError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise ModelLoadError(f"{path} must be a finite number")
        if minimum is not None and value < minimum:
            raise ModelLoadError(f"{path} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ModelLoadError(f"{path} must be <= {maximum}")

    @staticmethod
    def _require_string_or_none(value: Any, path: str) -> None:
        if value is not None and not isinstance(value, str):
            raise ModelLoadError(f"{path} must be a string or null")

    def _validate_state_fields(self) -> None:
        """Validate persisted primitive/container fields without changing state."""
        state = self.state
        self._require_finite_number(state["soh"], "soh", minimum=0.0, maximum=1.0)
        if state["soh"] <= 0.0:
            raise ModelLoadError("soh must be > 0")

        for key in ("soh_history", "capacity_estimates", "r_internal_history", "discharge_events"):
            value = state[key]
            if not isinstance(value, list):
                raise ModelLoadError(f"{key} must be a list")
            if any(not isinstance(entry, dict) for entry in value):
                raise ModelLoadError(f"{key} entries must be objects")

        for index, entry in enumerate(state["soh_history"]):
            path = f"soh_history[{index}]"
            if set(entry) != {"date", "soh", "capacity_ah_ref"}:
                raise ModelLoadError(f"{path} must contain date, soh, capacity_ah_ref only")
            if not isinstance(entry["date"], str):
                raise ModelLoadError(f"{path}.date must be a string")
            self._require_finite_number(entry["soh"], f"{path}.soh", minimum=0.0, maximum=1.0)
            if entry["soh"] <= 0.0:
                raise ModelLoadError(f"{path}.soh must be > 0")
            self._require_finite_number(
                entry["capacity_ah_ref"], f"{path}.capacity_ah_ref", minimum=0.0
            )
            if entry["capacity_ah_ref"] <= 0.0:
                raise ModelLoadError(f"{path}.capacity_ah_ref must be > 0")

        for index, entry in enumerate(state["capacity_estimates"]):
            path = f"capacity_estimates[{index}]"
            if set(entry) != {"timestamp", "ah_estimate", "confidence", "metadata"}:
                raise ModelLoadError(f"{path} has invalid keys")
            if not isinstance(entry["timestamp"], str):
                raise ModelLoadError(f"{path}.timestamp must be a string")
            self._require_finite_number(entry["ah_estimate"], f"{path}.ah_estimate", minimum=0.0)
            if entry["ah_estimate"] <= 0.0:
                raise ModelLoadError(f"{path}.ah_estimate must be > 0")
            self._require_finite_number(
                entry["confidence"], f"{path}.confidence", minimum=0.0, maximum=1.0
            )
            if not isinstance(entry["metadata"], dict):
                raise ModelLoadError(f"{path}.metadata must be an object")

        for index, entry in enumerate(state["r_internal_history"]):
            path = f"r_internal_history[{index}]"
            required = {"date", "r_ohm", "v_before", "v_sag", "load_percent", "event"}
            if set(entry) != required:
                raise ModelLoadError(f"{path} has invalid keys")
            if not isinstance(entry["date"], str) or not isinstance(entry["event"], str):
                raise ModelLoadError(f"{path}.date and event must be strings")
            for key in ("r_ohm", "v_before", "v_sag", "load_percent"):
                self._require_finite_number(entry[key], f"{path}.{key}", minimum=0.0)

        for key in (
            "battery_install_date",
            "new_battery_detected_timestamp",
            "last_upscmd_timestamp",
            "last_upscmd_type",
            "last_upscmd_status",
        ):
            self._require_string_or_none(state[key], key)
        self._require_finite_number(
            state["capacity_ah_measured"], "capacity_ah_measured", minimum=0.0
        ) if state["capacity_ah_measured"] is not None else None
        if state["capacity_ah_measured"] is not None and state["capacity_ah_measured"] <= 0.0:
            raise ModelLoadError("capacity_ah_measured must be > 0 or null")
        if (
            not isinstance(state["cycle_count"], int)
            or isinstance(state["cycle_count"], bool)
            or state["cycle_count"] < 0
        ):
            raise ModelLoadError("cycle_count must be a nonnegative integer")
        self._require_finite_number(
            state["cumulative_on_battery_sec"], "cumulative_on_battery_sec", minimum=0.0
        )
        for key in ("new_battery_detected",):
            if not isinstance(state[key], bool):
                raise ModelLoadError(f"{key} must be a boolean")

        physics = state["physics"]
        if not isinstance(physics, dict) or set(physics) != {
            "peukert_exponent",
            "ir_compensation",
            "rls_state",
        }:
            raise ModelLoadError(
                "physics must contain exactly peukert_exponent, ir_compensation, rls_state"
            )
        self._require_finite_number(
            physics["peukert_exponent"], "physics.peukert_exponent", minimum=1.0, maximum=1.5
        )
        ir = physics["ir_compensation"]
        if not isinstance(ir, dict) or set(ir) != {"k_volts_per_percent", "reference_load_percent"}:
            raise ModelLoadError("physics.ir_compensation has invalid keys")
        self._require_finite_number(
            ir["k_volts_per_percent"], "physics.ir_compensation.k_volts_per_percent"
        )
        self._require_finite_number(
            ir["reference_load_percent"],
            "physics.ir_compensation.reference_load_percent",
            minimum=0.0,
        )
        rls = physics["rls_state"]
        if not isinstance(rls, dict) or set(rls) != {"ir_k", "peukert"}:
            raise ModelLoadError("physics.rls_state must contain exactly ir_k and peukert")
        required_rls = {"theta", "P", "sample_count", "forgetting_factor"}
        for name in ("ir_k", "peukert"):
            params = rls[name]
            path = f"physics.rls_state.{name}"
            if not isinstance(params, dict) or set(params) != required_rls:
                raise ModelLoadError(f"{path} has invalid keys")
            self._require_finite_number(params["theta"], f"{path}.theta")
            self._require_finite_number(params["P"], f"{path}.P", minimum=0.0)
            if (
                not isinstance(params["sample_count"], int)
                or isinstance(params["sample_count"], bool)
                or params["sample_count"] < 0
            ):
                raise ModelLoadError(f"{path}.sample_count must be a nonnegative integer")
            self._require_finite_number(
                params["forgetting_factor"], f"{path}.forgetting_factor", minimum=0.0
            )

    def _validate_lut(self) -> None:
        """Validate the complete LUT without dropping or rewriting entries."""
        lut = self.state["lut"]
        if not isinstance(lut, list) or len(lut) < 2:
            raise ModelLoadError("lut must be a list with at least two entries")
        for index, entry in enumerate(lut):
            path = f"lut[{index}]"
            if not isinstance(entry, dict):
                raise ModelLoadError(f"{path} must be an object")
            if not {"v", "soc", "source"}.issubset(entry):
                raise ModelLoadError(f"{path} must contain v, soc, source")
            self._require_finite_number(entry["v"], f"{path}.v")
            self._require_finite_number(entry["soc"], f"{path}.soc", minimum=0.0, maximum=1.0)
            source = entry["source"]
            if not isinstance(source, str) or source not in {"standard", "anchor", "measured"}:
                raise ModelLoadError(f"{path}.source is invalid")
            allowed = {"v", "soc", "source"}
            if source == "measured":
                allowed.add("timestamp")
                if "timestamp" not in entry:
                    raise ModelLoadError(f"{path}.timestamp is required for measured LUT entries")
                self._require_finite_number(entry["timestamp"], f"{path}.timestamp")
            if set(entry) != allowed:
                raise ModelLoadError(f"{path} has invalid keys for source {source}")

    def _default_vrla_lut(self) -> ModelState:
        """
        Standard VRLA 12V discharge curve (7.2Ah reference capacity).

        Returns dict with LUT, SoH, and metadata.
        These are initial values; measured points replace standard entries
        as real discharge data accumulates.

        Reference: Typical sealed lead-acid (AGM/GEL) 12V battery
        - 13.4V: full charge (float voltage)
        - 12.4V: ~64% remaining (datasheet knee point)
        - 11.0V: very low (~6%)
        - 10.5V: cutoff anchor (0%, physical limit)
        """
        return {
            "soh": 1.0,
            "physics": {
                "peukert_exponent": DEFAULT_PEUKERT_EXPONENT,
                "ir_compensation": {
                    "k_volts_per_percent": DEFAULT_IR_K_THETA,
                    "reference_load_percent": 20.0,
                },
                "rls_state": {
                    "ir_k": {
                        "theta": DEFAULT_IR_K_THETA,
                        "P": 1.0,
                        "sample_count": 0,
                        "forgetting_factor": 0.97,
                    },
                    "peukert": {
                        "theta": DEFAULT_PEUKERT_EXPONENT,
                        "P": 1.0,
                        "sample_count": 0,
                        "forgetting_factor": 0.97,
                    },
                },
            },
            "lut": [
                {"v": 13.4, "soc": 1.00, "source": "standard"},
                {"v": 12.8, "soc": 0.85, "source": "standard"},
                {"v": 12.4, "soc": 0.64, "source": "standard"},
                {"v": 12.1, "soc": 0.40, "source": "standard"},
                {"v": 11.6, "soc": 0.18, "source": "standard"},
                {"v": 11.0, "soc": 0.06, "source": "standard"},
                {"v": 10.5, "soc": 0.00, "source": "anchor"},
            ],
            "soh_history": [
                {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "soh": 1.0,
                    "capacity_ah_ref": RATED_CAPACITY_AH,
                }
            ],
            # Enterprise-equivalent counters (accumulated over battery lifetime)
            "capacity_estimates": [],
            "capacity_ah_measured": None,
            "r_internal_history": [],
            "battery_install_date": None,
            "cycle_count": 0,  # OL→OB transitions (= transfer count)
            "cumulative_on_battery_sec": 0.0,
            "battery_epoch_id": str(uuid.uuid4()),
            "new_battery_detected": False,
            "new_battery_detected_timestamp": None,
            "discharge_events": [],
            "last_upscmd_timestamp": None,
            "last_upscmd_type": None,
            "last_upscmd_status": None,
        }

    def get_peukert_exponent(self) -> float:
        return self.physics.peukert_exponent

    def get_nominal_voltage(self) -> float:
        return NOMINAL_VOLTAGE

    def get_nominal_power_watts(self) -> float:
        return NOMINAL_POWER_WATTS

    def get_ir_k(self) -> float:
        return self.physics.ir_compensation.k_volts_per_percent

    def get_ir_reference_load(self) -> float:
        return self.physics.ir_compensation.reference_load_percent

    # --- Enterprise-equivalent counters ---

    def get_battery_install_date(self) -> str | None:
        return self.state.get("battery_install_date")

    def set_battery_install_date(self, date_str: str):
        self.state["battery_install_date"] = date_str

    def get_battery_epoch_id(self) -> str:
        """Return the UUID identifying the currently installed battery epoch."""
        epoch = self.state.get("battery_epoch_id")
        if not isinstance(epoch, str) or not epoch:
            raise ModelLoadError("battery model has no valid battery_epoch_id")
        return epoch

    def scientific_state(self) -> dict[str, Any]:
        """Return the canonical, scientific subset of persisted model state."""
        # Fingerprints are used as an observational guard.  Building the
        # canonical physics mapping here must not synchronize or otherwise
        # mutate persisted state: a read of the alarm state is not a model
        # write.  ``self.physics`` is the authoritative in-memory scientific
        # view between saves, while operational fields remain excluded.
        scientific = {
            key: copy.deepcopy(self.state.get(key)) for key in SCIENTIFIC_FINGERPRINT_FIELDS
        }
        scientific["physics"] = {
            "peukert_exponent": self.physics.peukert_exponent,
            "ir_compensation": {
                "k_volts_per_percent": self.physics.ir_compensation.k_volts_per_percent,
                "reference_load_percent": self.physics.ir_compensation.reference_load_percent,
            },
            "rls_state": {
                name: dataclasses.asdict(rls) for name, rls in self.physics.rls_state.items()
            },
        }
        return scientific

    def scientific_fingerprint(self) -> str:
        """Return a stable SHA-256 fingerprint of fields that affect safety science.

        Event counters, scheduler state, and operational metadata are intentionally
        excluded.  JSON key ordering and separators are fixed so the same state has
        the same fingerprint across processes.
        """
        canonical = json.dumps(
            self.scientific_state(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def reset_baseline(
        self,
        install_date: Optional[str] = None,
        *,
        event_open: bool = False,
    ) -> str:
        """Commit an operator-confirmed battery replacement baseline.

        The complete reset is one model transaction: the suspicion is acknowledged,
        its timestamp is cleared, a fresh UUID epoch is created, and the install date
        is updated.  An open discharge is rejected because assigning its observations
        to either epoch would be scientifically ambiguous.  On save failure both the
        in-memory state and the physics object are restored.
        """
        if event_open:
            raise RuntimeError("cannot reset battery baseline while a discharge event is open")

        previous_state = copy.deepcopy(self.state)
        previous_physics = copy.deepcopy(self.physics)
        new_date = install_date or datetime.now().strftime("%Y-%m-%d")
        new_capacity = self.get_capacity_ah()
        old_capacity = self.state.get("capacity_ah_measured")
        fresh_state = self._default_vrla_lut()
        fresh_state["soh_history"] = [
            {
                "date": new_date,
                "soh": 1.0,
                "capacity_ah_ref": round(new_capacity, 2),
            }
        ]
        fresh_state["capacity_estimates"] = []
        fresh_state["capacity_ah_measured"] = None
        fresh_state["r_internal_history"] = []
        fresh_state["discharge_events"] = []
        fresh_state["battery_install_date"] = new_date
        fresh_state["battery_epoch_id"] = str(uuid.uuid4())
        fresh_state["cycle_count"] = 0
        fresh_state["cumulative_on_battery_sec"] = 0.0
        fresh_state["new_battery_detected"] = False
        fresh_state["new_battery_detected_timestamp"] = None
        fresh_state["last_upscmd_timestamp"] = self.state["last_upscmd_timestamp"]
        fresh_state["last_upscmd_type"] = self.state["last_upscmd_type"]
        fresh_state["last_upscmd_status"] = self.state["last_upscmd_status"]
        self.state = fresh_state
        self.physics = PhysicsParams()
        try:
            model_hash = self.save()
        except Exception:
            self.state = previous_state
            self.physics = previous_physics
            raise
        logger.info(
            "baseline_reset: battery epoch rotated",
            extra={
                "event_type": "baseline_reset",
                "battery_epoch_id": self.state["battery_epoch_id"],
                "battery_install_date": new_date,
                "capacity_ah_old": old_capacity,
                "capacity_ah_new": new_capacity,
            },
        )
        return model_hash

    def get_cycle_count(self) -> int:
        return self.state.get("cycle_count", 0)

    def get_cumulative_on_battery_sec(self) -> float:
        return self.state.get("cumulative_on_battery_sec", 0.0)

    def compute_replacement_due(self) -> Optional[str]:
        """Compute predicted replacement date live from soh_history regression.

        Mirrors the OLD discharge_handler._predict_replacement gate exactly:
        returns None immediately when get_convergence_status().converged is False
        (discharge_handler.py:218-219 skipped regression when not converged).

        soh_history (model.py:36) and capacity_estimates (model.py:37) are INDEPENDENT
        arrays, so a model can have regression-quality soh_history while capacity_estimates
        is non-converged. Without this gate, the live value would diverge from the old
        persisted None for the default-config user (cycle-2 HIGH, T-26-08).

        Uses self.soh_threshold (NOT a hardcoded 0.80) so the live recompute reproduces
        the OLD persisted value for ALL configured soh_alert thresholds, not only 0.80
        (review HIGH #2). The shared latest_capacity_ah_ref helper selects the same
        baseline as battery-health.py (review HIGH #3).

        Returns:
            ISO8601 date string, "overdue", or None.
        """
        if not self.get_convergence_status().converged:
            return None
        soh_hist = self.get_soh_history()
        result = replacement_predictor.linear_regression_soh(
            soh_hist,
            threshold_soh=self.soh_threshold,
            capacity_ah_ref=latest_capacity_ah_ref(soh_hist),
        )
        return result[3] if result is not None else None

    def set_peukert_exponent(self, value: float):
        self.physics.peukert_exponent = value

    def get_rls_state(self, name: str) -> dict:
        """Get RLS estimator state as dict (for ScalarRLS.from_dict compatibility)."""
        rls = self.physics.rls_state.get(name)
        if rls is None:
            return dataclasses.asdict(RLSParams())
        return dataclasses.asdict(rls)

    def set_rls_state(self, name: str, theta: float, P: float, sample_count: int) -> None:
        """Update RLS estimator state (persisted on next save)."""
        rls = self.physics.rls_state.get(name)
        if rls is None:
            rls = RLSParams()
            self.physics.rls_state[name] = rls
        rls.theta = theta
        rls.P = P
        rls.sample_count = sample_count

    def _cap_history_entries(self, key: str, keep_count: int = 30) -> None:
        """Keep only the most recent keep_count entries from a list field."""
        items = self.state.get(key, [])
        if len(items) > keep_count:
            self.state[key] = items[-keep_count:]

    def _prune_lut(self, keep_count: int = 200) -> None:
        """Remove oldest measured LUT entries; retain non-measured and most recent measured.

        Strategy: keep all non-measured entries (standard, anchor, interpolated)
        plus most recent keep_count measured entries by timestamp.

        Dedup measured entries within ±0.1V — keep only the most recent per
        voltage band. Without this, ~80% of measured entries are duplicates at the
        same voltage, wasting the 200-entry prune budget.

        Args:
            keep_count: Maximum number of measured entries to retain (default 200)
        """
        lut = self.state.get("lut", [])
        non_measured = [e for e in lut if e.get("source") != "measured"]
        measured = [e for e in lut if e.get("source") == "measured"]

        if measured:
            measured.sort(key=lambda x: x.get("timestamp", 0))
            buckets: dict[int, dict] = {}
            for lut_entry in measured:
                bucket_key = round(lut_entry["v"] * 10)  # ±0.05V buckets
                buckets[bucket_key] = lut_entry  # later (newer) overwrites earlier
            measured = list(buckets.values())

        if len(measured) > keep_count:
            measured.sort(key=lambda x: x.get("timestamp", 0))
            measured = measured[-keep_count:]
        self.state["lut"] = sorted(non_measured + measured, key=lambda x: x["v"], reverse=True)

    def append_discharge_event(self, event: dict) -> None:
        """Append discharge completion to history.

        Args:
            event: {
                'timestamp': ISO8601 string,
                'event_reason': 'natural' | 'test_initiated',
                'duration_seconds': float,
                'depth_of_discharge': float,
                'measured_capacity_ah': float | None
            }
        """
        self.state["discharge_events"].append(event)

    def has_discharge_event(self, event_id: str) -> bool:
        """Return whether a nested discharge event with *event_id* is persisted in state."""
        return any(
            event.get("event_id") == event_id
            for event in self.state.get("discharge_events", [])
            if isinstance(event, dict)
        )

    def get_persisted_hash(self) -> str:
        """Return the SHA-256 hash of the exact bytes currently persisted on disk.

        A missing or unreadable model is an operational error. Callers updating a
        journal audit marker must never silently substitute the in-memory state.
        """
        try:
            persisted_bytes = self.model_path.read_bytes()
        except OSError as exc:
            raise ModelLoadError(f"Cannot hash persisted model {self.model_path}: {exc}") from exc
        return hashlib.sha256(persisted_bytes).hexdigest()

    def save(self) -> str:
        """
        Atomically write model to disk with history pruning.

        Prunes soh_history, r_internal_history, capacity_estimates,
        discharge_events (each capped at 30 entries), and LUT
        (deduplicates measured entries within ±0.1V, keeps most
        recent 200) to prevent unbounded growth.
        """
        self._sync_physics_to_state()
        self._cap_history_entries("soh_history")
        self._cap_history_entries("r_internal_history")
        self._prune_lut()
        self._cap_history_entries("capacity_estimates")
        self._cap_history_entries("discharge_events")
        return atomic_write_json(self.model_path, self.state)

    def get_lut(self):
        """Return the voltage→SoC lookup table entries."""
        return self.state.get("lut", [])

    def get_soh(self):
        """SoH estimate [0.0, 1.0]."""
        return self.state.get("soh", 1.0)

    def get_capacity_ah(self):
        """Runtime-configured rated capacity in Ah (default RATED_CAPACITY_AH).

        Sourced from the capacity_ah constructor argument (injected from config); not
        persisted in model.json. The measured/converged capacity is tracked separately
        via get_convergence_status().
        """
        return self.capacity_ah

    def add_soh_history_entry(self, date, soh, capacity_ah_ref):
        """Add a SoH history entry tagged with the capacity baseline it was computed against.

        Args:
            date: ISO8601 date string (e.g., '2026-03-16')
            soh: SoH estimate [0.0, 1.0]
            capacity_ah_ref: Capacity baseline used in the SoH calculation (Ah). Required —
                every entry carries it so the replacement regression can filter by baseline.
        """
        self.state["soh_history"].append(
            {"date": date, "soh": soh, "capacity_ah_ref": round(capacity_ah_ref, 2)}
        )
        self.state["soh"] = soh  # Update current SoH

    def get_soh_history(self):
        """Return list of {date, soh} entries."""
        return self.state.get("soh_history", [])

    def get_r_internal_history(self):
        """Return list of internal resistance measurements."""
        return self.state.get("r_internal_history", [])

    def append_capacity_estimate(
        self, ah_estimate: float, confidence: float, metadata: Dict, timestamp: str
    ) -> None:
        """Append a capacity estimate without persisting it.

        This primitive is intentionally separate from :meth:`add_capacity_estimate` so
        the event transaction can include capacity, SoH, and physics changes in one
        model commit.
        """
        self.state["capacity_estimates"].append(
            {
                "timestamp": timestamp,
                "ah_estimate": ah_estimate,
                "confidence": confidence,
                "metadata": metadata,
            }
        )
        self._cap_history_entries("capacity_estimates")

    def get_capacity_estimates(self) -> List[Dict]:
        """
        Get all capacity estimates, sorted by timestamp (latest first).

        Returns:
            List of {timestamp, ah_estimate, confidence, metadata} dicts,
            ordered newest to oldest
        """
        estimates = self.state.get("capacity_estimates", [])
        return sorted(estimates, key=lambda x: x.get("timestamp", ""), reverse=True)

    def get_convergence_status(self) -> ConvergenceStatus:
        """
        Return convergence status for MOTD + reporting.

        Computes coefficient of variation (CoV) from capacity estimates to track
        measurement stability. Returns a frozen ConvergenceStatus instance.

        Returns:
            ConvergenceStatus with fields:
                sample_count: Number of capacity measurements
                confidence_percent: 0–100%
                latest_ah: Latest measured capacity, or None if no samples
                rated_ah: Runtime-configured rated capacity (self.capacity_ah; default RATED_CAPACITY_AH)
                converged: True if count >= 3 AND CoV < 0.10
                capacity_ah_measured: Baseline stored on first convergence;
                    None until first convergence. Distinct from latest_ah —
                    this is the locked baseline used for new-battery detection.
                cov: Coefficient of variation (0.0 if no samples)
        """
        estimates = self.state.get("capacity_estimates", [])
        # Skip entries missing 'ah_estimate' so a corrupt sample degrades gracefully
        # instead of raising KeyError — same selection rule as is_capacity_converged().
        ah_values = [e["ah_estimate"] for e in estimates if "ah_estimate" in e]

        if not ah_values:
            return ConvergenceStatus(
                sample_count=len(estimates),
                confidence_percent=0.0,
                latest_ah=None,
                rated_ah=self.capacity_ah,
                converged=False,
                capacity_ah_measured=None,
                cov=0.0,
            )

        cov = compute_cov(ah_values)

        # 0.0 for n<3 per design (insufficient data to judge convergence)
        confidence = 0.0 if len(ah_values) < 3 else max(0.0, min(1.0, 1.0 - cov))

        return ConvergenceStatus(
            sample_count=len(estimates),
            confidence_percent=confidence * 100,
            latest_ah=ah_values[-1],
            rated_ah=self.capacity_ah,
            converged=is_capacity_converged(estimates),
            capacity_ah_measured=self.state.get("capacity_ah_measured", None),
            cov=cov,
        )

    def get_anchor_voltage(self) -> Optional[float]:
        """Return anchor point voltage (physical cutoff), or None if LUT has no anchor entry."""
        lut = self.get_lut()
        for entry in lut:
            if entry["soc"] == 0.0 and entry["source"] == "anchor":
                return entry["v"]
        return None

    def update_upscmd_result(
        self, upscmd_timestamp: str, upscmd_type: str, upscmd_status: str
    ) -> None:
        """Update last upscmd result (called after successful dispatch or error).

        Args:
            upscmd_timestamp: ISO8601 timestamp of upscmd attempt
            upscmd_type: Command sent, e.g., 'test.battery.start.deep' or 'test.battery.start.quick'
            upscmd_status: 'OK' or error message
        """
        self.state["last_upscmd_timestamp"] = upscmd_timestamp
        self.state["last_upscmd_type"] = upscmd_type
        self.state["last_upscmd_status"] = upscmd_status
        logger.debug(f"Upscmd result updated: type={upscmd_type}, status={upscmd_status}")

    def get_last_upscmd_timestamp(self) -> Optional[str]:
        """Get ISO8601 timestamp of last upscmd attempt, or None."""
        return self.state.get("last_upscmd_timestamp")

    def get_last_upscmd_status(self) -> Optional[str]:
        """Get status of last upscmd attempt ('OK', error string, or None if never attempted).

        Symmetric public getter for last_upscmd_status, matching get_last_upscmd_timestamp().
        Used by _calculate_days_since_last_test_success in SchedulerManager to distinguish
        successful diagnostics (status='OK') from failed dispatches — so a transient error
        cannot defer the next annual cadence ~365 days.
        """
        return self.state.get("last_upscmd_status")
