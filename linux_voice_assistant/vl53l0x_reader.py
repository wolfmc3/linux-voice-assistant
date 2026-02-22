"""VL53L0X distance reader."""

from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)
# For long-range operation we avoid compressing far-end values with calibration
# and increase sensitivity via slower/high-budget measurements.
VL53L0X_CAL_SCALE = 1.0
VL53L0X_CAL_OFFSET_MM = 0.0
VL53L0X_LONG_RANGE_SIGNAL_RATE_LIMIT_MCPS = 0.05
VL53L0X_LONG_RANGE_TIMING_BUDGET_MS = 330


class Vl53l0xReader:
    """Read distance from a VL53L0X over I2C.

    Uses Adafruit driver when available.
    """

    def __init__(self) -> None:
        self._sensor = None
        self._available = False
        self._last_read_error: Optional[str] = None
        self._init_sensor()

    @property
    def available(self) -> bool:
        return self._available

    def _init_sensor(self) -> None:
        try:
            import board  # type: ignore
            import busio  # type: ignore
            import adafruit_vl53l0x  # type: ignore

            i2c = busio.I2C(board.SCL, board.SDA)
            sensor = adafruit_vl53l0x.VL53L0X(i2c)
            # Long-range profile: favor sensitivity over speed to improve
            # reliability around ~2m on reflective targets.
            sensor.signal_rate_limit = VL53L0X_LONG_RANGE_SIGNAL_RATE_LIMIT_MCPS
            sensor.measurement_timing_budget = int(VL53L0X_LONG_RANGE_TIMING_BUDGET_MS * 1000)
            self._sensor = sensor
            self._available = True
            _LOGGER.info(
                "VL53L0X reader initialized (long-range: signal_rate_limit=%.2f MCPS, timing_budget=%sms)",
                VL53L0X_LONG_RANGE_SIGNAL_RATE_LIMIT_MCPS,
                VL53L0X_LONG_RANGE_TIMING_BUDGET_MS,
            )
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("VL53L0X unavailable: %s", err)

        self._available = False
        self._sensor = None

    def read_distance_mm(self) -> Optional[float]:
        if (not self._available) or (self._sensor is None):
            return None

        try:
            value = float(self._sensor.range)
        except Exception as err:  # noqa: BLE001
            err_text = str(err)
            if err_text != self._last_read_error:
                _LOGGER.warning("VL53L0X read failed: %s", err)
                self._last_read_error = err_text
            return None

        self._last_read_error = None
        # VL53L0X commonly returns ~8191mm when target is out of range.
        if (value <= 0.0) or (value >= 8190.0):
            return None
        corrected = (value * VL53L0X_CAL_SCALE) + VL53L0X_CAL_OFFSET_MM
        if corrected <= 0.0:
            return None
        return corrected

    def read_mm(self) -> Optional[float]:
        """Compatibility alias used by the distance activation logic."""
        return self.read_distance_mm()

    def set_timing_budget_ms(self, budget_ms: int) -> bool:
        if (not self._available) or (self._sensor is None):
            return False
        try:
            # Adafruit driver expects microseconds.
            self._sensor.measurement_timing_budget = int(max(5, budget_ms) * 1000)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("VL53L0X timing budget unsupported: %s", err)
            return False

    def set_intermeasurement_ms(self, intermeasurement_ms: int) -> bool:
        # Not supported by this driver wrapper.
        _LOGGER.debug(
            "VL53L0X intermeasurement not supported (requested=%sms)",
            intermeasurement_ms,
        )
        return False
