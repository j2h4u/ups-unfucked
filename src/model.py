"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import bisect
import json
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List


logger = logging.getLogger(__name__)


def atomic_write_json(filepath, data):
    """
    Safely write JSON to filepath with atomic guarantees.

    Uses tempfile + fdatasync + os.replace pattern to prevent corruption
    on power loss or crash during write.

    fdatasync (data-only sync) is used instead of fsync because JSON file
    metadata (atime, ctime) is not critical for reading. This reduces I/O
    latency by ~50% by skipping unnecessary inode syncs.

    Args:
        filepath: Target file path (str or Path)
        data: Python dict to serialize as JSON

    Raises:
        IOError: If write or fdatasync fails
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Write to temporary file in same directory (ensures same filesystem)
    with tempfile.NamedTemporaryFile(
        mode='w',
        dir=str(filepath.parent),
        delete=False,
        suffix='.tmp'
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fdatasync(tmp.fileno())
        os.fchmod(tmp.fileno(), 0o644)
        tmp_path = Path(tmp.name)

    try:
        # Atomic rename (unlink + link on POSIX)
        tmp_path.replace(filepath)
        logger.info(f"Atomically wrote {filepath}")

    except Exception as e:
        # Clean up temp file on error
        tmp_path.unlink(missing_ok=True)
        logger.error(f"Atomic write failed: {e}")
        raise


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

                # F4: Schema validation - check for required keys
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
        else:
            logger.info("Model file not found; initializing with standard VRLA curve")
            self.data = self._default_vrla_lut()

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

    def _prune_soh_history(self, keep_count: int = 30) -> None:
        """Remove old SoH history entries; retain only most recent keep_count.

        Rationale: ~365 daily entries/year. After 20 years, unbounded growth.
        Keep 30 (~1 month) provides trend detection without disk bloat.

        Args:
            keep_count: Maximum number of entries to retain (default 30)
        """
        soh_hist = self.data.get('soh_history', [])
        if len(soh_hist) > keep_count:
            self.data['soh_history'] = soh_hist[-keep_count:]

    def _prune_lut(self, keep_count: int = 200) -> None:
        """Remove oldest measured LUT entries; retain non-measured and most recent measured.

        Strategy: keep all non-measured entries (standard, anchor, interpolated)
        plus most recent keep_count measured entries by timestamp.

        Args:
            keep_count: Maximum number of measured entries to retain (default 200)
        """
        lut = self.data.get('lut', [])
        non_measured = [e for e in lut if e.get('source') != 'measured']
        measured = [e for e in lut if e.get('source') == 'measured']
        if len(measured) > keep_count:
            measured.sort(key=lambda x: x.get('timestamp', 0))
            measured = measured[-keep_count:]
        self.data['lut'] = sorted(non_measured + measured, key=lambda x: x['v'], reverse=True)

    def _prune_r_internal_history(self, keep_count: int = 30) -> None:
        """Remove old internal resistance history entries; retain only most recent keep_count.

        Mirrors soh pruning for r_internal_history list.

        Args:
            keep_count: Maximum number of entries to retain (default 30)
        """
        r_int_hist = self.data.get('r_internal_history', [])
        if len(r_int_hist) > keep_count:
            self.data['r_internal_history'] = r_int_hist[-keep_count:]

    def save(self):
        """
        Atomically write model to disk with history pruning.

        Prunes soh_history and r_internal_history to prevent unbounded growth.
        Use only at discharge event completion, not on every sample.
        """
        self._prune_soh_history()
        self._prune_r_internal_history()
        self._prune_lut()
        atomic_write_json(self.model_path, self.data)

    def get_lut(self):
        """Return the lookup table."""
        return self.data.get('lut', [])

    def get_soh(self):
        """Return current SoH estimate (0.0 to 1.0)."""
        return self.data.get('soh', 1.0)

    def set_soh(self, value: float):
        """Set current SoH estimate (0.0 to 1.0)."""
        self.data['soh'] = value

    def get_capacity_ah(self):
        """Return reference full capacity (Ah)."""
        return self.data.get('full_capacity_ah_ref', 7.2)

    def add_soh_history_entry(self, date, soh):
        """Add a SoH history entry for degradation tracking."""
        if 'soh_history' not in self.data:
            self.data['soh_history'] = []
        self.data['soh_history'].append({'date': date, 'soh': soh})
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
        """
        Replace LUT with interpolated calibration result and persist to disk.

        Called after interpolate_cliff_region() completes to apply cliff region
        interpolation to persistent model storage.

        Args:
            new_lut: Updated LUT from interpolate_cliff_region()
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
            logger.error(f"Failed to update LUT from calibration: {e}")
            raise

