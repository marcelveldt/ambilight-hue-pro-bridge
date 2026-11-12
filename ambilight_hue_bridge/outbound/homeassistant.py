"""Home Assistant websocket client: the ZHA lamps and Ziggurat server of the Zigbee target."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import orjson

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.store import ConfigStore

LOGGER = logging.getLogger(__name__)

# Home Assistant's websocket API as proxied by the Supervisor; reachable from add-ons (even
# host_network ones) when the add-on has homeassistant_api access.
_SUPERVISOR_WEBSOCKET_URL = "ws://supervisor/core/websocket"
_TIMEOUT_S = 10.0
_HUE_MANUFACTURERS = ("signify", "philips")
_COLOR_MODES = frozenset({"xy", "hs", "rgb", "rgbw", "rgbww"})
# Only Hue lamps with Hue's entertainment cluster (on endpoint 11) can be streamed to.
_ENTERTAINMENT_ENDPOINT = "11"
_ENTERTAINMENT_CLUSTER = "0xfc01"


class HomeAssistantError(Exception):
    """Home Assistant is unavailable or rejected a request; the message is user-readable."""


@dataclass(frozen=True)
class ZhaLight:
    """A Hue color light paired with Home Assistant's ZHA."""

    ieee: str
    nwk: int
    name: str
    area: str = ""
    model: str = ""


class HomeAssistantClient:
    """Queries Home Assistant over short-lived websocket connections (one per call)."""

    def __init__(self, store: ConfigStore) -> None:
        """
        Initialize the client (no network I/O until a query).

        :param store: Config store providing the manual connection settings.
        """
        self._store = store

    @property
    def is_addon(self) -> bool:
        """Return True when running as a Home Assistant add-on (the connection is automatic)."""
        return bool(os.environ.get("SUPERVISOR_TOKEN"))

    @property
    def available(self) -> bool:
        """Return True when a Home Assistant connection is set up (add-on or configured)."""
        return self._connection() is not None

    async def hue_lights(self) -> list[ZhaLight]:
        """
        Return the Hue color lights in ZHA that support entertainment, sorted by area and name.

        :raises HomeAssistantError: When Home Assistant or ZHA is unavailable.
        """
        devices, states, areas = await self._call(
            "zha/devices", "get_states", "config/area_registry/list"
        )
        color_lights = {
            state.get("entity_id")
            for state in states
            if str(state.get("entity_id", "")).startswith("light.")
            and _COLOR_MODES.intersection(
                (state.get("attributes") or {}).get("supported_color_modes") or ()
            )
        }
        area_names = {area.get("area_id"): area.get("name", "") for area in areas}
        lights: list[ZhaLight] = []
        for device in devices:
            manufacturer = str(device.get("manufacturer") or "").lower()
            if not any(name in manufacturer for name in _HUE_MANUFACTURERS):
                continue
            if not _has_entertainment_cluster(device):
                continue
            if not any(
                entity.get("entity_id") in color_lights for entity in device.get("entities") or ()
            ):
                continue
            nwk = _parse_nwk(device.get("nwk"))
            if nwk is None:
                continue
            ieee = str(device.get("ieee", "")).lower()
            lights.append(
                ZhaLight(
                    ieee=ieee,
                    nwk=nwk,
                    name=device.get("user_given_name") or device.get("name") or ieee,
                    area=area_names.get(device.get("area_id"), ""),
                    model=str(device.get("model") or ""),
                ),
            )
        return sorted(lights, key=lambda light: (light.area.casefold(), light.name.casefold()))

    async def ziggurat_url(self) -> str | None:
        """
        Return the websocket URL of ZHA's Ziggurat server, or None when ZHA uses another radio.

        :raises HomeAssistantError: When Home Assistant or ZHA is unavailable.
        """
        (settings,) = await self._call("zha/network/settings")
        return _ziggurat_path(settings)

    async def stream_info(self) -> tuple[str | None, dict[str, int]]:
        """
        Return ZHA's Ziggurat server URL (None for another radio) and the network address per IEEE.

        :raises HomeAssistantError: When Home Assistant or ZHA is unavailable.
        """
        settings, devices = await self._call("zha/network/settings", "zha/devices")
        addresses: dict[str, int] = {}
        for device in devices:
            nwk = _parse_nwk(device.get("nwk"))
            if nwk is not None:
                addresses[str(device.get("ieee", "")).lower()] = nwk
        return _ziggurat_path(settings), addresses

    def _connection(self) -> tuple[str, str] | None:
        """Return the (websocket URL, token) to connect with, or None when not set up."""
        if token := os.environ.get("SUPERVISOR_TOKEN"):
            return _SUPERVISOR_WEBSOCKET_URL, token
        settings = self._store.config.home_assistant
        if settings.url and settings.token:
            return _websocket_url(settings.url), settings.token
        return None

    async def _call(self, *commands: str) -> list[Any]:
        """
        Run websocket commands on one authenticated connection; return their results in order.

        :param commands: The websocket command types to run, without arguments.
        """
        connection = self._connection()
        if connection is None:
            msg = "Not connected to Home Assistant yet."
            raise HomeAssistantError(msg)
        url, token = connection
        try:
            async with (
                asyncio.timeout(_TIMEOUT_S),
                aiohttp.ClientSession() as session,
                # get_states can be several MB on a large installation.
                session.ws_connect(url, max_msg_size=0) as websocket,
            ):
                await _authenticate(websocket, token)
                results: list[Any] = []
                for message_id, command in enumerate(commands, start=1):
                    await websocket.send_json({"id": message_id, "type": command})
                    results.append(await _result(websocket, message_id, command))
                return results
        except TimeoutError as err:
            msg = "Home Assistant didn't answer in time. Is it running?"
            raise HomeAssistantError(msg) from err
        except (aiohttp.ClientError, OSError) as err:
            LOGGER.warning("Could not reach Home Assistant at %s: %s", url, err)
            msg = "Can't reach Home Assistant at this address."
            raise HomeAssistantError(msg) from err


