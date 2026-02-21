"""Shared models."""

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Union

if TYPE_CHECKING:
    from pymicro_wakeword import MicroWakeWord
    from pyopen_wakeword import OpenWakeWord

    from .gpio_controller import LvaGpioController
    from .local_ipc import LocalIpcBridge
    from .distance_reader import DistanceReader
    from .entity import (
        DistanceSensorEntity,
        DistanceActivationSwitchEntity,
        DistanceActivationSoundSwitchEntity,
        DistanceActivationThresholdNumberEntity,
        AttentionRequiredSwitchEntity,
        EngagedVadWindowNumberEntity,
        ESPHomeEntity,
        LedIntensityNumberEntity,
        MediaPlayerEntity,
        MuteSwitchEntity,
        NightModeSwitchEntity,
        RebootButtonEntity,
        ShutdownButtonEntity,
        SystemVolumeNumberEntity,
        ThinkingSoundEntity,
        VisionCooldownNumberEntity,
        VisionEnabledSwitchEntity,
        VisionMinConfidenceNumberEntity,
        LastAttentionStateSensorEntity,
        LastVisionLatencySensorEntity,
        LastVisionErrorSensorEntity,
        WakeWordDetectionSwitchEntity,
        WakeWordThresholdNumberEntity,
        WakeWordThresholdPresetSelectEntity,
    )
    from .mpv_player import MpvMediaPlayer
    from .satellite import VoiceSatelliteProtocol

_LOGGER = logging.getLogger(__name__)


class WakeWordType(str, Enum):
    MICRO_WAKE_WORD = "micro"
    OPEN_WAKE_WORD = "openWakeWord"


WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT = "ModelDefault"
WAKE_WORD_THRESHOLD_PRESET_CUSTOM = "Custom"
WAKE_WORD_THRESHOLD_PRESETS: Dict[str, float] = {
    "Strict": 0.60,
    "Default": 0.50,
    "Sensitive": 0.45,
    "VerySensitive": 0.40,
}
WAKE_WORD_THRESHOLD_PRESET_OPTIONS: List[str] = [
    WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT,
    *WAKE_WORD_THRESHOLD_PRESETS.keys(),
    WAKE_WORD_THRESHOLD_PRESET_CUSTOM,
]
WAKE_WORD_THRESHOLD_MIN = 0.10
WAKE_WORD_THRESHOLD_MAX = 0.95
WAKE_WORD_THRESHOLD_DEFAULT_CUSTOM = WAKE_WORD_THRESHOLD_PRESETS["Default"]


