import time
import signal
import sys
import os
import logging
from pathlib import Path
from systemd.journal import JournalHandler

from src.nut_client import NUTClient
from src.ema_ring_buffer import EMABuffer, ir_compensate
from src.model import BatteryModel
from src.soc_predictor import soc_from_voltage, charge_percentage
from src.runtime_calculator import runtime_minutes

# === INLINE CONFIGURATION (H2 fix: removed src/config.py) ===
POLL_INTERVAL = int(os.getenv('UPS_MONITOR_POLL_INTERVAL', '10'))
MODEL_DIR = Path(os.getenv('UPS_MONITOR_MODEL_DIR', str(Path.home() / '.config' / 'ups-battery-monitor')))
MODEL_PATH = MODEL_DIR / 'model.json'
NUT_HOST = os.getenv('UPS_MONITOR_NUT_HOST', 'localhost')
NUT_PORT = int(os.getenv('UPS_MONITOR_NUT_PORT', '3493'))
NUT_TIMEOUT = float(os.getenv('UPS_MONITOR_NUT_TIMEOUT', '2.0'))
UPS_NAME = os.getenv('UPS_MONITOR_UPS_NAME', 'cyberpower')
EMA_WINDOW = int(os.getenv('UPS_MONITOR_EMA_WINDOW', '120'))
IR_K = float(os.getenv('UPS_MONITOR_IR_K', '0.015'))
IR_L_BASE = float(os.getenv('UPS_MONITOR_IR_BASE', '20.0'))

# === INLINE LOGGING SETUP (H3 fix: removed src/logger.py) ===
logger = logging.getLogger('ups-battery-monitor')
logger.setLevel(logging.INFO)
logger.handlers.clear()

try:
    handler = JournalHandler()
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)
except Exception as e:
    # Fallback to stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)


class MonitorDaemon:
    """
    Main daemon for UPS battery monitoring.

    Polls NUT upsd, applies EMA smoothing, tracks battery state.
    """

    def __init__(self):
        """
        Initialize daemon with inline configuration.
        """
        self.running = True

        # Create model directory
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.nut_client = NUTClient(
            host=NUT_HOST,
            port=NUT_PORT,
            timeout=NUT_TIMEOUT,
            ups_name=UPS_NAME
        )

        self.ema_buffer = EMABuffer(
            window_sec=EMA_WINDOW,
            poll_interval_sec=POLL_INTERVAL
        )

        self.battery_model = BatteryModel(MODEL_PATH)

        # Metrics tracking for current battery state
        self.current_metrics = {
            "soc": None,
            "battery_charge": None,
            "time_rem_minutes": None,
            "timestamp": None,
        }
        self.last_soc = None
        self.last_time_rem = None

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(f"Daemon initialized: poll={POLL_INTERVAL}s, model={MODEL_PATH}, nut={NUT_HOST}:{NUT_PORT}")

        # H1 fix: Check NUT connectivity at startup
        self._check_nut_connectivity()

    def _check_nut_connectivity(self):
        """
        Verify NUT upsd is reachable before entering main loop.
        Only 4 lines as specified.
        """
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info("NUT upsd reachable, polling started")
        except Exception:
            logger.warning(f"NUT upsd unreachable at startup, will retry every {POLL_INTERVAL}s")

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT gracefully."""
        logger.info(f"Received signal {signum}; shutting down")
        self.running = False

    def run(self):
        """
        Main polling loop.

        Polls UPS every POLL_INTERVAL seconds, updates EMA, logs metrics.
        Runs until SIGTERM or SIGINT received.
        """
        logger.info("Starting main polling loop")
        poll_count = 0
        was_stabilized = False

        while self.running:
            try:
                # Poll UPS
                timestamp = time.time()
                ups_data = self.nut_client.get_ups_vars()

                # Extract voltage and load
                voltage = ups_data.get('battery.voltage')
                load = ups_data.get('ups.load')

                if voltage is not None and load is not None:
                    # Add to EMA buffer
                    self.ema_buffer.add_sample(timestamp, voltage, load)
                    poll_count += 1

                    # L1 fix: Log when EMA stabilizes
                    if self.ema_buffer.stabilized and not was_stabilized:
                        logger.info(f"EMA buffer stabilized after {poll_count} samples, IR compensation active")
                        was_stabilized = True

                    # Log every 6 polls (60 seconds at 10-sec interval)
                    if poll_count % 6 == 0:
                        v_ema = self.ema_buffer.voltage
                        l_ema = self.ema_buffer.load
                        stabilized = self.ema_buffer.stabilized

                        # Apply IR compensation if stabilized
                        if stabilized:
                            v_norm = ir_compensate(
                                v_ema, l_ema,
                                IR_L_BASE,
                                IR_K
                            )
                        else:
                            v_norm = None

                        # Calculate SoC and battery charge from normalized voltage
                        if stabilized and v_norm is not None:
                            soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
                            battery_charge = charge_percentage(soc)

                            # Calculate remaining runtime using SoC and load
                            capacity_ah = self.battery_model.get_capacity_ah()
                            soh = self.battery_model.get_soh()
                            time_rem = runtime_minutes(soc, l_ema, capacity_ah, soh)

                            # Store metrics
                            self.current_metrics["soc"] = soc
                            self.current_metrics["battery_charge"] = battery_charge
                            self.current_metrics["time_rem_minutes"] = time_rem
                            self.current_metrics["timestamp"] = timestamp

                            # Log significant SoC changes (>5%)
                            if self.last_soc is None or abs(soc - self.last_soc) > 0.05:
                                logger.info(f"SoC updated: {self.last_soc*100:.0f}% → {soc*100:.0f}%")
                                self.last_soc = soc

                            # Log runtime changes
                            if self.last_time_rem is None or abs(time_rem - self.last_time_rem) > 1.0:
                                logger.info(f"Remaining runtime: {time_rem:.1f} minutes")
                                self.last_time_rem = time_rem
                        else:
                            battery_charge = None
                            time_rem = None

                        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
                        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
                        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
                        logger.info(
                            f"Poll {poll_count}: V_ema={v_ema:.2f}V, "
                            f"L_ema={l_ema:.1f}%, "
                            f"V_norm={v_norm_str}, "
                            f"charge={charge_str}, "
                            f"time_rem={time_rem_str}, "
                            f"stabilized={stabilized}"
                        )
                else:
                    logger.warning(f"Poll {poll_count}: Missing voltage or load data")

                # Sleep until next poll
                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}", exc_info=True)
                time.sleep(POLL_INTERVAL)

        logger.info("Polling loop ended; daemon shutting down")


def main():
    """Entry point for daemon."""
    try:
        daemon = MonitorDaemon()
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