def _websocket_url(url: str) -> str:
    """Return the websocket API URL for a Home Assistant base URL (http(s) or ws(s))."""
    url = url.strip().rstrip("/")
    if url.startswith("http"):
        url = "ws" + url.removeprefix("http")
    return url if url.endswith("/api/websocket") else f"{url}/api/websocket"


def _ziggurat_path(settings: Any) -> str | None:
    """Return the Ziggurat websocket URL from ZHA's network settings, or None for another radio."""
    # The settings also hold the network key: only the device path is ever read from them.
    device = settings.get("device") if isinstance(settings, dict) else None
    path = device.get("path") if isinstance(device, dict) else None
    if isinstance(path, str) and path.startswith(("ws://", "wss://")):
        return path
    return None


def _has_entertainment_cluster(device: dict[str, Any]) -> bool:
    """Return whether a ZHA device has Hue's entertainment cluster, as zha/devices reports it."""
    endpoints = (device.get("signature") or {}).get("endpoints") or {}
    clusters = (endpoints.get(_ENTERTAINMENT_ENDPOINT) or {}).get("input_clusters") or ()
    return _ENTERTAINMENT_CLUSTER in {str(cluster).lower() for cluster in clusters}


def _parse_nwk(value: Any) -> int | None:
    """Return a ZHA network address given as a number or a hex string ("0x1A2B"), or None."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


async def _authenticate(websocket: aiohttp.ClientWebSocketResponse, token: str) -> None:
    """Complete Home Assistant's websocket auth handshake."""
    hello = await _receive(websocket)
    if hello.get("type") != "auth_required":
        msg = "That address doesn't look like Home Assistant. Check the URL."
        raise HomeAssistantError(msg)
    await websocket.send_json({"type": "auth", "access_token": token})
    if (await _receive(websocket)).get("type") != "auth_ok":
        msg = "Home Assistant didn't accept the access token."
        raise HomeAssistantError(msg)


async def _result(websocket: aiohttp.ClientWebSocketResponse, message_id: int, command: str) -> Any:
    """Return the result of a websocket command; raise a user-readable error when it failed."""
    reply = await _receive(websocket)
    if reply.get("id") != message_id:
        msg = "Unexpected response from Home Assistant"
        raise HomeAssistantError(msg)
    if reply.get("success"):
        return reply.get("result")
    error = reply.get("error") or {}
    code = error.get("code")
    if code == "unknown_command" and command.startswith("zha/"):
        msg = "Zigbee lamps need Home Assistant's Zigbee (ZHA) integration."
    elif code == "unauthorized":
        msg = "The access token must belong to an admin user."
    else:
        msg = f"Home Assistant request {command} failed: {error.get('message', code)}"
    raise HomeAssistantError(msg)


async def _receive(websocket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    """Return the next JSON message; raise when Home Assistant closed the connection."""
    message = await websocket.receive()
    if message.type is not aiohttp.WSMsgType.TEXT:
        msg = "Home Assistant closed the connection"
        raise HomeAssistantError(msg)
    try:
        data = orjson.loads(message.data)
    except orjson.JSONDecodeError as err:
        msg = "That address doesn't look like Home Assistant. Check the URL."
        raise HomeAssistantError(msg) from err
    return data if isinstance(data, dict) else {}
