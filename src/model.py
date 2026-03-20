"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import bisect
import json
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from src.capacity_estimator import compute_cov


class ModelLoadError(Exception):
    """Raised when model.json cannot be loaded or backed up."""

# RLS estimator defaults — single source of truth for _sync_physics_from_state,
# _default_vrla_lut, and PhysicsParams dataclass defaults.
DEFAULT_IR_K_THETA = 0.015
DEFAULT_PEUKERT_EXPONENT = 1.2


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

    def to_dict(self) -> dict:
        return {'theta': self.theta, 'P': self.P,
                'sample_count': self.sample_count, 'forgetting_factor': self.forgetting_factor}


@dataclass
class PhysicsParams:
    """Typed view of the physics sub-dict in model.json."""
    peukert_exponent: float = DEFAULT_PEUKERT_EXPONENT
    nominal_voltage: float = 12.0
    nominal_power_watts: float = 425.0
    ir_compensation: IRCompensation = field(default_factory=IRCompensation)
    rls_state: Dict[str, RLSParams] = field(default_factory=lambda: {
        'ir_k': RLSParams(theta=DEFAULT_IR_K_THETA),
        'peukert': RLSParams(theta=DEFAULT_PEUKERT_EXPONENT),
    })


logger = logging.getLogger('ups-battery-monitor')


def atomic_write(filepath, content: str) -> None:
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

    Raises:
        IOError: If write or fdatasync fails
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Same directory ensures same filesystem for atomic rename
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=str(filepath.parent),
            delete=False,
            suffix='.tmp'
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fdatasync(tmp.fileno())
            os.fchmod(tmp.fileno(), 0o600)

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
                    tmp_path, cleanup_err,
                    extra={'event_type': 'atomic_write_cleanup_failed'}
                )
        logger.error(f"Atomic write failed: {e}", exc_info=True,
                     extra={'event_type': 'atomic_write_failed'})
        raise


def atomic_write_json(filepath, data) -> None:
    """Atomically write dict as JSON. Thin wrapper around atomic_write."""
    atomic_write(filepath, json.dumps(data, indent=2))


