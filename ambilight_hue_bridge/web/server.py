"""Web configuration UI server (bridge pairing and setup)."""

from __future__ import annotations

import asyncio
import logging
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from hue_entertainment import discover_bridges
from hue_entertainment.zigbee import ZigguratError, read_segment_counts

from ambilight_hue_bridge.config.models import (
    CachedArea,
    CachedChannel,
    StreamTarget,
    ZigbeeLightRef,
)
from ambilight_hue_bridge.const import MAX_STREAM_SMOOTHING, MAX_ZIGBEE_LIGHTS
from ambilight_hue_bridge.identity import bridge_id
from ambilight_hue_bridge.outbound.controller import (
    active_bridge,
    lights_from_area,
    lights_from_zigbee,
    list_areas,
    pair_and_store,
    zigbee_channels,
)
from ambilight_hue_bridge.outbound.homeassistant import HomeAssistantClient, HomeAssistantError

if TYPE_CHECKING:
    from aiohttp.typedefs import Handler

    from ambilight_hue_bridge.config.models import PairedUser, RealBridge
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.engine.engine import Engine

LOGGER = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_PAIR_ERRORS = (TimeoutError, OSError, aiohttp.ClientError)
# Cap how long we'll wait on the real bridge when listing areas, so an unreachable bridge
# (e.g. unplugged during TV pairing) fails fast instead of hanging the web UI.
_LIST_AREAS_TIMEOUT = 5.0
_MIN_STREAM_PRIORITY = 0
_MAX_STREAM_PRIORITY = 100
# The Supervisor's address on its Docker network: Home Assistant ingress requests reach an add-on
# from here, also with host networking.
_SUPERVISOR_IP = ip_address("172.30.32.2")
_ADDON_ONLY_TEXT = "Open the bridge configuration from the Home Assistant sidebar."


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
        home_assistant: HomeAssistantClient | None = None,
    ) -> None:
        """
        Initialize the web server.

        :param store: Config store backing the UI.
        :param engine: Engine queried for live streaming status (optional).
        :param mac: Resolved host MAC, for the displayed bridge id.
        :param host_ip: LAN IP shown in the UI.
        :param http_port: TCP port the bridge is served on, shown in the UI.
        :param home_assistant: Client listing the ZHA lamps (defaults to one on ``store``).
        """
        self._store = store
        self._engine = engine
        self._home_assistant = (
            home_assistant if home_assistant is not None else HomeAssistantClient(store)
        )
        self._mac = mac
        self._lamps_lock = asyncio.Lock()
        self._host_ip = host_ip
        self._http_port = http_port
        self._resources: frozenset[web.AbstractResource | None] = frozenset()
        # Cached once so the index handler can inject the ingress <base> per request without
        # blocking file I/O on the event loop.
        self._index_html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def register(self, app: web.Application) -> None:
        """
        Register the web UI page and its JSON config API (under /cfg) on the app.

        As a Home Assistant add-on, both answer only Home Assistant: its ingress proxy and
        Home Assistant itself on this host. Any other client gets a 403.

        :param app: The application that also serves the Hue API.
        """
        routes = app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/cfg/status", self._handle_status),
                web.get("/cfg/tvs", self._handle_tvs),
                web.get("/cfg/tvs/{username}", self._handle_tv),
                web.put("/cfg/tvs/{username}", self._handle_assign_tv),
                web.delete("/cfg/tvs/{username}", self._handle_delete_tv),
                web.get("/cfg/areas", self._handle_areas_list),
                web.get("/cfg/zigbee/lights", self._handle_zigbee_lights),
                web.get("/cfg/homeassistant", self._handle_home_assistant),
                web.put("/cfg/homeassistant", self._handle_update_home_assistant),
                web.get("/cfg/discover", self._handle_discover),
                web.get("/cfg/bridges", self._handle_bridges),
                web.post("/cfg/bridges/pair", self._handle_pair),
                web.put("/cfg/bridges/{bridge_id}", self._handle_update_bridge),
                web.delete("/cfg/bridges/{bridge_id}", self._handle_delete_bridge),
            ],
        )
        # Per resource rather than per route: aiohttp adds the HEAD route of a GET itself.
        self._resources = frozenset(route.resource for route in routes)
        if self._home_assistant.is_addon:
            app.middlewares.append(self._home_assistant_only)

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
        """List the paired TVs, their assigned area + lights, and which ones are streaming."""
        owners = self._engine.stream_owners if self._engine is not None else frozenset()
        return web.json_response(
            [self._tv_dict(user, owners) for user in self._store.config.users],
        )

    async def _handle_tv(self, request: web.Request) -> web.StreamResponse:
        """Return one paired TV and its stream policy."""
        user = self._tv_by_username(request.match_info["username"])
        if user is None:
            return web.json_response({"error": "Unknown TV."}, status=404)
        owners = self._engine.stream_owners if self._engine is not None else frozenset()
        return web.json_response(self._tv_dict(user, owners))

    async def _handle_assign_tv(self, request: web.Request) -> web.StreamResponse:
        """Update a TV's target, assignment, smoothing, enabled state, or stream priority."""
        username = request.match_info["username"]
        user = self._tv_by_username(username)
        if user is None:
            return web.json_response({"error": "Unknown TV."}, status=404)
        body = await _read_json(request)
        enabled_update: bool | None = None
        priority_update: int | None = None
        if "enabled" in body:
            enabled_update = body["enabled"]
            if not isinstance(enabled_update, bool):
                return web.json_response({"error": "enabled must be a boolean."}, status=400)
        if "stream_priority" in body:
            priority_update = body["stream_priority"]
            if isinstance(priority_update, bool) or not isinstance(priority_update, int):
                return web.json_response(
                    {"error": "stream_priority must be an integer."},
                    status=400,
                )
            if not _MIN_STREAM_PRIORITY <= priority_update <= _MAX_STREAM_PRIORITY:
                return web.json_response(
                    {
                        "error": (
                            f"stream_priority must be between {_MIN_STREAM_PRIORITY} "
                            f"and {_MAX_STREAM_PRIORITY}."
                        )
                    },
                    status=400,
                )
        target_update: StreamTarget | None = None
        lamps_update: list[ZigbeeLightRef] | None = None
        if "target" in body:
            try:
                target_update = StreamTarget(body["target"])
            except ValueError:
                return web.json_response(
                    {"error": "target must be 'hue' or 'zigbee'."},
                    status=400,
                )
        if "zigbee_lights" in body:
            lamps_update = _parse_zigbee_lights(body["zigbee_lights"])
            if lamps_update is None:
                return web.json_response(
                    {
                        "error": (
                            f"zigbee_lights must be a list of up to {MAX_ZIGBEE_LIGHTS} lamps, "
                            "each with an ieee."
                        )
                    },
                    status=400,
                )
        # Only rebuild the lights when the target, area/split or lamps actually change - a
        # smoothing-only edit must not re-fetch the area and renumber lights the TV has already
        # been assigned.
        rebuild = False
        if target_update is not None:
            user.target = target_update
            rebuild = True
        if lamps_update is not None:
            await self._assign_lamps(user, lamps_update)
            rebuild = True
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
        policy_changed = enabled_update is not None or priority_update is not None
        if enabled_update is not None:
            user.enabled = enabled_update
        if priority_update is not None:
            user.stream_priority = priority_update
        if rebuild:
            await self._rebuild_lights(user)
        self._store.save()
        await self._notify_engine(username, rebuild=rebuild, policy_changed=policy_changed)
        owners = self._engine.stream_owners if self._engine is not None else frozenset()
        return web.json_response(self._tv_dict(user, owners))

    async def _handle_delete_tv(self, request: web.Request) -> web.StreamResponse:
        """Remove a paired TV (stopping its stream if it is streaming)."""
        username = request.match_info["username"]
        config = self._store.config
        if not any(user.username == username for user in config.users):
            return web.json_response({"error": "Unknown TV."}, status=404)
        config.users = [user for user in config.users if user.username != username]
        if self._engine is not None:
            await self._engine.remove_tv(username)
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

    async def _handle_zigbee_lights(self, _request: web.Request) -> web.StreamResponse:
        """List the Hue color lights in ZHA a TV can stream to directly, or why there are none."""
        try:
            url = await self._home_assistant.ziggurat_url()
            lights = await self._home_assistant.hue_lights() if url else []
        except HomeAssistantError as err:
            return web.json_response({"available": False, "reason": str(err), "lights": []})
        if url is None:
            reason = "ZHA in Home Assistant does not run on a Ziggurat radio"
            return web.json_response({"available": False, "reason": reason, "lights": []})
        return web.json_response(
            {
                "available": True,
                "lights": [
                    {
                        "ieee": light.ieee,
                        "name": light.name,
                        "area": light.area,
                        "model": light.model,
                    }
                    for light in lights
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

    async def _handle_home_assistant(self, _request: web.Request) -> web.StreamResponse:
        """Return the Home Assistant connection settings (never the token)."""
        return web.json_response(self._home_assistant_dict())

    async def _handle_update_home_assistant(self, request: web.Request) -> web.StreamResponse:
        """Set the manual Home Assistant URL and/or token; an empty URL disconnects."""
        body = await _read_json(request)
        settings = self._store.config.home_assistant
        if "url" in body:
            url = str(body["url"] or "").strip()
            if url and not url.startswith(("http://", "https://", "ws://", "wss://")):
                return web.json_response(
                    {"error": "The URL must start with http://, https://, ws:// or wss://."},
                    status=400,
                )
            if url != settings.url:
                # The saved token only ever goes to the URL it was entered for.
                settings.token = ""
            settings.url = url
        token = body.get("token")
        if isinstance(token, str) and token.strip():
            settings.token = token.strip()
        if not settings.url:
            settings.token = ""
        self._store.save()
        return web.json_response(self._home_assistant_dict())

    @web.middleware
    async def _home_assistant_only(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        """Pass the web UI and /cfg requests from Home Assistant on; answer others with a 403."""
        ui_request = request.match_info.route.resource in self._resources
        if ui_request and not _from_home_assistant(request):
            return web.Response(status=403, text=_ADDON_ONLY_TEXT)
        return await handler(request)

    def _tv_dict(self, user: PairedUser, owners: frozenset[str]) -> dict[str, Any]:
        """Build the JSON representation of a paired TV (with its assignment) for the UI."""
        return {
            "username": user.username,
            "devicetype": user.devicetype,
            "created": user.created,
            "streaming": user.username in owners,
            "enabled": user.enabled,
            "stream_priority": user.stream_priority,
            "target": user.target.value,
            "entertainment_area": user.entertainment_area,
            "split_gradients": user.split_gradients,
            "zigbee_lights": [
                {"ieee": lamp.ieee, "name": lamp.name, "zones": len(channels)}
                for lamp, channels in zigbee_channels(
                    user.zigbee_lights, split_gradients=user.split_gradients
                )
            ],
            "stream_smoothing": user.stream_smoothing if user.stream_smoothing is not None else 0.0,
            "lights": [light.name for light in user.lights],
        }

    async def _notify_engine(self, username: str, *, rebuild: bool, policy_changed: bool) -> None:
        """Tell the engine a TV's settings changed; new lights restart its active stream."""
        if self._engine is None:
            return
        if rebuild:
            await self._engine.restart_stream(username)
        if policy_changed or rebuild:
            self._engine.tv_settings_changed(username)

    async def _rebuild_lights(self, user: PairedUser) -> None:
        """Rebuild the virtual lights a TV sees from its selected lamps or assigned area."""
        if user.target is StreamTarget.ZIGBEE:
            user.lights = lights_from_zigbee(
                user.zigbee_lights, split_gradients=user.split_gradients
            )
        elif user.entertainment_area:
            area = await self._area_by_id(user.entertainment_area)
            user.lights = (
                lights_from_area(area, split_gradients=user.split_gradients) if area else []
            )
        else:
            user.lights = []

    async def _assign_lamps(self, user: PairedUser, lamps: list[ZigbeeLightRef]) -> None:
        """
        Give a TV a new lamp selection, reading the gradient segments of newly added lamps.

        :param user: The TV.
        :param lamps: Its new lamp selection.
        """
        # Reading new lamps waits on the network; a quick follow-up edit must not be overtaken
        # by an earlier one.
        async with self._lamps_lock:
            await self._set_lamp_segments(lamps, user.zigbee_lights)
            user.zigbee_lights = lamps

    async def _set_lamp_segments(
        self, lamps: list[ZigbeeLightRef], previous: list[ZigbeeLightRef]
    ) -> None:
        """
        Set the lamps' gradient segment counts: kept for lamps in ``previous``, read for new ones.

        A new lamp whose count cannot be read keeps 1 segment; re-adding it reads it again.

        :param lamps: A TV's new lamp selection.
        :param previous: The TV's lamp selection before this update.
        """
        stored = {lamp.ieee: lamp.segments for lamp in previous}
        added: list[ZigbeeLightRef] = []
        for lamp in lamps:
            if lamp.ieee in stored:
                lamp.segments = stored[lamp.ieee]
            else:
                added.append(lamp)
        if not added:
            return
        try:
            url, addresses = await self._home_assistant.stream_info()
            nwks = {lamp.ieee: addresses[lamp.ieee] for lamp in added if lamp.ieee in addresses}
            counts = await read_segment_counts(url, nwks.values()) if url and nwks else {}
        except (HomeAssistantError, ZigguratError) as err:
            LOGGER.warning(
                "Could not read whether the new Zigbee lamps are gradients (%s); re-add them to "
                "try again",
                err,
            )
            return
        for lamp in added:
            nwk = nwks.get(lamp.ieee)
            if nwk is None or nwk not in counts:
                LOGGER.warning(
                    "Zigbee lamp %s (%s) did not report whether it is a gradient; re-add it to "
                    "try again",
                    lamp.name,
                    lamp.ieee,
                )
                continue
            lamp.segments = counts[nwk]

    async def _area_by_id(self, area_id: str) -> Any:
        """Return the active bridge's entertainment area with the given id (live or cached)."""
        return next((area for area in await self._list_areas() if area.id == area_id), None)

    def _home_assistant_dict(self) -> dict[str, Any]:
        """Build the JSON representation of the Home Assistant connection (without the token)."""
        settings = self._store.config.home_assistant
        return {
            "addon": self._home_assistant.is_addon,
            "available": self._home_assistant.available,
            "url": settings.url,
            "token_set": bool(settings.token),
        }

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

    def _tv_by_username(self, username: str) -> PairedUser | None:
        """Return a paired TV by username, or None."""
        for user in self._store.config.users:
            if user.username == username:
                return user
        return None


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Read and parse a JSON request body, returning an empty dict on absence/parse error."""
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _from_home_assistant(request: web.Request) -> bool:
    """Return True for a request from the Supervisor (ingress) or from this host."""
    try:
        peer = ip_address(request.remote or "")
    except ValueError:
        return False
    return peer == _SUPERVISOR_IP or peer.is_loopback


def _parse_zigbee_lights(value: Any) -> list[ZigbeeLightRef] | None:
    """Parse a TV's selected lamps from a request body; None when malformed or too many."""
    if not isinstance(value, list):
        return None
    lamps: dict[str, ZigbeeLightRef] = {}
    for item in value:
        ieee = item.get("ieee") if isinstance(item, dict) else None
        if not isinstance(ieee, str) or not ieee.strip():
            return None
        ieee = ieee.strip().lower()
        lamps.setdefault(ieee, ZigbeeLightRef(ieee=ieee, name=str(item.get("name") or "")))
    return list(lamps.values()) if len(lamps) <= MAX_ZIGBEE_LIGHTS else None


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
