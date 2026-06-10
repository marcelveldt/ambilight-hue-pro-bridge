"""Web configuration UI server (bridge pairing and setup)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from hue_entertainment import discover_bridges

from ambilight_hue_bridge.config.models import VirtualLight
from ambilight_hue_bridge.const import (
    MAX_STREAM_RATE_HZ,
    MAX_STREAM_SMOOTHING,
    MIN_STREAM_RATE_HZ,
)
from ambilight_hue_bridge.identity import bridge_id
from ambilight_hue_bridge.outbound.controller import active_bridge, list_areas, pair_and_store

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import PairedUser, RealBridge
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

    def register(self, app: web.Application) -> None:
        """Register the web UI page and its JSON config API (under /cfg) on the app."""
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/cfg/status", self._handle_status),
                web.get("/cfg/settings", self._handle_get_settings),
                web.put("/cfg/settings", self._handle_put_settings),
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
                "http_port": self._http_port,
                "streaming": self._engine.is_streaming if self._engine is not None else False,
                "active_real_bridge": config.active_real_bridge,
            },
        )

    async def _handle_get_settings(self, _request: web.Request) -> web.StreamResponse:
        """Return the live streaming settings (smoothing + frame rate)."""
        bridge = self._store.config.virtual_bridge
        return web.json_response(
            {"stream_smoothing": bridge.stream_smoothing, "stream_rate_hz": bridge.stream_rate_hz},
        )

    async def _handle_put_settings(self, request: web.Request) -> web.StreamResponse:
        """
        Update the streaming settings.

        ``stream_smoothing`` takes effect immediately (the engine reads it per frame);
        ``stream_rate_hz`` applies on the next restart.
        """
        body = await _read_json(request)
        bridge = self._store.config.virtual_bridge
        if "stream_smoothing" in body:
            bridge.stream_smoothing = max(
                0.0, min(MAX_STREAM_SMOOTHING, float(body["stream_smoothing"]))
            )
        if "stream_rate_hz" in body:
            bridge.stream_rate_hz = max(
                MIN_STREAM_RATE_HZ, min(MAX_STREAM_RATE_HZ, int(body["stream_rate_hz"]))
            )
        self._store.save()
        return web.json_response(
            {"stream_smoothing": bridge.stream_smoothing, "stream_rate_hz": bridge.stream_rate_hz},
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
                    _mirror_from_area(area, split_gradients=user.split_gradients) if area else []
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
        """List the active bridge's entertainment areas (for per-TV assignment)."""
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.app_key:
            return web.json_response([])
        try:
            areas = await list_areas(bridge.host, bridge.app_key)
        except _PAIR_ERRORS as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(
            [{"id": area.id, "name": area.name, "channels": len(area.channels)} for area in areas],
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
            "stream_smoothing": user.stream_smoothing,
            "effective_smoothing": (
                user.stream_smoothing
                if user.stream_smoothing is not None
                else self._store.config.virtual_bridge.stream_smoothing
            ),
            "lights": [light.name for light in user.lights],
        }

    async def _area_by_id(self, area_id: str) -> Any:
        """Return the active bridge's entertainment area with the given id, or None."""
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.app_key:
            return None
        try:
            areas = await list_areas(bridge.host, bridge.app_key)
        except _PAIR_ERRORS as err:
            LOGGER.debug("Could not list areas: %s", err)
            return None
        return next((area for area in areas if area.id == area_id), None)

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


def _position_from_x(x: float) -> str:
    """Map an entertainment channel's x position to a coarse screen position."""
    if x < -0.3:
        return "left"
    if x > 0.3:
        return "right"
    return "center"


def _mirror_from_area(area: Any, *, split_gradients: bool = True) -> list[VirtualLight]:
    """
    Build the virtual lights the TV sees from a source entertainment area.

    With ``split_gradients`` each channel becomes its own light (so the TV can drive each
    gradient zone separately, numbered when they share a name). Otherwise channels of the same
    light (e.g. a gradient strip's zones) are merged into one light driving all its channels.

    :param area: An EntertainmentArea from the hue_entertainment library.
    :param split_gradients: Expose each channel as a separate light (vs one light per device).
    """
    if not split_gradients:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for channel in area.channels:
            key = channel.service_id or channel.name
            if key not in merged:
                merged[key] = {"name": channel.name, "x": channel.position[0], "channels": []}
                order.append(key)
            merged[key]["channels"].append(channel.channel_id)
        return [
            VirtualLight(
                id=str(index + 1),
                name=merged[key]["name"] or f"Light {index + 1}",
                position=_position_from_x(merged[key]["x"]),
                channels=merged[key]["channels"],
            )
            for index, key in enumerate(order)
        ]
    name_counts: dict[str, int] = {}
    for channel in area.channels:
        name_counts[channel.name] = name_counts.get(channel.name, 0) + 1
    seen: dict[str, int] = {}
    lights: list[VirtualLight] = []
    for index, channel in enumerate(area.channels):
        base = channel.name or f"Zone {index + 1}"
        if name_counts.get(channel.name, 0) > 1:
            # Same-named gradient zones: disambiguate by on-screen position so the user can tell
            # them apart (e.g. "Strip (left)") instead of an opaque "Strip 1".
            label = _zone_label(channel.position)
            seen[label] = seen.get(label, 0) + 1
            suffix = label if seen[label] == 1 else f"{label} {seen[label]}"
            name = f"{base} ({suffix})"
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


def _zone_label(position: Any) -> str:
    """Describe a channel's horizontal screen position from its x coordinate (left..right)."""
    x = position[0]
    if x < -0.6:
        return "far left"
    if x < -0.2:
        return "left"
    if x > 0.6:
        return "far right"
    if x > 0.2:
        return "right"
    return "center"
