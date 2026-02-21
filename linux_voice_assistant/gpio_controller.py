"""Integrated GPIO controller for Linux Voice Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from gpiozero import Button
except Exception:  # noqa: BLE001
    Button = None  # type: ignore[assignment]

try:
    from rpi_ws281x import Color, PixelStrip  # type: ignore
except Exception:  # noqa: BLE001
    Color = None  # type: ignore[assignment]
    PixelStrip = None  # type: ignore[assignment]

from .local_ipc import CONTROL_SOCKET_PATH, LocalIpcBridge, build_message

BUTTON_MUTE_GPIO = 17
BUTTON_VOL_UP_GPIO = 22
BUTTON_VOL_DOWN_GPIO = 23

LED_GPIO = 12
LED_COUNT = 14
LED_BRIGHTNESS = 70
LED_TICK_SECONDS = 0.02
MIN_COLOR_KEEP_FACTOR = 0.02
READY_PULSE_HZ = 0.28
READY_PULSE_MIN = 0.52
READY_COLOR = (28.0, 125.0, 255.0)
LED_INTENSITY_DEFAULT_PERCENT = 100.0
LED_NIGHT_SCALE_DEFAULT_ENABLED = False
LED_NIGHT_SCALE_FACTOR = 0.35
POLL_PREFERENCES_SECONDS = 1.0

POLL_SERVICE_SECONDS = 1.0
POLL_AUDIO_SECONDS = 0.15
POLL_BUTTON_SECONDS = 0.03
ALSA_PLAYBACK_STATUS_ROOT = Path("/proc/asound")
ALSA_ACTIVE_STATES = {"RUNNING", "DRAINING"}
ALSA_ACTIVITY_HOLD_SECONDS = 0.35


@dataclass
class RuntimeState:
    service_active: bool = False
    ha_connected: bool = False
    muted: bool = False
    listening_until: float = 0.0
    playback_until: float = 0.0
    playback_started_at: float = 0.0
    audio_playback_until: float = 0.0


class LedMode:
    OFF = "off"
    READY = "ready"
    MUTED = "muted"
    LISTENING = "listening"
    PLAYBACK = "playback"


class Ws2812Bar:
    """Minimal WS2812B bar driver with state-based effects."""

    def __init__(self, pin: int, count: int, brightness: int) -> None:
        self.count = count
        self._enabled = bool(PixelStrip and Color)
        self._strip = None
        self._mode = LedMode.OFF
        self._mode_changed_at = 0.0
        self._last_frame: Optional[list[tuple[int, int, int]]] = None
        self._user_intensity_factor = 1.0
        self._night_scale_factor = 1.0
        self._temporal_quant_error: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(count)]

        if not self._enabled:
            logging.warning("WS2812B disabled: python package rpi_ws281x not available")
            return

        try:
            self._strip = PixelStrip(count, pin, 800000, 10, False, brightness, 0)
            self._strip.begin()
            self.off()
        except Exception as err:  # noqa: BLE001
            logging.warning("WS2812B init failed, LEDs disabled: %s", err)
            self._enabled = False
            self._strip = None

    def set_mode(self, mode: str, now: float) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._mode_changed_at = now
        self._reset_temporal_quantization()
        self._last_frame = None

    def tick(self, now: float) -> None:
        if not self._enabled or self._strip is None:
            return
        frame = self._build_frame(now)
        self._show(frame)

    def set_intensity_percent(self, value: float) -> None:
        factor = max(0.0, min(1.0, float(value) / 100.0))
        if abs(factor - self._user_intensity_factor) < 1e-4:
            return
        self._user_intensity_factor = factor
        self._reset_temporal_quantization()
        self._last_frame = None

    def set_night_mode(self, enabled: bool) -> None:
        factor = LED_NIGHT_SCALE_FACTOR if enabled else 1.0
        if abs(factor - self._night_scale_factor) < 1e-4:
            return
        self._night_scale_factor = factor
        self._reset_temporal_quantization()
        self._last_frame = None

    def off(self) -> None:
        if not self._enabled or self._strip is None:
            return
        self._show([(0, 0, 0)] * self.count)

    def close(self) -> None:
        self.off()

    def _build_frame(self, now: float) -> list[tuple[int, int, int]]:
        if self._mode == LedMode.OFF:
            return [(0, 0, 0)] * self.count

        global_intensity = self._user_intensity_factor * self._night_scale_factor

        if self._mode == LedMode.READY:
            frame = self._frame_supercar_ready_fixed()
        elif self._mode == LedMode.MUTED:
            frame = self._frame_supercar_muted(now)
        elif self._mode == LedMode.LISTENING:
            frame = self._frame_supercar_listening(now)
        else:
            frame = self._frame_supercar_playback(now)

        transition = now - self._mode_changed_at
        if transition < 0.30 and self._mode != LedMode.OFF:
            reveal = max(1, int((transition / 0.30) * self.count))
            frame = [frame[i] if i < reveal else (0, 0, 0) for i in range(self.count)]
        if global_intensity < 0.999:
            frame = self._scale_frame(frame, global_intensity)
        return frame

    def _frame_supercar_ready(self, now: float) -> list[tuple[int, int, int]]:
        factor = READY_PULSE_MIN + ((1.0 - READY_PULSE_MIN) * ((math.sin(now * math.tau * READY_PULSE_HZ) + 1.0) * 0.5))
        color = self._scale_rgb((int(READY_COLOR[0]), int(READY_COLOR[1]), int(READY_COLOR[2])), factor)
        return [color] * self.count

    def _frame_supercar_ready_fixed(self) -> list[tuple[int, int, int]]:
        color = (int(READY_COLOR[0]), int(READY_COLOR[1]), int(READY_COLOR[2]))
        return [color] * self.count

    def _frame_supercar_muted(self, now: float) -> list[tuple[int, int, int]]:
        frame = [(2, 0, 0)] * self.count
        breath = 0.20 + 0.80 * ((math.sin(now * 1.6) + 1.0) * 0.5)
        for idx in range(self.count):
            spatial = 0.72 + 0.28 * ((math.sin(now * 2.1 + (idx * 0.55)) + 1.0) * 0.5)
            frame[idx] = self._scale_rgb((255, 25, 0), breath * spatial)
        return frame

    def _frame_supercar_listening(self, now: float) -> list[tuple[int, int, int]]:
        frame = [(0, 7, 0)] * self.count
        pos = ((math.sin(now * 2.1) + 1.0) * 0.5) * (self.count - 1)
        for idx in range(self.count):
            dist = abs(idx - pos)
            if dist > 4.8:
                continue
            intensity = max(0.0, 1.0 - (dist / 4.8)) ** 2.1
            frame[idx] = self._scale_rgb((60, 255, 90), 0.35 + 0.65 * intensity)
        return frame

    def _frame_supercar_playback(self, now: float) -> list[tuple[int, int, int]]:
        level_raw = (
            0.50
            + 0.24 * math.sin(now * 6.9)
            + 0.18 * math.sin(now * 13.7 + 0.9)
            + 0.10 * math.sin(now * 27.5 + 2.2)
        )
        level = max(0.0, min(1.0, level_raw))
        frame = [(1, 0, 0)] * self.count
        c_left = (self.count - 1) // 2
        c_right = self.count // 2
        half = max(1, self.count // 2)
        segments = min(half, max(1, int(round(level * half))))
        per_led_decay = 0.82
        for offset in range(segments):
            intensity = per_led_decay**offset
            color = self._scale_rgb((255, 0, 0), intensity)
            li = c_left - offset
            ri = c_right + offset
            if 0 <= li < self.count:
                frame[li] = color
            if 0 <= ri < self.count:
                frame[ri] = color
        return frame

    def _scale_rgb(self, color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
        r, g, b = color
        rr = max(0, min(255, int(round(r * factor))))
        gg = max(0, min(255, int(round(g * factor))))
        bb = max(0, min(255, int(round(b * factor))))

        if factor > MIN_COLOR_KEEP_FACTOR:
            if r > 0 and rr == 0:
                rr = 1
            if g > 0 and gg == 0:
                gg = 1
            if b > 0 and bb == 0:
                bb = 1

        return (rr, gg, bb)

    def _reset_temporal_quantization(self) -> None:
        for idx in range(self.count):
            self._temporal_quant_error[idx][0] = 0.0
            self._temporal_quant_error[idx][1] = 0.0
            self._temporal_quant_error[idx][2] = 0.0

    def _quantize_temporal(self, value: float, led_idx: int, channel: int) -> int:
        clamped = max(0.0, min(255.0, value))
        accum = clamped + self._temporal_quant_error[led_idx][channel]
        quantized = int(accum)
        if quantized < 0:
            quantized = 0
        elif quantized > 255:
            quantized = 255
        self._temporal_quant_error[led_idx][channel] = accum - quantized
        return quantized

    def _scale_frame(self, frame: list[tuple[int, int, int]], factor: float) -> list[tuple[int, int, int]]:
        scaled: list[tuple[int, int, int]] = []
        for idx, (r, g, b) in enumerate(frame):
            scaled.append(
                (
                    self._quantize_temporal(r * factor, idx, 0),
                    self._quantize_temporal(g * factor, idx, 1),
                    self._quantize_temporal(b * factor, idx, 2),
                )
            )
        return scaled

    def _show(self, frame: list[tuple[int, int, int]]) -> None:
        if self._strip is None:
            return
        if frame == self._last_frame:
            return
        self._last_frame = frame.copy()
        for idx, (r, g, b) in enumerate(frame):
            self._strip.setPixelColor(idx, Color(r, g, b))
        self._strip.show()


class LvaGpioController:
    def __init__(
        self,
        ipc_bridge: Optional[LocalIpcBridge],
        preferences_path: Path,
        feedback_sound_path: Path,
        feedback_sound_device: str,
    ) -> None:
        self.state = RuntimeState()
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._next_service_poll = 0.0
        self._next_audio_poll = 0.0
        self._next_button_poll = 0.0
        self._next_preferences_poll = 0.0
        self._led_mode = ""
        self._led_intensity_percent = LED_INTENSITY_DEFAULT_PERCENT
        self._led_night_mode_enabled = LED_NIGHT_SCALE_DEFAULT_ENABLED
        self._preferences_mtime: Optional[float] = None

        self._ipc_bridge = ipc_bridge
        self._preferences_path = preferences_path
        self._feedback_sound_path = str(feedback_sound_path)
        self._feedback_sound_device = feedback_sound_device

        self.led = Ws2812Bar(LED_GPIO, LED_COUNT, LED_BRIGHTNESS)
        self._load_led_preferences(force=True)

        self.button_mute = None
        self.button_vol_up = None
        self.button_vol_down = None
        self._gpio_module = None
        self._gpio_polling_enabled = False
        self._last_button_level: dict[int, int] = {}
        self._last_button_at: dict[int, float] = {}
        self._button_fired: dict[int, bool] = {}
        if Button is None:
            logging.warning("GPIO buttons disabled: python package gpiozero not available")
        else:
            try:
                self.button_mute = Button(BUTTON_MUTE_GPIO, pull_up=True, bounce_time=0.08)
                self.button_vol_up = Button(BUTTON_VOL_UP_GPIO, pull_up=True, bounce_time=0.08)
                self.button_vol_down = Button(BUTTON_VOL_DOWN_GPIO, pull_up=True, bounce_time=0.08)

                self.button_mute.when_pressed = self._on_button_mute
                self.button_vol_up.when_pressed = self._on_button_volume_up
                self.button_vol_down.when_pressed = self._on_button_volume_down
            except Exception as err:  # noqa: BLE001
                logging.warning("gpiozero edge detection unavailable, enabling GPIO polling fallback: %s", err)
                self._enable_gpio_polling_fallback()

    async def start(self) -> None:
        if self._running:
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        if self._ipc_bridge is not None:
            self._ipc_bridge.add_event_listener(self.on_ipc_event)

        self._task = asyncio.create_task(self._run(), name="gpio-controller")

    async def shutdown(self) -> None:
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._ipc_bridge is not None:
            self._ipc_bridge.remove_event_listener(self.on_ipc_event)

        self.led.off()
        self.led.close()

        if self.button_mute is not None:
            self.button_mute.close()
        if self.button_vol_up is not None:
            self.button_vol_up.close()
        if self.button_vol_down is not None:
            self.button_vol_down.close()
        if self._gpio_module is not None:
            try:
                self._gpio_module.cleanup()
            except Exception:  # noqa: BLE001
                pass

    def _schedule_task(self, coro: Any) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(coro))

    def _on_button_mute(self) -> None:
        logging.info("Button MUTE pressed")
        self._schedule_task(self._send_command("mute_toggle"))

    def _on_button_volume_up(self) -> None:
        logging.info("Button VOL+ pressed")
        self._schedule_task(self._volume_up())

    def _on_button_volume_down(self) -> None:
        logging.info("Button VOL- pressed")
        self._schedule_task(self._volume_down())

    def _enable_gpio_polling_fallback(self) -> None:
        try:
            import RPi.GPIO as gpio  # type: ignore

            gpio.setwarnings(False)
            gpio.setmode(gpio.BCM)
            gpio.setup(BUTTON_MUTE_GPIO, gpio.IN, pull_up_down=gpio.PUD_UP)
            gpio.setup(BUTTON_VOL_UP_GPIO, gpio.IN, pull_up_down=gpio.PUD_UP)
            gpio.setup(BUTTON_VOL_DOWN_GPIO, gpio.IN, pull_up_down=gpio.PUD_UP)

            self._gpio_module = gpio
            self._gpio_polling_enabled = True
            now = time.monotonic()
            for pin in (BUTTON_MUTE_GPIO, BUTTON_VOL_UP_GPIO, BUTTON_VOL_DOWN_GPIO):
                self._last_button_level[pin] = int(gpio.input(pin))
                self._last_button_at[pin] = now
                self._button_fired[pin] = False
        except Exception as err:  # noqa: BLE001
            logging.warning("GPIO polling fallback unavailable: %s", err)
            self._gpio_module = None
            self._gpio_polling_enabled = False

    def _poll_buttons(self, now: float) -> None:
        if not self._gpio_polling_enabled or self._gpio_module is None:
            return

        for pin, callback in (
            (BUTTON_MUTE_GPIO, self._on_button_mute),
            (BUTTON_VOL_UP_GPIO, self._on_button_volume_up),
            (BUTTON_VOL_DOWN_GPIO, self._on_button_volume_down),
        ):
            try:
                level = int(self._gpio_module.input(pin))
            except Exception:  # noqa: BLE001
                continue

            previous = self._last_button_level.get(pin, 1)
            if level != previous:
                self._last_button_level[pin] = level
                self._last_button_at[pin] = now
                if level == 1:
                    self._button_fired[pin] = False
                continue

            last_changed = self._last_button_at.get(pin, now)
            # Fire on stable falling level (pressed with pull-up) with debounce.
            if (level == 0) and (not self._button_fired.get(pin, False)) and (now - last_changed >= 0.08):
                callback()
                self._button_fired[pin] = True

    async def _volume_up(self) -> None:
        await self._send_command("volume_up")
        await self._play_volume_feedback()

    async def _volume_down(self) -> None:
        await self._send_command("volume_down")
        await self._play_volume_feedback()

    async def _send_command(self, cmd: str) -> None:
        if self._ipc_bridge is not None:
            self._ipc_bridge.handle_command(cmd)
            return

        packet = json.dumps(
            build_message(cmd.upper(), {"command": cmd}, source="gpio_controller"),
            separators=(",", ":"),
        ).encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.sendto(packet, str(CONTROL_SOCKET_PATH))
        except FileNotFoundError:
            logging.warning("IPC command socket unavailable: %s", CONTROL_SOCKET_PATH)
        except Exception as err:  # noqa: BLE001
            logging.warning("IPC command send failed (%s): %s", cmd, err)
        finally:
            sock.close()

    async def _play_volume_feedback(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "aplay",
            "-q",
            "-D",
            self._feedback_sound_device,
            self._feedback_sound_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        if rc != 0:
            logging.warning("Volume feedback sound failed (exit=%s)", rc)

    async def _is_service_active(self) -> bool:
        return self._running

    def on_ipc_event(self, payload: dict[str, object]) -> None:
        now = time.monotonic()
        event = ""
        payload_obj = payload.get("payload")
        if isinstance(payload_obj, dict):
            event = str(payload_obj.get("event", "")).strip().lower()
            effective_payload = payload_obj
        else:
            event = str(payload.get("event", "")).strip().lower()
            effective_payload = payload
        if not event:
            return

        if event == "ha_connected":
            self.state.ha_connected = True
            return
        if event == "ha_disconnected":
            self.state.ha_connected = False
            self.state.listening_until = now
            self.state.playback_until = now
            self.state.audio_playback_until = now
            return
        if event == "muted":
            self.state.muted = bool(effective_payload.get("value", False))
            return
        if event == "led_intensity":
            self._update_led_intensity(effective_payload.get("value"), source="ipc")
            return
        if event == "led_night_mode":
            self._update_led_night_mode(effective_payload.get("value"), source="ipc")
            return

        if event in ("wake_word", "listening_start", "run_start"):
            self.state.listening_until = max(self.state.listening_until, now + 12.0)
            return
        if event in ("listening_end", "intent_start"):
            self.state.listening_until = min(self.state.listening_until, now + 0.2)
            return

        if event == "tts_start":
            if self.state.playback_started_at <= 0.0:
                self.state.playback_started_at = now
            self.state.playback_until = max(self.state.playback_until, now + 20.0)
            return
        if event == "tts_end":
            self.state.playback_until = max(self.state.playback_until, now + 1.2)
            return
        if event in ("tts_finished", "run_end"):
            elapsed = max(0.0, now - self.state.playback_started_at) if self.state.playback_started_at > 0.0 else 0.0
            linger = max(1.2, min(4.0, 1.0 + elapsed * 0.18))
            self.state.playback_until = now + linger
            self.state.playback_started_at = 0.0
            return

    def _apply_led(self) -> None:
        now = time.monotonic()

        if not self.state.service_active:
            mode = LedMode.OFF
        elif self.state.muted:
            mode = LedMode.MUTED
        elif self._is_playback_active(now):
            mode = LedMode.PLAYBACK
        elif self._is_listening_active(now):
            mode = LedMode.LISTENING
        else:
            mode = LedMode.READY

        if mode != self._led_mode:
            self._led_mode = mode
            self.led.set_mode(mode, now)
            logging.info("LED mode -> %s", mode)
        self.led.tick(now)

    def _is_playback_active(self, now: float) -> bool:
        return now <= max(self.state.playback_until, self.state.audio_playback_until)

    def _is_listening_active(self, now: float) -> bool:
        return now <= self.state.listening_until

    def _update_audio_playback_state(self, now: float) -> None:
        if not ALSA_PLAYBACK_STATUS_ROOT.exists():
            return

        try:
            for status_path in ALSA_PLAYBACK_STATUS_ROOT.glob("card*/pcm*p/sub*/status"):
                try:
                    with status_path.open("r", encoding="utf-8") as status_file:
                        for line in status_file:
                            if not line.startswith("state:"):
                                continue
                            state_name = line.split(":", 1)[1].strip().upper()
                            if state_name in ALSA_ACTIVE_STATES:
                                self.state.audio_playback_until = now + ALSA_ACTIVITY_HOLD_SECONDS
                                return
                            break
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _normalize_led_intensity(value: object) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return LED_INTENSITY_DEFAULT_PERCENT
        return max(0.0, min(100.0, parsed))

    @staticmethod
    def _normalize_led_night_mode(value: object) -> bool:
        if isinstance(value, bool):
            return value
        try:
            return bool(int(float(value)))
        except (TypeError, ValueError):
            return LED_NIGHT_SCALE_DEFAULT_ENABLED

    def _update_led_intensity(self, value: object, source: str) -> None:
        target = self._normalize_led_intensity(value)
        if abs(target - self._led_intensity_percent) < 0.01:
            return
        self._led_intensity_percent = target
        self.led.set_intensity_percent(target)
        logging.info("LED intensity -> %.1f%% (source=%s)", target, source)

    def _update_led_night_mode(self, value: object, source: str) -> None:
        enabled = self._normalize_led_night_mode(value)
        if enabled == self._led_night_mode_enabled:
            return
        self._led_night_mode_enabled = enabled
        self.led.set_night_mode(enabled)
        logging.info("LED night mode -> %s (source=%s)", "on" if enabled else "off", source)

    def _load_led_preferences(self, force: bool = False) -> None:
        try:
            stat = self._preferences_path.stat()
        except FileNotFoundError:
            if force:
                self._update_led_intensity(LED_INTENSITY_DEFAULT_PERCENT, source="default")
                self._update_led_night_mode(LED_NIGHT_SCALE_DEFAULT_ENABLED, source="default")
            return
        except Exception:  # noqa: BLE001
            return

        if (not force) and (self._preferences_mtime is not None) and (stat.st_mtime <= self._preferences_mtime):
            return

        self._preferences_mtime = stat.st_mtime
        try:
            with self._preferences_path.open("r", encoding="utf-8") as preferences_file:
                preferences = json.load(preferences_file)
        except Exception:  # noqa: BLE001
            return

        self._update_led_intensity(preferences.get("led_intensity", LED_INTENSITY_DEFAULT_PERCENT), source="preferences")
        self._update_led_night_mode(preferences.get("led_night_mode", LED_NIGHT_SCALE_DEFAULT_ENABLED), source="preferences")

    async def _run(self) -> None:
        while self._running:
            try:
                now = time.monotonic()
                if now >= self._next_service_poll:
                    self._next_service_poll = now + POLL_SERVICE_SECONDS
                    self.state.service_active = await self._is_service_active()
                    if not self.state.service_active:
                        self.state.ha_connected = False

                if now >= self._next_audio_poll:
                    self._next_audio_poll = now + POLL_AUDIO_SECONDS
                    self._update_audio_playback_state(now)

                if now >= self._next_button_poll:
                    self._next_button_poll = now + POLL_BUTTON_SECONDS
                    self._poll_buttons(now)

                if now >= self._next_preferences_poll:
                    self._next_preferences_poll = now + POLL_PREFERENCES_SECONDS
                    self._load_led_preferences()

                self._apply_led()
                await asyncio.sleep(LED_TICK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logging.exception("Controller loop error: %s", err)
                await asyncio.sleep(LED_TICK_SECONDS)
