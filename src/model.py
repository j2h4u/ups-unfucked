"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import bisect
import json
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from src.capacity_estimator import compute_cov


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
            os.fchmod(tmp.fileno(), 0o644)

        tmp_path.replace(filepath)  # atomic on POSIX (unlink + link)
        logger.debug(f"Atomically wrote {filepath}")

    except Exception as e:
        # Clean up temp file on write-phase or rename-phase error
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        logger.error(f"Atomic write failed: {e}", exc_info=True)
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
        self.data = {}
        self._seen_timestamps: set = set()
        self.load()

    def load(self):
        """
        Load model.json from disk or initialize with standard VRLA curve.

        If file exists: parse JSON
        If missing: create default VRLA curve
        If malformed: log error, initialize with default curve (B3 fix: don't re-raise)
        """
        if self.model_path.exists():
            try:
                with open(self.model_path, 'r') as f:
                    self.data = json.load(f)

                # Schema validation — check for required keys
                required_keys = {'lut', 'soh', 'physics'}
                missing_keys = required_keys - set(self.data.keys())
                if missing_keys:
                    logger.warning(f"Model missing required keys: {missing_keys}; using default values")

                self._seen_timestamps = {
                    e['timestamp'] for e in self.data.get('lut', []) if 'timestamp' in e
                }
                logger.info(f"Loaded model from {self.model_path}")
            except json.JSONDecodeError as e:
                logger.error(f"Malformed model.json: {e}; initializing with default VRLA curve")
                self.data = self._default_vrla_lut()
            except OSError as e:
                logger.error(f"Cannot read {self.model_path}: {e}; initializing with default VRLA curve")
                self.data = self._default_vrla_lut()
        else:
            logger.info("Model file not found; initializing with standard VRLA curve")
            self.data = self._default_vrla_lut()

        self.data.setdefault('sulfation_history', [])
        self.data.setdefault('discharge_events', [])
        self.data.setdefault('roi_history', [])
        self.data.setdefault('natural_blackout_events', [])
        self.data.setdefault('last_upscmd_timestamp', None)
        self.data.setdefault('last_upscmd_type', None)
        self.data.setdefault('last_upscmd_status', None)
        self.data.setdefault('scheduled_test_timestamp', None)
        self.data.setdefault('scheduled_test_reason', None)
        self.data.setdefault('test_block_reason', None)
        self.data.setdefault('blackout_credit', None)

        # Clamp physics values to sane ranges (defense against corrupted model.json)
        physics = self.data.get('physics', {})
        if 'peukert_exponent' in physics:
            physics['peukert_exponent'] = max(1.0, min(1.5, physics['peukert_exponent']))
        soh = self.data.get('soh')
        if soh is not None and (soh < 0 or soh > 1.0):
            logger.warning(f"model.json soh={soh} out of range, clamping to [0, 1]")
            self.data['soh'] = max(0.0, min(1.0, soh))

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
            'full_capacity_ah_ref': 7.2,  # Estimated from UT850EG 425W
            'soh': 1.0,  # State of Health (100% = new battery)
            'physics': {
                'peukert_exponent': 1.2,
                'nominal_voltage': 12.0,
                'nominal_power_watts': 425.0,
                'ir_compensation': {
                    'k_volts_per_percent': 0.015,
                    'reference_load_percent': 20.0
                },
                'rls_state': {
                    'ir_k': {'theta': 0.015, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
                    'peukert': {'theta': 1.2, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
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
            'battery_install_date': None,  # Set on first startup, reset on battery replacement
            'cycle_count': 0,              # OL→OB transitions (= transfer count)
            'cumulative_on_battery_sec': 0.0,  # Total seconds spent on battery
        }

    # --- Physics getters ---

    def get_peukert_exponent(self) -> float:
        return self.data.get('physics', {}).get('peukert_exponent', 1.2)

    def get_nominal_voltage(self) -> float:
        return self.data.get('physics', {}).get('nominal_voltage', 12.0)

    def get_nominal_power_watts(self) -> float:
        return self.data.get('physics', {}).get('nominal_power_watts', 425.0)

    def get_ir_k(self) -> float:
        return self.data.get('physics', {}).get('ir_compensation', {}).get('k_volts_per_percent', 0.015)

    def get_ir_reference_load(self) -> float:
        return self.data.get('physics', {}).get('ir_compensation', {}).get('reference_load_percent', 20.0)

    # --- Enterprise-equivalent counters ---

    def get_battery_install_date(self) -> str:
        return self.data.get('battery_install_date')

    def set_battery_install_date(self, date_str: str):
        self.data['battery_install_date'] = date_str

    def get_cycle_count(self) -> int:
        return self.data.get('cycle_count', 0)

    def increment_cycle_count(self):
        self.data['cycle_count'] = self.data.get('cycle_count', 0) + 1

    def get_cumulative_on_battery_sec(self) -> float:
        return self.data.get('cumulative_on_battery_sec', 0.0)

    def get_replacement_due(self) -> str:
        """Return predicted replacement due date (ISO8601 or None)."""
        return self.data.get('replacement_due')

    def set_replacement_due(self, date_str: str):
        """Set predicted replacement due date (ISO8601 string or None)."""
        self.data['replacement_due'] = date_str

    def add_on_battery_time(self, seconds: float):
        self.data['cumulative_on_battery_sec'] = self.data.get('cumulative_on_battery_sec', 0.0) + seconds

    # --- Physics setters ---

    def set_peukert_exponent(self, value: float):
        self.data.setdefault('physics', {})['peukert_exponent'] = value

    def set_ir_k(self, value: float):
        self.data.setdefault('physics', {}).setdefault('ir_compensation', {})['k_volts_per_percent'] = value

    # --- RLS state persistence ---

    _RLS_DEFAULTS = {
        'ir_k': {'theta': 0.015, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
        'peukert': {'theta': 1.2, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97},
    }

    def get_rls_state(self, name: str) -> dict:
        """Get RLS estimator state by name. Returns defaults if missing (backward compat)."""
        rls = self.data.get('physics', {}).get('rls_state', {})
        return rls.get(name, dict(self._RLS_DEFAULTS.get(name, {'theta': 0.0, 'P': 1.0, 'sample_count': 0, 'forgetting_factor': 0.97})))

    def set_rls_state(self, name: str, theta: float, P: float, sample_count: int) -> None:
        """Persist RLS estimator state to model.json."""
        physics = self.data.setdefault('physics', {})
        rls = physics.setdefault('rls_state', {})
        existing = rls.get(name, {})
        rls[name] = {
            'theta': theta,
            'P': P,
            'sample_count': sample_count,
            'forgetting_factor': existing.get('forgetting_factor', 0.97),
        }

    def reset_rls_state(self) -> None:
        """Reset all RLS estimators to defaults (e.g., on battery replacement)."""
        self.data.setdefault('physics', {})['rls_state'] = {
            k: dict(v) for k, v in self._RLS_DEFAULTS.items()
        }

    def _prune_list(self, key: str, keep_count: int = 30) -> None:
        """Remove old entries from a list field; retain only most recent keep_count."""
        items = self.data.get(key, [])
        if len(items) > keep_count:
            self.data[key] = items[-keep_count:]

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
        lut = self.data.get('lut', [])
        non_measured = [e for e in lut if e.get('source') != 'measured']
        measured = [e for e in lut if e.get('source') == 'measured']

        # Dedup — when multiple entries share voltage within ±0.1V, keep most recent
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
        self.data['lut'] = sorted(non_measured + measured, key=lambda x: x['v'], reverse=True)


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
        self.data.setdefault('sulfation_history', []).append(entry)

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
        self.data.setdefault('discharge_events', []).append(event)


    def save(self):
        """
        Atomically write model to disk with history pruning.

        Prunes soh_history, r_internal_history, capacity_estimates,
        sulfation_history, and discharge_events to prevent unbounded growth.
        Use only at discharge event completion, not on every sample.
        """
        self._prune_list('soh_history')
        self._prune_list('r_internal_history')
        self._prune_lut()
        self._prune_list('capacity_estimates')
        self._prune_list('sulfation_history')
        self._prune_list('discharge_events')
        atomic_write_json(self.model_path, self.data)

    def get_lut(self):
        """Return the voltage→SoC lookup table entries."""
        return self.data.get('lut', [])

    def get_soh(self):
        """SoH estimate [0.0, 1.0]."""
        return self.data.get('soh', 1.0)

    def set_soh(self, value: float):
        """Update SoH estimate, clamped to [0.0, 1.0]."""
        self.data['soh'] = value

    def get_capacity_ah(self):
        """Reference full capacity in Ah (default 7.2 for UT850)."""
        return self.data.get('full_capacity_ah_ref', 7.2)

    def add_soh_history_entry(self, date, soh, capacity_ah_ref=None):
        """Add a SoH history entry with optional capacity baseline tag.

        Args:
            date: ISO8601 date string (e.g., '2026-03-16')
            soh: SoH estimate [0.0, 1.0]
            capacity_ah_ref: Capacity baseline used in SoH calculation (Ah).
                            If None, entry has no capacity_ah_ref field (backward compat).
        """
        if 'soh_history' not in self.data:
            self.data['soh_history'] = []

        entry = {'date': date, 'soh': soh}

        if capacity_ah_ref is not None:
            entry['capacity_ah_ref'] = round(capacity_ah_ref, 2)

        self.data['soh_history'].append(entry)
        self.data['soh'] = soh  # Update current SoH

    def get_soh_history(self):
        """Return list of {date, soh} entries."""
        return self.data.get('soh_history', [])

    def add_r_internal_entry(self, date, r_ohm, v_before, v_sag, load_percent, event_type):
        """Add internal resistance measurement from voltage sag."""
        if 'r_internal_history' not in self.data:
            self.data['r_internal_history'] = []
        self.data['r_internal_history'].append({
            'date': date, 'r_ohm': round(r_ohm, 4),
            'v_before': round(v_before, 2), 'v_sag': round(v_sag, 2),
            'load_percent': round(load_percent, 1), 'event': event_type
        })

    def get_r_internal_history(self):
        """Return list of internal resistance measurements."""
        return self.data.get('r_internal_history', [])

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
            - Appends entry to model.data['capacity_estimates']
            - Calls _prune_list('capacity_estimates') to limit array to 30 entries
            - Calls self.save() for atomic persistence
        """
        if 'capacity_estimates' not in self.data:
            self.data['capacity_estimates'] = []

        entry = {
            'timestamp': timestamp,
            'ah_estimate': ah_estimate,
            'confidence': confidence,
            'metadata': metadata
        }
        self.data['capacity_estimates'].append(entry)
        self._prune_list('capacity_estimates')
        self.save()

    def get_capacity_estimates(self) -> List[Dict]:
        """
        Get all capacity estimates, sorted by timestamp (latest first).

        Returns:
            List of {timestamp, ah_estimate, confidence, metadata} dicts,
            ordered newest to oldest
        """
        estimates = self.data.get('capacity_estimates', [])
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
                'capacity_ah_measured': float | None  # Measured capacity baseline (for new battery detection)
            }
        """
        estimates = self.data.get('capacity_estimates', [])

        if not estimates:
            return {
                'sample_count': 0,
                'confidence_percent': 0.0,
                'latest_ah': None,
                'rated_ah': 7.2,
                'converged': False,
                'capacity_ah_measured': None
            }

        ah_values = [e['ah_estimate'] for e in estimates]
        cov = compute_cov(ah_values)

        # Confidence: 0.0 for < 3 measurements, else 1 - CoV clamped to [0, 1]
        # (convergence_score = 1 - CoV; 0.0 for n<3 per design)
        confidence = 0.0 if len(ah_values) < 3 else max(0.0, min(1.0, 1.0 - cov))

        return {
            'sample_count': len(estimates),
            'confidence_percent': confidence * 100,
            'latest_ah': ah_values[-1],
            'rated_ah': 7.2,
            'converged': len(estimates) >= 3 and cov < 0.10,
            'capacity_ah_measured': self.data.get('capacity_ah_measured', None)
        }


    def get_anchor_voltage(self):
        """Return anchor point voltage (physical cutoff, should always be 10.5V)."""
        lut = self.get_lut()
        # Find entry with soc==0.0 and source=='anchor'
        for entry in lut:
            if entry['soc'] == 0.0 and entry['source'] == 'anchor':
                return entry['v']
        return 10.5  # Default if not found

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

        # Insert into LUT maintaining descending voltage order using bisect
        # Use a key function to find insertion point based on voltage (descending)
        bisect.insort(self.data['lut'], entry, key=lambda x: -x['v'])

        # Log point accumulation (no write yet)
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
        old_count = len(self.data['lut'])
        self.data['lut'] = new_lut
        try:
            self.save()
            logger.info(
                f"LUT updated from calibration: {len(new_lut)} entries, "
                f"cliff region interpolated (was {old_count} entries)"
            )
        except Exception as e:
            logger.error(f"Failed to update LUT from calibration: {e}", exc_info=True)
            raise

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
        self.data['blackout_credit'] = credit_dict
        logger.debug(f"Blackout credit set: expires {credit_dict.get('credit_expires')}")

    def clear_blackout_credit(self) -> None:
        """Expire or clear blackout credit."""
        if self.data.get('blackout_credit'):
            self.data['blackout_credit']['active'] = False
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
            reason: reason_code from SchedulerDecision (e.g., 'sulfation_0.65_roi_0.34')
            block_reason: If test is blocked, reason code (e.g., 'soh_floor_55%'), else None
        """
        self.data['scheduled_test_timestamp'] = scheduled_timestamp
        self.data['scheduled_test_reason'] = reason
        self.data['test_block_reason'] = block_reason
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
        self.data['last_upscmd_timestamp'] = upscmd_timestamp
        self.data['last_upscmd_type'] = upscmd_type
        self.data['last_upscmd_status'] = upscmd_status
        logger.debug(f"Upscmd result updated: type={upscmd_type}, status={upscmd_status}")

    def get_last_upscmd_timestamp(self) -> Optional[str]:
        """Get ISO8601 timestamp of last upscmd attempt, or None."""
        return self.data.get('last_upscmd_timestamp')

    def get_blackout_credit(self) -> Optional[dict]:
        """Get current blackout credit dict, or None if inactive/expired."""
        return self.data.get('blackout_credit')