class BatteryModel:
    """
    Battery model persistence and VRLA LUT management.

    Stores:
    - LUT: voltage → SoC lookup table with source tracking
    - SoH history: list of (date, SoH) points for degradation tracking
    - Metadata: capacity, current SoH estimate
    """

    def __init__(self, model_path=None):
        """
        Initialize battery model from file or create default.

        Args:
            model_path: Path to model.json (str or Path)
                       If None, defaults to ~/.config/ups-battery-monitor/model.json
        """
        if model_path is None:
            model_path = Path.home() / '.config' / 'ups-battery-monitor' / 'model.json'
        else:
            model_path = Path(model_path)

        self.model_path = model_path
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
                with open(self.model_path, 'r') as f:
                    self.state = json.load(f)
                self._seen_timestamps = {
                    e['timestamp'] for e in self.state.get('lut', []) if 'timestamp' in e
                }
                logger.info("Loaded model from %s", self.model_path,
                            extra={'event_type': 'model_loaded', 'model_path': str(self.model_path)})
            except json.JSONDecodeError as e:
                self._backup_corrupt_model(e)
                self.state = self._default_vrla_lut()
            except OSError as e:
                raise ModelLoadError(f"Cannot read {self.model_path}: {e}") from e
        else:
            logger.info("Model file not found; initializing with standard VRLA curve",
                        extra={'event_type': 'model_init_default', 'model_path': str(self.model_path)})
            self.state = self._default_vrla_lut()

        self._apply_defaults()
        self._sync_physics_from_state()
        self._validate_and_clamp_fields()
        self._validate_lut()

    def _backup_corrupt_model(self, parse_error: Exception) -> None:
        """Back up corrupt model.json, raising ModelLoadError if backup fails."""
        backup = self.model_path.with_suffix('.json.corrupt')
        logger.error("Malformed model.json: %s; backing up to %s, starting fresh",
                     parse_error, backup.name,
                     extra={'event_type': 'model_corrupt', 'model_path': str(self.model_path)})
        try:
            self.model_path.rename(backup)
        except OSError:
            # Target may already exist from a previous corrupt load;
            # use timestamped name to avoid overwriting earlier backup
            ts = datetime.now().strftime('%Y%m%dT%H%M%S')
            fallback = self.model_path.with_suffix(f'.json.corrupt.{ts}')
            try:
                self.model_path.rename(fallback)
                logger.warning("Backed up corrupt model.json to %s (primary target existed)",
                               fallback.name, extra={'event_type': 'model_backup_fallback'})
            except OSError as rename_err:
                logger.error("Cannot back up corrupt model.json: %s — refusing to overwrite",
                             rename_err, extra={'event_type': 'model_backup_failed'})
                raise ModelLoadError(
                    f"Cannot back up corrupt {self.model_path}: {rename_err}"
                ) from rename_err

    def _sync_physics_from_state(self):
        """Populate self.physics from self.state['physics'] dict."""
        physics = self.state.get('physics', {})
        ir = physics.get('ir_compensation', {})
        rls_data = physics.get('rls_state', {})

        rls_state = {}
        for name, default_theta in [('ir_k', DEFAULT_IR_K_THETA), ('peukert', DEFAULT_PEUKERT_EXPONENT)]:
            stored_params = rls_data.get(name, {})
            rls_state[name] = RLSParams(
                theta=stored_params.get('theta', default_theta),
                P=stored_params.get('P', 1.0),
                sample_count=stored_params.get('sample_count', 0),
                forgetting_factor=stored_params.get('forgetting_factor', 0.97),
            )

        self.physics = PhysicsParams(
            peukert_exponent=physics.get('peukert_exponent', DEFAULT_PEUKERT_EXPONENT),
            nominal_voltage=physics.get('nominal_voltage', 12.0),
            nominal_power_watts=physics.get('nominal_power_watts', 425.0),
            ir_compensation=IRCompensation(
                k_volts_per_percent=ir.get('k_volts_per_percent', DEFAULT_IR_K_THETA),
                reference_load_percent=ir.get('reference_load_percent', 20.0),
            ),
            rls_state=rls_state,
        )

    def _sync_physics_to_state(self):
        """Write self.physics back to self.state['physics'] for JSON serialization."""
        self.state['physics'] = {
            'peukert_exponent': self.physics.peukert_exponent,
            'nominal_voltage': self.physics.nominal_voltage,
            'nominal_power_watts': self.physics.nominal_power_watts,
            'ir_compensation': {
                'k_volts_per_percent': self.physics.ir_compensation.k_volts_per_percent,
                'reference_load_percent': self.physics.ir_compensation.reference_load_percent,
            },
            'rls_state': {
                name: rls.to_dict() for name, rls in self.physics.rls_state.items()
            },
        }

    def _apply_defaults(self):
        """Set default values for optional fields not present in loaded data."""
        required_keys = {'lut', 'soh', 'physics'}
        missing_keys = required_keys - set(self.state.keys())
        if missing_keys:
            logger.warning("Model missing required keys: %s; using default values", missing_keys,
                          extra={'event_type': 'model_missing_keys'})

        self.state.setdefault('sulfation_history', [])
        self.state.setdefault('discharge_events', [])
        self.state.setdefault('roi_history', [])
        self.state.setdefault('natural_blackout_events', [])
        self.state.setdefault('last_upscmd_timestamp', None)
        self.state.setdefault('last_upscmd_type', None)
        self.state.setdefault('last_upscmd_status', None)
        self.state.setdefault('scheduled_test_timestamp', None)
        self.state.setdefault('scheduled_test_reason', None)
        self.state.setdefault('test_block_reason', None)
        self.state.setdefault('blackout_credit', None)

    def _validate_and_clamp_fields(self):
        """Clamp physics values and validate scheduling field types."""
        self.physics.peukert_exponent = max(1.0, min(1.5, self.physics.peukert_exponent))
        soh = self.state.get('soh')
        if soh is not None and (soh < 0 or soh > 1.0):
            logger.warning("model.json soh=%s out of range, clamping to [0, 1]", soh,
                          extra={'event_type': 'model_field_clamped'})
            self.state['soh'] = max(0.0, min(1.0, soh))

        capacity_ref = self.state.get('full_capacity_ah_ref')
        if capacity_ref is not None and (not isinstance(capacity_ref, (int, float)) or capacity_ref <= 0):
            logger.warning("model.json full_capacity_ah_ref=%s invalid, resetting to 7.2", capacity_ref,
                          extra={'event_type': 'model_field_clamped'})
            self.state['full_capacity_ah_ref'] = 7.2

        for field in ('last_upscmd_timestamp', 'scheduled_test_timestamp',
                      'last_upscmd_type', 'last_upscmd_status',
                      'scheduled_test_reason', 'test_block_reason'):
            val = self.state.get(field)
            if val is not None and not isinstance(val, str):
                logger.warning("model.json %s=%r is not a string, clearing", field, val,
                              extra={'event_type': 'model_field_clamped'})
                self.state[field] = None

        for field in ('sulfation_history', 'discharge_events', 'roi_history', 'natural_blackout_events'):
            val = self.state.get(field)
            if val is not None and not isinstance(val, list):
                logger.warning("model.json %s=%r is not a list, resetting to []", field, val,
                              extra={'event_type': 'model_field_clamped'})
                self.state[field] = []

        credit = self.state.get('blackout_credit')
        if credit is not None and not isinstance(credit, dict):
            logger.warning("model.json blackout_credit is not a dict, clearing",
                          extra={'event_type': 'model_field_clamped'})
            self.state['blackout_credit'] = None

    def _validate_lut(self):
        """Drop LUT entries with missing or non-numeric v/soc values."""
        lut = self.state.get('lut', [])
        valid_lut = []
        for entry in lut:
            v, soc = entry.get('v'), entry.get('soc')
            if isinstance(v, (int, float)) and isinstance(soc, (int, float)):
                valid_lut.append(entry)
            else:
                logger.warning("Dropping invalid LUT entry: %s", entry,
                              extra={'event_type': 'model_lut_invalid_entry'})
        if len(valid_lut) != len(lut):
            self.state['lut'] = valid_lut

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
            'full_capacity_ah_ref': 7.2,
            'soh': 1.0,
            'physics': {
                'peukert_exponent': DEFAULT_PEUKERT_EXPONENT,
                'nominal_voltage': 12.0,
                'nominal_power_watts': 425.0,
                'ir_compensation': {
                    'k_volts_per_percent': DEFAULT_IR_K_THETA,
                    'reference_load_percent': 20.0
                },
                'rls_state': {
                    'ir_k': {'theta': DEFAULT_IR_K_THETA, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
                    'peukert': {'theta': DEFAULT_PEUKERT_EXPONENT, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
                }
            },
            'lut': [
                {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
                {'v': 12.8, 'soc': 0.85, 'source': 'standard'},
                {'v': 12.4, 'soc': 0.64, 'source': 'standard'},
                {'v': 12.1, 'soc': 0.40, 'source': 'standard'},
                {'v': 11.6, 'soc': 0.18, 'source': 'standard'},
                {'v': 11.0, 'soc': 0.06, 'source': 'standard'},
                {'v': 10.5, 'soc': 0.00, 'source': 'anchor'},
            ],
            'soh_history': [
                {'date': datetime.now().strftime('%Y-%m-%d'), 'soh': 1.0}
            ],
            # Enterprise-equivalent counters (accumulated over battery lifetime)
            'battery_install_date': None,
            'cycle_count': 0,              # OL→OB transitions (= transfer count)
            'cumulative_on_battery_sec': 0.0,
        }

    def get_peukert_exponent(self) -> float:
        return self.physics.peukert_exponent

    def get_nominal_voltage(self) -> float:
        return self.physics.nominal_voltage

    def get_nominal_power_watts(self) -> float:
        return self.physics.nominal_power_watts

    def get_ir_k(self) -> float:
        return self.physics.ir_compensation.k_volts_per_percent

    def get_ir_reference_load(self) -> float:
        return self.physics.ir_compensation.reference_load_percent

    # --- Enterprise-equivalent counters ---

    def get_battery_install_date(self) -> str:
        return self.state.get('battery_install_date')

    def set_battery_install_date(self, date_str: str):
        self.state['battery_install_date'] = date_str

    def get_cycle_count(self) -> int:
        return self.state.get('cycle_count', 0)

    def increment_cycle_count(self):
        """Increment OL→OB transition counter (includes flicker events, not just full discharges)."""
        self.state['cycle_count'] = self.state.get('cycle_count', 0) + 1

    def get_cumulative_on_battery_sec(self) -> float:
        return self.state.get('cumulative_on_battery_sec', 0.0)

    def get_replacement_due(self) -> str:
        """Return predicted replacement due date (ISO8601 or None)."""
        return self.state.get('replacement_due')

    def set_replacement_due(self, date_str: str):
        """Set predicted replacement due date (ISO8601 string or None)."""
        self.state['replacement_due'] = date_str

    def add_on_battery_time(self, seconds: float):
        """Accumulate on-battery time (additive, unit: seconds, no upper bound)."""
        self.state['cumulative_on_battery_sec'] = self.state.get('cumulative_on_battery_sec', 0.0) + seconds

    def set_peukert_exponent(self, value: float):
        self.physics.peukert_exponent = value

    def set_ir_k(self, value: float):
        self.physics.ir_compensation.k_volts_per_percent = value

    def get_rls_state(self, name: str) -> dict:
        """Get RLS estimator state as dict (for ScalarRLS.from_dict compatibility)."""
        rls = self.physics.rls_state.get(name)
        if rls is None:
            return RLSParams().to_dict()
        return rls.to_dict()

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
            'ir_k': RLSParams(theta=0.015),
            'peukert': RLSParams(theta=1.2),
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
        lut = self.state.get('lut', [])
        non_measured = [e for e in lut if e.get('source') != 'measured']
        measured = [e for e in lut if e.get('source') == 'measured']

        if measured:
            measured.sort(key=lambda x: x.get('timestamp', 0))
            buckets: dict[int, dict] = {}
            for lut_entry in measured:
                bucket_key = round(lut_entry['v'] * 10)  # ±0.05V buckets
                buckets[bucket_key] = lut_entry  # later (newer) overwrites earlier
            measured = list(buckets.values())

        if len(measured) > keep_count:
            measured.sort(key=lambda x: x.get('timestamp', 0))
            measured = measured[-keep_count:]
        self.state['lut'] = sorted(non_measured + measured, key=lambda x: x['v'], reverse=True)


    def append_sulfation_history(self, entry: dict) -> None:
        """Append sulfation measurement to history.

        Args:
            entry: {
                'timestamp': ISO8601 string,
                'event_type': 'natural' | 'test_initiated',
                'sulfation_score': float [0, 1],
                'days_since_deep': float,
                'ir_trend_rate': float,
                'recovery_delta': float,
                'temperature_celsius': float,
                'confidence_level': 'high' | 'medium' | 'low'
            }
        """
        self.state.setdefault('sulfation_history', []).append(entry)

    def append_discharge_event(self, event: dict) -> None:
        """Append discharge completion to history.

        Args:
            event: {
                'timestamp': ISO8601 string,
                'event_reason': 'natural' | 'test_initiated',
                'duration_seconds': float,
                'depth_of_discharge': float,
                'measured_capacity_ah': float | None,
                'cycle_roi': float
            }
        """
        self.state.setdefault('discharge_events', []).append(event)


    def save(self):
        """
        Atomically write model to disk with history pruning.

        Prunes soh_history, r_internal_history, capacity_estimates,
        sulfation_history, discharge_events (each capped at 30 entries),
        and LUT (deduplicates measured entries within ±0.1V, keeps most
        recent 200) to prevent unbounded growth.
        """
        self._sync_physics_to_state()
        self._cap_history_entries('soh_history')
        self._cap_history_entries('r_internal_history')
        self._prune_lut()
        self._cap_history_entries('capacity_estimates')
        self._cap_history_entries('sulfation_history')
        self._cap_history_entries('discharge_events')
        atomic_write_json(self.model_path, self.state)

    def get_lut(self):
        """Return the voltage→SoC lookup table entries."""
        return self.state.get('lut', [])

    def get_soh(self):
        """SoH estimate [0.0, 1.0]."""
        return self.state.get('soh', 1.0)

    def set_soh(self, value: float):
        """Update SoH estimate (stored as-is; clamping applied at load() time by _validate_and_clamp_fields)."""
        self.state['soh'] = value

    def get_capacity_ah(self):
        """Rated reference capacity in Ah (default 7.2 for UT850). Not measured — see get_latest_capacity()."""
        return self.state.get('full_capacity_ah_ref', 7.2)

    def add_soh_history_entry(self, date, soh, capacity_ah_ref=None):
        """Add a SoH history entry with optional capacity baseline tag.

        Args:
            date: ISO8601 date string (e.g., '2026-03-16')
            soh: SoH estimate [0.0, 1.0]
            capacity_ah_ref: Capacity baseline used in SoH calculation (Ah).
                            If None, entry has no capacity_ah_ref field (backward compat).
        """
        if 'soh_history' not in self.state:
            self.state['soh_history'] = []

        entry = {'date': date, 'soh': soh}

        if capacity_ah_ref is not None:
            entry['capacity_ah_ref'] = round(capacity_ah_ref, 2)

        self.state['soh_history'].append(entry)
        self.state['soh'] = soh  # Update current SoH

    def get_soh_history(self):
        """Return list of {date, soh} entries."""
        return self.state.get('soh_history', [])

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
        if 'r_internal_history' not in self.state:
            self.state['r_internal_history'] = []
        self.state['r_internal_history'].append({
            'date': date, 'r_ohm': round(r_ohm, 4),
            'v_before': round(v_before, 2), 'v_sag': round(v_sag, 2),
            'load_percent': round(load_percent, 1), 'event': event_type
        })

    def get_r_internal_history(self):
        """Return list of internal resistance measurements."""
        return self.state.get('r_internal_history', [])

    def add_capacity_estimate(self, ah_estimate: float, confidence: float, metadata: Dict, timestamp: str) -> None:
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
            - Calls self.save() for atomic persistence (may silently fail on OSError)
        """
        if 'capacity_estimates' not in self.state:
            self.state['capacity_estimates'] = []

        entry = {
            'timestamp': timestamp,
            'ah_estimate': ah_estimate,
            'confidence': confidence,
            'metadata': metadata
        }
        self.state['capacity_estimates'].append(entry)
        self._cap_history_entries('capacity_estimates')
        try:
            self.save()
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Failed to persist capacity estimate: {e}",
                         exc_info=True, extra={'event_type': 'capacity_persist_failed'})

    def get_capacity_estimates(self) -> List[Dict]:
        """
        Get all capacity estimates, sorted by timestamp (latest first).

        Returns:
            List of {timestamp, ah_estimate, confidence, metadata} dicts,
            ordered newest to oldest
        """
        estimates = self.state.get('capacity_estimates', [])
        return sorted(estimates, key=lambda x: x.get('timestamp', ''), reverse=True)

    def get_latest_capacity(self) -> Optional[float]:
        """
        Get the latest measured capacity value.

        Returns:
            Latest Ah estimate as float, or None if no estimates exist
        """
        estimates = self.get_capacity_estimates()
        if estimates:
            return estimates[0]['ah_estimate']
        return None

    def get_convergence_status(self) -> Dict[str, Any]:
        """
        Return convergence status for MOTD + reporting.

        Computes coefficient of variation (CoV) from capacity estimates to track
        measurement stability. Returns status dict for display and integration.

        Returns:
            {
                'sample_count': int,  # Number of capacity measurements
                'confidence_percent': float,  # 0–100%
                'latest_ah': float | None,  # Latest measured capacity
                'rated_ah': float,  # Firmware rated capacity (7.2 for UT850)
                'converged': bool,  # True if count >= 3 AND CoV < 0.10
                'capacity_ah_measured': float | None  # Baseline stored on first convergence;
                    # None until first convergence. Distinct from latest_ah — this is the
                    # locked baseline used for new-battery detection, not the most recent measurement.
            }
        """
        estimates = self.state.get('capacity_estimates', [])

        if not estimates:
            return {
                'sample_count': 0,
                'confidence_percent': 0.0,
                'latest_ah': None,
                'rated_ah': 7.2,
                'converged': False,
                'capacity_ah_measured': None,
                'cov': 0.0,
                'mean_ah': 0.0,
            }

        ah_values = [e['ah_estimate'] for e in estimates]
        cov = compute_cov(ah_values)

        # 0.0 for n<3 per design (insufficient data to judge convergence)
        confidence = 0.0 if len(ah_values) < 3 else max(0.0, min(1.0, 1.0 - cov))

        return {
            'sample_count': len(estimates),
            'confidence_percent': confidence * 100,
            'latest_ah': ah_values[-1],
            'rated_ah': 7.2,
            'converged': len(estimates) >= 3 and cov < 0.10,
            'capacity_ah_measured': self.state.get('capacity_ah_measured', None),
            'cov': cov,
            'mean_ah': sum(ah_values) / len(ah_values),
        }


    def get_anchor_voltage(self):
        """Return anchor point voltage (physical cutoff, should always be 10.5V)."""
        lut = self.get_lut()
        for entry in lut:
            if entry['soc'] == 0.0 and entry['source'] == 'anchor':
                return entry['v']
        return 10.5

    def has_measured_data(self):
        """True if LUT contains any 'measured' source entries."""
        lut = self.get_lut()
        return any(entry['source'] == 'measured' for entry in lut)

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
            'v': round(voltage, 2),
            'soc': round(soc, 3),
            'source': 'measured',
            'timestamp': timestamp
        }

        bisect.insort(self.state['lut'], entry, key=lambda x: -x['v'])

        logger.debug(f"Calibration point accumulated: voltage={voltage:.2f}V, soc={soc:.1%}, timestamp={timestamp}")

    def calibration_batch_flush(self) -> None:
        """Persist accumulated calibration points to disk.

        Call once per REPORTING_INTERVAL, not per point. Reduces SSD wear by ~60x during testing.

        Saves LUT (already sorted by calibration_write), preserves atomicity.

        Side effects:
            - Writes model.json to disk (atomic rename)
        """
        self.save()

    def update_lut_from_calibration(self, new_lut: List[Dict]):
        """Replace LUT with calibration result and persist to disk.

        Args:
            new_lut: Updated LUT entries
        """
        old_count = len(self.state['lut'])
        self.state['lut'] = new_lut
        self.save()
        logger.info(
            "LUT updated from calibration: %d entries, cliff region interpolated (was %d entries)",
            len(new_lut), old_count,
            extra={'event_type': 'lut_calibration_updated'}
        )

    # --- Scheduling State Management ---

    def set_blackout_credit(self, credit_dict: dict) -> None:
        """Grant blackout credit after natural deep discharge.

        Args:
            credit_dict: {
                'active': bool,
                'credited_event_timestamp': str (ISO8601),
                'credit_expires': str (ISO8601),
                'desulfation_credit': float (0.0–1.0)
            }
        """
        self.state['blackout_credit'] = credit_dict
        logger.debug(f"Blackout credit set: expires {credit_dict.get('credit_expires')}")

    def clear_blackout_credit(self) -> None:
        """Expire or clear blackout credit."""
        if self.state.get('blackout_credit'):
            self.state['blackout_credit']['active'] = False
            logger.debug("Blackout credit cleared")

    def update_scheduling_state(
        self,
        scheduled_timestamp: Optional[str],
        reason: str,
        block_reason: Optional[str] = None
    ) -> None:
        """Update scheduled test info and block reason.

        Args:
            scheduled_timestamp: ISO8601 timestamp of next proposed/eligible test
            reason: Stored as scheduled_test_reason (e.g., 'sulfation_0.65_roi_0.34')
            block_reason: If test is blocked, reason code (e.g., 'soh_floor_55%'), else None
        """
        self.state['scheduled_test_timestamp'] = scheduled_timestamp
        self.state['scheduled_test_reason'] = reason
        self.state['test_block_reason'] = block_reason
        logger.debug(f"Scheduling state updated: reason={reason}, blocked={block_reason}")

    def update_upscmd_result(
        self,
        upscmd_timestamp: str,
        upscmd_type: str,
        upscmd_status: str
    ) -> None:
        """Update last upscmd result (called after successful dispatch or error).

        Args:
            upscmd_timestamp: ISO8601 timestamp of upscmd attempt
            upscmd_type: Command sent, e.g., 'test.battery.start.deep' or 'test.battery.start.quick'
            upscmd_status: 'OK' or error message
        """
        self.state['last_upscmd_timestamp'] = upscmd_timestamp
        self.state['last_upscmd_type'] = upscmd_type
        self.state['last_upscmd_status'] = upscmd_status
        logger.debug(f"Upscmd result updated: type={upscmd_type}, status={upscmd_status}")

    def get_last_upscmd_timestamp(self) -> Optional[str]:
        """Get ISO8601 timestamp of last upscmd attempt, or None."""
        return self.state.get('last_upscmd_timestamp')

    def get_blackout_credit(self) -> Optional[dict]:
        """Get current blackout credit dict, or None if inactive/expired."""
        return self.state.get('blackout_credit')

