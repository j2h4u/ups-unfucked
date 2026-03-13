"""Battery model persistence with atomic JSON writes and VRLA LUT initialization."""

import json
import os
import tempfile
import logging
from pathlib import Path
from typing import Dict, Any, List


logger = logging.getLogger(__name__)


def atomic_write_json(filepath, data):
    """
    Safely write JSON to filepath with atomic guarantees.

    Uses tempfile + fsync + os.replace pattern to prevent corruption
    on power loss or crash during write.

    Args:
        filepath: Target file path (str or Path)
        data: Python dict to serialize as JSON

    Raises:
        IOError: If write or fsync fails
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
        tmp_path = Path(tmp.name)

    try:
        # Flush to disk (force kernel to write buffers)
        fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

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
                {'date': '2026-03-13', 'soh': 1.0}
            ]
        }

    def save(self):
        """
        Atomically write model to disk.

        Use only at discharge event completion, not on every sample.
        """
        atomic_write_json(self.model_path, self.data)

    def get_lut(self):
        """Return the lookup table."""
        return self.data.get('lut', [])

    def get_soh(self):
        """Return current SoH estimate (0.0 to 1.0)."""
        return self.data.get('soh', 1.0)

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
        Write calibration datapoint to model.json with fsync during BLACKOUT_TEST.

        Called from monitor.py discharge buffer handler to capture intermediate
        measurements in real-time (fsync cost acceptable for one-time calibration).

        Args:
            voltage: Measured battery voltage (V)
            soc: Calculated SoC as fraction (0.0-1.0)
            timestamp: Unix timestamp of measurement

        Raises:
            IOError: If atomic write fails
        """
        # Check for duplicates within ±0.01V tolerance
        existing = [e for e in self.data['lut'] if abs(e['v'] - voltage) < 0.01]
        if existing:
            return  # Skip duplicate

        # Add new entry
        self.data['lut'].append({
            'v': round(voltage, 2),
            'soc': round(soc, 3),
            'source': 'measured',
            'timestamp': timestamp
        })

        # Sort LUT descending by voltage (maintain consistency with save())
        self.data['lut'].sort(key=lambda x: x['v'], reverse=True)

        # Atomic write with fsync
        logger.info(f"Calibration write: voltage={voltage:.2f}V, soc={soc:.1%}, timestamp={timestamp}")
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

