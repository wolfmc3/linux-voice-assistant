#!/usr/bin/env python3
"""Vision daemon for attention glance checks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time
import uuid
from typing import Optional

from ..config import load_config
from ..local_ipc import CONTROL_SOCKET_PATH, IPC_DIR, VISD_SOCKET_PATH, normalize_message, send_ipc_message
from .detector import Detector, SimpleFaceGlanceDetector

_LOGGER = logging.getLogger(__name__)
_FACE_SNAPSHOT_PATH = "/face/latest.jpg"


class _VisdProtocol(asyncio.DatagramProtocol):
    def __init__(self, daemon: "VisionDaemon") -> None:
        self.daemon = daemon

    def datagram_received(self, data: bytes, _addr) -> None:  # type: ignore[override]
        try:
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                return
            message = normalize_message(payload, default_source="core")
            if message is None:
                return
            self.daemon.handle_message(message)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Invalid visd IPC packet")


class VisionDaemon:
    def __init__(
        self,
        detector: Detector,
        *,
        camera_index: int = 0,
        burst_seconds: float = 0.9,
        frame_count: int = 5,
        width: int = 320,
        height: int = 240,
        face_snapshot_host: str = "0.0.0.0",
        face_snapshot_port: int = 8766,
    ) -> None:
        self._detector = detector
        self._camera_index = camera_index
        self._burst_seconds = max(0.7, min(1.2, burst_seconds))
        self._frame_count = max(4, min(6, frame_count))
        self._width = width
        self._height = height
        self._face_snapshot_host = str(face_snapshot_host)
        self._face_snapshot_port = int(face_snapshot_port)
        self._transport: Optional[asyncio.transports.DatagramTransport] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._http_server: Optional[asyncio.base_events.Server] = None
        self._last_face_jpeg: Optional[bytes] = None
        self._last_face_updated_at: float = 0.0
        self._last_face_uuid: str = ""
        self._placeholder_jpeg: Optional[bytes] = None

    async def start(self) -> None:
        IPC_DIR.mkdir(parents=True, exist_ok=True)
        if VISD_SOCKET_PATH.exists():
            VISD_SOCKET_PATH.unlink()

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _VisdProtocol(self),
            local_addr=str(VISD_SOCKET_PATH),
            family=socket.AF_UNIX,
        )
        self._transport = transport
        os.chmod(VISD_SOCKET_PATH, 0o666)
        self._placeholder_jpeg = await asyncio.to_thread(self._build_placeholder_jpeg)
        # Initialize with a valid image so the endpoint always serves a JPEG.
        self._last_face_jpeg = self._placeholder_jpeg
        self._last_face_updated_at = time.time()
        self._last_face_uuid = uuid.uuid4().hex
        self._http_server = await asyncio.start_server(
            self._handle_http_client,
            host=self._face_snapshot_host,
            port=self._face_snapshot_port,
        )
        _LOGGER.info("visd ready (%s)", VISD_SOCKET_PATH)
        _LOGGER.info(
            "visd face snapshot endpoint ready (http://%s:%s%s)",
            self._face_snapshot_host,
            self._face_snapshot_port,
            _FACE_SNAPSHOT_PATH,
        )

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._http_server is not None:
            self._http_server.close()
            self._http_server = None
        try:
            if VISD_SOCKET_PATH.exists():
                VISD_SOCKET_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

    def handle_message(self, message: dict[str, object]) -> None:
        msg_type = str(message.get("type", "")).strip().upper()
        payload_obj = message.get("payload")
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        if msg_type != "VISION_GLANCE_REQUEST":
            return
        request_id = str(payload.get("request_id", "")).strip()
        reason = str(payload.get("reason", "unknown")).strip()
        if not request_id:
            return
        if self._task is not None and (not self._task.done()):
            return
        self._task = asyncio.create_task(self._run_glance(request_id, reason), name=f"visd-glance-{request_id}")

    async def _run_glance(self, request_id: str, reason: str) -> None:
        started = time.monotonic()
        result_state = "NO_FACE"
        confidence = 0.0
        error = ""
        try:
            frames = await asyncio.to_thread(self._capture_frames)
            detection = self._detector.analyze(frames)
            result_state = detection.state
            confidence = detection.confidence
            self._update_last_face_snapshot(frames, detection)
        except Exception as err:  # noqa: BLE001
            error = str(err)
            _LOGGER.warning("vision glance failed: %s", err)

        latency_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        payload = {
            "request_id": request_id,
            "reason": reason,
            "state": result_state,
            "confidence": max(0.0, min(1.0, confidence)),
            "latency_ms": latency_ms,
        }
        if error:
            payload["error"] = error
        send_ipc_message(CONTROL_SOCKET_PATH, "VISION_GLANCE_RESULT", payload, source="visd")

    def _capture_frames(self):
        try:
            import cv2  # type: ignore
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"opencv_unavailable: {err}") from err

        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("camera_open_failed")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)

        frames = []
        deadline = time.monotonic() + self._burst_seconds
        while (time.monotonic() < deadline) and (len(frames) < self._frame_count):
            ok, frame = cap.read()
            if not ok:
                continue
            frames.append(frame)
        cap.release()
        if not frames:
            fallback_frame = self._capture_single_frame_rpicam(cv2)
            if fallback_frame is not None:
                frames.append(fallback_frame)
        if not frames:
            raise RuntimeError("camera_no_frames")
        return frames

    def _update_last_face_snapshot(self, frames, detection) -> None:
        face_box = getattr(detection, "face_box", None)
        face_frame_index = getattr(detection, "face_frame_index", None)
        if face_box is None or face_frame_index is None:
            return
        if face_frame_index < 0 or face_frame_index >= len(frames):
            return
        try:
            import cv2  # type: ignore
        except Exception:
            return

        frame = frames[face_frame_index]
        x, y, w, h = face_box
        frame_h, frame_w = frame.shape[:2]
        pad_x = max(2, int(round(w * 0.20)))
        pad_y_top = max(2, int(round(h * 0.35)))
        pad_y_bottom = max(2, int(round(h * 0.35)))
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y_top)
        x1 = min(frame_w, x + w + pad_x)
        y1 = min(frame_h, y + h + pad_y_bottom)
        if x1 <= x0 or y1 <= y0:
            return
        crop = frame[y0:y1, x0:x1]
        ok, encoded = cv2.imencode(".jpg", crop)
        if not ok:
            return
        self._last_face_jpeg = bytes(encoded.tobytes())
        self._last_face_updated_at = time.time()
        self._last_face_uuid = uuid.uuid4().hex

    def _build_placeholder_jpeg(self) -> bytes:
        try:
            import cv2  # type: ignore
            import numpy as np

            img = np.zeros((128, 128, 3), dtype=np.uint8)
            ok, encoded = cv2.imencode(".jpg", img)
            if ok:
                return bytes(encoded.tobytes())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to build placeholder JPEG: %s", err)
        return b""

    async def _handle_http_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            try:
                method, path, _version = (
                    request_line.decode("iso-8859-1").strip().split(" ", 2)
                )
            except ValueError:
                await self._http_send(
                    writer,
                    400,
                    b"Bad Request",
                    content_type="text/plain",
                )
                return

            # Drain headers.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line or line in (b"\r\n", b"\n"):
                    break

            if method != "GET":
                await self._http_send(
                    writer,
                    405,
                    b"Method Not Allowed",
                    content_type="text/plain",
                )
                return

            route = path.split("?", 1)[0]
            if route != _FACE_SNAPSHOT_PATH:
                await self._http_send(
                    writer,
                    404,
                    b"Not Found",
                    content_type="text/plain",
                )
                return

            body = self._last_face_jpeg or b""
            await self._http_send(writer, 200, body, content_type="image/jpeg")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("HTTP snapshot request failed", exc_info=True)
            try:
                await self._http_send(
                    writer,
                    500,
                    b"Internal Server Error",
                    content_type="text/plain",
                )
            except Exception:  # noqa: BLE001
                pass

    async def _http_send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }.get(status, "OK")
        headers = [
            f"HTTP/1.1 {status} {reason}\r\n",
            f"Content-Type: {content_type}\r\n",
            f"Content-Length: {len(body)}\r\n",
            "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n",
            "Pragma: no-cache\r\n",
            "Expires: 0\r\n",
            "Connection: close\r\n",
            "\r\n",
        ]
        writer.write("".join(headers).encode("ascii") + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def _capture_single_frame_rpicam(self, cv2_module):
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
                timeout=max(0.8, self._burst_seconds),
                check=False,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("rpicam fallback failed: %s", err)
            return None

        if result.returncode != 0 or not result.stdout:
            return None

        try:
            import numpy as np

            frame = cv2_module.imdecode(
                np.frombuffer(result.stdout, dtype=np.uint8),
                cv2_module.IMREAD_COLOR,
            )
            return frame
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("rpicam decode failed: %s", err)
            return None


async def main() -> None:
    config = load_config().visd
    log_level_name = str(config.log_level).strip().upper()
    logging.basicConfig(level=getattr(logging, log_level_name, logging.INFO))
    daemon = VisionDaemon(
        detector=SimpleFaceGlanceDetector(),
        camera_index=config.camera_index,
        burst_seconds=config.burst_seconds,
        frame_count=config.frame_count,
        width=config.width,
        height=config.height,
        face_snapshot_host=config.face_snapshot_host,
        face_snapshot_port=config.face_snapshot_port,
    )
    await daemon.start()
    try:
        await asyncio.Event().wait()
    finally:
        daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())


def cli() -> None:
    asyncio.run(main())
