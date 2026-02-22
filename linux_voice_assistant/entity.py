import logging
from abc import abstractmethod
from collections.abc import Iterable
from typing import Callable, List, Optional, Union

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    BinarySensorStateResponse,
    ButtonCommandRequest,
    CameraImageRequest,
    CameraImageResponse,
    ListEntitiesBinarySensorResponse,
    ListEntitiesCameraResponse,
    ListEntitiesMediaPlayerResponse,
    ListEntitiesNumberResponse,
    ListEntitiesRequest,
    ListEntitiesSensorResponse,
    ListEntitiesTextSensorResponse,
    ListEntitiesSwitchResponse,
    ListEntitiesButtonResponse,
    ListEntitiesSelectResponse,
    NumberCommandRequest,
    NumberMode,
    NumberStateResponse,
    SensorStateResponse,
    SelectCommandRequest,
    SelectStateResponse,
    SwitchCommandRequest,
    SwitchStateResponse,
    TextSensorStateResponse,
    MediaPlayerCommandRequest,
    MediaPlayerStateResponse,
    SubscribeHomeAssistantStatesRequest,
)
from aioesphomeapi.model import (
    MediaPlayerCommand,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    SensorStateClass,
    EntityCategory,
)
from google.protobuf import message

from .api_server import APIServer
from .models import (
    WAKE_WORD_THRESHOLD_MAX,
    WAKE_WORD_THRESHOLD_MIN,
    WAKE_WORD_THRESHOLD_PRESET_OPTIONS,
)
from .mpv_player import MpvMediaPlayer
from .util import call_all

_LOGGER = logging.getLogger(__name__)

SUPPORTED_MEDIA_PLAYER_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

class ESPHomeEntity:
    def __init__(self, server: APIServer) -> None:
        self.server = server

    @abstractmethod
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        pass


# -----------------------------------------------------------------------------


class FaceSnapshotCameraEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_image_bytes: Callable[[], bytes],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_image_bytes = get_image_bytes

    def update_get_image_bytes(self, get_image_bytes: Callable[[], bytes]) -> None:
        self._get_image_bytes = get_image_bytes

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesCameraResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
            )
            return

        if isinstance(msg, CameraImageRequest):
            image_data = b""
            try:
                image_data = self._get_image_bytes()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to fetch face snapshot image: %s", err)
            yield CameraImageResponse(key=self.key, data=image_data, done=True)


# -----------------------------------------------------------------------------


class MediaPlayerEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        music_player: MpvMediaPlayer,
        announce_player: MpvMediaPlayer,
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self.state = MediaPlayerState.IDLE
        self.volume = 1.0
        self.muted = False
        self.previous_volume = 1.0
        self.music_player = music_player
        self.announce_player = announce_player

    def play(
        self,
        url: Union[str, List[str]],
        announcement: bool = False,
        done_callback: Optional[Callable[[], None]] = None,
    ) -> Iterable[message.Message]:
        if announcement:
            if self.music_player.is_playing:
                # Announce, resume music
                self.music_player.pause()
                self.announce_player.play(
                    url,
                    done_callback=lambda: call_all(
                        self.music_player.resume, done_callback
                    ),
                )
            else:
                # Announce, idle
                self.announce_player.play(
                    url,
                    done_callback=lambda: call_all(
                        self.server.send_messages(
                            [self._update_state(MediaPlayerState.IDLE)]
                        ),
                        done_callback,
                    ),
                )
        else:
            # Music
            self.music_player.play(
                url,
                done_callback=lambda: call_all(
                    self.server.send_messages(
                        [self._update_state(MediaPlayerState.IDLE)]
                    ),
                    done_callback,
                ),
            )

        yield self._update_state(MediaPlayerState.PLAYING)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, MediaPlayerCommandRequest) and (msg.key == self.key):
            if msg.has_media_url:
                announcement = msg.has_announcement and msg.announcement
                yield from self.play(msg.media_url, announcement=announcement)
            elif msg.has_command:
                command = MediaPlayerCommand(msg.command)
                if msg.command == MediaPlayerCommand.PAUSE:
                    self.music_player.pause()
                    yield self._update_state(MediaPlayerState.PAUSED)
                elif msg.command == MediaPlayerCommand.PLAY:
                    self.music_player.resume()
                    yield self._update_state(MediaPlayerState.PLAYING)
                elif command == MediaPlayerCommand.MUTE:
                    if not self.muted:
                        self.previous_volume = self.volume
                        self.volume = 0
                        self.music_player.set_volume(0)
                        self.announce_player.set_volume(0)
                        self.muted = True
                    yield self._update_state(self.state)
                elif command == MediaPlayerCommand.UNMUTE:
                    if self.muted:
                        self.volume = self.previous_volume
                        self.music_player.set_volume(int(self.volume * 100))
                        self.announce_player.set_volume(int(self.volume * 100))
                        self.muted = False
                    yield self._update_state(self.state)                    
            elif msg.has_volume:
                volume = int(msg.volume * 100)
                self.music_player.set_volume(volume)
                self.announce_player.set_volume(volume)
                self.volume = msg.volume
                yield self._update_state(self.state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesMediaPlayerResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                supports_pause=True,
                feature_flags=SUPPORTED_MEDIA_PLAYER_FEATURES,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self._get_state_message()

    def _update_state(self, new_state: MediaPlayerState) -> MediaPlayerStateResponse:
        self.state = new_state
        return self._get_state_message()

    def _get_state_message(self) -> MediaPlayerStateResponse:
        return MediaPlayerStateResponse(
            key=self.key,
            state=self.state,
            volume=self.volume,
            muted=self.muted,
        )

# -----------------------------------------------------------------------------

class MuteSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_muted: Callable[[], bool],
        set_muted: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_muted = get_muted
        self._set_muted = set_muted
        self._switch_state = self._get_muted()  # Sync internal state with actual muted value on init

    def update_set_muted(self, set_muted: Callable[[bool], None]) -> None:
        # Update the callback used to change the mute state.
        self._set_muted = set_muted
    
    def update_get_muted(self, get_muted: Callable[[], bool]) -> None:
        # Update the callback used to read the mute state.
        self._get_muted = get_muted

    def sync_with_state(self) -> None:
        # Sync internal switch state with the actual mute state.
        self._switch_state = self._get_muted()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            # User toggled the switch - update our internal state and trigger actions
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_muted(new_state)
            # Return the new state immediately
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:microphone-off",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            # Always return our internal switch state
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
            
class ThinkingSoundEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_thinking_sound_enabled: Callable[[], bool],
        set_thinking_sound_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_thinking_sound_enabled = get_thinking_sound_enabled
        self._set_thinking_sound_enabled = set_thinking_sound_enabled
        self._switch_state = self._get_thinking_sound_enabled()  # Sync internal state
        
    def update_get_thinking_sound_enabled(self, get_thinking_sound_enabled: Callable[[], bool]) -> None:
        # Update the callback used to read the thinking sound enabled state.
        self._get_thinking_sound_enabled = get_thinking_sound_enabled

    def update_set_thinking_sound_enabled(self, set_thinking_sound_enabled: Callable[[bool], None]) -> None:
        # Update the callback used to change the thinking sound enabled state.
        self._set_thinking_sound_enabled = set_thinking_sound_enabled

    def sync_with_state(self) -> None:
        # Sync internal switch state with the actual thinking sound enabled state.
        self._switch_state = self._get_thinking_sound_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            # User toggled the switch - update our internal state and trigger actions
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_thinking_sound_enabled(new_state)
            # Return the new state immediately
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:music-note",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            # Always return our internal switch state
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class NightModeSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_enabled = get_enabled
        self._set_enabled = set_enabled
        self._switch_state = self._get_enabled()

    def update_get_enabled(self, get_enabled: Callable[[], bool]) -> None:
        self._get_enabled = get_enabled

    def update_set_enabled(self, set_enabled: Callable[[bool], None]) -> None:
        self._set_enabled = set_enabled

    def sync_with_state(self) -> None:
        self._switch_state = self._get_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_enabled(new_state)
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:weather-night",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class WakeWordDetectionSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_enabled = get_enabled
        self._set_enabled = set_enabled
        self._switch_state = self._get_enabled()

    def update_get_enabled(self, get_enabled: Callable[[], bool]) -> None:
        self._get_enabled = get_enabled

    def update_set_enabled(self, set_enabled: Callable[[bool], None]) -> None:
        self._set_enabled = set_enabled

    def sync_with_state(self) -> None:
        self._switch_state = self._get_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_enabled(new_state)
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:account-voice",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class DistanceActivationSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_enabled = get_enabled
        self._set_enabled = set_enabled
        self._switch_state = self._get_enabled()

    def update_get_enabled(self, get_enabled: Callable[[], bool]) -> None:
        self._get_enabled = get_enabled

    def update_set_enabled(self, set_enabled: Callable[[bool], None]) -> None:
        self._set_enabled = set_enabled

    def sync_with_state(self) -> None:
        self._switch_state = self._get_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_enabled(new_state)
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:motion-sensor",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class DistanceActivationSoundSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_enabled = get_enabled
        self._set_enabled = set_enabled
        self._switch_state = self._get_enabled()

    def update_get_enabled(self, get_enabled: Callable[[], bool]) -> None:
        self._get_enabled = get_enabled

    def update_set_enabled(self, set_enabled: Callable[[bool], None]) -> None:
        self._set_enabled = set_enabled

    def sync_with_state(self) -> None:
        self._switch_state = self._get_enabled()

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            new_state = bool(msg.state)
            self._switch_state = new_state
            self._set_enabled(new_state)
            yield SwitchStateResponse(key=self.key, state=self._switch_state)
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:volume-high",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield SwitchStateResponse(key=self.key, state=self._switch_state)


class SystemVolumeNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_volume: Callable[[], float],
        set_volume: Callable[[float], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_volume = get_volume
        self._set_volume = set_volume
        self._state = max(0.0, min(100.0, self._get_volume()))

    def update_get_volume(self, get_volume: Callable[[], float]) -> None:
        self._get_volume = get_volume

    def update_set_volume(self, set_volume: Callable[[float], bool]) -> None:
        self._set_volume = set_volume

    def sync_with_state(self) -> None:
        self._state = max(0.0, min(100.0, self._get_volume()))

    def get_volume(self) -> float:
        self.sync_with_state()
        return self._state

    def set_volume(self, volume: float) -> bool:
        target = max(0.0, min(100.0, float(volume)))
        if not self._set_volume(target):
            self.sync_with_state()
            return False
        self.sync_with_state()
        return True

    def get_state_message(self) -> NumberStateResponse:
        return NumberStateResponse(key=self.key, state=self._state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            self.set_volume(msg.state)
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:volume-high",
                min_value=0.0,
                max_value=100.0,
                step=1.0,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="%",
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class LedIntensityNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_intensity: Callable[[], float],
        set_intensity: Callable[[float], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_intensity = get_intensity
        self._set_intensity = set_intensity
        self._state = max(0.0, min(100.0, self._get_intensity()))

    def update_get_intensity(self, get_intensity: Callable[[], float]) -> None:
        self._get_intensity = get_intensity

    def update_set_intensity(self, set_intensity: Callable[[float], bool]) -> None:
        self._set_intensity = set_intensity

    def sync_with_state(self) -> None:
        self._state = max(0.0, min(100.0, self._get_intensity()))

    def get_state_message(self) -> NumberStateResponse:
        return NumberStateResponse(key=self.key, state=self._state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            self._set_intensity(msg.state)
            self.sync_with_state()
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:led-strip-variant",
                min_value=0.0,
                max_value=100.0,
                step=1.0,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="%",
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class DistanceActivationThresholdNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_threshold: Callable[[], float],
        set_threshold: Callable[[float], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_threshold = get_threshold
        self._set_threshold = set_threshold
        self._state = max(10.0, min(2000.0, self._get_threshold()))

    def update_get_threshold(self, get_threshold: Callable[[], float]) -> None:
        self._get_threshold = get_threshold

    def update_set_threshold(self, set_threshold: Callable[[float], bool]) -> None:
        self._set_threshold = set_threshold

    def sync_with_state(self) -> None:
        self._state = max(10.0, min(2000.0, self._get_threshold()))

    def get_state_message(self) -> NumberStateResponse:
        return NumberStateResponse(key=self.key, state=self._state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            self._set_threshold(msg.state)
            self.sync_with_state()
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:ruler",
                min_value=10.0,
                max_value=2000.0,
                step=1.0,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="mm",
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class DistanceSensorEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_distance_mm: Callable[[], Optional[float]],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_distance_mm = get_distance_mm

    def update_get_distance_mm(self, get_distance_mm: Callable[[], Optional[float]]) -> None:
        self._get_distance_mm = get_distance_mm

    def get_state_message(self) -> SensorStateResponse:
        value = self._get_distance_mm()
        if value is None:
            return SensorStateResponse(key=self.key, missing_state=True)
        return SensorStateResponse(key=self.key, state=float(value), missing_state=False)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:ruler",
                unit_of_measurement="mm",
                accuracy_decimals=0,
                force_update=False,
                device_class="distance",
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.get_state_message()


class VisionEnabledSwitchEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_enabled = get_enabled
        self._set_enabled = set_enabled
        self._switch_state = self._get_enabled()

    def update_get_enabled(self, get_enabled: Callable[[], bool]) -> None:
        self._get_enabled = get_enabled

    def update_set_enabled(self, set_enabled: Callable[[bool], None]) -> None:
        self._set_enabled = set_enabled

    def sync_with_state(self) -> None:
        self._switch_state = self._get_enabled()

    def get_state_message(self) -> SwitchStateResponse:
        self.sync_with_state()
        return SwitchStateResponse(key=self.key, state=self._switch_state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SwitchCommandRequest) and (msg.key == self.key):
            self._set_enabled(bool(msg.state))
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:camera-outline",
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.get_state_message()


class AttentionRequiredSwitchEntity(VisionEnabledSwitchEntity):
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSwitchResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:eye-outline",
            )
            return
        yield from super().handle_message(msg)


class VisionCooldownNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_value: Callable[[], float],
        set_value: Callable[[float], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_value = get_value
        self._set_value = set_value
        self._state = self._get_value()

    def update_get_value(self, get_value: Callable[[], float]) -> None:
        self._get_value = get_value

    def update_set_value(self, set_value: Callable[[float], bool]) -> None:
        self._set_value = set_value

    def sync_with_state(self) -> None:
        self._state = self._get_value()

    def get_state_message(self) -> NumberStateResponse:
        return NumberStateResponse(key=self.key, state=float(self._state))

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            self._set_value(msg.state)
            self.sync_with_state()
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:timer-cog",
                min_value=0.0,
                max_value=15.0,
                step=0.5,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="s",
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class VisionMinConfidenceNumberEntity(VisionCooldownNumberEntity):
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:percent",
                min_value=0.0,
                max_value=1.0,
                step=0.05,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="",
                entity_category=EntityCategory.CONFIG,
            )
            return
        yield from super().handle_message(msg)


class EngagedVadWindowNumberEntity(VisionCooldownNumberEntity):
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:microphone-clock",
                min_value=0.5,
                max_value=8.0,
                step=0.1,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="s",
                entity_category=EntityCategory.CONFIG,
            )
            return
        yield from super().handle_message(msg)


class LastAttentionStateSensorEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_state: Callable[[], str],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_state = get_state

    def update_get_state(self, get_state: Callable[[], str]) -> None:
        self._get_state = get_state

    def get_state_message(self) -> TextSensorStateResponse:
        state = self._get_state().strip()
        if not state:
            return TextSensorStateResponse(key=self.key, missing_state=True)
        return TextSensorStateResponse(key=self.key, state=state, missing_state=False)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesTextSensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:eye-check-outline",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.get_state_message()


class LastVisionErrorSensorEntity(LastAttentionStateSensorEntity):
    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesTextSensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:alert-circle-outline",
                entity_category=EntityCategory.DIAGNOSTIC,
            )
            return
        yield from super().handle_message(msg)


class LastVisionLatencySensorEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_latency_ms: Callable[[], float],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_latency_ms = get_latency_ms

    def update_get_latency_ms(self, get_latency_ms: Callable[[], float]) -> None:
        self._get_latency_ms = get_latency_ms

    def get_state_message(self) -> SensorStateResponse:
        return SensorStateResponse(key=self.key, state=float(max(0.0, self._get_latency_ms())), missing_state=False)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:timer-outline",
                unit_of_measurement="ms",
                accuracy_decimals=0,
                force_update=False,
                device_class="duration",
                state_class=SensorStateClass.MEASUREMENT,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.get_state_message()


class DiagnosticBinarySensorEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        icon: str,
        get_state: Callable[[], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._icon = icon
        self._get_state = get_state

    def update_get_state(self, get_state: Callable[[], bool]) -> None:
        self._get_state = get_state

    def get_state_message(self) -> BinarySensorStateResponse:
        return BinarySensorStateResponse(
            key=self.key,
            state=bool(self._get_state()),
            missing_state=False,
        )

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesBinarySensorResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon=self._icon,
                is_status_binary_sensor=False,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            yield self.get_state_message()


class FacePresentBinarySensorEntity(DiagnosticBinarySensorEntity):
    pass


class VisionSearchingBinarySensorEntity(DiagnosticBinarySensorEntity):
    pass


class WakeWordThresholdPresetSelectEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_preset: Callable[[], str],
        set_preset: Callable[[str], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_preset = get_preset
        self._set_preset = set_preset
        self._state = self._get_preset()

    def update_get_preset(self, get_preset: Callable[[], str]) -> None:
        self._get_preset = get_preset

    def update_set_preset(self, set_preset: Callable[[str], None]) -> None:
        self._set_preset = set_preset

    def sync_with_state(self) -> None:
        self._state = self._get_preset()

    def get_state_message(self) -> SelectStateResponse:
        return SelectStateResponse(key=self.key, state=self._state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, SelectCommandRequest) and (msg.key == self.key):
            self._set_preset(msg.state)
            self.sync_with_state()
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesSelectResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:tune-variant",
                options=WAKE_WORD_THRESHOLD_PRESET_OPTIONS,
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class WakeWordThresholdNumberEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        get_threshold: Callable[[], float],
        set_threshold: Callable[[float], bool],
    ) -> None:
        ESPHomeEntity.__init__(self, server)

        self.key = key
        self.name = name
        self.object_id = object_id
        self._get_threshold = get_threshold
        self._set_threshold = set_threshold
        self._state = max(
            WAKE_WORD_THRESHOLD_MIN * 100.0,
            min(WAKE_WORD_THRESHOLD_MAX * 100.0, self._get_threshold() * 100.0),
        )

    def update_get_threshold(self, get_threshold: Callable[[], float]) -> None:
        self._get_threshold = get_threshold

    def update_set_threshold(self, set_threshold: Callable[[float], bool]) -> None:
        self._set_threshold = set_threshold

    def sync_with_state(self) -> None:
        self._state = max(
            WAKE_WORD_THRESHOLD_MIN * 100.0,
            min(WAKE_WORD_THRESHOLD_MAX * 100.0, self._get_threshold() * 100.0),
        )

    def get_state_message(self) -> NumberStateResponse:
        return NumberStateResponse(key=self.key, state=self._state)

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, NumberCommandRequest) and (msg.key == self.key):
            # Entity value is in percent for UI readability.
            self._set_threshold(msg.state / 100.0)
            self.sync_with_state()
            yield self.get_state_message()
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesNumberResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                icon="mdi:percent",
                min_value=WAKE_WORD_THRESHOLD_MIN * 100.0,
                max_value=WAKE_WORD_THRESHOLD_MAX * 100.0,
                step=1.0,
                mode=NumberMode.NUMBER_MODE_SLIDER,
                unit_of_measurement="%",
                entity_category=EntityCategory.CONFIG,
            )
        elif isinstance(msg, SubscribeHomeAssistantStatesRequest):
            self.sync_with_state()
            yield self.get_state_message()


class ShutdownButtonEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        shutdown_system: Callable[[], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._shutdown_system = shutdown_system

    def update_shutdown_system(self, shutdown_system: Callable[[], None]) -> None:
        self._shutdown_system = shutdown_system

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ButtonCommandRequest) and (msg.key == self.key):
            try:
                _LOGGER.info("Received shutdown button command from Home Assistant")
                self._shutdown_system()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Shutdown button action failed")
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesButtonResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:power",
            )


class RebootButtonEntity(ESPHomeEntity):
    def __init__(
        self,
        server: APIServer,
        key: int,
        name: str,
        object_id: str,
        reboot_system: Callable[[], None],
    ) -> None:
        ESPHomeEntity.__init__(self, server)
        self.key = key
        self.name = name
        self.object_id = object_id
        self._reboot_system = reboot_system

    def update_reboot_system(self, reboot_system: Callable[[], None]) -> None:
        self._reboot_system = reboot_system

    def handle_message(self, msg: message.Message) -> Iterable[message.Message]:
        if isinstance(msg, ButtonCommandRequest) and (msg.key == self.key):
            try:
                _LOGGER.info("Received reboot button command from Home Assistant")
                self._reboot_system()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Reboot button action failed")
        elif isinstance(msg, ListEntitiesRequest):
            yield ListEntitiesButtonResponse(
                object_id=self.object_id,
                key=self.key,
                name=self.name,
                entity_category=EntityCategory.CONFIG,
                icon="mdi:restart",
            )
