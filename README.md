# Linux Voice Assistant

Experimental Linux voice assistant for [Home Assistant][homeassistant] that uses the [ESPHome][esphome] protocol.

Runs on Linux `aarch64` and `x86_64` platforms. Tested with Python 3.13 and Python 3.11.
Supports announcements, start/continue conversation, timers, distance activation, and attention gating.

## Architecture

The project can run as three cooperating local processes:

* `linux_voice_assistant` (core audio + ESPHome API)
* `linux_voice_assistant.visd` (camera glance checks, on demand)
* `linux_voice_assistant.frontpaneld` (touch + encoder -> logical IPC commands)

IPC sockets:

* control: `/tmp/lva-ipc/control.sock`
* events: `/tmp/lva-ipc/gpio-events.sock`
* vision daemon: `/tmp/lva-ipc/visd.sock`

IPC envelope:

```json
{"type":"MESSAGE_TYPE","payload":{},"ts":1700000000.0,"source":"core|visd|frontpaneld"}
```

## Installation

Install system dependencies (`apt-get`):

* `libportaudio2` or `portaudio19-dev` (for `sounddevice`)
* `build-essential` (for `pymicro-features`)
* `libmpv-dev` (for `python-mpv`)

Clone and install project:

``` sh
git clone https://github.com/OHF-Voice/linux-voice-assistant.git
cd linux-voice-assistant
script/setup
```

## Running

Configuration is file-based only (no runtime CLI flags).

Default config path:

* `/home/user/linux-voice-assistant/config.json`
* or path from env var `LVA_CONFIG_PATH`

Run services:

* `python3 -m linux_voice_assistant`
* `python3 -m linux_voice_assistant.visd`
* `python3 -m linux_voice_assistant.frontpaneld`

Edit `config.json` to change behavior.

### Microphone

Set `core.audio_input_device` in `config.json`.

The microphone device **must** support 16Khz mono audio.

### Speaker

Set `core.audio_output_device` in `config.json`.

### Sounds

Customize via:
* `core.wakeup_sound`
* `core.timer_finished_sound`
* `core.processing_sound`
* `core.mute_sound`
* `core.unmute_sound`

Available sounds:
* **Wake sounds**: `wake_word_triggered.flac` (default), `wake_word_triggered_old.wav`
* **Timer sounds**: `timer_finished.flac` (default), `timer_finished_old.wav`

The optional "thinking" sound plays while the assistant is processing.
Enable it via `core.enable_thinking_sound: true`.

This enables the thinking sound by default and sets the Home Assistant switch to ON. The switch can be toggled at any time from the device page.

### GPIO (LED bar + buttons)

The GPIO controller can run integrated in `linux-voice-assistant` (legacy behavior).
For production front panel handling (touch/encoder), use `linux_voice_assistant.frontpaneld`.

Supported hardware behavior:
* WS2812B LED bar state rendering (`OFF`, `READY`, `MUTED`, `LISTENING`, `PLAYBACK`)
* Mute button on `GPIO17` (`mute_toggle`)
* Volume up/down buttons on `GPIO22`/`GPIO23` (`volume_up`, `volume_down`)
* Local volume feedback sound via `aplay`

Useful config:
* `core.gpio_feedback_device`
* `core.enable_gpio_control`

Notes:
* External IPC sockets (`/tmp/lva-ipc/control.sock`, `/tmp/lva-ipc/gpio-events.sock`) are still available.
* `gpiozero` and `rpi_ws281x` are optional: if unavailable, GPIO/LED features are disabled with warnings.

### VL53L0X / VL53L1X Distance Sensor

The satellite exposes a **Distance** sensor to Home Assistant when a VL53L0X or VL53L1X is connected on I2C.

Behavior:
* Read distance internally every `1s`
* Publish sensor state to Home Assistant every `5s`
* Unit: `mm`

Sensor selection:

* `core.distance_sensor_model`: `"l0x"` or `"l1x"`

### Trigger Modes (Wake Word / Distance / Both)

Control in `config.json`:
* `core.wake_word_detection`
* `core.distance_activation`
* `core.distance_activation_threshold_mm`

### Attention Gating (Vision Overlay on Distance)

When distance activation is enabled, you can require a quick vision check (`FACE_TOWARD`) before starting listening:

Use:
* `core.vision_enabled`
* `core.attention_required`
* `core.vision_cooldown_s`
* `core.vision_min_confidence`
* `core.engaged_vad_window_s`

Behavior summary:

