#!/usr/bin/env python3
"""Front panel daemon: touch keys + rotary encoder -> IPC commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import load_config, resolve_repo_path
from ..gpio_controller import LvaGpioController
from ..local_ipc import (
    CONTROL_SOCKET_PATH,
    GPIO_EVENT_SOCKET_PATH,
    IPC_DIR,
    normalize_message,
    send_ipc_message,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PinState:
    level: int = 1
    changed_at: float = 0.0
    fired: bool = False


class _IpcEventProtocol(asyncio.DatagramProtocol):
    def __init__(self, daemon: "FrontPanelDaemon") -> None:
        self._daemon = daemon

    def datagram_received(self, data: bytes, _addr) -> None:  # type: ignore[override]
        try:
            packet = json.loads(data.decode("utf-8"))
            if not isinstance(packet, dict):
                return
            message = normalize_message(packet, default_source="core")
            if message is None:
                return
            self._daemon.handle_event_message(message)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("frontpaneld event decode failed: %s", err)


class FrontPanelDaemon:
    def __init__(
        self,
        *,
        mute_pin: int,
        vol_up_pin: int,
        vol_down_pin: int,
        enc_a_pin: int,
        enc_b_pin: int,
        enable_gpio_control: bool,
        preferences_path: Path,
        feedback_sound_path: Path,
        feedback_sound_device: str,
    ) -> None:
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_transport: asyncio.transports.DatagramTransport | None = None
        self._pins = {
            mute_pin: PinState(),
            vol_up_pin: PinState(),
            vol_down_pin: PinState(),
        }
        self._mute_pin = mute_pin
        self._vol_up_pin = vol_up_pin
        self._vol_down_pin = vol_down_pin
        self._enc_a_pin = enc_a_pin
        self._enc_b_pin = enc_b_pin
        self._gpio = None
        self._encoder_last_state = 0
        self._encoder_acc = 0
        self._encoder_last_emit = 0.0
        self._gpio_controller: LvaGpioController | None = None
        self._enable_gpio_control = enable_gpio_control
        self._preferences_path = preferences_path
        self._feedback_sound_path = feedback_sound_path
        self._feedback_sound_device = feedback_sound_device

    def _setup_gpio(self) -> None:
        import RPi.GPIO as gpio  # type: ignore

        gpio.setwarnings(False)
        gpio.setmode(gpio.BCM)
        for pin in (self._mute_pin, self._vol_up_pin, self._vol_down_pin, self._enc_a_pin, self._enc_b_pin):
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)
        self._gpio = gpio
        self._encoder_last_state = self._read_encoder_state()
        now = time.monotonic()
        for pin in self._pins:
            self._pins[pin] = PinState(level=int(gpio.input(pin)), changed_at=now, fired=False)

    def _cleanup_gpio(self) -> None:
        if self._gpio is None:
            return
        try:
            self._gpio.cleanup()
        except Exception:  # noqa: BLE001
            pass
        self._gpio = None

    def _read_pin(self, pin: int) -> int:
        assert self._gpio is not None
        return int(self._gpio.input(pin))

    def _read_encoder_state(self) -> int:
        a = self._read_pin(self._enc_a_pin)
        b = self._read_pin(self._enc_b_pin)
        return ((a & 1) << 1) | (b & 1)

    def _send(self, msg_type: str, payload: dict[str, object] | None = None) -> None:
        try:
            send_ipc_message(CONTROL_SOCKET_PATH, msg_type, payload or {}, source="frontpaneld")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("frontpanel send failed (%s): %s", msg_type, err)

    def _handle_touch(self, pin: int) -> None:
        if pin == self._mute_pin:
            self._send("MUTE_TOGGLE")
        elif pin == self._vol_up_pin:
            self._send("VOLUME_STEP", {"steps": 1})
        elif pin == self._vol_down_pin:
            self._send("VOLUME_STEP", {"steps": -1})

    def _poll_touch(self, now: float) -> None:
        for pin, state in self._pins.items():
            level = self._read_pin(pin)
            if level != state.level:
                state.level = level
                state.changed_at = now
                if level == 1:
                    state.fired = False
                continue

            if (level == 0) and (not state.fired) and ((now - state.changed_at) >= 0.05):
                self._handle_touch(pin)
                state.fired = True

    def _poll_encoder(self, now: float) -> None:
        state = self._read_encoder_state()
        if state == self._encoder_last_state:
            return

        transition = (self._encoder_last_state << 2) | state
        direction = 0
        if transition in (0b0001, 0b0111, 0b1110, 0b1000):
            direction = 1
        elif transition in (0b0010, 0b0100, 0b1101, 0b1011):
            direction = -1
        self._encoder_last_state = state
        if direction == 0:
            return

        self._encoder_acc += direction
        if abs(self._encoder_acc) < 2:
            return

        if (now - self._encoder_last_emit) < 0.05:
            return

        emit_dir = 1 if self._encoder_acc > 0 else -1
        self._encoder_acc = 0
        self._encoder_last_emit = now
        self._send("VOLUME_DELTA", {"steps": emit_dir * 2})

    async def _start_event_listener(self) -> None:
        assert self._loop is not None
        IPC_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(IPC_DIR, 0o777)
        except Exception:  # noqa: BLE001
            pass

        if GPIO_EVENT_SOCKET_PATH.exists():
            GPIO_EVENT_SOCKET_PATH.unlink()

        transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _IpcEventProtocol(self),
            local_addr=str(GPIO_EVENT_SOCKET_PATH),
            family=socket.AF_UNIX,
        )
        self._event_transport = transport
        try:
            os.chmod(GPIO_EVENT_SOCKET_PATH, 0o666)
        except Exception:  # noqa: BLE001
            pass

    def _stop_event_listener(self) -> None:
        if self._event_transport is not None:
            self._event_transport.close()
            self._event_transport = None
        try:
            if GPIO_EVENT_SOCKET_PATH.exists():
                GPIO_EVENT_SOCKET_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

    def handle_event_message(self, message: dict[str, object]) -> None:
        if self._gpio_controller is None:
            return
        self._gpio_controller.on_ipc_event(message)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._setup_gpio()
        await self._start_event_listener()
        if self._enable_gpio_control:
            self._gpio_controller = LvaGpioController(
                ipc_bridge=None,
                preferences_path=self._preferences_path,
                feedback_sound_path=self._feedback_sound_path,
                feedback_sound_device=self._feedback_sound_device,
            )
            await self._gpio_controller.start()
            _LOGGER.info("frontpaneld integrated GPIO/LED controller started")
        else:
            _LOGGER.info("GPIO/LED controller disabled by config")
        self._running = True
        _LOGGER.info("frontpaneld started")
        try:
            while self._running:
                now = time.monotonic()
                self._poll_touch(now)
                self._poll_encoder(now)
                await asyncio.sleep(0.01)
        finally:
            if self._gpio_controller is not None:
                await self._gpio_controller.shutdown()
                self._gpio_controller = None
            self._stop_event_listener()
            self._cleanup_gpio()

    def stop(self) -> None:
        self._running = False


async def main() -> None:
    app_config = load_config()
    config = app_config.frontpaneld
    core_config = app_config.core
    log_level_name = str(config.log_level).strip().upper()
    logging.basicConfig(level=getattr(logging, log_level_name, logging.INFO))
    daemon = FrontPanelDaemon(
        mute_pin=config.mute_pin,
        vol_up_pin=config.vol_up_pin,
        vol_down_pin=config.vol_down_pin,
        enc_a_pin=config.enc_a_pin,
        enc_b_pin=config.enc_b_pin,
        enable_gpio_control=bool(core_config.enable_gpio_control),
        preferences_path=resolve_repo_path(core_config.preferences_file),
        feedback_sound_path=resolve_repo_path(core_config.processing_sound),
        feedback_sound_device=core_config.gpio_feedback_device,
    )
    try:
        await daemon.run()
    except KeyboardInterrupt:
        daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    asyncio.run(main())
