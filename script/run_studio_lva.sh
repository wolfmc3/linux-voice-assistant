#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/user
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
if [[ ! -d "$XDG_RUNTIME_DIR" ]]; then
  echo "XDG runtime dir non disponibile: $XDG_RUNTIME_DIR" >&2
  exit 1
fi
export XDG_RUNTIME_DIR
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"

start_pulseaudio() {
  if pulseaudio --check >/dev/null 2>&1; then
    return 0
  fi
  pulseaudio --start >/dev/null 2>&1 || pulseaudio --daemonize=yes >/dev/null 2>&1 || true
}

pulse_ready() {
  if command -v pactl >/dev/null 2>&1; then
    pactl -s "$PULSE_SERVER" info >/dev/null 2>&1
    return $?
  fi
  [[ -S "${PULSE_SERVER#unix:}" ]]
}

for _ in $(seq 1 30); do
  start_pulseaudio
  if pulse_ready; then
    break
  fi
  sleep 1
done

cd /home/user/linux-voice-assistant

script/run &
APP_PID=$!

mpv --no-video --really-quiet \
  --audio-device="alsa/sysdefault:CARD=wm8960soundcard" \
  /home/user/linux-voice-assistant/sounds/wake_word_triggered.flac >/dev/null 2>&1 || true

wait "$APP_PID"
