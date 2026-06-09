"""Web configuration UI server (bridge pairing and setup)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from hue_entertainment import discover_bridges

from ambilight_hue_bridge.identity import bridge_id
from ambilight_hue_bridge.outbound.controller import list_areas, pair_and_store

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import RealBridge
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.engine.engine import Engine

LOGGER = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_PAIR_ERRORS = (TimeoutError, OSError, aiohttp.ClientError)


class WebServer:
    """Serves the web configuration UI and its JSON API."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        engine: Engine | None,
        mac: str,
        host_ip: str,
    ) -> None:
        """
        Initialize the web server.

        :param store: Config store backing the UI.
        :param engine: Engine queried for live streaming status (optional).
        :param mac: Resolved host MAC, for the displayed bridge id.
        :param host_ip: LAN IP shown in the UI.
        """
        self._store = store
        self._engine = engine
        self._mac = mac
        self._host_ip = host_ip

    def create_app(self) -> web.Application:
        """Build the aiohttp application for the configuration UI."""
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/api/status", self._handle_status),
                web.get("/api/discover", self._handle_discover),
                web.get("/api/bridges", self._handle_bridges),
                web.post("/api/bridges/pair", self._handle_pair),
                web.get("/api/bridges/{bridge_id}/areas", self._handle_areas),
                web.put("/api/bridges/{bridge_id}", self._handle_update_bridge),
                web.delete("/api/bridges/{bridge_id}", self._handle_delete_bridge),
            ],
        )
        return app

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        """Serve the single-page configuration UI."""
        return web.FileResponse(_STATIC_DIR / "index.html")

    async def _handle_status(self, _request: web.Request) -> web.StreamResponse:
        """Return the virtual bridge status and exposed lights."""
        config = self._store.config
        return web.json_response(
            {
                "name": config.virtual_bridge.name,
                "bridge_id": bridge_id(self._mac),
                "host": self._host_ip,
                "http_port": config.virtual_bridge.http_port,
                "streaming": self._engine.is_streaming if self._engine is not None else False,
                "active_real_bridge": config.active_real_bridge,
                "virtual_lights": [
                    {
                        "id": light.id,
                        "name": light.name,
                        "position": light.position,
                        "channels": light.channels,
                    }
                    for light in config.virtual_lights
                ],
            },
        )

    async def _handle_discover(self, _request: web.Request) -> web.StreamResponse:
        """Discover Hue bridges on the local network via mDNS."""
        try:
            found = await discover_bridges()
        except OSError as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(
            [{"id": bridge.id, "host": bridge.host, "name": bridge.name} for bridge in found],
        )

    async def _handle_bridges(self, _request: web.Request) -> web.StreamResponse:
        """List the configured real Hue bridges."""
        return web.json_response(
            [self._bridge_dict(bridge) for bridge in self._store.config.real_bridges],
        )

    async def _handle_pair(self, request: web.Request) -> web.StreamResponse:
        """Pair with a real Hue bridge (the link button must be pressed first)."""
        body = await _read_json(request)
        host = str(body.get("host", "")).strip()
        if not host:
            return web.json_response({"error": "A bridge host is required."}, status=400)
        try:
            bridge = await pair_and_store(self._store, host)
        except _PAIR_ERRORS as err:
            LOGGER.warning("Pairing with %s failed: %s", host, err)
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(self._bridge_dict(bridge))

    async def _handle_areas(self, request: web.Request) -> web.StreamResponse:
        """List the entertainment areas (and channels) on a configured bridge."""
        bridge = self._bridge_by_id(request.match_info["bridge_id"])
        if bridge is None:
            return web.json_response({"error": "Unknown bridge."}, status=404)
        try:
            areas = await list_areas(bridge.host, bridge.app_key)
        except _PAIR_ERRORS as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(
            [
                {
                    "id": area.id,
                    "name": area.name,
                    "channels": [
                        {
                            "channel_id": channel.channel_id,
                            "service_id": channel.service_id,
                            "position": list(channel.position),
                        }
                        for channel in area.channels
                    ],
                }
                for area in areas
            ],
        )

    async def _handle_update_bridge(self, request: web.Request) -> web.StreamResponse:
        """Update a bridge's entertainment area and/or mark it active."""
        bridge = self._bridge_by_id(request.match_info["bridge_id"])
        if bridge is None:
            return web.json_response({"error": "Unknown bridge."}, status=404)
        body = await _read_json(request)
        if "entertainment_area" in body:
            bridge.entertainment_area = str(body["entertainment_area"])
        if body.get("active"):
            self._store.config.active_real_bridge = bridge.id
        self._store.save()
        return web.json_response(self._bridge_dict(bridge))

    async def _handle_delete_bridge(self, request: web.Request) -> web.StreamResponse:
        """Remove a configured bridge."""
        target = request.match_info["bridge_id"]
        config = self._store.config
        config.real_bridges = [bridge for bridge in config.real_bridges if bridge.id != target]
        if config.active_real_bridge == target:
            config.active_real_bridge = ""
        self._store.save()
        return web.json_response({"deleted": target})

    def _bridge_dict(self, bridge: RealBridge) -> dict[str, Any]:
        """Build the JSON representation of a real bridge for the UI."""
        return {
            "id": bridge.id,
            "host": bridge.host,
            "model": bridge.model,
            "paired": bool(bridge.app_key and bridge.client_key),
            "entertainment_area": bridge.entertainment_area,
            "active": self._store.config.active_real_bridge == bridge.id,
        }

    def _bridge_by_id(self, bridge_id_value: str) -> RealBridge | None:
        """Return a configured bridge by id, or None."""
        for bridge in self._store.config.real_bridges:
            if bridge.id == bridge_id_value:
                return bridge
        return None


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Read and parse a JSON request body, returning an empty dict on absence/parse error."""
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