* Primary gate: distance sensor
* Secondary gate (optional): `visd` glance burst (`NO_FACE` / `FACE_AWAY` / `FACE_TOWARD`)
* Listening start uses VAD for distance/manual triggers
* If VAD does not start in `engaged_vad_window_s`, listening is cancelled

## Wake Word

Change the default wake word with `core.wake_model` where the value is the model ID in `wakewords`.

You can include more wakeword directories with `core.wake_word_dirs` where each directory contains either [microWakeWord][] or [openWakeWord][] config files and `.tflite` models.

If you want to add [other wakewords][wakewords-collection], make sure to create a small JSON config file to identify it as an openWakeWord model. For example, download the [GLaDOS][glados] model to `glados.tflite` and create `glados.json` with:

``` json
{
  "type": "openWakeWord",
  "wake_word": "GLaDOS",
  "model": "glados.tflite"
}
```

Add that directory to `core.wake_word_dirs` in `config.json`.

### Wake-Word Threshold (Home Assistant)

The satellite now exposes two configuration entities in Home Assistant:

* **Wake Word Threshold Preset** (`select`): choose one preset:
  * `ModelDefault` (backward-compatible behavior)
  * `Strict` = `60%`
  * `Default` = `50%`
  * `Sensitive` = `45%`
  * `VerySensitive` = `40%`
  * `Custom` (uses the numeric value below)
* **Wake Word Threshold** (`number`): custom threshold slider in `%` (`10..95`), used when preset is `Custom`.

How to change it in Home Assistant:

1. Open your Linux Voice Assistant device page.
2. Set **Wake Word Threshold Preset** to one of the presets above, or `Custom`.
3. If using `Custom`, set **Wake Word Threshold** to the desired percentage.

Changes are applied live (no full service restart required) and persisted to `preferences.json`.

### Additional Home Assistant Entities (Attention)

New entities for tuning/debug:

* `switch.vision_enabled`
* `switch.attention_required`
* `number.vision_cooldown_s`
* `number.vision_min_confidence`
* `number.engaged_vad_window_s`
* `sensor.last_attention_state`
* `sensor.last_vision_latency_ms`
* `sensor.last_vision_error`

### Systemd (Core + visd + frontpaneld)

Service files are included in `systemd/`:

* `linux-voice-assistant.service`
* `linux-voice-assistant-visd.service`
* `linux-voice-assistant-frontpaneld.service`

Install and start:

``` sh
sudo cp systemd/linux-voice-assistant*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now linux-voice-assistant.service
sudo systemctl enable --now linux-voice-assistant-visd.service
sudo systemctl enable --now linux-voice-assistant-frontpaneld.service
```

Logs:

``` sh
journalctl -u linux-voice-assistant.service -f
journalctl -u linux-voice-assistant-visd.service -f
journalctl -u linux-voice-assistant-frontpaneld.service -f
```

### Wake-Word Score Debug Logs

Set `core.log_level` to `DEBUG`.

When debug is enabled, wake-word scoring logs are emitted at a throttled interval (about every 300ms per model), for example:

* `model=<id>, score=<pct>, threshold=<pct>, result=triggered|not_triggered`

## Connecting to Home Assistant

1. In Home Assistant, go to "Settings" -> "Device & services"
2. Click the "Add integration" button
3. Choose "ESPHome" and then "Set up another instance of ESPHome"
4. Enter the IP address of your voice satellite with port 6053
5. Click "Submit"

## Acoustic Echo Cancellation

Enable the echo cancel PulseAudio module:

``` sh
pactl load-module module-echo-cancel \
  aec_method=webrtc \
  aec_args="analog_gain_control=0 digital_gain_control=1 noise_suppression=1"
```

Verify that the `echo-cancel-source` and `echo-cancel-sink` devices are present:

``` sh
pactl list short sources
pactl list short sinks
```

Use the new devices:

``` sh
# Set in config.json:
# core.audio_input_device = "Echo-Cancel Source"
# core.audio_output_device = "pipewire/echo-cancel-sink"
```

<!-- Links -->
[homeassistant]: https://www.home-assistant.io/
[esphome]: https://esphome.io/
[microWakeWord]: https://github.com/kahrendt/microWakeWord
[openWakeWord]: https://github.com/dscripka/openWakeWord
[wakewords-collection]: https://github.com/fwartner/home-assistant-wakewords-collection
[glados]: https://github.com/fwartner/home-assistant-wakewords-collection/blob/main/en/glados/glados.tflite
