"""Application configuration loading."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)
_MODULE_DIR = Path(__file__).parent
_REPO_DIR = _MODULE_DIR.parent
DEFAULT_CONFIG_PATH = _REPO_DIR / "config.json"


@dataclass
class CoreConfig:
    name: str = "LinuxVoiceAssistant"
    audio_input_device: Optional[str] = None
    audio_input_block_size: int = 1024
    audio_output_device: Optional[str] = None
    system_volume_device: Optional[str] = None
    system_volume_control: str = "Speaker"
    wake_word_dirs: list[str] = field(default_factory=lambda: ["wakewords"])
    wake_model: str = "okay_nabu"
    stop_model: str = "stop"
    download_dir: str = "local"
    refractory_seconds: float = 2.0
    wakeup_sound: str = "sounds/wake_word_triggered.flac"
    timer_finished_sound: str = "sounds/timer_finished.flac"
    processing_sound: str = "sounds/processing.wav"
    mute_sound: str = "sounds/mute_switch_on.flac"
    unmute_sound: str = "sounds/mute_switch_off.flac"
    preferences_file: str = "preferences.json"
    host: str = "0.0.0.0"
    port: int = 6053
    enable_thinking_sound: bool = False
    enable_gpio_control: bool = True
    gpio_feedback_device: str = "sysdefault:CARD=wm8960soundcard"
    wake_word_detection: Optional[bool] = None
    distance_activation: Optional[bool] = None
    distance_activation_threshold_mm: Optional[float] = None
    distance_sensor_model: Optional[str] = None
    vision_enabled: Optional[bool] = None
    attention_required: Optional[bool] = None
    vision_cooldown_s: Optional[float] = None
    vision_min_confidence: Optional[float] = None
    engaged_vad_window_s: Optional[float] = None
    log_level: str = "INFO"


@dataclass
class VisdConfig:
    camera_index: int = 0
    burst_seconds: float = 0.9
    frame_count: int = 5
    width: int = 320
    height: int = 240
    face_snapshot_host: str = "0.0.0.0"
    face_snapshot_port: int = 8766
    log_level: str = "INFO"


@dataclass
class FrontpaneldConfig:
    mute_pin: int = 17
    vol_up_pin: int = 22
    vol_down_pin: int = 23
    enc_a_pin: int = 5
    enc_b_pin: int = 6
    log_level: str = "INFO"


@dataclass
class AppConfig:
    core: CoreConfig = field(default_factory=CoreConfig)
    visd: VisdConfig = field(default_factory=VisdConfig)
    frontpaneld: FrontpaneldConfig = field(default_factory=FrontpaneldConfig)


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _apply_mapping(target: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(target, key):
            continue
        if isinstance(getattr(target, key), bool) or key in {
            "wake_word_detection",
            "distance_activation",
            "vision_enabled",
            "attention_required",
        }:
            bool_value = _coerce_bool(value)
            if bool_value is None and key in {
                "wake_word_detection",
                "distance_activation",
                "vision_enabled",
                "attention_required",
            }:
                setattr(target, key, None)
            elif bool_value is not None:
                setattr(target, key, bool_value)
            continue
        setattr(target, key, value)


def _write_default_config(path: Path, config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as config_file:
        json.dump(asdict(config), config_file, ensure_ascii=False, indent=4)


def get_config_path() -> Path:
    env_path = os.environ.get("LVA_CONFIG_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def resolve_repo_path(path: str | Path) -> Path:
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj
    return (_REPO_DIR / path_obj).resolve()


def load_config(path: Optional[Path] = None) -> AppConfig:
    config = AppConfig()
    config_path = get_config_path() if path is None else path

    if not config_path.exists():
        _write_default_config(config_path, config)
        _LOGGER.info("Created default config: %s", config_path)
        return config

    with open(config_path, "r", encoding="utf-8") as config_file:
        loaded = json.load(config_file)
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid config format in {config_path}: expected object")

    core = loaded.get("core")
    if isinstance(core, dict):
        _apply_mapping(config.core, core)
    visd = loaded.get("visd")
    if isinstance(visd, dict):
        _apply_mapping(config.visd, visd)
    frontpaneld = loaded.get("frontpaneld")
    if isinstance(frontpaneld, dict):
        _apply_mapping(config.frontpaneld, frontpaneld)
    return config
