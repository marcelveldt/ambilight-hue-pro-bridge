"""Legacy Hue v1 REST API emulation served to the TV over HTTP."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
from aiohttp import web

from ambilight_hue_bridge.color import state_to_rgb16
from ambilight_hue_bridge.const import (
    BRIDGE_API_VERSION,
    BRIDGE_DATASTORE_VERSION,
    BRIDGE_MODEL_ID,
    BRIDGE_SW_VERSION,
    VERBOSE,
)
from ambilight_hue_bridge.discovery.description import build_description_xml
from ambilight_hue_bridge.identity import bridge_id, mac_with_colons
from ambilight_hue_bridge.outbound.controller import tv_lights

from .light_repr import build_v1_light, default_light_state

if TYPE_CHECKING:
    from aiohttp.typedefs import Handler

    from ambilight_hue_bridge.config.models import VirtualLight
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.engine.engine import Engine

    from .pairing import PairingManager

LOGGER = logging.getLogger(__name__)


def _json(data: Any, status: int = 200) -> web.Response:
    """Return an aiohttp JSON response serialized with orjson."""
    return web.Response(body=orjson.dumps(data), status=status, content_type="application/json")


def _error(error_type: int, address: str, description: str) -> web.Response:
    """Return a Hue-style error response (HTTP 200 with an error list)."""
    return _json([{"error": {"type": error_type, "address": address, "description": description}}])


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Read and parse a JSON request body, returning an empty dict on absence/parse error."""
    # Note: the logging middleware reads the body first; aiohttp caches it, so re-reading
    # here works, but ``can_read_body`` would already be false — hence we just try to parse.
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _now() -> str:
    """Return the current UTC time as a Hue-style timestamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _coerce_light_state(state: dict[str, Any], body: dict[str, Any]) -> None:
    """
    Merge a client-supplied state body into ``state``, coercing the numeric/xy fields.

    The TV is untrusted input on the hot path; coercing here keeps a malformed value (e.g.
    ``{"bri": "x"}`` or a short ``xy``) from poisoning the stored state, the color math, or a
    later ``GET /lights/<id>``.
    """
    for key, value in body.items():
        if key in ("bri", "hue", "sat", "ct"):
            state[key] = _as_int(value, state.get(key, 0))
        elif key == "xy":
            state[key] = _as_xy(value, state.get(key))
        elif key == "on":
            state[key] = bool(value)
        else:
            state[key] = value


def _as_int(value: Any, default: Any) -> int:
    """Coerce a client-supplied value to int, falling back to default on bad input."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _as_xy(value: Any, default: Any) -> list[float]:
    """Coerce a client-supplied value to a 2-element [x, y] float list, else the default."""
    for candidate in (value, default):
        if isinstance(candidate, (list, tuple)) and len(candidate) == 2:
            try:
                return [float(candidate[0]), float(candidate[1])]
            except (TypeError, ValueError):
                continue
    return [0.0, 0.0]


def _log_request(level: int, request: web.Request, status: int, suffix: str) -> None:
    """Emit one request-trace line: peer, scheme (http/https), method, path, status, body."""
    LOGGER.log(
        level,
        "%s %s %s %s -> %d%s",
        request.remote,
        request.scheme,
        request.method,
        request.path_qs,
        status,
        suffix,
    )


@web.middleware
async def log_requests(request: web.Request, handler: Handler) -> web.StreamResponse:
    """
    Log every inbound HTTP request with its peer, scheme, method, path, status and body.

    This doubles as the TV-capture trace: it surfaces unrouted probes (which would
    otherwise 404 silently), the scheme (so we see whether the TV uses HTTP or HTTPS) and
    the status it received. The TV's requests log at DEBUG; the web UI's own ``/`` and ``/cfg``
    polling is demoted further to VERBOSE so DEBUG stays useful.
    """
    body = ""
    if request.can_read_body:
        # aiohttp caches the body, so handlers re-reading via request.json() still work.
        with suppress(Exception):
            body = await request.text()
    level = VERBOSE if request.path == "/" or request.path.startswith("/cfg") else logging.DEBUG
    suffix = f"  body={body}" if body else ""
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _log_request(level, request, exc.status, suffix)
        raise
    _log_request(level, request, response.status, suffix)
    return response


