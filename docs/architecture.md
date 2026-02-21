# Linux Voice Assistant Context-Aware Architecture

## Process Model

- `linux-voice-assistant` (core audio + ESPHome API): wake word, VA pipeline, distance activation, IPC control.
- `visd` (vision daemon): short camera burst on request, tri-state attention output.
- `frontpaneld` (input daemon): touch + encoder, sends logical commands to core.
- Optional integrated `gpio_controller` remains supported for LED effects and legacy buttons.

## IPC Schema

All new IPC messages use:

```json
{
  "type": "MESSAGE_TYPE",
  "payload": {},
  "ts": 1700000000.0,
  "source": "core|visd|frontpaneld|external"
}
```

Legacy packets (`{"cmd":"..."}` / `{"event":"..."}`) are still accepted.

## IPC Channels

- Core receives commands/events on `/tmp/lva-ipc/control.sock`
- Core emits events on `/tmp/lva-ipc/gpio-events.sock`
- visd receives requests on `/tmp/lva-ipc/visd.sock`

## State Machine

- `IDLE`
- `PROX_VERIFY`
- `VISION_GLANCE`
- `ENGAGED`
- `LISTENING`
- `PROCESSING`
- `SPEAKING`
- `MUTED` (overlay state)

Main flow:

1. Distance threshold hit (`PROX_VERIFY`)
2. If attention enabled and cooldown allows: `VISION_GLANCE_REQUEST`
3. `VISION_GLANCE_RESULT`:
   - `FACE_TOWARD` above threshold -> `ENGAGED`
   - else reject (or fallback if configured)
4. Core starts VA request with `USE_VAD`
5. If no VAD start inside `engaged_vad_window_s`: cancel and cooldown

## Failure Modes

- No camera / visd down: distance-only fallback is supported.
- Distance sensor unavailable: no distance trigger; wake word/manual still work.
- I2C read errors: reader returns `None`, retries, and re-inits without crashing core.
- IPC unavailable: daemons keep running and retry naturally on next send.

## Metrics

Core tracks:

- `vision_requests`
- `vision_success`
- `vision_timeout`
- `false_triggers_prevented`
- `xrun_counter` (placeholder for ALSA XRUN integration)
