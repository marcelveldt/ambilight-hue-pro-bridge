"""Legacy Hue v1 REST API emulation served to the TV over HTTP."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
from aiohttp import web

from ambilight_hue_bridge.const import (
    BRIDGE_API_VERSION,
    BRIDGE_DATASTORE_VERSION,
    BRIDGE_MODEL_ID,
    BRIDGE_SW_VERSION,
)
from ambilight_hue_bridge.discovery.description import build_description_xml
from ambilight_hue_bridge.identity import bridge_id, mac_with_colons

from .light_repr import build_v1_light, default_light_state

if TYPE_CHECKING:
    from aiohttp.typedefs import Handler

    from ambilight_hue_bridge.config.store import ConfigStore

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


@web.middleware
async def _log_requests(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Log every inbound request (method, path, body) — doubles as the TV-capture trace."""
    body = ""
    if request.can_read_body:
        with suppress(Exception):
            body = await request.text()
    LOGGER.info("TV %s %s%s", request.method, request.path_qs, f"  body={body}" if body else "")
    return await handler(request)


class HueV1Emulator:
    """Serves the legacy Hue v1 REST API and the UPnP descriptor to the TV."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        pairing: PairingManager,
        host_ip: str,
        mac: str,
    ) -> None:
        """
        Initialize the emulator.

        :param store: Config store providing the virtual bridge and lights.
        :param pairing: Pairing manager for username/clientkey handling.
        :param host_ip: LAN IP address advertised in the UPnP descriptor.
        :param mac: Resolved host MAC used to derive the bridge id and UDN.
        """
        self._store = store
        self._pairing = pairing
        self._host_ip = host_ip
        self._mac = mac
        self._states: dict[str, dict[str, Any]] = {
            light.id: default_light_state() for light in store.config.virtual_lights
        }
        self._groups: dict[str, dict[str, Any]] = {}
        self._next_group_id = 1

    def create_app(self) -> web.Application:
        """Build the aiohttp application with the v1 API and descriptor routes."""
        app = web.Application(middlewares=[_log_requests])
        app.add_routes(
            [
                web.get("/description.xml", self._handle_description),
                web.post("/api", self._handle_create_user),
                web.post("/api/", self._handle_create_user),
                web.get("/api/config", self._handle_public_config),
                web.get("/api/{user}", self._handle_datastore),
                web.get("/api/{user}/config", self._handle_config),
                web.get("/api/{user}/lights", self._handle_lights),
                web.get("/api/{user}/lights/{light_id}", self._handle_light),
                web.put("/api/{user}/lights/{light_id}/state", self._handle_light_state),
                web.get("/api/{user}/groups", self._handle_groups),
                web.post("/api/{user}/groups", self._handle_create_group),
                web.get("/api/{user}/groups/{group_id}", self._handle_group),
                web.put("/api/{user}/groups/{group_id}", self._handle_group_put),
                web.put("/api/{user}/groups/{group_id}/action", self._handle_group_action),
                web.get("/api/{user}/capabilities", self._handle_capabilities),
                web.get("/api/{user}/scenes", self._handle_empty),
                web.get("/api/{user}/schedules", self._handle_empty),
                web.get("/api/{user}/sensors", self._handle_empty),
                web.get("/api/{user}/rules", self._handle_empty),
                web.get("/api/{user}/resourcelinks", self._handle_empty),
            ]
        )
        return app

    async def _handle_description(self, _request: web.Request) -> web.StreamResponse:
        """Serve the UPnP descriptor."""
        xml = build_description_xml(
            name=self._store.config.virtual_bridge.name,
            mac=self._mac,
            host=self._host_ip,
            port=self._store.config.virtual_bridge.http_port,
        )
        return web.Response(text=xml, content_type="application/xml")

    async def _handle_create_user(self, request: web.Request) -> web.StreamResponse:
        """Handle pushlink pairing (POST /api)."""
        body = await _read_json(request)
        devicetype = str(body.get("devicetype", "unknown"))
        generate_clientkey = bool(body.get("generateclientkey", False))
        # TODO(M4): enforce a link-button window instead of always accepting.
        user = self._pairing.create_user(devicetype, generate_clientkey=generate_clientkey)
        success: dict[str, str] = {"username": user.username}
        if generate_clientkey:
            success["clientkey"] = user.clientkey
        return _json([{"success": success}])

    async def _handle_public_config(self, _request: web.Request) -> web.StreamResponse:
        """Serve the unauthenticated short config (GET /api/config)."""
        return _json(self._short_config())

    async def _handle_datastore(self, request: web.Request) -> web.StreamResponse:
        """Serve the full datastore (GET /api/{user})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json(
            {
                "lights": self._lights_dict(),
                "groups": self._groups_dict(),
                "config": self._full_config(),
                "schedules": {},
                "scenes": {},
                "sensors": {},
                "rules": {},
                "resourcelinks": {},
            },
        )

    async def _handle_config(self, request: web.Request) -> web.StreamResponse:
        """Serve the authenticated full config (GET /api/{user}/config)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json(self._full_config())

    async def _handle_lights(self, request: web.Request) -> web.StreamResponse:
        """List all exposed lights (GET /api/{user}/lights)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json(self._lights_dict())

    async def _handle_light(self, request: web.Request) -> web.StreamResponse:
        """Return a single light (GET /api/{user}/lights/{light_id})."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        light_id = request.match_info["light_id"]
        light = self._light_by_id(light_id)
        if light is None:
            return _error(3, f"/lights/{light_id}", "resource, /lights/{light_id}, not available")
        state = self._states.setdefault(light_id, default_light_state())
        return _json(build_v1_light(light, state))

    async def _handle_light_state(self, request: web.Request) -> web.StreamResponse:
        """Apply a light state change (PUT /api/{user}/lights/{light_id}/state)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        light_id = request.match_info["light_id"]
        if light_id not in self._states:
            return _error(3, f"/lights/{light_id}", "resource, /lights/{light_id}, not available")
        body = await _read_json(request)
        result: list[dict[str, Any]] = []
        for key, value in body.items():
            self._states[light_id][key] = value
            result.append({"success": {f"/lights/{light_id}/state/{key}": value}})
        # TODO(M3): forward the resulting color to the outbound Entertainment stream.
        return _json(result)

    async def _handle_groups(self, request: web.Request) -> web.StreamResponse:
        """List groups (GET /api/{user}/groups)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        return _json(self._groups_dict())

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
        group_id = request.match_info["group_id"]
        group = self._group_by_id(group_id)
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
        if "stream" in body:
            active = bool(body["stream"].get("active", False))
            group = self._groups.get(group_id)
            if group is not None and "stream" in group:
                group["stream"]["active"] = active
            LOGGER.info(
                "TV %s entertainment stream for group %s",
                "STARTED" if active else "STOPPED",
                group_id,
            )
            # TODO(M5): start/stop the inbound DTLS server on activation.
            result.append({"success": {f"/groups/{group_id}/stream/active": active}})
        for key in ("name", "lights", "class", "locations"):
            if key in body:
                if group_id in self._groups:
                    self._groups[group_id][key] = body[key]
                result.append({"success": {f"/groups/{group_id}/{key}": body[key]}})
        return _json(result or [{"success": {}}])

    async def _handle_group_action(self, request: web.Request) -> web.StreamResponse:
        """Apply a group action (PUT /api/{user}/groups/{group_id}/action)."""
        if (unauthorized := self._unauthorized(request)) is not None:
            return unauthorized
        group_id = request.match_info["group_id"]
        body = await _read_json(request)
        # TODO(M3): forward the resulting color to the outbound Entertainment stream.
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

    def _light_ids(self) -> list[str]:
        """Return the ids of all exposed virtual lights."""
        return [light.id for light in self._store.config.virtual_lights]

    def _light_by_id(self, light_id: str) -> Any:
        """Return the VirtualLight with the given id, or None."""
        for light in self._store.config.virtual_lights:
            if light.id == light_id:
                return light
        return None

    def _lights_dict(self) -> dict[str, Any]:
        """Build the ``{id: light}`` mapping of all exposed lights."""
        result: dict[str, Any] = {}
        for light in self._store.config.virtual_lights:
            if light.id not in self._states:
                self._states[light.id] = default_light_state()
            result[light.id] = build_v1_light(light, self._states[light.id])
        return result

    def _groups_dict(self) -> dict[str, Any]:
        """Build the ``{id: group}`` mapping, including the implicit group 0."""
        return {"0": self._group_zero(), **self._groups}

    def _group_by_id(self, group_id: str) -> dict[str, Any] | None:
        """Return a group by id (including group 0), or None."""
        if group_id == "0":
            return self._group_zero()
        return self._groups.get(group_id)

    def _group_zero(self) -> dict[str, Any]:
        """Build the implicit 'all lights' group 0."""
        return {
            "name": "Group 0",
            "lights": self._light_ids(),
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
