"""VL53L1X distance reader with resilient I2C handling."""

from __future__ import annotations

import logging
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)
VL53L1X_MAX_MM = 4000.0


class Vl53l1xReader:
    """Read distance from a VL53L1X over I2C."""

    def __init__(self, *, max_read_retries: int = 2, reinit_cooldown_s: float = 1.0) -> None:
        self._sensor = None
        self._available = False
        self._last_error: Optional[str] = None
        self._max_read_retries = max(0, int(max_read_retries))
        self._reinit_cooldown_s = max(0.1, float(reinit_cooldown_s))
        self._last_reinit_at = 0.0
        self._i2c = None
        self._init_sensor()

    @property
    def available(self) -> bool:
        return self._available

    def _init_sensor(self) -> None:
        try:
            import board  # type: ignore
            import busio  # type: ignore
            import adafruit_vl53l1x  # type: ignore

            i2c = busio.I2C(board.SCL, board.SDA)
            sensor = adafruit_vl53l1x.VL53L1X(i2c)
            try:
                sensor.distance_mode = 2
            except Exception:  # noqa: BLE001
                pass
            sensor.start_ranging()
            self._i2c = i2c
            self._sensor = sensor
            self._available = True
            self._last_error = None
            _LOGGER.info("VL53L1X reader initialized")
            return
        except Exception as err:  # noqa: BLE001
            err_text = str(err)
            if err_text != self._last_error:
                _LOGGER.warning("VL53L1X unavailable: %s", err)
                self._last_error = err_text

        self._sensor = None
        self._i2c = None
        self._available = False

    def _maybe_reinit(self) -> None:
        now = time.monotonic()
        if (now - self._last_reinit_at) < self._reinit_cooldown_s:
            return
        self._last_reinit_at = now
        self._init_sensor()

    def _read_once(self) -> Optional[float]:
        if (not self._available) or (self._sensor is None):
            return None

        try:
            if not bool(getattr(self._sensor, "data_ready", True)):
                return None
            value_cm = getattr(self._sensor, "distance", None)
            if value_cm is None:
                return None
            value = float(value_cm) * 10.0
            try:
                self._sensor.clear_interrupt()
            except Exception:  # noqa: BLE001
                pass
        except Exception as err:  # noqa: BLE001
            err_text = str(err)
            if err_text != self._last_error:
                _LOGGER.warning("VL53L1X read failed: %s", err)
                self._last_error = err_text
            return None

        self._last_error = None
        if (value <= 0.0) or (value >= VL53L1X_MAX_MM):
            return None
        return value

    def read_distance_mm(self) -> Optional[float]:
        if not self._available:
            self._maybe_reinit()
            if not self._available:
                return None

        for attempt in range(self._max_read_retries + 1):
            value = self._read_once()
            if value is not None:
                return value
            if attempt < self._max_read_retries:
                continue

        self._maybe_reinit()
        return None

    def read_mm(self) -> Optional[float]:
        return self.read_distance_mm()

    def set_timing_budget_ms(self, budget_ms: int) -> bool:
        if self._sensor is None:
            return False
        try:
            self._sensor.timing_budget = int(budget_ms)
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("VL53L1X timing budget unsupported: %s", budget_ms)
            return False

    def set_intermeasurement_ms(self, intermeasurement_ms: int) -> bool:
        if self._sensor is None:
            return False
        try:
            self._sensor.inter_measurement = int(intermeasurement_ms)
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("VL53L1X intermeasurement unsupported: %s", intermeasurement_ms)
            return False
