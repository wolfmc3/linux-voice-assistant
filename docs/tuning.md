# Tuning Guide (Raspberry Pi Zero 2 W)

## Recommended Defaults

- `distance_sensor_model`: `l1x` (or `l0x` if legacy hardware)
- `distance_activation_threshold_mm`: `120`
- `vision_enabled`: `on`
- `attention_required`: `on`
- `vision_cooldown_s`: `4.0`
- `vision_min_confidence`: `0.60`
- `engaged_vad_window_s`: `2.5`

## CPU/RAM Notes

- Keep vision burst short (`0.7-1.2s`) and low resolution (`320x240`).
- Avoid continuous camera open; `visd` only opens camera on request.
- Keep wake-word models minimal to reduce CPU spikes.
- Prefer `--audio-input-block-size 1024` on Zero 2 W for stable processing.

## Distance Polling Strategy

Distance polling can be adjusted at higher-level logic:

- `IDLE`: slower cadence (~1Hz)
- `PROX_VERIFY`: medium cadence
- `ENGAGED`: tighter checks if needed

Reader supports timing hooks:

- `set_timing_budget_ms(...)`
- `set_intermeasurement_ms(...)`

## Debug Checklist

- Camera disconnected: verify `VISION_GLANCE_RESULT` with `error` and fallback behavior.
- VL53 disabled: verify no crashes and wake-word/manual operation.
- Mute active: verify no auto-listening trigger starts pipeline.
- TV/photo false trigger scenario: verify `FACE_AWAY/NO_FACE`, cooldown enforcement, and reduced false triggers.
