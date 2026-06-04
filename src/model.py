"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import bisect
import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from src import replacement_predictor
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.capacity_estimator import compute_cov


class ModelLoadError(Exception):
    """Raised when model.json cannot be loaded or backed up."""


class ModelState(TypedDict, total=False):
    """Typed schema for the persisted model.json state dict.

    Single source of truth for the set of legitimate top-level keys. `total=False`
    because keys are populated incrementally — defaults at load(), capacity/new-battery
    keys only after the first qualifying discharge. load() rejects any key NOT declared
    here (see _reject_unknown_state_keys): this is a single-host, no-backward-compat
    project, so an unknown key means stale/retired state (e.g. the v3.0 sulfation_history,
    roi_history, blackout_credit) that must be removed, not silently round-tripped.
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
    mean_ah: float


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


def atomic_write(filepath, content: str, mode: int = 0o600) -> None:
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

    Raises:
        IOError: If write or fdatasync fails
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

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
        logger.debug(f"Atomically wrote {filepath}")

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


def atomic_write_json(filepath, data, mode: int = 0o600) -> None:
    """Atomically write dict as JSON. Thin wrapper around atomic_write."""
    atomic_write(filepath, json.dumps(data, indent=2), mode=mode)


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
        self.state = {}
        self._seen_timestamps: set = set()
        self.load()

    def load(self):
        """
        Load model.json from disk or initialize with standard VRLA curve.

        If file exists: parse JSON, apply defaults, validate
        If missing: create default VRLA curve
        If malformed JSON: backup corrupt file to .corrupt, start fresh
        If unreadable (permission/IO error): raise ModelLoadError to prevent
            silent fallback that would overwrite good data on next save()

        Raises:
            ModelLoadError: If model.json exists but cannot be read (OSError).
        """
        if self.model_path.exists():
            try:
                with open(self.model_path, "r") as f:
                    self.state = json.load(f)
                self._seen_timestamps = {
                    e["timestamp"] for e in self.state.get("lut", []) if "timestamp" in e
                }
                logger.info(
                    "Loaded model from %s",
                    self.model_path,
                    extra={"event_type": "model_loaded", "model_path": str(self.model_path)},
                )
            except json.JSONDecodeError as e:
                self._backup_corrupt_model(e)
                self.state = self._default_vrla_lut()
            except OSError as e:
                raise ModelLoadError(f"Cannot read {self.model_path}: {e}") from e
        else:
            logger.info(
                "Model file not found; initializing with standard VRLA curve",
                extra={"event_type": "model_init_default", "model_path": str(self.model_path)},
            )
            self.state = self._default_vrla_lut()

        self._reject_unknown_state_keys()
        self._apply_defaults()
        self._sync_physics_from_state()
        self._validate_and_clamp_fields()
        self._validate_lut()

    def _backup_corrupt_model(self, parse_error: Exception) -> None:
        """Back up corrupt model.json, raising ModelLoadError if backup fails."""
        backup = self.model_path.with_suffix(".json.corrupt")
        logger.error(
            "Malformed model.json: %s; backing up to %s, starting fresh",
            parse_error,
            backup.name,
            extra={"event_type": "model_corrupt", "model_path": str(self.model_path)},
        )
        try:
            self.model_path.rename(backup)
        except OSError:
            # Target may already exist from a previous corrupt load;
            # use timestamped name to avoid overwriting earlier backup
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            fallback = self.model_path.with_suffix(f".json.corrupt.{ts}")
            try:
                self.model_path.rename(fallback)
                logger.warning(
                    "Backed up corrupt model.json to %s (primary target existed)",
                    fallback.name,
                    extra={"event_type": "model_backup_fallback"},
                )
            except OSError as rename_err:
                logger.error(
                    "Cannot back up corrupt model.json: %s — refusing to overwrite",
                    rename_err,
                    extra={"event_type": "model_backup_failed"},
                )
                raise ModelLoadError(
                    f"Cannot back up corrupt {self.model_path}: {rename_err}"
                ) from rename_err

    def _sync_physics_from_state(self):
        """Populate self.physics from self.state['physics'] dict."""
        physics = self.state.get("physics", {})
        ir = physics.get("ir_compensation", {})
        rls_data = physics.get("rls_state", {})

        rls_state = {}
        for name, default_theta in [
            ("ir_k", DEFAULT_IR_K_THETA),
            ("peukert", DEFAULT_PEUKERT_EXPONENT),
        ]:
            stored_params = rls_data.get(name, {})
            rls_state[name] = RLSParams(
                theta=stored_params.get("theta", default_theta),
                P=stored_params.get("P", 1.0),
                sample_count=stored_params.get("sample_count", 0),
                forgetting_factor=stored_params.get("forgetting_factor", 0.97),
            )

        self.physics = PhysicsParams(
            peukert_exponent=physics.get("peukert_exponent", DEFAULT_PEUKERT_EXPONENT),
            ir_compensation=IRCompensation(
                k_volts_per_percent=ir.get("k_volts_per_percent", DEFAULT_IR_K_THETA),
                reference_load_percent=ir.get("reference_load_percent", 20.0),
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

    def _reject_unknown_state_keys(self) -> None:
        """Fail-fast if model.json carries keys outside the ModelState schema.

        No-backward-compat policy: a key not in KNOWN_STATE_KEYS is retired/garbage
        state (e.g. v3.0 sulfation_history/roi_history/blackout_credit). Rather than
        silently round-tripping it through save(), refuse to load so the operator
        removes it. KNOWN_STATE_KEYS is derived from ModelState, so the schema is the
        single source of truth.
        """
        unknown = set(self.state) - KNOWN_STATE_KEYS
        if unknown:
            raise ModelLoadError(
                f"{self.model_path} contains unknown state key(s): "
                f"{', '.join(sorted(unknown))}. This single-host project keeps no "
                f"backward-compat shims — remove these key(s) from the file (or delete "
                f"it to regenerate) and restart."
            )

    def _apply_defaults(self):
        """Set default values for optional fields not present in loaded data."""
        required_keys = {"lut", "soh", "physics"}
        missing_keys = required_keys - set(self.state.keys())
        if missing_keys:
            logger.warning(
                "Model missing required keys: %s; using default values",
                missing_keys,
                extra={"event_type": "model_missing_keys"},
            )

        self.state.setdefault("discharge_events", [])
        self.state.setdefault("last_upscmd_timestamp", None)
        self.state.setdefault("last_upscmd_type", None)
        self.state.setdefault("last_upscmd_status", None)

    def _validate_and_clamp_fields(self):
        """Clamp physics values and validate scheduling field types."""
        self.physics.peukert_exponent = max(1.0, min(1.5, self.physics.peukert_exponent))
        soh = self.state.get("soh")
        if soh is not None and (soh < 0 or soh > 1.0):
            logger.warning(
                "model.json soh=%s out of range, clamping to [0, 1]",
                soh,
                extra={"event_type": "model_field_clamped"},
            )
            self.state["soh"] = max(0.0, min(1.0, soh))

        for key in (
            "last_upscmd_timestamp",
            "last_upscmd_type",
            "last_upscmd_status",
        ):
            val = self.state.get(key)
            if val is not None and not isinstance(val, str):
                logger.warning(
                    "model.json %s=%r is not a string, clearing",
                    key,
                    val,
                    extra={"event_type": "model_field_clamped"},
                )
                self.state[key] = None

        for key in ("discharge_events",):
            val = self.state.get(key)
            if val is not None and not isinstance(val, list):
                logger.warning(
                    "model.json %s=%r is not a list, resetting to []",
                    key,
                    val,
                    extra={"event_type": "model_field_clamped"},
                )
                self.state[key] = []

    def _validate_lut(self):
        """Drop LUT entries with missing or non-numeric v/soc values."""
        lut = self.state.get("lut", [])
        valid_lut = []
        for entry in lut:
            v, soc = entry.get("v"), entry.get("soc")
            if isinstance(v, (int, float)) and isinstance(soc, (int, float)):
                valid_lut.append(entry)
            else:
                logger.warning(
                    "Dropping invalid LUT entry: %s",
                    entry,
                    extra={"event_type": "model_lut_invalid_entry"},
                )
        if len(valid_lut) != len(lut):
            self.state["lut"] = valid_lut

    def _default_vrla_lut(self) -> Dict[str, Any]:
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
            "battery_install_date": None,
            "cycle_count": 0,  # OL→OB transitions (= transfer count)
            "cumulative_on_battery_sec": 0.0,
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

    def get_cycle_count(self) -> int:
        return self.state.get("cycle_count", 0)

    def increment_cycle_count(self):
        """Increment OL→OB transition counter (includes flicker events, not just full discharges)."""
        self.state["cycle_count"] = self.state.get("cycle_count", 0) + 1

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

    def add_on_battery_time(self, seconds: float):
        """Accumulate on-battery time (additive, unit: seconds, no upper bound)."""
        self.state["cumulative_on_battery_sec"] = (
            self.state.get("cumulative_on_battery_sec", 0.0) + seconds
        )

    def set_peukert_exponent(self, value: float):
        self.physics.peukert_exponent = value

    def set_ir_k(self, value: float):
        self.physics.ir_compensation.k_volts_per_percent = value

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

    def reset_rls_state(self) -> None:
        """Reset all RLS estimators to defaults (e.g., on battery replacement)."""
        self.physics.rls_state = {
            "ir_k": RLSParams(theta=0.015),
            "peukert": RLSParams(theta=1.2),
        }

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
        self.state.setdefault("discharge_events", []).append(event)

    def save(self):
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
        atomic_write_json(self.model_path, self.state)

    def get_lut(self):
        """Return the voltage→SoC lookup table entries."""
        return self.state.get("lut", [])

    def get_soh(self):
        """SoH estimate [0.0, 1.0]."""
        return self.state.get("soh", 1.0)

    def set_soh(self, value: float):
        """Update SoH estimate (stored as-is; clamping applied at load() time by _validate_and_clamp_fields)."""
        self.state["soh"] = value

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
        if "soh_history" not in self.state:
            self.state["soh_history"] = []

        self.state["soh_history"].append(
            {"date": date, "soh": soh, "capacity_ah_ref": round(capacity_ah_ref, 2)}
        )
        self.state["soh"] = soh  # Update current SoH

    def get_soh_history(self):
        """Return list of {date, soh} entries."""
        return self.state.get("soh_history", [])

    def add_r_internal_entry(self, date, r_ohm, v_before, v_sag, load_percent, event_type):
        """Add internal resistance measurement from voltage sag observation.

        Args:
            date: ISO8601 date string (e.g., '2026-03-16')
            r_ohm: Calculated internal resistance (ohms)
            v_before: Battery voltage before load transition (V)
            v_sag: Battery voltage during sag (V)
            load_percent: UPS load at time of measurement (0-100)
            event_type: EventType enum value; stored as event_type.name string
        """
        if "r_internal_history" not in self.state:
            self.state["r_internal_history"] = []
        self.state["r_internal_history"].append(
            {
                "date": date,
                "r_ohm": round(r_ohm, 4),
                "v_before": round(v_before, 2),
                "v_sag": round(v_sag, 2),
                "load_percent": round(load_percent, 1),
                "event": event_type,
            }
        )

    def get_r_internal_history(self):
        """Return list of internal resistance measurements."""
        return self.state.get("r_internal_history", [])

    def add_capacity_estimate(
        self, ah_estimate: float, confidence: float, metadata: Dict, timestamp: str
    ) -> None:
        """
        Add a capacity measurement to the estimates array.

        Stores measured capacity with confidence metadata for convergence tracking.
        Automatically prunes to keep last 30 entries (no unbounded growth).
        Persists atomically to disk.

        Args:
            ah_estimate: Measured capacity in Ah (float)
            confidence: Confidence metric [0.0, 1.0] based on CoV across measurements
            metadata: Dict with measurement details (delta_soc_percent, duration_sec, discharge_slope_mohm, load_avg_percent, etc.)
            timestamp: ISO8601 timestamp string

        Side effects:
            - Appends entry to model.state['capacity_estimates']
            - Calls _cap_history_entries('capacity_estimates') to limit array to 30 entries
            - Calls self.save() for atomic persistence; on save failure the in-memory
              append is rolled back so memory and disk stay consistent
        """
        if "capacity_estimates" not in self.state:
            self.state["capacity_estimates"] = []

        entry = {
            "timestamp": timestamp,
            "ah_estimate": ah_estimate,
            "confidence": confidence,
            "metadata": metadata,
        }
        self.state["capacity_estimates"].append(entry)
        self._cap_history_entries("capacity_estimates")
        try:
            self.save()
        except (OSError, TypeError, ValueError) as e:
            # Keep memory == disk: a learned estimate that lives in RAM but never reached
            # disk is exactly the silent divergence this milestone removes. On a restart the
            # in-memory-only sample would vanish and convergence replay would see a different
            # count than the running daemon. Roll the append back so both views agree; the
            # estimate re-derives on the next discharge.
            self.state["capacity_estimates"].pop()
            logger.error(
                f"Failed to persist capacity estimate, rolled back in-memory append: {e}",
                exc_info=True,
                extra={"event_type": "capacity_persist_failed"},
            )

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
                mean_ah: Mean of ah_estimates (0.0 if no samples)
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
                mean_ah=0.0,
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
            mean_ah=sum(ah_values) / len(ah_values),
        )

    def get_anchor_voltage(self) -> Optional[float]:
        """Return anchor point voltage (physical cutoff), or None if LUT has no anchor entry."""
        lut = self.get_lut()
        for entry in lut:
            if entry["soc"] == 0.0 and entry["source"] == "anchor":
                return entry["v"]
        return None

    def calibration_write(self, voltage: float, soc: float, timestamp: float):
        """
        Accumulate calibration datapoint in memory without persisting to disk.

        Called from monitor.py discharge buffer handler to capture intermediate
        measurements. Points are accumulated in memory and persisted once per
        REPORTING_INTERVAL via calibration_batch_flush() to reduce SSD wear by ~60x.

        Args:
            voltage: Measured battery voltage (V)
            soc: Calculated SoC as fraction (0.0-1.0)
            timestamp: Unix timestamp of measurement
        """
        if timestamp in self._seen_timestamps:
            return
        self._seen_timestamps.add(timestamp)

        entry = {
            "v": round(voltage, 2),
            "soc": round(soc, 3),
            "source": "measured",
            "timestamp": timestamp,
        }

        bisect.insort(self.state["lut"], entry, key=lambda x: -x["v"])

        logger.debug(
            f"Calibration point accumulated: voltage={voltage:.2f}V, soc={soc:.1%}, timestamp={timestamp}"
        )

    def calibration_batch_flush(self) -> None:
        """Persist accumulated calibration points to disk.

        Call once per REPORTING_INTERVAL, not per point. Reduces SSD wear by ~60x during testing.

        Saves LUT (already sorted by calibration_write), preserves atomicity.

        Side effects:
            - Writes model.json to disk (atomic rename)
        """
        self.save()

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
