"""Local IPC bridge for external controllers and helper daemons."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

IPC_DIR = Path("/tmp/lva-ipc")
CONTROL_SOCKET_PATH = IPC_DIR / "control.sock"
GPIO_EVENT_SOCKET_PATH = IPC_DIR / "gpio-events.sock"
VISD_SOCKET_PATH = IPC_DIR / "visd.sock"

IpcMessage = dict[str, object]


def build_message(
    message_type: str,
    payload: Optional[dict[str, object]] = None,
    *,
    source: str = "core",
    ts: Optional[float] = None,
) -> IpcMessage:
    """Build a stable IPC envelope."""
    return {
        "type": str(message_type),
        "payload": dict(payload or {}),
        "ts": float(time.time() if ts is None else ts),
        "source": source,
    }


def normalize_message(packet: dict[str, object], *, default_source: str = "external") -> Optional[IpcMessage]:
    """Normalize legacy and current packet formats into IPC envelope."""
    packet_type = packet.get("type")
    if isinstance(packet_type, str) and packet_type.strip():
        payload = packet.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        ts = packet.get("ts")
        try:
            ts_value = float(ts) if ts is not None else None
        except (TypeError, ValueError):
            ts_value = None
        source = packet.get("source")
        source_value = str(source).strip() if isinstance(source, str) else default_source
        return build_message(packet_type.strip(), payload, source=source_value, ts=ts_value)

    legacy_cmd = packet.get("cmd")
    if isinstance(legacy_cmd, str) and legacy_cmd.strip():
        command = legacy_cmd.strip()
        return build_message(command.upper(), {"command": command.lower()}, source=default_source)

    legacy_event = packet.get("event")
    if isinstance(legacy_event, str) and legacy_event.strip():
        event_payload = dict(packet)
        event_payload.pop("event", None)
        return build_message("LEGACY_EVENT", {"event": legacy_event.strip(), **event_payload}, source=default_source)

    return None


class _ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, bridge: "LocalIpcBridge") -> None:
        self.bridge = bridge

    def datagram_received(self, data: bytes, _addr) -> None:  # type: ignore[override]
        try:
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                return
            message = normalize_message(payload)
            if message is None:
                return
            self.bridge.handle_message(message)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Invalid IPC command packet: %s", err)


class LocalIpcBridge:
    """IPC bridge using Unix datagram sockets.

    - Receives commands on CONTROL_SOCKET_PATH.
    - Sends events to GPIO_EVENT_SOCKET_PATH.
    - Supports in-process event listeners.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transport: Optional[asyncio.transports.DatagramTransport] = None
        self._control_handler: Optional[Callable[[str], None]] = None
        self._message_handler: Optional[Callable[[IpcMessage], None]] = None
        self._event_listeners: set[Callable[[dict[str, object]], None]] = set()
        self._event_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._event_socket.setblocking(False)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        IPC_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(IPC_DIR, 0o777)
        except Exception:  # noqa: BLE001
            pass

        if CONTROL_SOCKET_PATH.exists():
            CONTROL_SOCKET_PATH.unlink()

        transport, _protocol = await self._loop.create_datagram_endpoint(
            lambda: _ControlProtocol(self),
            local_addr=str(CONTROL_SOCKET_PATH),
            family=socket.AF_UNIX,
        )
        self._transport = transport

        try:
            os.chmod(CONTROL_SOCKET_PATH, 0o666)
        except Exception:  # noqa: BLE001
            pass

        _LOGGER.info("Local IPC started (%s)", CONTROL_SOCKET_PATH)

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

        try:
            self._event_socket.close()
        except Exception:  # noqa: BLE001
            pass

        try:
            if CONTROL_SOCKET_PATH.exists():
                CONTROL_SOCKET_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

    def set_control_handler(self, handler: Optional[Callable[[str], None]]) -> None:
        self._control_handler = handler

    def set_message_handler(self, handler: Optional[Callable[[IpcMessage], None]]) -> None:
        self._message_handler = handler

    def add_event_listener(self, listener: Callable[[dict[str, object]], None]) -> None:
        self._event_listeners.add(listener)

    def remove_event_listener(self, listener: Callable[[dict[str, object]], None]) -> None:
        self._event_listeners.discard(listener)

    def handle_command(self, cmd: str, *, source: str = "external") -> None:
        self.handle_message(build_message(cmd.upper(), {"command": cmd.lower()}, source=source))

    def handle_message(self, message: IpcMessage) -> None:
        msg_type = str(message.get("type", "")).strip()
        if not msg_type:
            return

        if self._message_handler is not None:
            try:
                self._message_handler(message)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IPC message handler failed for type=%s", msg_type)

        payload = message.get("payload")
        cmd = None
        if isinstance(payload, dict):
            cmd_value = payload.get("command")
            if isinstance(cmd_value, str) and cmd_value.strip():
                cmd = cmd_value.strip().lower()
        if cmd is None:
            cmd = msg_type.lower()

        if self._control_handler is None:
            return
        try:
            self._control_handler(cmd)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("IPC control handler failed for cmd=%s", cmd)

    def send_message(
        self,
        message_type: str,
        payload: Optional[dict[str, object]] = None,
        *,
        socket_path: Path = GPIO_EVENT_SOCKET_PATH,
        source: str = "core",
    ) -> None:
        message = build_message(message_type, payload, source=source)
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
        try:
            self._event_socket.sendto(encoded, str(socket_path))
        except FileNotFoundError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("IPC send failed (type=%s, socket=%s): %s", message_type, socket_path, err)

    def emit_event(self, event: str, **data: object) -> None:
        payload = {"event": event, **data}
        message = build_message("EVENT", payload, source="core")
        for listener in tuple(self._event_listeners):
            try:
                listener(message)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("IPC in-process event listener failed (event=%s)", event)
        self.send_message("EVENT", payload, socket_path=GPIO_EVENT_SOCKET_PATH, source="core")


def send_ipc_message(
    socket_path: Path,
    message_type: str,
    payload: Optional[dict[str, object]] = None,
    *,
    source: str,
) -> None:
    """Send a one-shot IPC message to a unix datagram socket."""
    packet = build_message(message_type, payload, source=source)
    encoded = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.sendto(encoded, str(socket_path))
    finally:
        sock.close()
