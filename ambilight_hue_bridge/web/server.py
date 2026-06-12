"""Web configuration UI server (bridge pairing and setup)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from hue_entertainment import discover_bridges

from ambilight_hue_bridge.config.models import CachedArea, CachedChannel
from ambilight_hue_bridge.const import MAX_STREAM_SMOOTHING
from ambilight_hue_bridge.identity import bridge_id
from ambilight_hue_bridge.outbound.controller import (
    active_bridge,
    lights_from_area,
    list_areas,
    pair_and_store,
)

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import PairedUser, RealBridge
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.engine.engine import Engine

LOGGER = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_PAIR_ERRORS = (TimeoutError, OSError, aiohttp.ClientError)
# Cap how long we'll wait on the real bridge when listing areas, so an unreachable bridge
# (e.g. unplugged during TV pairing) fails fast instead of hanging the web UI.
_LIST_AREAS_TIMEOUT = 5.0


class WebServer:
    """Serves the web configuration UI and its JSON API."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        engine: Engine | None,
        mac: str,
        host_ip: str,
        http_port: int,
    ) -> None:
        """
        Initialize the web server.

        :param store: Config store backing the UI.
        :param engine: Engine queried for live streaming status (optional).
        :param mac: Resolved host MAC, for the displayed bridge id.
        :param host_ip: LAN IP shown in the UI.
        :param http_port: TCP port the bridge is served on, shown in the UI.
        """
        self._store = store
        self._engine = engine
        self._mac = mac
        self._host_ip = host_ip
        self._http_port = http_port
        # Cached once so the index handler can inject the ingress <base> per request without
        # blocking file I/O on the event loop.
        self._index_html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def register(self, app: web.Application) -> None:
        """Register the web UI page and its JSON config API (under /cfg) on the app."""
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/cfg/status", self._handle_status),
                web.get("/cfg/tvs", self._handle_tvs),
                web.put("/cfg/tvs/{username}", self._handle_assign_tv),
                web.delete("/cfg/tvs/{username}", self._handle_delete_tv),
                web.get("/cfg/areas", self._handle_areas_list),
                web.get("/cfg/discover", self._handle_discover),
                web.get("/cfg/bridges", self._handle_bridges),
                web.post("/cfg/bridges/pair", self._handle_pair),
                web.put("/cfg/bridges/{bridge_id}", self._handle_update_bridge),
                web.delete("/cfg/bridges/{bridge_id}", self._handle_delete_bridge),
            ],
        )

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        """Serve the single-page configuration UI, rewriting the <base> for HA ingress."""
        # Behind Home Assistant ingress the page is served under /api/hassio_ingress/<token>/;
        # point <base> there so the UI's relative requests resolve against that prefix. Without
        # the header (direct/Docker access) the base stays "/".
        ingress_path = request.headers.get("X-Ingress-Path")
        html = self._index_html
        if ingress_path:
            html = html.replace('<base href="/" />', f'<base href="{ingress_path}/" />', 1)
        return web.Response(text=html, content_type="text/html")

    async def _handle_status(self, _request: web.Request) -> web.StreamResponse:
        """Return the virtual bridge status and exposed lights."""
        config = self._store.config
        return web.json_response(
            {
                "name": config.virtual_bridge.name,
                "bridge_id": bridge_id(self._mac),
                "host": self._host_ip,
                "http_port": self._http_port,
                "streaming": self._engine.is_streaming if self._engine is not None else False,
                "active_real_bridge": config.active_real_bridge,
            },
        )

    async def _handle_tvs(self, _request: web.Request) -> web.StreamResponse:
        """List the paired TVs, their assigned area + lights, and which one is streaming."""
        owner = self._engine.stream_owner if self._engine is not None else None
        return web.json_response(
            [self._tv_dict(user, owner) for user in self._store.config.users],
        )

    async def _handle_assign_tv(self, request: web.Request) -> web.StreamResponse:
        """Assign a source entertainment area (+ split mode) to a TV and rebuild its lights."""
        username = request.match_info["username"]
        user = next((u for u in self._store.config.users if u.username == username), None)
        if user is None:
            return web.json_response({"error": "Unknown TV."}, status=404)
        body = await _read_json(request)
        # Only rebuild the lights when the area/split actually change - a smoothing-only edit
        # must not re-fetch the area and renumber lights the TV has already been assigned.
        rebuild = False
        if "entertainment_area" in body:
            user.entertainment_area = str(body["entertainment_area"])
            rebuild = True
        if "split_gradients" in body:
            user.split_gradients = bool(body["split_gradients"])
            rebuild = True
        if "stream_smoothing" in body:
            value = body["stream_smoothing"]
            user.stream_smoothing = (
                None if value is None else max(0.0, min(MAX_STREAM_SMOOTHING, float(value)))
            )
        if rebuild:
            if user.entertainment_area:
                area = await self._area_by_id(user.entertainment_area)
                user.lights = (
                    lights_from_area(area, split_gradients=user.split_gradients) if area else []
                )
            else:
                user.lights = []
        self._store.save()
        owner = self._engine.stream_owner if self._engine is not None else None
        return web.json_response(self._tv_dict(user, owner))

    async def _handle_delete_tv(self, request: web.Request) -> web.StreamResponse:
        """Remove a paired TV (stopping its stream if it is the one currently streaming)."""
        username = request.match_info["username"]
        config = self._store.config
        if not any(user.username == username for user in config.users):
            return web.json_response({"error": "Unknown TV."}, status=404)
        config.users = [user for user in config.users if user.username != username]
        if self._engine is not None and self._engine.stream_owner == username:
            await self._engine.stop_stream()
        self._store.save()
        return web.json_response({"deleted": username})

    async def _handle_areas_list(self, _request: web.Request) -> web.StreamResponse:
        """List the active bridge's entertainment areas (live, falling back to the cache)."""
        areas = await self._list_areas()
        return web.json_response(
            [{"id": area.id, "name": area.name, "channels": len(area.channels)} for area in areas],
        )

    async def _list_areas(self) -> list[Any]:
        """
        Return the active bridge's entertainment areas, refreshing the persisted cache.

        On success the areas are cached on the bridge; if the bridge is unreachable (e.g.
        unplugged during the TV discovery dance) the last cached areas are returned, so the web
        UI and per-TV assignment keep working without the real bridge online.
        """
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.app_key:
            return []
        try:
            areas = await asyncio.wait_for(
                list_areas(bridge.host, bridge.app_key), timeout=_LIST_AREAS_TIMEOUT
            )
        except _PAIR_ERRORS as err:
            LOGGER.debug(
                "Listing areas failed (%s); using %d cached", err, len(bridge.cached_areas)
            )
            return bridge.cached_areas
        bridge.cached_areas = _areas_to_cache(areas)
        self._store.save()
        return areas

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

    async def _handle_update_bridge(self, request: web.Request) -> web.StreamResponse:
        """Mark a configured bridge as the active one (the bridge TVs stream to)."""
        bridge = self._bridge_by_id(request.match_info["bridge_id"])
        if bridge is None:
            return web.json_response({"error": "Unknown bridge."}, status=404)
        body = await _read_json(request)
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

    def _tv_dict(self, user: PairedUser, owner: str | None) -> dict[str, Any]:
        """Build the JSON representation of a paired TV (with its assignment) for the UI."""
        return {
            "username": user.username,
            "devicetype": user.devicetype,
            "created": user.created,
            "streaming": user.username == owner,
            "entertainment_area": user.entertainment_area,
            "split_gradients": user.split_gradients,
            "stream_smoothing": user.stream_smoothing if user.stream_smoothing is not None else 0.0,
            "lights": [light.name for light in user.lights],
        }

    async def _area_by_id(self, area_id: str) -> Any:
        """Return the active bridge's entertainment area with the given id (live or cached)."""
        return next((area for area in await self._list_areas() if area.id == area_id), None)

    def _bridge_dict(self, bridge: RealBridge) -> dict[str, Any]:
        """Build the JSON representation of a real bridge for the UI."""
        return {
            "id": bridge.id,
            "host": bridge.host,
            "model": bridge.model,
            "paired": bool(bridge.app_key and bridge.client_key),
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


def _areas_to_cache(areas: list[Any]) -> list[CachedArea]:
    """Convert live ``EntertainmentArea`` objects into the persistable cache form."""
    return [
        CachedArea(
            id=area.id,
            name=area.name,
            channels=[
                CachedChannel(
                    channel_id=channel.channel_id,
                    service_id=channel.service_id,
                    name=channel.name,
                    position=list(channel.position),
                )
                for channel in area.channels
            ],
        )
        for area in areas
    ]
