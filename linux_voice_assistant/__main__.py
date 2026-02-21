#!/usr/bin/env python3
import asyncio
import json
import logging
import statistics
import sys
import threading
import time
from pathlib import Path
from queue import Queue
from typing import Dict, List, Optional, Set, Union

import numpy as np
import soundcard as sc
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
from pyopen_wakeword import OpenWakeWord, OpenWakeWordFeatures

from .config import load_config, resolve_repo_path
from .gpio_controller import LvaGpioController
from .local_ipc import LocalIpcBridge
from .models import AvailableWakeWord, Preferences, ServerState, WakeWordType
from .mpv_player import MpvMediaPlayer
from .satellite import VoiceSatelliteProtocol
from .util import get_mac
from .zeroconf import HomeAssistantZeroconf

_LOGGER = logging.getLogger(__name__)
async def main() -> None:
    app_config = load_config()
    core_config = app_config.core

    log_level_name = str(core_config.log_level).strip().upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(level=log_level)

    download_dir = resolve_repo_path(core_config.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    system_volume_device = core_config.system_volume_device
    if (
        system_volume_device is None
        and core_config.audio_output_device
        and core_config.audio_output_device.startswith("alsa/")
    ):
        system_volume_device = core_config.audio_output_device.split("/", 1)[1]

    # Resolve microphone
    audio_input_device = core_config.audio_input_device
    if audio_input_device is not None:
        try:
            audio_input_device = int(audio_input_device)
        except ValueError:
            pass

        mic = sc.get_microphone(audio_input_device)
    else:
        mic = sc.default_microphone()

    # Load available wake words
    wake_word_dirs = [resolve_repo_path(ww_dir) for ww_dir in core_config.wake_word_dirs]
    wake_word_dirs.append(download_dir / "external_wake_words")
    available_wake_words: Dict[str, AvailableWakeWord] = {}

    for wake_word_dir in wake_word_dirs:
        for model_config_path in wake_word_dir.glob("*.json"):
            model_id = model_config_path.stem
            if model_id == core_config.stop_model:
                # Don't show stop model as an available wake word
                continue

            with open(model_config_path, "r", encoding="utf-8") as model_config_file:
                model_config = json.load(model_config_file)
                model_type = WakeWordType(model_config["type"])
                if model_type == WakeWordType.OPEN_WAKE_WORD:
                    wake_word_path = model_config_path.parent / model_config["model"]
                else:
                    wake_word_path = model_config_path

                available_wake_words[model_id] = AvailableWakeWord(
                    id=model_id,
                    type=WakeWordType(model_type),
                    wake_word=model_config["wake_word"],
                    trained_languages=model_config.get("trained_languages", []),
                    wake_word_path=wake_word_path,
                )

    _LOGGER.debug("Available wake words: %s", list(sorted(available_wake_words.keys())))

    # Load preferences
    preferences_path = resolve_repo_path(core_config.preferences_file)
    if preferences_path.exists():
        _LOGGER.debug("Loading preferences: %s", preferences_path)
        with open(preferences_path, "r", encoding="utf-8") as preferences_file:
            preferences_dict = json.load(preferences_file)
            preferences = Preferences(**preferences_dict)
    else:
        preferences = Preferences()

    if core_config.enable_thinking_sound:
        preferences.thinking_sound = 1

    # Load wake/stop models
    active_wake_words: Set[str] = set()
    wake_models: Dict[str, Union[MicroWakeWord, OpenWakeWord]] = {}
    if preferences.active_wake_words:
        # Load preferred models
        for wake_word_id in preferences.active_wake_words:
            wake_word = available_wake_words.get(wake_word_id)
            if wake_word is None:
                _LOGGER.warning("Unrecognized wake word id: %s", wake_word_id)
                continue

            _LOGGER.debug("Loading wake model: %s", wake_word_id)
            wake_models[wake_word_id] = wake_word.load()
            active_wake_words.add(wake_word_id)

    if not wake_models:
        # Load default model
        wake_word_id = core_config.wake_model
        wake_word = available_wake_words[wake_word_id]

        _LOGGER.debug("Loading wake model: %s", wake_word_id)
        wake_models[wake_word_id] = wake_word.load()
        active_wake_words.add(wake_word_id)

    # TODO: allow openWakeWord for "stop"
    stop_model: Optional[MicroWakeWord] = None
    for wake_word_dir in wake_word_dirs:
        stop_config_path = wake_word_dir / f"{core_config.stop_model}.json"
        if not stop_config_path.exists():
            continue

        _LOGGER.debug("Loading stop model: %s", stop_config_path)
        stop_model = MicroWakeWord.from_config(stop_config_path)
        break

    assert stop_model is not None

    state = ServerState(
        name=core_config.name,
        mac_address=get_mac(),
        audio_queue=Queue(),
        entities=[],
        available_wake_words=available_wake_words,
        wake_words=wake_models,
        active_wake_words=active_wake_words,
        stop_word=stop_model,
        music_player=MpvMediaPlayer(device=core_config.audio_output_device),
        tts_player=MpvMediaPlayer(device=core_config.audio_output_device),
        wakeup_sound=str(resolve_repo_path(core_config.wakeup_sound)),
        timer_finished_sound=str(resolve_repo_path(core_config.timer_finished_sound)),
        processing_sound=str(resolve_repo_path(core_config.processing_sound)),
        mute_sound=str(resolve_repo_path(core_config.mute_sound)),
        unmute_sound=str(resolve_repo_path(core_config.unmute_sound)),
        system_volume_device=system_volume_device,
        system_volume_control=core_config.system_volume_control,
        preferences=preferences,
        preferences_path=preferences_path,
        refractory_seconds=core_config.refractory_seconds,
        download_dir=download_dir,
        ipc_bridge=LocalIpcBridge(),
    )
    pref_wake_word_detection = bool(int(getattr(preferences, "wake_word_detection", 1)))
    pref_distance_activation = bool(int(getattr(preferences, "distance_activation", 0)))
    pref_distance_activation_sound = bool(int(getattr(preferences, "distance_activation_sound", 1)))
    pref_distance_threshold = max(
        10.0,
        min(2000.0, float(getattr(preferences, "distance_activation_threshold_mm", 120.0))),
    )
    pref_distance_model = str(getattr(preferences, "distance_sensor_model", "l0x")).strip().lower()
    if pref_distance_model not in {"l0x", "l1x"}:
        pref_distance_model = "l0x"
    pref_vision_enabled = bool(int(getattr(preferences, "vision_enabled", 1)))
    pref_attention_required = bool(int(getattr(preferences, "attention_required", 1)))
    pref_vision_cooldown = max(0.0, min(15.0, float(getattr(preferences, "vision_cooldown_s", 4.0))))
    pref_vision_min_conf = max(0.0, min(1.0, float(getattr(preferences, "vision_min_confidence", 0.6))))
    pref_engaged_vad_window = max(0.5, min(8.0, float(getattr(preferences, "engaged_vad_window_s", 2.5))))

    state.wake_word_detection_enabled = (
        pref_wake_word_detection
        if (core_config.wake_word_detection is None)
        else bool(core_config.wake_word_detection)
    )
    state.distance_activation_enabled = (
        pref_distance_activation
        if (core_config.distance_activation is None)
        else bool(core_config.distance_activation)
    )
    state.distance_activation_sound_enabled = pref_distance_activation_sound
    state.distance_activation_threshold_mm = (
        pref_distance_threshold
        if (core_config.distance_activation_threshold_mm is None)
        else max(10.0, min(2000.0, float(core_config.distance_activation_threshold_mm)))
    )
    state.distance_sensor_model = (
        pref_distance_model if (core_config.distance_sensor_model is None) else str(core_config.distance_sensor_model)
    )
    state.vision_enabled = (
        pref_vision_enabled if (core_config.vision_enabled is None) else bool(core_config.vision_enabled)
    )
    state.attention_required = (
        pref_attention_required
        if (core_config.attention_required is None)
        else bool(core_config.attention_required)
    )
    state.vision_cooldown_s = (
        pref_vision_cooldown
        if (core_config.vision_cooldown_s is None)
        else max(0.0, min(15.0, float(core_config.vision_cooldown_s)))
    )
    state.vision_min_confidence = (
        pref_vision_min_conf
        if (core_config.vision_min_confidence is None)
        else max(0.0, min(1.0, float(core_config.vision_min_confidence)))
    )
    state.engaged_vad_window_s = (
        pref_engaged_vad_window
        if (core_config.engaged_vad_window_s is None)
        else max(0.5, min(8.0, float(core_config.engaged_vad_window_s)))
    )
    state.preferences.wake_word_detection = 1 if state.wake_word_detection_enabled else 0
    state.preferences.distance_activation = 1 if state.distance_activation_enabled else 0
    state.preferences.distance_activation_sound = 1 if state.distance_activation_sound_enabled else 0
    state.preferences.distance_activation_threshold_mm = state.distance_activation_threshold_mm
    state.preferences.distance_sensor_model = state.distance_sensor_model
    state.preferences.vision_enabled = 1 if state.vision_enabled else 0
    state.preferences.attention_required = 1 if state.attention_required else 0
    state.preferences.vision_cooldown_s = state.vision_cooldown_s
    state.preferences.vision_min_confidence = state.vision_min_confidence
    state.preferences.engaged_vad_window_s = state.engaged_vad_window_s
    _LOGGER.info(
        "Trigger config: wake_word=%s distance=%s distance_sound=%s threshold_mm=%.1f sensor=%s vision=%s attention=%s",
        "on" if state.wake_word_detection_enabled else "off",
        "on" if state.distance_activation_enabled else "off",
        "on" if state.distance_activation_sound_enabled else "off",
        state.distance_activation_threshold_mm,
        state.distance_sensor_model,
        "on" if state.vision_enabled else "off",
        "on" if state.attention_required else "off",
    )

    if core_config.enable_thinking_sound:
        state.save_preferences()

    process_audio_thread = threading.Thread(
        target=process_audio,
        args=(state, mic, core_config.audio_input_block_size),
        daemon=True,
    )
    process_audio_thread.start()

    loop = asyncio.get_running_loop()
    if state.ipc_bridge is not None:
        await state.ipc_bridge.start()

    if core_config.enable_gpio_control:
        try:
            state.gpio_controller = LvaGpioController(
                ipc_bridge=state.ipc_bridge,
                preferences_path=preferences_path,
                feedback_sound_path=Path(state.processing_sound),
                feedback_sound_device=core_config.gpio_feedback_device,
            )
            await state.gpio_controller.start()
            _LOGGER.info("Integrated GPIO controller started")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to start integrated GPIO controller")
            state.gpio_controller = None
    else:
        _LOGGER.info("Integrated GPIO controller disabled by config")

    server = await loop.create_server(
        lambda: VoiceSatelliteProtocol(state), host=core_config.host, port=core_config.port
    )

    # Auto discovery (zeroconf, mDNS)
    discovery = HomeAssistantZeroconf(port=core_config.port, name=core_config.name)
    await discovery.register_server()

    try:
        async with server:
            _LOGGER.info("Server started (host=%s, port=%s)", core_config.host, core_config.port)
            await server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.audio_queue.put_nowait(None)
        process_audio_thread.join()
        if state.gpio_controller is not None:
            await state.gpio_controller.shutdown()
            state.gpio_controller = None
        if state.ipc_bridge is not None:
            state.ipc_bridge.stop()

    _LOGGER.debug("Server stopped")


# -----------------------------------------------------------------------------


def process_audio(state: ServerState, mic, block_size: int):
    """Process audio chunks from the microphone."""

    wake_words: List[Union[MicroWakeWord, OpenWakeWord]] = []
    micro_features: Optional[MicroWakeWordFeatures] = None
    micro_inputs: List[np.ndarray] = []

    oww_features: Optional[OpenWakeWordFeatures] = None
    oww_inputs: List[np.ndarray] = []
    has_oww = False

    last_active: Optional[float] = None
    debug_scores_enabled = _LOGGER.isEnabledFor(logging.DEBUG)
    score_log_interval = 0.30
    last_score_log: Dict[str, float] = {}

    try:
        _LOGGER.debug("Opening audio input device: %s", mic.name)
        with mic.recorder(samplerate=16000, channels=1, blocksize=block_size) as mic_in:
            while True:
                try:
                    audio_chunk_array = mic_in.record(block_size).reshape(-1)
                except Exception as err:  # noqa: BLE001
                    if "xrun" in str(err).lower():
                        state.xrun_counter += 1
                        _LOGGER.warning("Audio XRUN detected (count=%s): %s", state.xrun_counter, err)
                        continue
                    raise
                audio_chunk = (
                    (np.clip(audio_chunk_array, -1.0, 1.0) * 32767.0)
                    .astype("<i2")  # little-endian 16-bit signed
                    .tobytes()
                )

                if state.satellite is None:
                    continue

                if (not wake_words) or (state.wake_words_changed and state.wake_words):
                    # Update list of wake word models to process
                    state.wake_words_changed = False
                    wake_words = [
                        ww
                        for ww in state.wake_words.values()
                        if ww.id in state.active_wake_words
                    ]

                    has_oww = False
                    for wake_word in wake_words:
                        if isinstance(wake_word, OpenWakeWord):
                            has_oww = True

                    if micro_features is None:
                        micro_features = MicroWakeWordFeatures()

                    if has_oww and (oww_features is None):
                        oww_features = OpenWakeWordFeatures.from_builtin()

                try:
                    state.satellite.handle_audio(audio_chunk)

                    assert micro_features is not None
                    micro_inputs.clear()
                    micro_inputs.extend(micro_features.process_streaming(audio_chunk))

                    if has_oww:
                        assert oww_features is not None
                        oww_inputs.clear()
                        oww_inputs.extend(oww_features.process_streaming(audio_chunk))

                    for wake_word in wake_words:
                        if not state.wake_word_detection_enabled:
                            continue
                        activated = False
                        score: Optional[float] = None
                        threshold: float

                        if isinstance(wake_word, MicroWakeWord):
                            threshold = wake_word.probability_cutoff
                            for micro_input in micro_inputs:
                                if wake_word.process_streaming(micro_input):
                                    activated = True
                                probs = getattr(wake_word, "_probabilities", None)
                                if probs:
                                    score = float(statistics.mean(probs))
                        elif isinstance(wake_word, OpenWakeWord):
                            threshold = (
                                state.wake_word_threshold
                                if state.wake_word_threshold is not None
                                else 0.5
                            )
                            for oww_input in oww_inputs:
                                for prob in wake_word.process_streaming(oww_input):
                                    score = float(prob)
                                    if prob >= threshold:
                                        activated = True
                        else:
                            continue

                        if debug_scores_enabled and (score is not None):
                            now = time.monotonic()
                            last_logged = last_score_log.get(wake_word.id)
                            if (last_logged is None) or (
                                (now - last_logged) >= score_log_interval
                            ):
                                _LOGGER.debug(
                                    "Wake-word score: model=%s score=%.1f%% threshold=%.1f%% result=%s",
                                    wake_word.id,
                                    score * 100.0,
                                    threshold * 100.0,
                                    "triggered" if activated else "not_triggered",
                                )
                                last_score_log[wake_word.id] = now

                        if activated and not state.muted:
                            # Check refractory
                            now = time.monotonic()
                            if (last_active is None) or (
                                (now - last_active) > state.refractory_seconds
                            ):
                                state.satellite.wakeup(wake_word)
                                last_active = now

                    # Always process to keep state correct
                    stopped = False
                    for micro_input in micro_inputs:
                        if state.stop_word.process_streaming(micro_input):
                            stopped = True

                    if stopped and (state.stop_word.id in state.active_wake_words) and not state.muted:
                        state.satellite.stop()
                except Exception:
                    _LOGGER.exception("Unexpected error handling audio")
    except Exception:
        _LOGGER.exception("Unexpected error processing audio")
        sys.exit(1)


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    asyncio.run(main())