class HueV1Emulator:
    """Serves the legacy Hue v1 REST API and the UPnP descriptor to the TV."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        pairing: PairingManager,
        host_ip: str,
        mac: str,
        http_port: int,
        https_port: int = 0,
        engine: Engine | None = None,
    ) -> None:
        """
        Initialize the emulator.

        :param store: Config store providing the virtual bridge and lights.
        :param pairing: Pairing manager for username/clientkey handling.
        :param host_ip: LAN IP address advertised in the UPnP descriptor.
        :param mac: Resolved host MAC used to derive the bridge id and UDN.
        :param http_port: TCP port the API/descriptor are served on (for the descriptor URL).
        :param https_port: TLS port advertised in the N-UPnP response (0 => fall back to HTTP).
        :param engine: Optional engine that streams color updates to the real bridge.
        """
        self._store = store
        self._pairing = pairing
        self._host_ip = host_ip
        self._mac = mac
        self._http_port = http_port
        self._https_port = https_port
        self._engine = engine
        # Per-TV light state, keyed by "<username>/<light_id>" (two TVs may use the same ids).
        self._states: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, dict[str, Any]] = {}
        self._next_group_id = 1

    def register(self, app: web.Application) -> None:
        """Register the TV-facing Hue v1 API and UPnP descriptor routes on the app."""
        app.add_routes(
            [
                web.get("/description.xml", self._handle_description),
                web.post("/api", self._handle_create_user),
                web.post("/api/", self._handle_create_user),
                web.get("/api/config", self._handle_public_config),
                web.get("/api/nupnp", self._handle_nupnp),
                web.get("/nupnp", self._handle_nupnp),
                web.get("/api/{user}", self._handle_datastore),
                web.get("/api/{user}/config", self._handle_config),
                web.get("/api/{user}/lights", self._handle_lights),
                web.get("/api/{user}/lights/{light_id}", self._handle_light),
                web.put("/api/{user}/lights/{light_id}/state", self._handle_light_state),
                web.get("/api/{user}/groups", self._handle_groups),
                web.post("/api/{user}/groups", self._handle_create_group),
                # Newer TVs (e.g. OLED807) POST to the collection with a trailing slash; without
                # this the create 404s and the TV falls back to an unusable group id of "null".
                web.post("/api/{user}/groups/", self._handle_create_group),
                web.get("/api/{user}/groups/{group_id}", self._handle_group),
                web.put("/api/{user}/groups/{group_id}", self._handle_group_put),
                web.delete("/api/{user}/groups/{group_id}", self._handle_delete_group),
                web.put("/api/{user}/groups/{group_id}/action", self._handle_group_action),
                web.get("/api/{user}/capabilities", self._handle_capabilities),
                web.get("/api/{user}/scenes", self._handle_empty),
                web.get("/api/{user}/schedules", self._handle_empty),
                web.get("/api/{user}/sensors", self._handle_empty),
                web.get("/api/{user}/rules", self._handle_empty),
                web.get("/api/{user}/resourcelinks", self._handle_empty),
            ]
        )

    async def _handle_description(self, _request: web.Request) -> web.StreamResponse:
        """Serve the UPnP descriptor."""
        xml = build_description_xml(
            name=self._store.config.virtual_bridge.name,
            mac=self._mac,
            host=self._host_ip,
            port=self._http_port,
        )
        return web.Response(text=xml, content_type="application/xml")

    async def _handle_create_user(self, request: web.Request) -> web.StreamResponse:
        """Handle pushlink pairing (POST /api)."""
        body = await _read_json(request)
        # A real bridge requires the devicetype parameter; reject probes that omit it so we
        # don't mint junk whitelist entries. We otherwise always accept (no physical link
        # button on a software bridge), which is what lets the TV pair unattended.
        if "devicetype" not in body:
            return _error(6, "/api/", "parameter, devicetype, not available")
        devicetype = str(body["devicetype"])
        generate_clientkey = bool(body.get("generateclientkey", False))
        user = self._pairing.create_user(devicetype, generate_clientkey=generate_clientkey)
        success: dict[str, str] = {"username": user.username}
        if generate_clientkey:
            success["clientkey"] = user.clientkey
        return _json([{"success": success}])

    async def _handle_public_config(self, _request: web.Request) -> web.StreamResponse:
        """Serve the unauthenticated short config (GET /api/config)."""
        return _json(self._short_config())

    async def _handle_nupnp(self, _request: web.Request) -> web.StreamResponse:
        """
        Serve the local N-UPnP discovery JSON (GET /api/nupnp and /nupnp).

        Mirrors discovery.meethue.com's response shape for N-UPnP-aware LAN clients (e.g.
        aiohue). It does NOT help the Ambilight TVs - they discover via SSDP and never query a
        nupnp endpoint - it is provided for completeness with other Hue clients on the LAN.
        """
        return _json(
            [
                {
                    "id": bridge_id(self._mac).lower(),
                    "internalipaddress": self._host_ip,
                    "port": self._https_port or self._http_port,
                },
            ],
        )

    async def _handle_datastore(self, request: web.Request) -> web.StreamResponse:
        """Serve the full datastore (GET /api/{user})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        lights = tv_lights(self._store, user)
        return _json(
            {
                "lights": self._lights_dict(user, lights),
                "groups": self._groups_dict(lights),
                "config": self._full_config(),
                "schedules": {},
                "scenes": {},
                "sensors": {},
                "rules": {},
                "resourcelinks": {},
            },
        )

    async def _handle_config(self, request: web.Request) -> web.StreamResponse:
        """
        Serve the bridge config (GET /api/{user}/config).

        A real bridge returns the FULL config for a whitelisted user and the reduced/public
        config for any other username - it never returns an unauthorized error here. Clients
        (incl. Philips TVs doing an IP-scan/identity probe) rely on this to recognize the
        device as a Hue bridge and to check whether their username is whitelisted.
        """
        user = request.match_info.get("user", "")
        if self._pairing.is_known_user(user):
            return _json(self._full_config())
        return _json(self._short_config())

    async def _handle_lights(self, request: web.Request) -> web.StreamResponse:
        """List the lights exposed to this TV (GET /api/{user}/lights)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        return _json(self._lights_dict(user, tv_lights(self._store, user)))

    async def _handle_light(self, request: web.Request) -> web.StreamResponse:
        """Return a single light (GET /api/{user}/lights/{light_id})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        light_id = request.match_info["light_id"]
        light = self._light_by_id(tv_lights(self._store, user), light_id)
        if light is None:
            return _error(3, f"/lights/{light_id}", "resource, /lights/{light_id}, not available")
        return _json(build_v1_light(light, self._state(user, light_id)))

    async def _handle_light_state(self, request: web.Request) -> web.StreamResponse:
        """Apply a light state change (PUT /api/{user}/lights/{light_id}/state)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        light_id = request.match_info["light_id"]
        if self._light_by_id(tv_lights(self._store, user), light_id) is None:
            return _error(3, f"/lights/{light_id}", "resource, /lights/{light_id}, not available")
        body = await _read_json(request)
        state = self._state(user, light_id)
        result = [
            {"success": {f"/lights/{light_id}/state/{key}": value}} for key, value in body.items()
        ]
        _coerce_light_state(state, body)
        # The TV sends alert=select/lselect to say "blink this light" while assigning zones; flash
        # it over the stream so the user can locate it. alert=none cancels.
        alert = body.get("alert")
        if self._engine is not None:
            if alert in ("select", "lselect"):
                self._engine.identify(user, light_id, sustained=alert == "lselect")
            elif alert == "none":
                self._engine.stop_identify(light_id)
        self._submit_color(user, light_id)
        return _json(result)

    async def _handle_groups(self, request: web.Request) -> web.StreamResponse:
        """List groups (GET /api/{user}/groups)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        return _json(self._groups_dict(tv_lights(self._store, user)))

    async def _handle_create_group(self, request: web.Request) -> web.StreamResponse:
        """Create a (possibly entertainment) group (POST /api/{user}/groups)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        body = await _read_json(request)
        group_id = str(self._next_group_id)
        self._next_group_id += 1
        group = self._build_group(body)
        self._groups[group_id] = group
        LOGGER.info("TV created group %s (type=%s)", group_id, group.get("type"))
        return _json([{"success": {"id": group_id}}])

    async def _handle_group(self, request: web.Request) -> web.StreamResponse:
        """Return a single group (GET /api/{user}/groups/{group_id})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        group_id = request.match_info["group_id"]
        group = self._group_by_id(group_id, tv_lights(self._store, user))
        if group is None:
            return _error(3, f"/groups/{group_id}", "resource, /groups/{group_id}, not available")
        return _json(group)

    async def _handle_group_put(self, request: web.Request) -> web.StreamResponse:
        """Update a group, incl. entertainment stream activation (PUT /groups/{group_id})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        group_id = request.match_info["group_id"]
        body = await _read_json(request)
        result: list[dict[str, Any]] = []
        stream = body.get("stream")
        if isinstance(stream, dict):
            active = bool(stream.get("active", False))
            user = request.match_info.get("user", "")
            owner = self._engine.stream_owner if self._engine is not None else None
            busy = self._engine is not None and self._engine.is_streaming
            # Single-stream guard: deny a second TV while another is actively streaming.
            if active and busy and owner not in (None, user):
                LOGGER.warning(
                    "Denying entertainment stream for %s: %s is already streaming", user, owner
                )
                return _error(
                    307,
                    f"/groups/{group_id}/stream",
                    "another entertainment stream is already active",
                )
            # Track the group the TV streams to even if it never POST-created it, so it stays
            # resolvable across GETs/restarts and is visible in the web UI.
            group = self._ensure_entertainment_group(group_id, tv_lights(self._store, user))
            group["stream"]["active"] = active
            # The TV re-reads the group to confirm it owns the stream before it begins the
            # DTLS HueStream; with owner left null it may refuse to start or tear down.
            group["stream"]["owner"] = user if active else None
            if self._engine is not None and active:
                # Open the outbound stream now so the DTLS handshake to the real bridge is done
                # before the TV's first frames arrive (the inbound server is always on). We do
                # NOT tear it down on active=false: the TV rapidly toggles the stream while in
                # its configure menu, and a full reconnect (~4 s) per toggle thrashes the real
                # bridge. Instead the engine keeps the stream warm and idle-times-out on its own
                # once the TV stops sending frames.
                self._engine.start_stream(user)
            LOGGER.info(
                "TV %s entertainment stream for group %s",
                "started" if active else "stopped",
                group_id,
            )
            result.append({"success": {f"/groups/{group_id}/stream/active": active}})
        for key in ("name", "lights", "class", "locations"):
            if key in body:
                if group_id in self._groups:
                    self._groups[group_id][key] = body[key]
                result.append({"success": {f"/groups/{group_id}/{key}": body[key]}})
        return _json(result or [{"success": {}}])

    async def _handle_delete_group(self, request: web.Request) -> web.StreamResponse:
        """Delete a group (DELETE /api/{user}/groups/{group_id}); TVs do this to re-configure."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        group_id = request.match_info["group_id"]
        removed = self._groups.pop(group_id, None)
        # If the TV was streaming the group it just deleted, stop the outbound stream.
        if removed is not None and self._engine is not None and self._engine.is_streaming:
            await self._engine.stop_stream()
        return _json([{"success": f"/groups/{group_id} deleted"}])

    async def _handle_group_action(self, request: web.Request) -> web.StreamResponse:
        """Apply a group action (PUT /api/{user}/groups/{group_id}/action)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        user = request.match_info.get("user", "")
        group_id = request.match_info["group_id"]
        body = await _read_json(request)
        for light_id in self._group_light_ids(group_id, tv_lights(self._store, user)):
            _coerce_light_state(self._state(user, light_id), body)
            self._submit_color(user, light_id)
        return _json(
            [
                {"success": {f"/groups/{group_id}/action/{key}": value}}
                for key, value in body.items()
            ],
        )

    async def _handle_capabilities(self, request: web.Request) -> web.StreamResponse:
        """Return bridge capabilities, including entertainment streaming (GET .../capabilities)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json(self._capabilities())

    async def _handle_empty(self, request: web.Request) -> web.StreamResponse:
        """Return an empty resource collection (scenes/schedules/sensors/rules/resourcelinks)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json({})

    def _unauthorized(self, request: web.Request) -> web.Response | None:
        """Return an unauthorized error if the request's username is not paired."""
        user = request.match_info.get("user", "")
        if not self._pairing.is_known_user(user):
            return _error(1, "/", "unauthorized user")
        return None

    def _state(self, user: str, light_id: str) -> dict[str, Any]:
        """Return the per-TV light state, creating a default on first access."""
        return self._states.setdefault(f"{user}/{light_id}", default_light_state())

    def _group_light_ids(self, group_id: str, lights: list[VirtualLight]) -> list[str]:
        """Return the light ids belonging to a group (group 0 = the TV's lights)."""
        if group_id == "0":
            return [light.id for light in lights]
        group = self._groups.get(group_id)
        if group is None:
            return []
        return [str(light_id) for light_id in group.get("lights", [])]

    def _submit_color(self, user: str, light_id: str) -> None:
        """Push a light's current color to the outbound stream, if an engine is wired."""
        if self._engine is not None:
            self._engine.submit_color(user, light_id, state_to_rgb16(self._state(user, light_id)))

    def _light_by_id(self, lights: list[VirtualLight], light_id: str) -> Any:
        """Return the VirtualLight with the given id from a light list, or None."""
        for light in lights:
            if light.id == light_id:
                return light
        return None

    def _ensure_entertainment_group(
        self, group_id: str, lights: list[VirtualLight]
    ) -> dict[str, Any]:
        """Return the group for an id, lazily creating an Entertainment group if unknown."""
        group = self._groups.get(group_id)
        if group is None or group.get("type") != "Entertainment" or "stream" not in group:
            group = self._build_group(
                {
                    "type": "Entertainment",
                    "name": f"Entertainment {group_id}",
                    "lights": [light.id for light in lights],
                },
            )
            self._groups[group_id] = group
        return group

    def _lights_dict(self, user: str, lights: list[VirtualLight]) -> dict[str, Any]:
        """Build the ``{id: light}`` mapping of the lights exposed to this TV."""
        return {light.id: build_v1_light(light, self._state(user, light.id)) for light in lights}

    def _groups_dict(self, lights: list[VirtualLight]) -> dict[str, Any]:
        """Build the ``{id: group}`` mapping, including the implicit group 0."""
        return {"0": self._group_zero(lights), **self._groups}

    def _group_by_id(self, group_id: str, lights: list[VirtualLight]) -> dict[str, Any] | None:
        """Return a group by id (including group 0), or None."""
        if group_id == "0":
            return self._group_zero(lights)
        return self._groups.get(group_id)

    def _group_zero(self, lights: list[VirtualLight]) -> dict[str, Any]:
        """Build the implicit 'all lights' group 0."""
        return {
            "name": "Group 0",
            "lights": [light.id for light in lights],
            "sensors": [],
            "type": "LightGroup",
            "state": {"all_on": True, "any_on": True},
            "recycle": False,
            "action": _default_action(),
        }

    def _build_group(self, body: dict[str, Any]) -> dict[str, Any]:
        """Build a new group dict from a create request body."""
        group: dict[str, Any] = {
            "name": str(body.get("name", "Group")),
            "lights": [str(light_id) for light_id in body.get("lights", [])],
            "sensors": [],
            "type": str(body.get("type", "LightGroup")),
            "state": {"all_on": False, "any_on": False},
            "recycle": bool(body.get("recycle", False)),
            "action": _default_action(),
        }
        if group["type"] == "Entertainment":
            group["class"] = str(body.get("class", "TV"))
            group["stream"] = {
                "proxymode": "auto",
                "proxynode": "/bridge",
                "active": False,
                "owner": None,
            }
            group["locations"] = body.get("locations", {})
        return group

    def _short_config(self) -> dict[str, Any]:
        """Build the unauthenticated short config payload."""
        return {
            "name": self._store.config.virtual_bridge.name,
            "datastoreversion": BRIDGE_DATASTORE_VERSION,
            "swversion": BRIDGE_SW_VERSION,
            "apiversion": BRIDGE_API_VERSION,
            "mac": mac_with_colons(self._mac),
            "bridgeid": bridge_id(self._mac),
            "factorynew": False,
            "replacesbridgeid": None,
            "modelid": BRIDGE_MODEL_ID,
            "starterkitid": "",
        }

    def _full_config(self) -> dict[str, Any]:
        """Build the authenticated full config payload."""
        config = self._short_config()
        config.update(
            {
                "ipaddress": self._host_ip,
                "netmask": "255.255.255.0",
                "gateway": self._host_ip,
                "proxyaddress": "none",
                "proxyport": 0,
                "UTC": _now(),
                "localtime": _now(),
                "timezone": "Etc/UTC",
                "dhcp": True,
                "linkbutton": False,
                "portalservices": False,
                "portalconnection": "disconnected",
                "zigbeechannel": 25,
                "whitelist": {
                    user.username: {
                        "name": user.devicetype,
                        "create date": user.created,
                        "last use date": user.created,
                    }
                    for user in self._store.config.users
                },
                "swupdate2": {
                    "bridge": {"state": "noupdates", "lastinstall": "2021-01-01T00:00:00"},
                    "state": "noupdates",
                    "checkforupdate": False,
                    "autoinstall": {"on": False, "updatetime": "T14:00:00"},
                },
            },
        )
        return config

    def _capabilities(self) -> dict[str, Any]:
        """Build the capabilities payload (advertising entertainment streaming)."""
        return {
            "lights": {"available": 60, "total": 63},
            "sensors": {"available": 240, "total": 250},
            "groups": {"available": 60, "total": 64},
            "scenes": {"available": 172, "total": 200},
            "rules": {"available": 233, "total": 250},
            "schedules": {"available": 95, "total": 100},
            "resourcelinks": {"available": 59, "total": 64},
            "streaming": {"available": 1, "total": 1, "channels": 20},
            "timezones": {"values": ["Etc/UTC"]},
        }


def _default_action() -> dict[str, Any]:
    """Return the default group action block."""
    return {
        "on": True,
        "bri": 254,
        "hue": 0,
        "sat": 0,
        "effect": "none",
        "xy": [0.0, 0.0],
        "ct": 366,
        "alert": "none",
        "colormode": "xy",
    }
