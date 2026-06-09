"""Service supervisor: wires together discovery, the v1 emulator, and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

from aiohttp import web

from .config.models import VirtualLight
from .config.store import ConfigStore
from .const import CONFIG_FILENAME
from .discovery.ssdp import SSDPServer
from .emulator.pairing import PairingManager
from .emulator.rest_v1 import HueV1Emulator
from .engine.engine import Engine
from .identity import bridge_id, bridge_udn, get_host_mac

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


def get_host_ip() -> str:
    """Return the host's primary LAN IPv4 address (falls back to loopback)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just selects the routable source address.
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class BridgeApp:
    """Owns the service lifecycle: config, the SSDP responder, and the v1 emulator."""

    def __init__(self, data_dir: Path, *, http_port: int | None = None) -> None:
        """
        Initialize the application (no I/O until :meth:`run`).

        :param data_dir: Directory for persistent configuration and state.
        :param http_port: Optional override for the virtual bridge HTTP port.
        """
        self._store = ConfigStore(data_dir / CONFIG_FILENAME)
        self._http_port_override = http_port
        self._shutdown = asyncio.Event()
        self._engine: Engine | None = None
        self._ssdp: SSDPServer | None = None
        self._runner: web.AppRunner | None = None

    async def run(self) -> None:
        """Start all services and run until :meth:`request_stop` (or cancellation)."""
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    async def start(self) -> None:
        """Load config and start the HTTP API and SSDP responder."""
        self._store.load()
        self._ensure_defaults()
        config = self._store.config
        if self._http_port_override is not None:
            config.virtual_bridge.http_port = self._http_port_override
        mac = config.virtual_bridge.mac or get_host_mac()
        host_ip = get_host_ip()
        port = config.virtual_bridge.http_port

        engine = Engine(self._store)
        self._engine = engine
        emulator = HueV1Emulator(
            store=self._store,
            pairing=PairingManager(self._store),
            host_ip=host_ip,
            mac=mac,
            engine=engine,
        )
        runner = web.AppRunner(emulator.create_app(), access_log=None)
        self._runner = runner
        await runner.setup()
        await web.TCPSite(runner, host="0.0.0.0", port=port).start()
        LOGGER.info("Virtual Hue bridge HTTP API listening on %s:%d", host_ip, port)

        ssdp = SSDPServer(
            host_ip=host_ip,
            http_port=port,
            bridge_id=bridge_id(mac),
            udn=bridge_udn(mac),
        )
        self._ssdp = ssdp
        await ssdp.start()
        LOGGER.info(
            "%s ready - bridge id %s, %d virtual light(s)",
            config.virtual_bridge.name,
            bridge_id(mac),
            len(config.virtual_lights),
        )

    async def stop(self) -> None:
        """Stop the outbound stream, the SSDP responder and the HTTP API."""
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
        if self._ssdp is not None:
            await self._ssdp.stop()
            self._ssdp = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def request_stop(self) -> None:
        """Signal the running service to shut down."""
        self._shutdown.set()

    def _ensure_defaults(self) -> None:
        """Seed example virtual lights on first run so the TV has something to select."""
        config = self._store.config
        if config.virtual_lights:
            return
        config.virtual_lights = [
            VirtualLight(id="1", name="Ambilight Left", position="left"),
            VirtualLight(id="2", name="Ambilight Center", position="center"),
            VirtualLight(id="3", name="Ambilight Right", position="right"),
        ]
        self._store.save()
        LOGGER.info("Seeded %d default virtual lights", len(config.virtual_lights))
