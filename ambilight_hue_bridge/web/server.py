"""Web configuration UI server (bridge pairing and setup)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from hue_entertainment import discover_bridges

from ambilight_hue_bridge.config.models import VirtualLight
from ambilight_hue_bridge.identity import bridge_id
from ambilight_hue_bridge.outbound.controller import active_bridge, list_areas, pair_and_store

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
                web.get("/api/channels", self._handle_channels),
                web.get("/api/lights", self._handle_lights_list),
                web.post("/api/lights", self._handle_create_light),
                web.post("/api/lights/auto-map", self._handle_auto_map),
                web.put("/api/lights/{light_id}", self._handle_update_light),
                web.delete("/api/lights/{light_id}", self._handle_delete_light),
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
        # Mirror the area by default: with no lights configured yet, create one per channel
        # (named after the real light) so it works with zero manual mapping.
        if bridge.entertainment_area and not self._store.config.virtual_lights:
            area = await self._active_area()
            if area is not None:
                self._store.config.virtual_lights = _mirror_from_area(area)
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

    async def _handle_channels(self, _request: web.Request) -> web.StreamResponse:
        """Return the entertainment channels of the active bridge's selected area."""
        channels = await self._active_area_channels()
        if channels is None:
            return web.json_response({"channels": []})
        return web.json_response({"channels": channels})

    async def _handle_lights_list(self, _request: web.Request) -> web.StreamResponse:
        """List the virtual lights exposed to the TV."""
        return web.json_response(
            [_light_dict(light) for light in self._store.config.virtual_lights],
        )

    async def _handle_create_light(self, request: web.Request) -> web.StreamResponse:
        """Create a new virtual light."""
        body = await _read_json(request)
        new_id = self._next_light_id()
        light = VirtualLight(
            id=new_id,
            name=str(body.get("name") or f"Light {new_id}"),
            position=str(body.get("position", "center")),
        )
        self._store.config.virtual_lights.append(light)
        self._store.save()
        return web.json_response(_light_dict(light))

    async def _handle_update_light(self, request: web.Request) -> web.StreamResponse:
        """Update a virtual light's name, position and channel mapping."""
        light = self._virtual_light_by_id(request.match_info["light_id"])
        if light is None:
            return web.json_response({"error": "Unknown light."}, status=404)
        body = await _read_json(request)
        if "name" in body:
            light.name = str(body["name"])
        if "position" in body:
            light.position = str(body["position"])
        channels = body.get("channels")
        if isinstance(channels, list):
            light.channels = [int(channel) for channel in channels]
        self._store.save()
        return web.json_response(_light_dict(light))

    async def _handle_delete_light(self, request: web.Request) -> web.StreamResponse:
        """Remove a virtual light."""
        target = request.match_info["light_id"]
        config = self._store.config
        config.virtual_lights = [light for light in config.virtual_lights if light.id != target]
        self._store.save()
        return web.json_response({"deleted": target})

    async def _handle_auto_map(self, _request: web.Request) -> web.StreamResponse:
        """Replace the virtual lights with one per channel of the active area."""
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.app_key or not bridge.entertainment_area:
            return web.json_response({"error": "Select an entertainment area first."}, status=400)
        area = await self._active_area()
        if area is None:
            return web.json_response({"error": "Entertainment area not found."}, status=404)
        self._store.config.virtual_lights = _mirror_from_area(area)
        self._store.save()
        return web.json_response(
            [_light_dict(light) for light in self._store.config.virtual_lights]
        )

    async def _active_area(self) -> Any:
        """Return the active bridge's selected EntertainmentArea, or None."""
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.app_key or not bridge.entertainment_area:
            return None
        try:
            areas = await list_areas(bridge.host, bridge.app_key)
        except _PAIR_ERRORS as err:
            LOGGER.debug("Could not list areas: %s", err)
            return None
        return next((area for area in areas if area.id == bridge.entertainment_area), None)

    async def _active_area_channels(self) -> list[dict[str, Any]] | None:
        """Return the channels of the active area as JSON dicts, or None if unavailable."""
        area = await self._active_area()
        if area is None:
            return None
        return [
            {
                "channel_id": channel.channel_id,
                "name": channel.name,
                "position": list(channel.position),
            }
            for channel in area.channels
        ]

    def _virtual_light_by_id(self, light_id: str) -> VirtualLight | None:
        """Return a virtual light by id, or None."""
        for light in self._store.config.virtual_lights:
            if light.id == light_id:
                return light
        return None

    def _next_light_id(self) -> str:
        """Return the next free numeric virtual-light id as a string."""
        ids = [int(light.id) for light in self._store.config.virtual_lights if light.id.isdigit()]
        return str(max(ids, default=0) + 1)

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


def _light_dict(light: VirtualLight) -> dict[str, Any]:
    """Build the JSON representation of a virtual light for the UI."""
    return {
        "id": light.id,
        "name": light.name,
        "position": light.position,
        "modelid": light.modelid,
        "channels": light.channels,
    }


def _position_from_x(x: float) -> str:
    """Map an entertainment channel's x position to a coarse screen position."""
    if x < -0.3:
        return "left"
    if x > 0.3:
        return "right"
    return "center"


def _mirror_from_area(area: Any) -> list[VirtualLight]:
    """
    Build one virtual light per channel of an area, named after its real light.

    Channels that share a light name (e.g. the zones of a gradient strip) are numbered.

    :param area: An EntertainmentArea from the hue_entertainment library.
    """
    name_counts: dict[str, int] = {}
    for channel in area.channels:
        name_counts[channel.name] = name_counts.get(channel.name, 0) + 1
    seen: dict[str, int] = {}
    lights: list[VirtualLight] = []
    for index, channel in enumerate(area.channels):
        base = channel.name or f"Zone {index + 1}"
        if name_counts.get(channel.name, 0) > 1:
            seen[base] = seen.get(base, 0) + 1
            name = f"{base} {seen[base]}"
        else:
            name = base
        lights.append(
            VirtualLight(
                id=str(index + 1),
                name=name,
                position=_position_from_x(channel.position[0]),
                channels=[channel.channel_id],
            ),
        )
    return lights
