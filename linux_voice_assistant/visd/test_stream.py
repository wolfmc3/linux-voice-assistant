#!/usr/bin/env python3
"""Camera test stream with inline detection overlay for debugging."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from ..config import load_config
from ..config import resolve_repo_path
from ..gpio_controller import LED_BRIGHTNESS, LED_COUNT, LED_GPIO, LedMode, Ws2812Bar
from .detector import SimpleFaceGlanceDetector

_LOGGER = logging.getLogger(__name__)
STREAM_HOST = "0.0.0.0"
STREAM_PORT = 8088
DETECTION_WIDTH = 320
DETECTION_HEIGHT = 240


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def build_parser() -> argparse.ArgumentParser:
    config = load_config().visd
    parser = argparse.ArgumentParser(
        prog="lva-visd-test-stream",
        description=(
            "Avvia uno stream MJPEG per test della telecamera con overlay "
            "dei rilevamenti visivi (facce, stato, confidenza)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=config.log_level,
        help="Livello log (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser


@dataclass
class StreamState:
    frame_jpeg: bytes = b""
    frame_seq: int = 0
    status: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class VisionSettings:
    vision_enabled: bool
    attention_required: bool
    min_confidence: float


class _RpiCamMjpegSource:
    """Capture single JPEG frames using rpicam-jpeg."""

    def __init__(
        self,
        *,
        camera_index: int,
        width: int,
        height: int,
        fps: float,
    ) -> None:
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._fps = max(1.0, min(20.0, float(fps)))

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def read_jpeg(self) -> bytes | None:
        cmd = [
            "rpicam-jpeg",
            "-n",
            "-t",
            "1",
            "--camera",
            str(self._camera_index),
            "--width",
            str(self._width),
            "--height",
            str(self._height),
            "-o",
            "-",
        ]
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                timeout=max(0.8, 2.0 / self._fps),
                check=False,
            )
        except Exception:  # noqa: BLE001
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        return bytes(result.stdout)


class _DirectLedSync:
    def __init__(self) -> None:
        self._bar = Ws2812Bar(LED_GPIO, LED_COUNT, LED_BRIGHTNESS)
        self._mode = ""
        self._set_mode(LedMode.READY)

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._bar.set_mode(mode, time.monotonic())

    def update(self, *, ok: bool, has_error: bool) -> None:
        if has_error:
            self._set_mode(LedMode.MUTED)
        elif ok:
            self._set_mode(LedMode.LISTENING)
        else:
            self._set_mode(LedMode.READY)
        self._bar.tick(time.monotonic())

    def close(self) -> None:
        self._bar.set_mode(LedMode.OFF, time.monotonic())
        self._bar.off()
        self._bar.close()


class _PersonDetector:
    def __init__(self) -> None:
        self._hog = None
        self._profile = None
        self._upper_body = None
        try:
            import cv2  # type: ignore

            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self._hog = hog
            profile_path = cv2.data.haarcascades + "haarcascade_profileface.xml"
            profile = cv2.CascadeClassifier(profile_path)
            if not profile.empty():
                self._profile = profile
            upper_body_path = cv2.data.haarcascades + "haarcascade_upperbody.xml"
            upper_body = cv2.CascadeClassifier(upper_body_path)
            if not upper_body.empty():
                self._upper_body = upper_body
        except Exception:  # noqa: BLE001
            self._hog = None
            self._profile = None
            self._upper_body = None

    def detect(self, frame: np.ndarray) -> tuple[bool, int]:
        total = 0
        has_person = False
        if self._hog is None and self._profile is None and self._upper_body is None:
            return False, 0
        try:
            if self._hog is not None:
                rects, _weights = self._hog.detectMultiScale(
                    frame,
                    # Diagnostic stream: favor recall so "person" triggers sooner
                    # than "looking toward camera".
                    winStride=(4, 4),
                    padding=(8, 8),
                    scale=1.03,
                    hitThreshold=-0.2,
                )
                count = int(len(rects))
                if count > 0:
                    has_person = True
                    total += count

            if self._profile is not None:
                import cv2  # type: ignore

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                left = self._profile.detectMultiScale(
                    gray,
                    scaleFactor=1.08,
                    minNeighbors=3,
                    minSize=(22, 22),
                )
                mirrored = cv2.flip(gray, 1)
                right = self._profile.detectMultiScale(
                    mirrored,
                    scaleFactor=1.08,
                    minNeighbors=3,
                    minSize=(22, 22),
                )
                profile_count = int(len(left)) + int(len(right))
                if profile_count > 0:
                    has_person = True
                    total += profile_count

                if self._upper_body is not None:
                    upper = self._upper_body.detectMultiScale(
                        gray,
                        scaleFactor=1.08,
                        minNeighbors=3,
                        minSize=(36, 36),
                    )
                    upper_count = int(len(upper))
                    if upper_count > 0:
                        has_person = True
                        total += upper_count
            return has_person, total
        except Exception:  # noqa: BLE001
            return False, 0


class _Handler(BaseHTTPRequestHandler):
    state: StreamState
    stop_event: threading.Event

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._serve_index()
            return
        if self.path == "/status":
            self._serve_status()
            return
        if self.path == "/stream.mjpg":
            self._serve_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        _LOGGER.debug("http %s - %s", self.address_string(), format % args)

    def _serve_index(self) -> None:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>LVA Camera Test</title>"
            "<style>body{font-family:monospace;background:#111;color:#eee;margin:1rem;}"
            "img{max-width:100%;border:1px solid #444;}"
            "pre{background:#1c1c1c;padding:0.75rem;}"
            "</style></head><body>"
            "<h2>LVA Camera Detection Test Stream</h2>"
            "<img src='/stream.mjpg' alt='stream'/><pre id='status'>loading...</pre>"
            "<script>"
            "async function tick(){"
            "const r=await fetch('/status',{cache:'no-store'});"
            "const j=await r.json();"
            "document.getElementById('status').textContent=JSON.stringify(j,null,2);"
            "}"
            "setInterval(tick,500);tick();"
            "</script></body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self) -> None:
        with self.state.lock:
            payload = json.dumps(self.state.status).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_stream(self) -> None:
        boundary = "frame"
        self.send_response(HTTPStatus.OK)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={boundary}",
        )
        self.end_headers()

        last_seq = -1
        while not self.stop_event.is_set():
            with self.state.lock:
                seq = self.state.frame_seq
                frame = self.state.frame_jpeg
            if (not frame) or (seq == last_seq):
                time.sleep(0.03)
                continue
            last_seq = seq
            try:
                self.wfile.write(f"--{boundary}\r\n".encode("ascii"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                return


def _build_overlay_lines(status: dict[str, Any]) -> list[str]:
    state = str(status.get("state", "NO_FACE"))
    confidence = float(status.get("confidence", 0.0))
    threshold = float(status.get("min_confidence", 0.6))
    face_count = int(status.get("face_count", 0))
    stream_fps = float(status.get("fps", 0.0))
    proc_ms = float(status.get("processing_ms", 0.0))
    analysis_window = int(status.get("analysis_window", 1))
    cpu_load = float(status.get("cpu_load_ratio", 0.0))
    person_detected = bool(status.get("person_detected", False))
    face_detected = bool(status.get("face_detected", False))
    toward = bool(status.get("looking_toward_camera", False))
    vision_enabled = bool(status.get("vision_enabled", True))
    attention_required = bool(status.get("attention_required", True))
    would_trigger = bool(status.get("would_trigger", False))
    return [
        f"state={state}",
        f"confidence={confidence:.2f} threshold={threshold:.2f}",
        f"faces={face_count} fps={stream_fps:.1f}",
        f"proc_ms={proc_ms:.1f} window={analysis_window}",
        f"cpu_load={cpu_load:.2f}",
        f"person={person_detected} face={face_detected} toward={toward}",
        f"vis_enabled={vision_enabled} attention_required={attention_required}",
        f"would_trigger={would_trigger}",
    ]


def _detect_local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        infos = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
        for info in infos:
            ip = str(info[4][0])
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = str(sock.getsockname()[0])
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass

    return sorted(ips)


def _retune_adaptive(
    *,
    ema_processing_ms: float,
    target_fps: float,
    max_fps: float,
    effective_fps: float,
    window_size: int,
    window_max: int,
    uncertain_streak: int,
    stable_streak: int,
    reaction_boost: bool,
    cpu_load_ratio: float,
) -> tuple[float, int]:
    if ema_processing_ms <= 0.0:
        return effective_fps, window_size

    max_sustainable_fps = max(1.0, 1000.0 / ema_processing_ms)
    cpu_load = max(0.0, min(2.0, float(cpu_load_ratio)))
    if cpu_load <= 0.45:
        cpu_floor_fps = 2.5
    elif cpu_load <= 0.70:
        cpu_floor_fps = 1.8
    else:
        cpu_floor_fps = 1.0

    desired_fps = min(float(max_fps), max_sustainable_fps * 0.9)
    desired_fps = max(float(target_fps), desired_fps, cpu_floor_fps)
    if reaction_boost:
        desired_fps = min(
            max(2.0, float(target_fps) * 3.0, cpu_floor_fps + 0.8),
            max_sustainable_fps * 0.9,
            float(max_fps),
        )
    if uncertain_streak >= 2:
        desired_fps = max(desired_fps, min(float(max_fps), cpu_floor_fps + 0.6))
    if stable_streak >= 3:
        desired_fps = max(float(target_fps), desired_fps - 0.25)
    next_effective_fps = (effective_fps * 0.75) + (desired_fps * 0.25)
    next_effective_fps = max(float(target_fps), min(float(max_fps), next_effective_fps))

    next_window = window_size
    if uncertain_streak >= 2 and window_size < window_max:
        next_window += 1
    elif stable_streak >= 3 and window_size > 1:
        next_window -= 1
    return next_effective_fps, next_window


def _cpu_load_ratio() -> float:
    cpu_count = max(1, int(os.cpu_count() or 1))
    try:
        one_min_load = float(os.getloadavg()[0])
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, one_min_load / float(cpu_count))


def _load_vision_settings() -> VisionSettings:
    app_config = load_config()
    core = app_config.core
    preferences_path = resolve_repo_path(core.preferences_file)
    preferences: dict[str, Any] = {}
    try:
        with open(preferences_path, "r", encoding="utf-8") as pref_file:
            loaded = json.load(pref_file)
            if isinstance(loaded, dict):
                preferences = loaded
    except Exception:  # noqa: BLE001
        preferences = {}

    pref_vision_enabled = bool(int(preferences.get("vision_enabled", 1)))
    pref_attention_required = bool(int(preferences.get("attention_required", 1)))
    pref_vision_min_conf = _clamp_confidence(preferences.get("vision_min_confidence", 0.6))

    vision_enabled = pref_vision_enabled if (core.vision_enabled is None) else bool(core.vision_enabled)
    attention_required = (
        pref_attention_required if (core.attention_required is None) else bool(core.attention_required)
    )
    min_confidence = (
        pref_vision_min_conf
        if (core.vision_min_confidence is None)
        else _clamp_confidence(core.vision_min_confidence)
    )
    return VisionSettings(
        vision_enabled=vision_enabled,
        attention_required=attention_required,
        min_confidence=min_confidence,
    )


def _would_trigger_service_logic(
    *,
    state: str,
    confidence: float,
    settings: VisionSettings,
) -> bool:
    if not settings.vision_enabled:
        return False
    if settings.attention_required:
        return (state == "FACE_TOWARD") and (confidence >= settings.min_confidence)
    return state in {"FACE_TOWARD", "FACE_AWAY"}


def run(args: argparse.Namespace) -> None:
    try:
        import cv2  # type: ignore
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"opencv_unavailable: {err}") from err

    app_config = load_config()
    visd_config = app_config.visd
    camera_index = int(visd_config.camera_index)
    width = int(visd_config.width)
    height = int(visd_config.height)
    frame_count = max(1, min(2, int(visd_config.frame_count)))
    vision_settings = _load_vision_settings()
    min_conf = vision_settings.min_confidence
    fps = 1.0
    max_adaptive_fps = 4.0
    jpeg_quality = 80
    selected_backend = "auto"

    detector = SimpleFaceGlanceDetector()
    cascade = getattr(detector, "_cascade", None)
    if cascade is None:
        raise RuntimeError("face_cascade_unavailable")
    person_detector = _PersonDetector()

    cap = None
    # Match visd behavior: prefer OpenCV capture first, then fallback.
    active_backend = "opencv" if selected_backend == "auto" else selected_backend

    if selected_backend in {"auto", "opencv"}:
        if active_backend == "opencv":
            cap = cv2.VideoCapture(camera_index)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)
            elif selected_backend == "opencv":
                raise RuntimeError("camera_open_failed")
            else:
                active_backend = "rpicam"

    rpicam_source = _RpiCamMjpegSource(
        camera_index=camera_index,
        width=width,
        height=height,
        fps=max(6.0, fps * 3.0),
    )
    if active_backend == "rpicam":
        rpicam_source.start()
    opencv_failures = 0
    led_sync = _DirectLedSync()
    led_sync.update(ok=False, has_error=False)

    state = StreamState(
        status={
            "state": "BOOTING",
            "confidence": 0.0,
            "face_count": 0,
            "fps": 0.0,
            "effective_fps": fps,
            "min_confidence": min_conf,
            "vision_enabled": vision_settings.vision_enabled,
            "attention_required": vision_settings.attention_required,
            "would_trigger": False,
            "camera_index": camera_index,
            "capture_backend": active_backend,
            "analysis_window": frame_count,
        }
    )
    stop_event = threading.Event()

    handler_type = type(
        "LvaTestStreamHandler",
        (_Handler,),
        {"state": state, "stop_event": stop_event},
    )
    server = ThreadingHTTPServer((STREAM_HOST, STREAM_PORT), handler_type)
    threading.Thread(
        target=server.serve_forever,
        name="lva-test-stream-http",
        daemon=True,
    ).start()

    _LOGGER.info("Test stream pronto: http://%s:%s", STREAM_HOST, STREAM_PORT)
    if STREAM_HOST in {"0.0.0.0", "::"}:
        local_ips = _detect_local_ips()
        if local_ips:
            _LOGGER.info(
                "Accesso da altri device: %s",
                ", ".join(f"http://{ip}:{STREAM_PORT}" for ip in local_ips),
            )

    recent_frames: deque[Any] = deque(maxlen=frame_count)
    effective_fps = fps
    analysis_window = max(1, min(2, frame_count))
    ema_processing_ms = 0.0
    uncertain_streak = 0
    stable_streak = 0
    last_state = "BOOTING"
    last_good_frame_at = time.monotonic()
    cpu_ema = _cpu_load_ratio()
    cpu_sample_at = 0.0
    settings_reload_at = 0.0
    frame_window_started = time.monotonic()
    frame_window_count = 0

    try:
        while not stop_event.is_set():
            frame_started = time.monotonic()
            now = time.monotonic()
            if now >= settings_reload_at:
                settings_reload_at = now + 1.0
                try:
                    vision_settings = _load_vision_settings()
                    min_conf = vision_settings.min_confidence
                except Exception:  # noqa: BLE001
                    pass
            frame = None
            if active_backend == "opencv" and cap is not None:
                ok, frame = cap.read()
                if ok and frame is not None:
                    opencv_failures = 0
                else:
                    opencv_failures += 1
                    if selected_backend == "auto" and opencv_failures >= 4:
                        _LOGGER.warning(
                            "OpenCV non fornisce frame, switch backend -> rpicam"
                        )
                        if cap is not None:
                            cap.release()
                            cap = None
                        active_backend = "rpicam"
                        rpicam_source.start()
            if active_backend == "rpicam":
                jpg = rpicam_source.read_jpeg()
                if jpg:
                    decoded = cv2.imdecode(
                        np.frombuffer(jpg, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if decoded is not None:
                        frame = decoded

            if frame is None:
                now = time.monotonic()
                if (now - last_good_frame_at) > 1.2:
                    led_sync.update(ok=False, has_error=True)
                    with state.lock:
                        state.status = {
                            **state.status,
                            "state": "ERROR",
                            "error": "camera_stalled",
                            "capture_backend": active_backend,
                        }
                time.sleep(0.02)
                continue
            last_good_frame_at = time.monotonic()

            detection_frame = cv2.resize(
                frame,
                (DETECTION_WIDTH, DETECTION_HEIGHT),
                interpolation=cv2.INTER_AREA,
            )
            recent_frames.append(detection_frame)
            frames_for_detection = list(recent_frames)[-analysis_window:]
            detection = detector.analyze(frames_for_detection)

            gray = cv2.cvtColor(detection_frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray,
                scaleFactor=1.15,
                minNeighbors=3,
                minSize=(28, 28),
            )
            sx = float(frame.shape[1]) / float(DETECTION_WIDTH)
            sy = float(frame.shape[0]) / float(DETECTION_HEIGHT)
            for (x, y, fw, fh) in faces:
                x0 = int(round(x * sx))
                y0 = int(round(y * sy))
                x1 = int(round((x + fw) * sx))
                y1 = int(round((y + fh) * sy))
                cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 255, 0), 2)

            frame_window_count += 1
            elapsed = max(0.001, time.monotonic() - frame_window_started)
            if elapsed >= 1.0:
                current_fps = frame_window_count / elapsed
                frame_window_count = 0
                frame_window_started = time.monotonic()
            else:
                current_fps = float(state.status.get("fps", 0.0))

            accepted = _would_trigger_service_logic(
                state=str(detection.state),
                confidence=_clamp_confidence(detection.confidence),
                settings=vision_settings,
            )
            status_payload = {
                "state": detection.state,
                "confidence": _clamp_confidence(detection.confidence),
                "face_count": int(len(faces)),
                "fps": current_fps,
                "ok": accepted,
                "would_trigger": accepted,
                "min_confidence": min_conf,
                "vision_enabled": vision_settings.vision_enabled,
                "attention_required": vision_settings.attention_required,
                "camera_index": camera_index,
                "capture_backend": active_backend,
                "timestamp": time.time(),
            }
            has_person, person_count = person_detector.detect(detection_frame)
            face_detected = int(len(faces)) > 0
            # Practical fallback: if a face is detected, a person is present.
            if face_detected and (not has_person):
                has_person = True
                person_count = max(1, int(person_count))
            looking_toward = detection.state == "FACE_TOWARD"
            status_payload["person_detected"] = has_person
            status_payload["person_count"] = person_count
            status_payload["face_detected"] = face_detected
            status_payload["looking_toward_camera"] = looking_toward
            confidence = float(status_payload["confidence"])
            is_ok = bool(status_payload["ok"])
            is_uncertain = (
                (status_payload["state"] == "FACE_AWAY")
                or (abs(confidence - min_conf) < 0.12)
                or ((status_payload["state"] == "NO_FACE") and (len(faces) > 0))
            )
            if is_ok:
                stable_streak += 1
                uncertain_streak = 0
            elif is_uncertain:
                uncertain_streak += 1
                stable_streak = 0
            else:
                uncertain_streak = 0
                stable_streak = 0

            processing_ms = max(0.1, (time.monotonic() - frame_started) * 1000.0)
            if ema_processing_ms <= 0.0:
                ema_processing_ms = processing_ms
            else:
                ema_processing_ms = (ema_processing_ms * 0.8) + (processing_ms * 0.2)
            now = time.monotonic()
            if now >= cpu_sample_at:
                cpu_sample_at = now + 0.6
                cpu_now = _cpu_load_ratio()
                cpu_ema = (cpu_ema * 0.8) + (cpu_now * 0.2)
            state_changed = str(status_payload["state"]) != last_state
            reaction_boost = is_uncertain or state_changed
            effective_fps, analysis_window = _retune_adaptive(
                ema_processing_ms=ema_processing_ms,
                target_fps=fps,
                max_fps=max_adaptive_fps,
                effective_fps=effective_fps,
                window_size=analysis_window,
                window_max=frame_count,
                uncertain_streak=uncertain_streak,
                stable_streak=stable_streak,
                reaction_boost=reaction_boost,
                cpu_load_ratio=cpu_ema,
            )
            last_state = str(status_payload["state"])
            status_payload["analysis_window"] = analysis_window
            status_payload["processing_ms"] = processing_ms
            status_payload["processing_ema_ms"] = ema_processing_ms
            status_payload["effective_fps"] = effective_fps
            status_payload["cpu_load_ratio"] = cpu_ema
            led_sync.update(ok=bool(status_payload["ok"]), has_error=False)
            color = (0, 255, 0) if status_payload["ok"] else (0, 165, 255)
            for idx, line in enumerate(_build_overlay_lines(status_payload)):
                y = 20 + (idx * 20)
                cv2.putText(
                    frame,
                    line,
                    (12, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    color,
                    1,
                    cv2.LINE_AA,
                )

            ok_jpeg, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
            )
            if ok_jpeg:
                with state.lock:
                    state.frame_seq += 1
                    state.frame_jpeg = bytes(encoded.tobytes())
                    state.status = status_payload

            to_sleep = max(
                0.0,
                (1.0 / effective_fps) - (time.monotonic() - frame_started),
            )
            if to_sleep > 0:
                time.sleep(to_sleep)
    except KeyboardInterrupt:
        _LOGGER.info("Interrotto da tastiera")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        if cap is not None:
            cap.release()
        rpicam_source.stop()
        led_sync.close()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_level_name = str(args.log_level).strip().upper()
    logging.basicConfig(level=getattr(logging, log_level_name, logging.INFO))
    run(args)


def cli() -> None:
    main()


if __name__ == "__main__":
    main()
