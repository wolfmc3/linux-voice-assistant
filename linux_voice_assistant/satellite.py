"""Voice satellite protocol."""

import asyncio
import hashlib
import logging
import posixpath
import re
import subprocess
import shutil
import time
from collections.abc import Iterable
from typing import Dict, Optional, Set, Union
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    DeviceInfoRequest,
    DeviceInfoResponse,
    ButtonCommandRequest,
    ListEntitiesDoneResponse,
    ListEntitiesRequest,
    MediaPlayerCommandRequest,
    NumberCommandRequest,
    SelectCommandRequest,
    SwitchStateResponse,
    SubscribeHomeAssistantStatesRequest,
    SwitchCommandRequest,
    VoiceAssistantAnnounceFinished,
    VoiceAssistantAnnounceRequest,
    VoiceAssistantAudio,
    VoiceAssistantConfigurationRequest,
    VoiceAssistantConfigurationResponse,
    VoiceAssistantEventResponse,
    VoiceAssistantExternalWakeWord,
    VoiceAssistantRequest,
    VoiceAssistantSetConfiguration,
    VoiceAssistantTimerEventResponse,
    VoiceAssistantWakeWord,
    AuthenticationRequest,
)
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model import (
    VoiceAssistantEventType,
    VoiceAssistantFeature,
    VoiceAssistantTimerEventType,
)
from google.protobuf import message
from pymicro_wakeword import MicroWakeWord
from pyopen_wakeword import OpenWakeWord

from .api_server import APIServer
from .entity import (
    DistanceActivationSwitchEntity,
    DistanceActivationSoundSwitchEntity,
    DistanceActivationThresholdNumberEntity,
    DistanceSensorEntity,
    LedIntensityNumberEntity,
    MediaPlayerEntity,
    MuteSwitchEntity,
    NightModeSwitchEntity,
    RebootButtonEntity,
    ShutdownButtonEntity,
    SystemVolumeNumberEntity,
    ThinkingSoundEntity,
    WakeWordDetectionSwitchEntity,
    WakeWordThresholdNumberEntity,
    WakeWordThresholdPresetSelectEntity,
)
from .vl53l0x_reader import Vl53l0xReader
from .models import (
    AvailableWakeWord,
    ServerState,
    WakeWordType,
    WAKE_WORD_THRESHOLD_DEFAULT_CUSTOM,
    WAKE_WORD_THRESHOLD_PRESET_CUSTOM,
    WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT,
    normalize_wake_word_threshold,
    normalize_wake_word_threshold_preset,
    resolve_wake_word_threshold,
)
from .util import call_all

_LOGGER = logging.getLogger(__name__)

PROTO_TO_MESSAGE_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}