def normalize_wake_word_threshold(value: object) -> float:
    """Normalize numeric threshold into valid bounds."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = WAKE_WORD_THRESHOLD_DEFAULT_CUSTOM

    return max(WAKE_WORD_THRESHOLD_MIN, min(WAKE_WORD_THRESHOLD_MAX, parsed))


def normalize_wake_word_threshold_preset(value: object) -> str:
    """Normalize threshold preset to known options."""
    if isinstance(value, str) and (value in WAKE_WORD_THRESHOLD_PRESET_OPTIONS):
        return value

    return WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT


def resolve_wake_word_threshold(
    preset: object,
    custom_value: object,
) -> Optional[float]:
    """Resolve threshold value from preset/custom settings."""
    normalized_preset = normalize_wake_word_threshold_preset(preset)
    if normalized_preset == WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT:
        return None

    if normalized_preset == WAKE_WORD_THRESHOLD_PRESET_CUSTOM:
        return normalize_wake_word_threshold(custom_value)

    return WAKE_WORD_THRESHOLD_PRESETS[normalized_preset]


@dataclass
class AvailableWakeWord:
    id: str
    type: WakeWordType
    wake_word: str
    trained_languages: List[str]
    wake_word_path: Path

    def load(self) -> "Union[MicroWakeWord, OpenWakeWord]":
        if self.type == WakeWordType.MICRO_WAKE_WORD:
            from pymicro_wakeword import MicroWakeWord

            return MicroWakeWord.from_config(config_path=self.wake_word_path)

        if self.type == WakeWordType.OPEN_WAKE_WORD:
            from pyopen_wakeword import OpenWakeWord

            oww_model = OpenWakeWord.from_model(model_path=self.wake_word_path)
            setattr(oww_model, "wake_word", self.wake_word)

            return oww_model

        raise ValueError(f"Unexpected wake word type: {self.type}")


@dataclass
class Preferences:
    active_wake_words: List[str] = field(default_factory=list)
    thinking_sound: int = 0  # 0 = disabled, 1 = enabled
    led_intensity: int = 100  # 0..100%
    led_night_mode: int = 0  # 0 = disabled, 1 = enabled
    wake_word_detection: int = 1  # 0 = disabled, 1 = enabled
    distance_activation: int = 0  # 0 = disabled, 1 = enabled
    distance_activation_sound: int = 1  # 0 = disabled, 1 = enabled
    distance_activation_threshold_mm: float = 120.0
    distance_sensor_model: str = "l0x"
    vision_enabled: int = 1  # 0 = disabled, 1 = enabled
    attention_required: int = 1  # 0 = distance-only, 1 = require attention
    vision_cooldown_s: float = 4.0
    vision_min_confidence: float = 0.60
    engaged_vad_window_s: float = 2.5
    wake_word_threshold_preset: str = WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT
    wake_word_threshold_custom: float = WAKE_WORD_THRESHOLD_DEFAULT_CUSTOM

@dataclass
class ServerState:
    name: str
    mac_address: str
    audio_queue: "Queue[Optional[bytes]]"
    entities: "List[ESPHomeEntity]"
    available_wake_words: "Dict[str, AvailableWakeWord]"
    wake_words: "Dict[str, Union[MicroWakeWord, OpenWakeWord]]"
    active_wake_words: Set[str]
    stop_word: "MicroWakeWord"
    music_player: "MpvMediaPlayer"
    tts_player: "MpvMediaPlayer"
    wakeup_sound: str
    processing_sound: str
    timer_finished_sound: str
    mute_sound: str
    unmute_sound: str
    system_volume_device: Optional[str]
    system_volume_control: str
    preferences: Preferences
    preferences_path: Path
    download_dir: Path

    media_player_entity: "Optional[MediaPlayerEntity]" = None
    satellite: "Optional[VoiceSatelliteProtocol]" = None
    mute_switch_entity: "Optional[MuteSwitchEntity]" = None
    thinking_sound_entity: "Optional[ThinkingSoundEntity]" = None
    system_volume_entity: "Optional[SystemVolumeNumberEntity]" = None
    shutdown_button_entity: "Optional[ShutdownButtonEntity]" = None
    reboot_button_entity: "Optional[RebootButtonEntity]" = None
    led_intensity_entity: "Optional[LedIntensityNumberEntity]" = None
    night_mode_entity: "Optional[NightModeSwitchEntity]" = None
    wake_word_threshold_select_entity: "Optional[WakeWordThresholdPresetSelectEntity]" = None
    wake_word_threshold_number_entity: "Optional[WakeWordThresholdNumberEntity]" = None
    distance_sensor_entity: "Optional[DistanceSensorEntity]" = None
    wake_word_detection_entity: "Optional[WakeWordDetectionSwitchEntity]" = None
    distance_activation_entity: "Optional[DistanceActivationSwitchEntity]" = None
    distance_activation_sound_entity: "Optional[DistanceActivationSoundSwitchEntity]" = None
    distance_activation_threshold_entity: "Optional[DistanceActivationThresholdNumberEntity]" = None
    vision_enabled_entity: "Optional[VisionEnabledSwitchEntity]" = None
    attention_required_entity: "Optional[AttentionRequiredSwitchEntity]" = None
    vision_cooldown_entity: "Optional[VisionCooldownNumberEntity]" = None
    vision_min_confidence_entity: "Optional[VisionMinConfidenceNumberEntity]" = None
    engaged_vad_window_entity: "Optional[EngagedVadWindowNumberEntity]" = None
    last_attention_state_entity: "Optional[LastAttentionStateSensorEntity]" = None
    last_vision_latency_entity: "Optional[LastVisionLatencySensorEntity]" = None
    last_vision_error_entity: "Optional[LastVisionErrorSensorEntity]" = None
    wake_words_changed: bool = False
    refractory_seconds: float = 2.0
    thinking_sound_enabled: bool = False
    muted: bool = False
    connected: bool = False
    ipc_bridge: "Optional[LocalIpcBridge]" = None
    gpio_controller: "Optional[LvaGpioController]" = None
    distance_reader: "Optional[DistanceReader]" = None
    wake_word_detection_enabled: bool = True
    distance_activation_enabled: bool = False
    distance_activation_sound_enabled: bool = True
    distance_activation_threshold_mm: float = 120.0
    distance_sensor_model: str = "l0x"
    vision_enabled: bool = True
    attention_required: bool = True
    vision_cooldown_s: float = 4.0
    vision_min_confidence: float = 0.60
    engaged_vad_window_s: float = 2.5
    attention_state: str = "UNKNOWN"
    last_vision_latency_ms: float = 0.0
    last_vision_error: str = ""
    vision_request_counter: int = 0
    vision_success_counter: int = 0
    vision_timeout_counter: int = 0
    false_triggers_prevented_counter: int = 0
    xrun_counter: int = 0
    wake_word_threshold: Optional[float] = None
    wake_word_default_thresholds: Dict[str, float] = field(default_factory=dict)
    
    def save_preferences(self) -> None:
        """Save preferences as JSON."""
        _LOGGER.debug("Saving preferences: %s", self.preferences_path)
        self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.preferences_path, "w", encoding="utf-8") as preferences_file:
            json.dump(
                asdict(self.preferences), preferences_file, ensure_ascii=False, indent=4
            )
