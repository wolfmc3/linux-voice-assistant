"""VL53L0X distance reader."""

from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)
VL53L0X_CAL_SCALE = 0.966
VL53L0X_CAL_OFFSET_MM = -21.0


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
            self._sensor = sensor
            self._available = True
            _LOGGER.info("VL53L0X reader initialized")
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