class VoiceSatelliteProtocol(APIServer):

    def __init__(self, state: ServerState) -> None:
        super().__init__(state.name)
        
        self.state = state
        self.state.connected = False
        self._is_streaming_audio = False
        self._tts_url: Optional[str] = None
        self._tts_played = False
        self._continue_conversation = False
        self._timer_finished = False
        self._processing = False
        self._pipeline_active = False
        self._external_wake_words: Dict[str, VoiceAssistantExternalWakeWord] = {}
        self._disconnect_event = asyncio.Event()
        self._distance_mm: Optional[float] = None
        self._distance_reader: Optional[Vl53l0xReader] = None
        self._distance_task: Optional[asyncio.Task[None]] = None
        self._distance_last_publish = 0.0
        self._distance_activation_latched = False
        self._distance_last_trigger = 0.0
        self._listening_trigger: Optional[str] = None

        existing_media_players = [
            entity
            for entity in self.state.entities
            if isinstance(entity, MediaPlayerEntity)
        ]
        if existing_media_players:
            # Keep the first instance and remove any extras.
            self.state.media_player_entity = existing_media_players[0]
            for extra in existing_media_players[1:]:
                self.state.entities.remove(extra)

        existing_mute_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, MuteSwitchEntity)
        ]
        if existing_mute_switches:
            self.state.mute_switch_entity = existing_mute_switches[0]
            for extra in existing_mute_switches[1:]:
                self.state.entities.remove(extra)
                
        if self.state.media_player_entity is None:
            self.state.media_player_entity = MediaPlayerEntity(
                server=self,
                key=len(state.entities),
                name="Media Player",
                object_id="linux_voice_assistant_media_player",
                music_player=state.music_player,
                announce_player=state.tts_player,
            )
            self.state.entities.append(self.state.media_player_entity)
        elif self.state.media_player_entity not in self.state.entities:
            self.state.entities.append(self.state.media_player_entity)

        self.state.media_player_entity.server = self

        # Add/update mute switch entity (like ESPHome Voice PE)
        mute_switch = self.state.mute_switch_entity
        if mute_switch is None:
            mute_switch = MuteSwitchEntity(
                server=self,
                key=len(state.entities),
                name="Mute",
                object_id="mute",
                get_muted=lambda: self.state.muted,
                set_muted=self._set_muted,
            )
            self.state.entities.append(mute_switch)
            self.state.mute_switch_entity = mute_switch
        elif mute_switch not in self.state.entities:
            self.state.entities.append(mute_switch)

        mute_switch.server = self
        mute_switch.update_get_muted(lambda: self.state.muted)
        mute_switch.update_set_muted(self._set_muted)
        mute_switch.sync_with_state()
        
        existing_thinking_sound_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, ThinkingSoundEntity)
        ]
        if existing_thinking_sound_switches:
            self.state.thinking_sound_entity = existing_thinking_sound_switches[0]
            for extra in existing_thinking_sound_switches[1:]:
                self.state.entities.remove(extra)

        existing_night_mode_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, NightModeSwitchEntity)
        ]
        if existing_night_mode_switches:
            self.state.night_mode_entity = existing_night_mode_switches[0]
            for extra in existing_night_mode_switches[1:]:
                self.state.entities.remove(extra)

        existing_system_volume_numbers = [
            entity
            for entity in self.state.entities
            if isinstance(entity, SystemVolumeNumberEntity)
        ]
        if existing_system_volume_numbers:
            self.state.system_volume_entity = existing_system_volume_numbers[0]
            for extra in existing_system_volume_numbers[1:]:
                self.state.entities.remove(extra)

        existing_led_intensity_numbers = [
            entity
            for entity in self.state.entities
            if isinstance(entity, LedIntensityNumberEntity)
        ]
        if existing_led_intensity_numbers:
            self.state.led_intensity_entity = existing_led_intensity_numbers[0]
            for extra in existing_led_intensity_numbers[1:]:
                self.state.entities.remove(extra)

        existing_wake_word_threshold_selects = [
            entity
            for entity in self.state.entities
            if isinstance(entity, WakeWordThresholdPresetSelectEntity)
        ]
        if existing_wake_word_threshold_selects:
            self.state.wake_word_threshold_select_entity = existing_wake_word_threshold_selects[0]
            for extra in existing_wake_word_threshold_selects[1:]:
                self.state.entities.remove(extra)

        existing_wake_word_threshold_numbers = [
            entity
            for entity in self.state.entities
            if isinstance(entity, WakeWordThresholdNumberEntity)
        ]
        if existing_wake_word_threshold_numbers:
            self.state.wake_word_threshold_number_entity = existing_wake_word_threshold_numbers[0]
            for extra in existing_wake_word_threshold_numbers[1:]:
                self.state.entities.remove(extra)

        existing_shutdown_buttons = [
            entity
            for entity in self.state.entities
            if isinstance(entity, ShutdownButtonEntity)
        ]
        if existing_shutdown_buttons:
            self.state.shutdown_button_entity = existing_shutdown_buttons[0]
            for extra in existing_shutdown_buttons[1:]:
                self.state.entities.remove(extra)

        existing_reboot_buttons = [
            entity
            for entity in self.state.entities
            if isinstance(entity, RebootButtonEntity)
        ]
        if existing_reboot_buttons:
            self.state.reboot_button_entity = existing_reboot_buttons[0]
            for extra in existing_reboot_buttons[1:]:
                self.state.entities.remove(extra)

        existing_distance_sensors = [
            entity
            for entity in self.state.entities
            if isinstance(entity, DistanceSensorEntity)
        ]
        if existing_distance_sensors:
            self.state.distance_sensor_entity = existing_distance_sensors[0]
            for extra in existing_distance_sensors[1:]:
                self.state.entities.remove(extra)

        existing_wake_word_detection_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, WakeWordDetectionSwitchEntity)
        ]
        if existing_wake_word_detection_switches:
            self.state.wake_word_detection_entity = existing_wake_word_detection_switches[0]
            for extra in existing_wake_word_detection_switches[1:]:
                self.state.entities.remove(extra)

        existing_distance_activation_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, DistanceActivationSwitchEntity)
        ]
        if existing_distance_activation_switches:
            self.state.distance_activation_entity = existing_distance_activation_switches[0]
            for extra in existing_distance_activation_switches[1:]:
                self.state.entities.remove(extra)

        existing_distance_activation_threshold_numbers = [
            entity
            for entity in self.state.entities
            if isinstance(entity, DistanceActivationThresholdNumberEntity)
        ]
        if existing_distance_activation_threshold_numbers:
            self.state.distance_activation_threshold_entity = existing_distance_activation_threshold_numbers[0]
            for extra in existing_distance_activation_threshold_numbers[1:]:
                self.state.entities.remove(extra)

        existing_distance_activation_sound_switches = [
            entity
            for entity in self.state.entities
            if isinstance(entity, DistanceActivationSoundSwitchEntity)
        ]
        if existing_distance_activation_sound_switches:
            self.state.distance_activation_sound_entity = existing_distance_activation_sound_switches[0]
            for extra in existing_distance_activation_sound_switches[1:]:
                self.state.entities.remove(extra)

        system_volume = self.state.system_volume_entity
        if system_volume is None:
            system_volume = SystemVolumeNumberEntity(
                server=self,
                key=len(state.entities),
                name="Speaker Volume",
                object_id="speaker_volume",
                get_volume=self._get_system_volume,
                set_volume=self._set_system_volume,
            )
            self.state.entities.append(system_volume)
            self.state.system_volume_entity = system_volume
        elif system_volume not in self.state.entities:
            self.state.entities.append(system_volume)

        system_volume.server = self
        system_volume.update_get_volume(self._get_system_volume)
        system_volume.update_set_volume(self._set_system_volume)
        system_volume.sync_with_state()

        led_intensity = self.state.led_intensity_entity
        if led_intensity is None:
            led_intensity = LedIntensityNumberEntity(
                server=self,
                key=len(state.entities),
                name="LED Intensity",
                object_id="led_intensity",
                get_intensity=self._get_led_intensity,
                set_intensity=self._set_led_intensity,
            )
            self.state.entities.append(led_intensity)
            self.state.led_intensity_entity = led_intensity
        elif led_intensity not in self.state.entities:
            self.state.entities.append(led_intensity)

        led_intensity.server = self
        led_intensity.update_get_intensity(self._get_led_intensity)
        led_intensity.update_set_intensity(self._set_led_intensity)
        led_intensity.sync_with_state()

        led_night_mode = self.state.night_mode_entity
        if led_night_mode is None:
            led_night_mode = NightModeSwitchEntity(
                server=self,
                key=len(state.entities),
                name="LED Night Mode",
                object_id="led_night_mode",
                get_enabled=self._get_led_night_mode,
                set_enabled=self._set_led_night_mode,
            )
            self.state.entities.append(led_night_mode)
            self.state.night_mode_entity = led_night_mode
        elif led_night_mode not in self.state.entities:
            self.state.entities.append(led_night_mode)

        led_night_mode.server = self
        led_night_mode.update_get_enabled(self._get_led_night_mode)
        led_night_mode.update_set_enabled(self._set_led_night_mode)
        led_night_mode.sync_with_state()

        wake_word_threshold_preset = self.state.wake_word_threshold_select_entity
        if wake_word_threshold_preset is None:
            wake_word_threshold_preset = WakeWordThresholdPresetSelectEntity(
                server=self,
                key=len(state.entities),
                name="Wake Word Threshold Preset",
                object_id="wake_word_threshold_preset",
                get_preset=self._get_wake_word_threshold_preset,
                set_preset=self._set_wake_word_threshold_preset,
            )
            self.state.entities.append(wake_word_threshold_preset)
            self.state.wake_word_threshold_select_entity = wake_word_threshold_preset
        elif wake_word_threshold_preset not in self.state.entities:
            self.state.entities.append(wake_word_threshold_preset)

        wake_word_threshold_preset.server = self
        wake_word_threshold_preset.update_get_preset(self._get_wake_word_threshold_preset)
        wake_word_threshold_preset.update_set_preset(self._set_wake_word_threshold_preset)
        wake_word_threshold_preset.sync_with_state()

        wake_word_threshold_number = self.state.wake_word_threshold_number_entity
        if wake_word_threshold_number is None:
            wake_word_threshold_number = WakeWordThresholdNumberEntity(
                server=self,
                key=len(state.entities),
                name="Wake Word Threshold",
                object_id="wake_word_threshold",
                get_threshold=self._get_wake_word_threshold_custom,
                set_threshold=self._set_wake_word_threshold_custom,
            )
            self.state.entities.append(wake_word_threshold_number)
            self.state.wake_word_threshold_number_entity = wake_word_threshold_number
        elif wake_word_threshold_number not in self.state.entities:
            self.state.entities.append(wake_word_threshold_number)

        wake_word_threshold_number.server = self
        wake_word_threshold_number.update_get_threshold(self._get_wake_word_threshold_custom)
        wake_word_threshold_number.update_set_threshold(self._set_wake_word_threshold_custom)
        wake_word_threshold_number.sync_with_state()

        shutdown_button = self.state.shutdown_button_entity
        if shutdown_button is None:
            shutdown_button = ShutdownButtonEntity(
                server=self,
                key=len(state.entities),
                name="Shutdown",
                object_id="shutdown",
                shutdown_system=self._shutdown_system,
            )
            self.state.entities.append(shutdown_button)
            self.state.shutdown_button_entity = shutdown_button
        elif shutdown_button not in self.state.entities:
            self.state.entities.append(shutdown_button)

        shutdown_button.server = self
        shutdown_button.update_shutdown_system(self._shutdown_system)

        reboot_button = self.state.reboot_button_entity
        if reboot_button is None:
            reboot_button = RebootButtonEntity(
                server=self,
                key=len(state.entities),
                name="Reboot",
                object_id="reboot",
                reboot_system=self._reboot_system,
            )
            self.state.entities.append(reboot_button)
            self.state.reboot_button_entity = reboot_button
        elif reboot_button not in self.state.entities:
            self.state.entities.append(reboot_button)

        reboot_button.server = self
        reboot_button.update_reboot_system(self._reboot_system)

        distance_sensor = self.state.distance_sensor_entity
        if distance_sensor is None:
            distance_sensor = DistanceSensorEntity(
                server=self,
                key=len(state.entities),
                name="Distance",
                object_id="distance",
                get_distance_mm=self._get_distance_mm,
            )
            self.state.entities.append(distance_sensor)
            self.state.distance_sensor_entity = distance_sensor
        elif distance_sensor not in self.state.entities:
            self.state.entities.append(distance_sensor)

        distance_sensor.server = self
        distance_sensor.update_get_distance_mm(self._get_distance_mm)

        wake_word_detection_switch = self.state.wake_word_detection_entity
        if wake_word_detection_switch is None:
            wake_word_detection_switch = WakeWordDetectionSwitchEntity(
                server=self,
                key=len(state.entities),
                name="Wake Word Detection",
                object_id="wake_word_detection",
                get_enabled=self._get_wake_word_detection_enabled,
                set_enabled=self._set_wake_word_detection_enabled,
            )
            self.state.entities.append(wake_word_detection_switch)
            self.state.wake_word_detection_entity = wake_word_detection_switch
        elif wake_word_detection_switch not in self.state.entities:
            self.state.entities.append(wake_word_detection_switch)

        wake_word_detection_switch.server = self
        wake_word_detection_switch.update_get_enabled(self._get_wake_word_detection_enabled)
        wake_word_detection_switch.update_set_enabled(self._set_wake_word_detection_enabled)
        wake_word_detection_switch.sync_with_state()

        distance_activation_switch = self.state.distance_activation_entity
        if distance_activation_switch is None:
            distance_activation_switch = DistanceActivationSwitchEntity(
                server=self,
                key=len(state.entities),
                name="Distance Activation",
                object_id="distance_activation",
                get_enabled=self._get_distance_activation_enabled,
                set_enabled=self._set_distance_activation_enabled,
            )
            self.state.entities.append(distance_activation_switch)
            self.state.distance_activation_entity = distance_activation_switch
        elif distance_activation_switch not in self.state.entities:
            self.state.entities.append(distance_activation_switch)

        distance_activation_switch.server = self
        distance_activation_switch.update_get_enabled(self._get_distance_activation_enabled)
        distance_activation_switch.update_set_enabled(self._set_distance_activation_enabled)
        distance_activation_switch.sync_with_state()

        distance_activation_sound_switch = self.state.distance_activation_sound_entity
        if distance_activation_sound_switch is None:
            distance_activation_sound_switch = DistanceActivationSoundSwitchEntity(
                server=self,
                key=len(state.entities),
                name="Distance Activation Sound",
                object_id="distance_activation_sound",
                get_enabled=self._get_distance_activation_sound_enabled,
                set_enabled=self._set_distance_activation_sound_enabled,
            )
            self.state.entities.append(distance_activation_sound_switch)
            self.state.distance_activation_sound_entity = distance_activation_sound_switch
        elif distance_activation_sound_switch not in self.state.entities:
            self.state.entities.append(distance_activation_sound_switch)

        distance_activation_sound_switch.server = self
        distance_activation_sound_switch.update_get_enabled(self._get_distance_activation_sound_enabled)
        distance_activation_sound_switch.update_set_enabled(self._set_distance_activation_sound_enabled)
        distance_activation_sound_switch.sync_with_state()

        distance_activation_threshold_number = self.state.distance_activation_threshold_entity
        if distance_activation_threshold_number is None:
            distance_activation_threshold_number = DistanceActivationThresholdNumberEntity(
                server=self,
                key=len(state.entities),
                name="Distance Activation Threshold",
                object_id="distance_activation_threshold",
                get_threshold=self._get_distance_activation_threshold_mm,
                set_threshold=self._set_distance_activation_threshold_mm,
            )
            self.state.entities.append(distance_activation_threshold_number)
            self.state.distance_activation_threshold_entity = distance_activation_threshold_number
        elif distance_activation_threshold_number not in self.state.entities:
            self.state.entities.append(distance_activation_threshold_number)

        distance_activation_threshold_number.server = self
        distance_activation_threshold_number.update_get_threshold(self._get_distance_activation_threshold_mm)
        distance_activation_threshold_number.update_set_threshold(self._set_distance_activation_threshold_mm)
        distance_activation_threshold_number.sync_with_state()

        # Add/update thinking sound entity
        thinking_sound_switch = self.state.thinking_sound_entity
        if thinking_sound_switch is None:
            thinking_sound_switch = ThinkingSoundEntity(
                server=self,
                key=len(state.entities),
                name="Thinking Sound",
                object_id="thinking_sound",
                get_thinking_sound_enabled=lambda: self.state.thinking_sound_enabled,
                set_thinking_sound_enabled=self._set_thinking_sound_enabled,
            )
            self.state.entities.append(thinking_sound_switch)
            self.state.thinking_sound_entity = thinking_sound_switch
        elif thinking_sound_switch not in self.state.entities:
            self.state.entities.append(thinking_sound_switch)

        # Load thinking sound enabled state from preferences (default to False if not set or unknown)
        if hasattr(self.state.preferences, 'thinking_sound') and self.state.preferences.thinking_sound in (0, 1):
            self.state.thinking_sound_enabled = bool(self.state.preferences.thinking_sound)
        else:
            self.state.thinking_sound_enabled = False

        thinking_sound_switch.server = self
        thinking_sound_switch.update_get_thinking_sound_enabled(lambda: self.state.thinking_sound_enabled)
        thinking_sound_switch.update_set_thinking_sound_enabled(self._set_thinking_sound_enabled)
        thinking_sound_switch.sync_with_state()

        self._apply_wake_word_threshold(log_startup=True)
        self.state.satellite = self
        self._start_distance_task()

        if self.state.ipc_bridge is not None:
            self.state.ipc_bridge.set_control_handler(self._handle_local_command)
            self._publish_led_intensity()
            self._publish_led_night_mode()
    
    def _set_thinking_sound_enabled(self, new_state: bool) -> None:
        self.state.thinking_sound_enabled = bool(new_state)
        self.state.preferences.thinking_sound = 1 if self.state.thinking_sound_enabled else 0

        if self.state.thinking_sound_enabled:
            _LOGGER.debug("Thinking sound enabled")
        else:
            _LOGGER.debug("Thinking sound disabled")
            pass
        self.state.save_preferences()

    def _get_wake_word_detection_enabled(self) -> bool:
        return bool(self.state.wake_word_detection_enabled)

    def _set_wake_word_detection_enabled(self, enabled: bool) -> None:
        self.state.wake_word_detection_enabled = bool(enabled)
        self.state.preferences.wake_word_detection = 1 if self.state.wake_word_detection_enabled else 0
        self.state.save_preferences()
        _LOGGER.info(
            "Wake-word detection %s",
            "enabled" if self.state.wake_word_detection_enabled else "disabled",
        )

    def _get_distance_activation_enabled(self) -> bool:
        return bool(self.state.distance_activation_enabled)

    def _set_distance_activation_enabled(self, enabled: bool) -> None:
        self.state.distance_activation_enabled = bool(enabled)
        self.state.preferences.distance_activation = 1 if self.state.distance_activation_enabled else 0
        if not self.state.distance_activation_enabled:
            self._distance_activation_latched = False
        self.state.save_preferences()
        _LOGGER.info(
            "Distance activation %s",
            "enabled" if self.state.distance_activation_enabled else "disabled",
        )

    def _get_distance_activation_sound_enabled(self) -> bool:
        return bool(self.state.distance_activation_sound_enabled)

    def _set_distance_activation_sound_enabled(self, enabled: bool) -> None:
        self.state.distance_activation_sound_enabled = bool(enabled)
        self.state.preferences.distance_activation_sound = 1 if self.state.distance_activation_sound_enabled else 0
        self.state.save_preferences()
        _LOGGER.info(
            "Distance activation sound %s",
            "enabled" if self.state.distance_activation_sound_enabled else "disabled",
        )

    def _get_distance_activation_threshold_mm(self) -> float:
        return float(self.state.distance_activation_threshold_mm)

    def _set_distance_activation_threshold_mm(self, value: float) -> bool:
        target = max(10.0, min(2000.0, float(value)))
        self.state.distance_activation_threshold_mm = target
        self.state.preferences.distance_activation_threshold_mm = target
        self.state.save_preferences()
        _LOGGER.info("Distance activation threshold set to %.1f mm", target)
        return True

    def _get_distance_mm(self) -> Optional[float]:
        return self._distance_mm

    def _publish_distance_state(self) -> None:
        if self.state.distance_sensor_entity is None:
            return
        self.send_messages([self.state.distance_sensor_entity.get_state_message()])

    def _start_direct_listening(self, trigger: str) -> bool:
        if self.state.muted:
            return False
        if not self.state.connected:
            return False
        if self._is_streaming_audio:
            return False

        self.send_messages([VoiceAssistantRequest(start=True)])
        self._is_streaming_audio = True
        self._listening_trigger = trigger
        self.duck()
        if trigger == "distance" and self.state.distance_activation_sound_enabled:
            self.state.tts_player.play(self.state.wakeup_sound)
        self._emit_ipc_event("distance_trigger", source=trigger)
        _LOGGER.info("Direct listening started (trigger=%s)", trigger)
        return True

    def _stop_distance_listening(self) -> None:
        if not self._is_streaming_audio:
            return
        if self._listening_trigger != "distance":
            return

        self.send_messages([VoiceAssistantRequest(start=False)])
        self._is_streaming_audio = False
        self._listening_trigger = None
        self._emit_ipc_event("distance_trigger_cancelled", reason="out_of_range")
        _LOGGER.info("Direct listening cancelled (trigger=distance, reason=out_of_range)")

    def _handle_distance_activation(self, now: float) -> None:
        if not self.state.distance_activation_enabled:
            self._stop_distance_listening()
            self._distance_activation_latched = False
            return

        distance = self._distance_mm
        threshold = max(1.0, float(self.state.distance_activation_threshold_mm))

        if (distance is None) or (distance > threshold):
            self._stop_distance_listening()
            self._distance_activation_latched = False
            return

        if self._distance_activation_latched:
            return

        if (now - self._distance_last_trigger) < self.state.refractory_seconds:
            return

        if self._start_direct_listening("distance"):
            self._distance_last_trigger = now
            self._distance_activation_latched = True

    def _start_distance_task(self) -> None:
        if self._distance_task is not None:
            return

        if self.state.vl53l0x_reader is None:
            self.state.vl53l0x_reader = Vl53l0xReader()
        self._distance_reader = self.state.vl53l0x_reader
        self._distance_task = asyncio.create_task(self._distance_loop())

    async def _distance_loop(self) -> None:
        while True:
            try:
                now = time.monotonic()
                if self._distance_reader is not None:
                    self._distance_mm = self._distance_reader.read_distance_mm()
                else:
                    self._distance_mm = None

                self._handle_distance_activation(now)

                if (now - self._distance_last_publish) >= 5.0:
                    self._publish_distance_state()
                    self._distance_last_publish = now
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Distance task failed")
                await asyncio.sleep(1.0)

    def _get_led_night_mode(self) -> bool:
        return bool(int(getattr(self.state.preferences, "led_night_mode", 0)))

    def _publish_led_night_mode(self) -> None:
        self._emit_ipc_event("led_night_mode", value=self._get_led_night_mode())

    def _set_led_night_mode(self, enabled: bool) -> None:
        new_value = 1 if bool(enabled) else 0
        if new_value == int(getattr(self.state.preferences, "led_night_mode", 0)):
            self._publish_led_night_mode()
            return

        self.state.preferences.led_night_mode = new_value
        self.state.save_preferences()
        _LOGGER.info("LED night mode %s", "enabled" if new_value else "disabled")
        self._publish_led_night_mode()

    @staticmethod
    def _normalize_led_intensity(value: object) -> int:
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError):
            return 100
        return max(0, min(100, parsed))

    def _get_led_intensity(self) -> float:
        normalized = self._normalize_led_intensity(self.state.preferences.led_intensity)
        self.state.preferences.led_intensity = normalized
        return float(normalized)

    def _publish_led_intensity(self) -> None:
        self._emit_ipc_event("led_intensity", value=self._get_led_intensity())

    def _set_led_intensity(self, value: float) -> bool:
        normalized = self._normalize_led_intensity(value)
        if normalized == self.state.preferences.led_intensity:
            self._publish_led_intensity()
            return True

        self.state.preferences.led_intensity = normalized
        self.state.save_preferences()
        _LOGGER.info("LED intensity set to %s%%", normalized)
        self._publish_led_intensity()
        return True

    def _get_wake_word_threshold_preset(self) -> str:
        preset = normalize_wake_word_threshold_preset(
            getattr(self.state.preferences, "wake_word_threshold_preset", WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT)
        )
        self.state.preferences.wake_word_threshold_preset = preset
        return preset

    def _get_wake_word_threshold_custom(self) -> float:
        custom = normalize_wake_word_threshold(
            getattr(self.state.preferences, "wake_word_threshold_custom", WAKE_WORD_THRESHOLD_DEFAULT_CUSTOM)
        )
        self.state.preferences.wake_word_threshold_custom = custom
        return custom

    def _set_wake_word_threshold_preset(self, preset: str) -> None:
        normalized = normalize_wake_word_threshold_preset(preset)
        if normalized == self._get_wake_word_threshold_preset():
            self._apply_wake_word_threshold()
            self._publish_wake_word_threshold_state()
            return

        self.state.preferences.wake_word_threshold_preset = normalized
        self.state.save_preferences()
        self._apply_wake_word_threshold()
        self._publish_wake_word_threshold_state()
        _LOGGER.info("Wake word threshold preset set to %s", normalized)

    def _set_wake_word_threshold_custom(self, threshold: float) -> bool:
        normalized = normalize_wake_word_threshold(threshold)
        current_custom = self._get_wake_word_threshold_custom()
        current_preset = self._get_wake_word_threshold_preset()

        changed = False
        if abs(normalized - current_custom) > 1e-6:
            self.state.preferences.wake_word_threshold_custom = normalized
            changed = True

        if current_preset != WAKE_WORD_THRESHOLD_PRESET_CUSTOM:
            self.state.preferences.wake_word_threshold_preset = WAKE_WORD_THRESHOLD_PRESET_CUSTOM
            changed = True

        if changed:
            self.state.save_preferences()
            _LOGGER.info("Wake word threshold custom set to %.2f%%", normalized * 100.0)

        self._apply_wake_word_threshold()
        self._publish_wake_word_threshold_state()
        return True

    def _apply_wake_word_threshold(
        self,
        *,
        log_startup: bool = False,
        log_change: bool = True,
    ) -> None:
        threshold = resolve_wake_word_threshold(
            self._get_wake_word_threshold_preset(),
            self._get_wake_word_threshold_custom(),
        )
        self.state.wake_word_threshold = threshold

        for wake_word in self.state.wake_words.values():
            if isinstance(wake_word, MicroWakeWord):
                if wake_word.id not in self.state.wake_word_default_thresholds:
                    self.state.wake_word_default_thresholds[wake_word.id] = wake_word.probability_cutoff

                if threshold is None:
                    default_threshold = self.state.wake_word_default_thresholds.get(wake_word.id)
                    if default_threshold is not None:
                        wake_word.probability_cutoff = default_threshold
                    continue

                wake_word.probability_cutoff = threshold

        if threshold is None:
            message = "Wake word threshold using model defaults"
        else:
            message = f"Wake word threshold active: {threshold * 100:.2f}%"

        if log_startup:
            _LOGGER.debug("%s (preset=%s)", message, self._get_wake_word_threshold_preset())
        elif not log_change:
            _LOGGER.debug("%s (preset=%s)", message, self._get_wake_word_threshold_preset())
        else:
            _LOGGER.info("%s (preset=%s)", message, self._get_wake_word_threshold_preset())

    def _publish_wake_word_threshold_state(self) -> None:
        states = []
        if self.state.wake_word_threshold_select_entity is not None:
            self.state.wake_word_threshold_select_entity.sync_with_state()
            states.append(self.state.wake_word_threshold_select_entity.get_state_message())
        if self.state.wake_word_threshold_number_entity is not None:
            self.state.wake_word_threshold_number_entity.sync_with_state()
            states.append(self.state.wake_word_threshold_number_entity.get_state_message())
        if states:
            self.send_messages(states)

    def _set_muted(self, new_state: bool) -> None:
        self.state.muted = bool(new_state)
        self._emit_ipc_event("muted", value=self.state.muted)

        if self.state.muted:
            # voice_assistant.stop behavior
            _LOGGER.debug("Muting voice assistant (voice_assistant.stop)")
            self._is_streaming_audio = False
            self.state.tts_player.stop()
            # Stop any ongoing voice processing
            self.state.stop_word.is_active = False
            self.state.tts_player.play(self.state.mute_sound)
        else:
            # voice_assistant.start_continuous behavior
            _LOGGER.debug("Unmuting voice assistant (voice_assistant.start_continuous)")
            self.state.tts_player.play(self.state.unmute_sound)
            # Resume normal operation - wake word detection will be active again
            pass

    def _emit_ipc_event(self, event: str, **data: object) -> None:
        if self.state.ipc_bridge is None:
            return
        self.state.ipc_bridge.emit_event(event, **data)

    def _get_system_volume(self) -> float:
        cmd = ["amixer"]
        if self.state.system_volume_device:
            cmd.extend(["-D", self.state.system_volume_device])
        cmd.extend(["sget", self.state.system_volume_control])

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            _LOGGER.warning(
                "Unable to read system volume (%s): %s",
                self.state.system_volume_control,
                result.stderr.strip() or result.stdout.strip(),
            )
            return 0.0

        if match := re.search(r"\[(\d{1,3})%\]", result.stdout):
            return float(max(0, min(100, int(match.group(1)))))

        _LOGGER.warning(
            "Unable to parse system volume from amixer output for control '%s'",
            self.state.system_volume_control,
        )
        return 0.0

    def _set_system_volume(self, value: float) -> bool:
        target = max(0, min(100, int(round(value))))
        cmd = ["amixer"]
        if self.state.system_volume_device:
            cmd.extend(["-D", self.state.system_volume_device])
        cmd.extend(["sset", self.state.system_volume_control, f"{target}%"])

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return True

        _LOGGER.warning(
            "Unable to set system volume to %s%% (%s): %s",
            target,
            self.state.system_volume_control,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False

    def _run_systemctl_action(self, action: str) -> None:
        commands = (
            ["sudo", "-n", "systemctl", action],
            ["systemctl", action],
        )
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                _LOGGER.info("Executed system action '%s' using: %s", action, " ".join(cmd))
                return

        _LOGGER.error(
            "Failed system action '%s': %s",
            action,
            result.stderr.strip() or result.stdout.strip(),
        )

    def _shutdown_system(self) -> None:
        self._run_systemctl_action("poweroff")

    def _reboot_system(self) -> None:
        self._run_systemctl_action("reboot")

    def _adjust_volume(self, step: int) -> None:
        if self.state.system_volume_entity is not None:
            current = int(round(self.state.system_volume_entity.get_volume()))
            target = max(0, min(100, current + step))
            if target != current and self.state.system_volume_entity.set_volume(target):
                self.send_messages([self.state.system_volume_entity.get_state_message()])
                _LOGGER.debug("Local IPC system volume set to %s%%", target)
            return

        entity = self.state.media_player_entity
        if entity is None:
            return

        current = int(round(entity.volume * 100))
        target = max(0, min(100, current + step))
        if target == current:
            return

        entity.music_player.set_volume(target)
        entity.announce_player.set_volume(target)
        entity.volume = target / 100.0
        self.send_messages([entity._get_state_message()])  # noqa: SLF001
        _LOGGER.debug("Local IPC volume set to %s%%", target)

    def _handle_local_command(self, cmd: str) -> None:
        cmd = cmd.strip().lower()

        if cmd == "mute_toggle":
            self._set_muted(not self.state.muted)
            if self.state.mute_switch_entity is not None:
                self.state.mute_switch_entity.sync_with_state()
                self.send_messages(
                    [SwitchStateResponse(key=self.state.mute_switch_entity.key, state=self.state.muted)]
                )
            return

        if cmd == "mute_on":
            self._set_muted(True)
            return

        if cmd == "mute_off":
            self._set_muted(False)
            return

        if cmd == "volume_up":
            self._adjust_volume(5)
            return

        if cmd == "volume_down":
            self._adjust_volume(-5)
            return

        if cmd == "shutdown":
            self._shutdown_system()
            return

        if cmd == "reboot":
            self._reboot_system()
            return
            
    def handle_voice_event(
        self, event_type: VoiceAssistantEventType, data: Dict[str, str]
    ) -> None:
        _LOGGER.debug("Voice event: type=%s, data=%s", event_type.name, data)
        self._emit_ipc_event("voice_event", type=event_type.name)

        if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START:
            self._emit_ipc_event("run_start")
            self._tts_url = data.get("url")
            self._tts_played = False
            self._continue_conversation = False
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START and self.state.thinking_sound_enabled:
            self._emit_ipc_event("intent_start")
            # Play short "thinking/processing" sound if configured
            processing = getattr(self.state, "processing_sound", None)
            if processing:
                _LOGGER.debug("Playing processing sound: %s", processing)
                self.state.stop_word.is_active = True
                self._processing = True
                self.duck()
                self.state.tts_player.play(self.state.processing_sound)            
        elif event_type in (
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_END,
        ):
            self._emit_ipc_event("listening_end")
            self._is_streaming_audio = False
            self._listening_trigger = None
        elif event_type in (
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_START,
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START,
        ):
            self._emit_ipc_event("listening_start")
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS:
            if data.get("tts_start_streaming") == "1":
                # Start streaming early
                self.play_tts()
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END:
            if data.get("continue_conversation") == "1":
                self._continue_conversation = True
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END:
            self._emit_ipc_event("tts_end")
            self._tts_url = data.get("url")
            self.play_tts()
        elif event_type in (
            VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START,
            VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_START,
        ):
            self._emit_ipc_event("tts_start")
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END:
            self._emit_ipc_event("run_end")
            self._is_streaming_audio = False
            self._listening_trigger = None
            if not self._tts_played:
                self._tts_finished()

            self._tts_played = False

        # TODO: handle error

    def handle_timer_event(
        self,
        event_type: VoiceAssistantTimerEventType,
        msg: VoiceAssistantTimerEventResponse,
    ) -> None:
        _LOGGER.debug("Timer event: type=%s", event_type.name)
        if event_type == VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_FINISHED:
            if not self._timer_finished:
                self.state.active_wake_words.add(self.state.stop_word.id)
                self._timer_finished = True
                self.duck()
                self._play_timer_finished()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, VoiceAssistantEventResponse):
            # Pipeline event
            data: Dict[str, str] = {}
            for arg in msg.data:
                data[arg.name] = arg.value

            self.handle_voice_event(VoiceAssistantEventType(msg.event_type), data)
        elif isinstance(msg, VoiceAssistantAnnounceRequest):
            _LOGGER.debug("Announcing: %s", msg.text)

            assert self.state.media_player_entity is not None

            urls = []
            if msg.preannounce_media_id:
                urls.append(msg.preannounce_media_id)

            urls.append(msg.media_id)

            self.state.active_wake_words.add(self.state.stop_word.id)
            self._continue_conversation = msg.start_conversation

            self.duck()
            yield from self.state.media_player_entity.play(
                urls, announcement=True, done_callback=self._tts_finished
            )
        elif isinstance(msg, VoiceAssistantTimerEventResponse):
            self.handle_timer_event(VoiceAssistantTimerEventType(msg.event_type), msg)
        elif isinstance(msg, DeviceInfoRequest):
            # Compute dynamic device name
            base_name = re.sub(r'[\s-]+', '-', self.state.name.lower()).strip('-')
            mac_no_colon = self.state.mac_address.replace(":", "").lower()
            mac_last6 = mac_no_colon[-6:]
            device_name = f"{base_name}-{mac_last6}"
            
            yield DeviceInfoResponse(
                uses_password=False,
                name=device_name,
                mac_address=self.state.mac_address,
                manufacturer="Open Home Foundation",
                model="Linux Voice Assistant",                
                voice_assistant_feature_flags=(
                    VoiceAssistantFeature.VOICE_ASSISTANT
                    | VoiceAssistantFeature.API_AUDIO
                    | VoiceAssistantFeature.ANNOUNCE
                    | VoiceAssistantFeature.START_CONVERSATION
                    | VoiceAssistantFeature.TIMERS
                ),
            )
        elif isinstance(
            msg,
            (
                ListEntitiesRequest,
                SubscribeHomeAssistantStatesRequest,
                MediaPlayerCommandRequest,
                NumberCommandRequest,
                SelectCommandRequest,
                SwitchCommandRequest,
                ButtonCommandRequest,
            ),
        ):
            for entity in self.state.entities:
                yield from entity.handle_message(msg)

            if isinstance(msg, ListEntitiesRequest):
                yield ListEntitiesDoneResponse()
        elif isinstance(msg, VoiceAssistantConfigurationRequest):
            available_wake_words = [
                VoiceAssistantWakeWord(
                    id=ww.id,
                    wake_word=ww.wake_word,
                    trained_languages=ww.trained_languages,
                )
                for ww in self.state.available_wake_words.values()
            ]

            for eww in msg.external_wake_words:
                if eww.model_type != "micro":
                    continue

                available_wake_words.append(
                    VoiceAssistantWakeWord(
                        id=eww.id,
                        wake_word=eww.wake_word,
                        trained_languages=eww.trained_languages,
                    )
                )

                self._external_wake_words[eww.id] = eww

            yield VoiceAssistantConfigurationResponse(
                available_wake_words=available_wake_words,
                active_wake_words=[
                    ww.id
                    for ww in self.state.wake_words.values()
                    if ww.id in self.state.active_wake_words
                ],
                max_active_wake_words=2,
            )
            _LOGGER.info("Connected to Home Assistant")
            self._emit_ipc_event("ha_connected")
            self._publish_led_intensity()
            self._publish_led_night_mode()
            self._publish_wake_word_threshold_state()
        elif isinstance(msg, VoiceAssistantSetConfiguration):
            # Change active wake words
            active_wake_words: Set[str] = set()

            for wake_word_id in msg.active_wake_words:
                if wake_word_id in self.state.wake_words:
                    # Already active
                    active_wake_words.add(wake_word_id)
                    continue

                model_info = self.state.available_wake_words.get(wake_word_id)
                if not model_info:
                    # Check external wake words (may require download)
                    external_wake_word = self._external_wake_words.get(wake_word_id)
                    if not external_wake_word:
                        continue

                    model_info = self._download_external_wake_word(external_wake_word)
                    if not model_info:
                        continue

                    self.state.available_wake_words[wake_word_id] = model_info

                _LOGGER.debug("Loading wake word: %s", model_info.wake_word_path)
                loaded_wake_word = model_info.load()
                self.state.wake_words[wake_word_id] = loaded_wake_word
                self._apply_wake_word_threshold(log_change=False)

                _LOGGER.info("Wake word set: %s", wake_word_id)
                active_wake_words.add(wake_word_id)
                break

            self.state.active_wake_words = active_wake_words
            _LOGGER.debug("Active wake words: %s", active_wake_words)

            self.state.preferences.active_wake_words = list(active_wake_words)
            self.state.save_preferences()
            self.state.wake_words_changed = True

    def handle_audio(self, audio_chunk: bytes) -> None:

        if not self._is_streaming_audio or self.state.muted:
            return

        self.send_messages([VoiceAssistantAudio(data=audio_chunk)])

    def wakeup(self, wake_word: Union[MicroWakeWord, OpenWakeWord]) -> None:
        if not self.state.wake_word_detection_enabled:
            return

        if self._timer_finished:
            # Stop timer instead
            self._timer_finished = False
            self.state.tts_player.stop()
            _LOGGER.debug("Stopping timer finished sound")
            return

        if self.state.muted:
            # Don't respond to wake words when muted (voice_assistant.stop behavior)
            return
        
        wake_word_phrase = wake_word.wake_word
        _LOGGER.debug("Detected wake word: %s", wake_word_phrase)
        self._emit_ipc_event("wake_word", phrase=wake_word_phrase)
        self.send_messages(
            [VoiceAssistantRequest(start=True, wake_word_phrase=wake_word_phrase)]
        )
        self.duck()
        self._is_streaming_audio = True
        self._listening_trigger = "wake_word"
        self.state.tts_player.play(self.state.wakeup_sound)

    def stop(self) -> None:
        self.state.active_wake_words.discard(self.state.stop_word.id)
        self.state.tts_player.stop()

        if self._timer_finished:
            self._timer_finished = False
            _LOGGER.debug("Stopping timer finished sound")
        else:
            _LOGGER.debug("TTS response stopped manually")
            self._tts_finished()

    def play_tts(self) -> None:
        if (not self._tts_url) or self._tts_played:
            return

        self._tts_played = True
        _LOGGER.debug("Playing TTS response: %s", self._tts_url)

        self.state.active_wake_words.add(self.state.stop_word.id)
        self.state.tts_player.play(self._tts_url, done_callback=self._tts_finished)

    def duck(self) -> None:
        _LOGGER.debug("Ducking music")
        self.state.music_player.duck()

    def unduck(self) -> None:
        _LOGGER.debug("Unducking music")
        self.state.music_player.unduck()

    def _tts_finished(self) -> None:
        self._emit_ipc_event("tts_finished")
        self.state.active_wake_words.discard(self.state.stop_word.id)
        self.send_messages([VoiceAssistantAnnounceFinished()])

        if self._continue_conversation:
            self.send_messages([VoiceAssistantRequest(start=True)])
            self._is_streaming_audio = True
            _LOGGER.debug("Continuing conversation")
        else:
            self.unduck()

        _LOGGER.debug("TTS response finished")

    def _play_timer_finished(self) -> None:
        if not self._timer_finished:
            self.unduck()
            return

        self.state.tts_player.play(
            self.state.timer_finished_sound,
            done_callback=lambda: call_all(
                lambda: time.sleep(1.0), self._play_timer_finished
            ),
        )

    def connection_lost(self, exc):
        super().connection_lost(exc)

        self._disconnect_event.set()
        self._is_streaming_audio = False
        self._listening_trigger = None
        self._tts_url = None
        self._tts_played = False
        self._continue_conversation = False
        self._timer_finished = False
        self._distance_activation_latched = False

        # Stop any ongoing audio playback and wake/stop word processing.
        try:
            self.state.music_player.stop()
        except Exception:  # pragma: no cover - defensive safety net
            _LOGGER.exception("Failed to stop music player during disconnect")

        try:
            self.state.tts_player.stop()
        except Exception:  # pragma: no cover - defensive safety net
            _LOGGER.exception("Failed to stop TTS player during disconnect")

        self.state.stop_word.is_active = False
        self.state.connected = False
        if self.state.satellite is self:
            self.state.satellite = None

        if self._distance_task is not None:
            self._distance_task.cancel()
            self._distance_task = None

        if self.state.mute_switch_entity is not None:
            self.state.mute_switch_entity.sync_with_state()

        _LOGGER.info("Disconnected from Home Assistant; waiting for reconnection")
        self._emit_ipc_event("ha_disconnected")

    def process_packet(self, msg_type: int, packet_data: bytes) -> None:
        super().process_packet(msg_type, packet_data)

        if msg_type == PROTO_TO_MESSAGE_TYPE[AuthenticationRequest]:
            self.state.connected = True
            # Send states after connect
            states = []
            for entity in self.state.entities:
                states.extend(entity.handle_message(SubscribeHomeAssistantStatesRequest()))
            self.send_messages(states)
            _LOGGER.debug("Sent entity states after connect")        
        _LOGGER.info("Disconnected from Home Assistant")

    def _download_external_wake_word(
        self, external_wake_word: VoiceAssistantExternalWakeWord
    ) -> Optional[AvailableWakeWord]:
        eww_dir = self.state.download_dir / "external_wake_words"
        eww_dir.mkdir(parents=True, exist_ok=True)

        config_path = eww_dir / f"{external_wake_word.id}.json"
        should_download_config = not config_path.exists()

        # Check if we need to download the model file
        model_path = eww_dir / f"{external_wake_word.id}.tflite"
        should_download_model = True
        if model_path.exists():
            model_size = model_path.stat().st_size
            if model_size == external_wake_word.model_size:
                with open(model_path, "rb") as model_file:
                    model_hash = hashlib.sha256(model_file.read()).hexdigest()

                if model_hash == external_wake_word.model_hash:
                    should_download_model = False
                    _LOGGER.debug(
                        "Model size and hash match for %s. Skipping download.",
                        external_wake_word.id,
                    )

        if should_download_config or should_download_model:
            # Download config
            _LOGGER.debug("Downloading %s to %s", external_wake_word.url, config_path)
            with urlopen(external_wake_word.url) as request:
                if request.status != 200:
                    _LOGGER.warning(
                        "Failed to download: %s, status=%s",
                        external_wake_word.url,
                        request.status,
                    )
                    return None

                with open(config_path, "wb") as model_file:
                    shutil.copyfileobj(request, model_file)

        if should_download_model:
            # Download model file
            parsed_url = urlparse(external_wake_word.url)
            parsed_url = parsed_url._replace(
                path=posixpath.join(posixpath.dirname(parsed_url.path), model_path.name)
            )
            model_url = urlunparse(parsed_url)

            _LOGGER.debug("Downloading %s to %s", model_url, model_path)
            with urlopen(model_url) as request:
                if request.status != 200:
                    _LOGGER.warning(
                        "Failed to download: %s, status=%s", model_url, request.status
                    )
                    return None

                with open(model_path, "wb") as model_file:
                    shutil.copyfileobj(request, model_file)

        return AvailableWakeWord(
            id=external_wake_word.id,
            type=WakeWordType.MICRO_WAKE_WORD,
            wake_word=external_wake_word.wake_word,
            trained_languages=external_wake_word.trained_languages,
            wake_word_path=config_path,
        )

    def process_packet(self, msg_type: int, packet_data: bytes) -> None:
        super().process_packet(msg_type, packet_data)

        if msg_type == PROTO_TO_MESSAGE_TYPE[AuthenticationRequest]:
            self.state.connected = True
            # Send states after connect
            states = []
            for entity in self.state.entities:
                states.extend(entity.handle_message(SubscribeHomeAssistantStatesRequest()))
            self.send_messages(states)
            _LOGGER.debug("Sent entity states after connect")
