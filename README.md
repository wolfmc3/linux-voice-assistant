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

Use `script/run` or `python3 -m linux_voice_assistant`

You must specify `--name <NAME>` with a name that will be available in Home Assistant.

See `--help` for more options.

### Microphone

Use `--audio-input-device` to change the microphone device. Use `--list-input-devices` to see the available microphones. 

The microphone device **must** support 16Khz mono audio.

### Speaker

Use `--audio-output-device` to change the speaker device. Use `--list-output-devices` to see the available speakers.

### Sounds

Customize wake word and timer sounds (defaults are used if not specified):
``` sh
python3 -m linux_voice_assistant ... \
    --wakeup-sound sounds/wake_word_triggered_old.wav \
    --timer-finished-sound sounds/timer_finished.flac
```

Available sounds:
* **Wake sounds**: `wake_word_triggered.flac` (default), `wake_word_triggered_old.wav`
* **Timer sounds**: `timer_finished.flac` (default), `timer_finished_old.wav`

The optional "thinking" sound plays while the assistant is processing. Enable it on startup with:
``` sh
python3 -m linux_voice_assistant ... \
    --enable-thinking-sound
```

This enables the thinking sound by default and sets the Home Assistant switch to ON. The switch can be toggled at any time from the device page.

### GPIO (LED bar + buttons)

The GPIO controller can run integrated in `linux-voice-assistant` (legacy behavior).
For production front panel handling (touch/encoder), use `linux_voice_assistant.frontpaneld`.

Supported hardware behavior:
* WS2812B LED bar state rendering (`OFF`, `READY`, `MUTED`, `LISTENING`, `PLAYBACK`)
* Mute button on `GPIO17` (`mute_toggle`)
* Volume up/down buttons on `GPIO22`/`GPIO23` (`volume_up`, `volume_down`)
* Local volume feedback sound via `aplay`

Useful options:
``` sh
python3 -m linux_voice_assistant ... \
    --gpio-feedback-device "sysdefault:CARD=wm8960soundcard"
```

To disable integrated GPIO handling:
``` sh
python3 -m linux_voice_assistant ... \
    --disable-gpio-control
```

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

``` sh
# default: l0x
python3 -m linux_voice_assistant ... --distance-sensor-model l1x
```

### Trigger Modes (Wake Word / Distance / Both)

You can control how listening is triggered:

``` sh
# Wake word only (default)
python3 -m linux_voice_assistant ... \
    --wake-word-detection \
    --no-distance-activation

# Distance only (direct listening when close)
python3 -m linux_voice_assistant ... \
    --no-wake-word-detection \
    --distance-activation \
    --distance-activation-threshold-mm 120

# Both wake word and distance trigger
python3 -m linux_voice_assistant ... \
    --wake-word-detection \
    --distance-activation \
    --distance-activation-threshold-mm 120
```

### Attention Gating (Vision Overlay on Distance)

When distance activation is enabled, you can require a quick vision check (`FACE_TOWARD`) before starting listening:

``` sh
python3 -m linux_voice_assistant ... \
    --distance-activation \
    --vision-enabled \
    --attention-required \
    --vision-cooldown-s 4.0 \
    --vision-min-confidence 0.60 \
    --engaged-vad-window-s 2.5
```

Behavior summary:

* Primary gate: distance sensor
* Secondary gate (optional): `visd` glance burst (`NO_FACE` / `FACE_AWAY` / `FACE_TOWARD`)
* Listening start uses VAD for distance/manual triggers
* If VAD does not start in `engaged_vad_window_s`, listening is cancelled

## Wake Word

Change the default wake word with `--wake-model <id>` where `<id>` is the name of a model in the `wakewords` directory. For example, `--wake-model hey_jarvis` will load `wakewords/hey_jarvis.tflite` by default.

You can include more wakeword directories by adding `--wake-word-dir <DIR>` where `<DIR>` contains either [microWakeWord][] or [openWakeWord][] config files and `.tflite` models. For example, `--wake-word-dir wakewords/openWakeWord` will include the default wake words for openWakeWord.

If you want to add [other wakewords][wakewords-collection], make sure to create a small JSON config file to identify it as an openWakeWord model. For example, download the [GLaDOS][glados] model to `glados.tflite` and create `glados.json` with:

``` json
{
  "type": "openWakeWord",
  "wake_word": "GLaDOS",
  "model": "glados.tflite"
}
```

Add `--wake-word-dir <DIR>` with the directory containing `glados.tflite` and `glados.json` to your command-line.

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

Enable debug logs with:

``` sh
python3 -m linux_voice_assistant ... --debug
```

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
# The device names may be different on your system.
# Double check with --list-input-devices and --list-output-devices
python3 -m linux_voice_assistant ... \
     --audio-input-device 'Echo-Cancel Source' \
     --audio-output-device 'pipewire/echo-cancel-sink'
```

<!-- Links -->
[homeassistant]: https://www.home-assistant.io/
[esphome]: https://esphome.io/
[microWakeWord]: https://github.com/kahrendt/microWakeWord
[openWakeWord]: https://github.com/dscripka/openWakeWord
[wakewords-collection]: https://github.com/fwartner/home-assistant-wakewords-collection
[glados]: https://github.com/fwartner/home-assistant-wakewords-collection/blob/main/en/glados/glados.tflite
