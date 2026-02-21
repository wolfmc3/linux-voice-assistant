#!/usr/bin/env python3
"""Vision daemon for attention glance checks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from typing import Optional

from ..config import load_config
from ..local_ipc import CONTROL_SOCKET_PATH, IPC_DIR, VISD_SOCKET_PATH, normalize_message, send_ipc_message
from .detector import Detector, SimpleFaceGlanceDetector

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self._detector = detector
        self._camera_index = camera_index
        self._burst_seconds = max(0.7, min(1.2, burst_seconds))
        self._frame_count = max(4, min(6, frame_count))
        self._width = width
        self._height = height
        self._transport: Optional[asyncio.transports.DatagramTransport] = None
        self._task: Optional[asyncio.Task[None]] = None

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
        _LOGGER.info("visd ready (%s)", VISD_SOCKET_PATH)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
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
            raise RuntimeError("camera_no_frames")
        return frames


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
