"""Local IPC bridge for external controllers (e.g. GPIO LED process)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

IPC_DIR = Path("/tmp/lva-ipc")
CONTROL_SOCKET_PATH = IPC_DIR / "control.sock"
GPIO_EVENT_SOCKET_PATH = IPC_DIR / "gpio-events.sock"


class _ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, bridge: "LocalIpcBridge") -> None:
        self.bridge = bridge

    def datagram_received(self, data: bytes, _addr) -> None:  # type: ignore[override]
        try:
            payload = json.loads(data.decode("utf-8"))
            cmd = str(payload.get("cmd", "")).strip()
            if not cmd:
                return
            self.bridge.handle_command(cmd)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Invalid IPC command packet: %s", err)


class LocalIpcBridge:
    """IPC bridge using Unix datagram sockets.

    - Receives commands on CONTROL_SOCKET_PATH.
    - Sends events to GPIO_EVENT_SOCKET_PATH.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transport: Optional[asyncio.transports.DatagramTransport] = None
        self._control_handler: Optional[Callable[[str], None]] = None
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

    def handle_command(self, cmd: str) -> None:
        if self._control_handler is None:
            return
        try:
            self._control_handler(cmd)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("IPC control handler failed for cmd=%s", cmd)

    def emit_event(self, event: str, **data: object) -> None:
        payload = {"event": event, **data}
        message = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            self._event_socket.sendto(message, str(GPIO_EVENT_SOCKET_PATH))
        except FileNotFoundError:
            # GPIO service not listening.
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("IPC event send failed (%s): %s", event, err)
