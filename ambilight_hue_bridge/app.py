"""Service supervisor: wires together discovery, the v1 emulator, and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

from aiohttp import web

from .config.store import ConfigStore
from .const import BRIDGE_MODEL_ID, CERT_FILENAME, CERT_KEY_FILENAME, CONFIG_FILENAME
from .discovery.cert import load_or_create_ssl_context
from .discovery.mdns import MDNSAdvertiser
from .discovery.ssdp import SSDPServer
from .emulator.inbound import InboundStreamer
from .emulator.pairing import PairingManager
from .emulator.rest_v1 import HueV1Emulator, log_requests
from .engine.engine import Engine
from .identity import bridge_id, bridge_udn, get_host_mac
from .web.server import WebServer

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
    """
    Owns the service lifecycle.

    Config, the HTTP/HTTPS server, SSDP + mDNS discovery, the v1 emulator, and the
    inbound/outbound streaming.
    """

    def __init__(self, data_dir: Path, *, http_port: int, https_port: int) -> None:
        """
        Initialize the application (no I/O until :meth:`run`).

        :param data_dir: Directory for persistent configuration and state.
        :param http_port: TCP port for the combined Hue v1 API + web UI server.
        :param https_port: TCP port for the TLS listener (0 disables HTTPS).
        """
        self._data_dir = data_dir
        self._store = ConfigStore(data_dir / CONFIG_FILENAME)
        self._http_port = http_port
        self._https_port = https_port
        self._shutdown = asyncio.Event()
        self._engine: Engine | None = None
        self._inbound: InboundStreamer | None = None
        self._ssdp: SSDPServer | None = None
        self._mdns: MDNSAdvertiser | None = None
        self._runner: web.AppRunner | None = None

    async def run(self) -> None:
        """Start all services and run until :meth:`request_stop` (or cancellation)."""
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.stop()

    async def start(self) -> None:
        """Load config and start the HTTP/HTTPS server, SSDP + mDNS discovery, and inbound DTLS."""
        self._store.load()
        config = self._store.config
        http_port = self._http_port
        mac = config.virtual_bridge.mac or get_host_mac()
        host_ip = get_host_ip()

        engine = Engine(self._store)
        self._engine = engine
        pairing = PairingManager(self._store)
        emulator = HueV1Emulator(
            store=self._store,
            pairing=pairing,
            host_ip=host_ip,
            mac=mac,
            http_port=http_port,
            https_port=self._https_port,
            engine=engine,
        )
        web_server = WebServer(
            store=self._store,
            engine=engine,
            mac=mac,
            host_ip=host_ip,
            http_port=http_port,
        )

        # A single aiohttp app serves both the TV-facing Hue v1 API/descriptor and the web UI,
        # so the whole bridge listens on one port (older TVs assume the Hue bridge is on :80).
        app = web.Application(middlewares=[log_requests])
        emulator.register(app)
        web_server.register(app)
        runner = web.AppRunner(app, access_log=None)
        self._runner = runner
        await runner.setup()
        await web.TCPSite(runner, host="0.0.0.0", port=http_port).start()
        LOGGER.info("HTTP server (Hue API + web UI) listening on %s:%d", host_ip, http_port)

        # The newer Hue/CLIP-v2 path connects over TLS; serve the same app on 443 with a
        # Hue-style cert (harmless for the v1 TVs, ready for the very new sets).
        https_port = await self._start_https(runner, mac, host_ip)

        # Primary discovery is SSDP: the current Ambilight TVs find the bridge via SSDP
        # M-SEARCH + the UPnP descriptor on HTTP. mDNS is added below as belt-and-suspenders.
        ssdp = SSDPServer(
            host_ip=host_ip,
            http_port=http_port,
            bridge_id=bridge_id(mac),
            udn=bridge_udn(mac),
        )
        self._ssdp = ssdp
        await ssdp.start()

        # Real Hue bridges also advertise _hue._tcp via mDNS on the TLS port; mirror that for
        # newer/Android-era Hue clients. Only meaningful when HTTPS actually bound (the service
        # points clients at the TLS endpoint), so gate it on the live HTTPS port.
        if config.virtual_bridge.enable_mdns and https_port:
            mdns = MDNSAdvertiser(host_ip=host_ip, port=https_port, bridge_id=bridge_id(mac))
            self._mdns = mdns
            await mdns.start()

        if config.virtual_bridge.enable_inbound_dtls:
            inbound = InboundStreamer(store=self._store, pairing=pairing, engine=engine)
            self._inbound = inbound
            await inbound.start()

        LOGGER.info(
            "%s ready - bridge id %s, %d paired TV(s)",
            config.virtual_bridge.name,
            bridge_id(mac),
            len(config.users),
        )

    async def stop(self) -> None:
        """Stop inbound streaming, the outbound stream, discovery and the HTTP server."""
        if self._inbound is not None:
            await self._inbound.stop()
            self._inbound = None
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
        if self._mdns is not None:
            await self._mdns.stop()
            self._mdns = None
        if self._ssdp is not None:
            await self._ssdp.stop()
            self._ssdp = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def request_stop(self) -> None:
        """Signal the running service to shut down."""
        self._shutdown.set()

    async def _start_https(self, runner: web.AppRunner, mac: str, host_ip: str) -> int:
        """
        Start the TLS listener serving the same app, returning the bound port (0 if disabled).

        :param runner: The shared aiohttp runner whose app is also served over TLS.
        :param mac: Resolved host MAC, used to derive the certificate identity (CN).
        :param host_ip: LAN IP, for logging.
        """
        if self._https_port <= 0:
            return 0
        loop = asyncio.get_running_loop()
        # Cert generation + file I/O are one-time and bounded; keep them off the event loop.
        context = await loop.run_in_executor(
            None,
            lambda: load_or_create_ssl_context(
                self._data_dir / CERT_FILENAME,
                self._data_dir / CERT_KEY_FILENAME,
                bridge_id=bridge_id(mac),
                model_id=BRIDGE_MODEL_ID,
            ),
        )
        try:
            site = web.TCPSite(runner, host="0.0.0.0", port=self._https_port, ssl_context=context)
            await site.start()
        except OSError as err:
            LOGGER.warning(
                "Could not start HTTPS on port %d (%s) - continuing with HTTP only. Newer "
                "Ambilight TVs may need HTTPS; run with privileges so port 443 can bind.",
                self._https_port,
                err,
            )
            return 0
        LOGGER.info(
            "HTTPS server listening on %s:%d (Hue-style self-signed cert)",
            host_ip,
            self._https_port,
        )
        return self._https_port
