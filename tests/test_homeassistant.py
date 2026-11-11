"""Tests for the Home Assistant websocket client against a local fake Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web

from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.outbound.homeassistant import (
    HomeAssistantClient,
    HomeAssistantError,
    ZhaLight,
    _websocket_url,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from aiohttp.test_utils import TestServer

_TOKEN = "secret-token"
_NETWORK_KEY = "2ccade06c3c7b4bb1a9e2f8d5c0f8a11"


def _device(
    ieee: str,
    nwk: object,
    entity_id: str,
    *,
    manufacturer: str = "Signify Netherlands B.V.",
    name: str = "Signify Netherlands B.V. LCT015",
    user_given_name: str | None = None,
    area_id: str | None = "living",
) -> dict[str, Any]:
    """Build a ZHA device as zha/devices reports it."""
    return {
        "ieee": ieee,
        "nwk": nwk,
        "manufacturer": manufacturer,
        "model": "LCT015",
        "name": name,
        "user_given_name": user_given_name,
        "area_id": area_id,
        "entities": [{"entity_id": entity_id, "name": name}],
    }


def _state(entity_id: str, *color_modes: str) -> dict[str, Any]:
    """Build an entity state as get_states reports it."""
    return {
        "entity_id": entity_id,
        "state": "on",
        "attributes": {"supported_color_modes": list(color_modes)},
    }


_RESULTS: dict[str, Any] = {
    "zha/devices": [
        _device(
            "00:17:88:01:00:00:00:01",
            6699,
            "light.couch",
            user_given_name="Couch lamp",
        ),
        # A hex-string network address (zigpy's repr) and a "Philips" manufacturer.
        _device(
            "00:17:88:01:00:00:00:02",
            "0x1A2B",
            "light.tv",
            manufacturer="Philips",
            name="Philips LCA001",
            area_id="den",
        ),
        # A Hue white-ambiance lamp: no color mode.
        _device("00:17:88:01:00:00:00:03", 3, "light.hall"),
        # A color lamp from another brand.
        _device("00:0b:57:ff:fe:00:00:04", 4, "light.ikea", manufacturer="IKEA of Sweden"),
        # A Hue remote: no light entity at all.
        _device("00:17:88:01:00:00:00:05", 5, "sensor.dimmer_battery"),
    ],
    "get_states": [
        _state("light.couch", "color_temp", "xy"),
        _state("light.tv", "hs"),
        _state("light.hall", "color_temp"),
        _state("light.ikea", "xy"),
        _state("sensor.dimmer_battery"),
    ],
    "config/area_registry/list": [
        {"area_id": "living", "name": "Living room"},
        {"area_id": "den", "name": "Den"},
    ],
    "zha/network/settings": {
        "radio_type": "ziggurat",
        "device": {"path": "ws://core-ziggurat:9999", "baudrate": 460800},
        "settings": {"network_key": {"key": _NETWORK_KEY, "tx_counter": 1}},
    },
}


class _FakeHomeAssistant:
    """A minimal Home Assistant websocket API: the auth handshake plus canned results."""

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.commands: list[str] = []

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.send_json({"type": "auth_required", "ha_version": "2026.11.0"})
        auth = await websocket.receive_json()
        if auth.get("type") != "auth" or auth.get("access_token") != _TOKEN:
            await websocket.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await websocket.close()
            return websocket
        await websocket.send_json({"type": "auth_ok", "ha_version": "2026.11.0"})
        async for message in websocket:
            data = message.json()
            self.commands.append(data["type"])
            if data["type"] in self.results:
                reply = {"success": True, "result": self.results[data["type"]]}
            else:
                reply = {
                    "success": False,
                    "error": {"code": "unknown_command", "message": "Unknown command."},
                }
            await websocket.send_json({"id": data["id"], "type": "result", **reply})
        return websocket


@pytest.fixture
async def fake_ha(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeHomeAssistant, str]:
    """Serve a fake Home Assistant on localhost; return it and its base URL."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    fake = _FakeHomeAssistant(dict(_RESULTS))
    app = web.Application()
    app.router.add_get("/api/websocket", fake.handle)
    server = await aiohttp_server(app)
    return fake, str(server.make_url("")).rstrip("/")


def _client(tmp_path: Path, url: str, token: str = _TOKEN) -> HomeAssistantClient:
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.home_assistant.url = url
    store.config.home_assistant.token = token
    return HomeAssistantClient(store)


async def test_hue_lights_keeps_only_hue_color_lights(tmp_path: Path, fake_ha) -> None:
    """Only Signify/Philips lamps with a color-capable light entity are listed, with areas."""
    fake, url = fake_ha
    lights = await _client(tmp_path, url).hue_lights()
    assert lights == [
        ZhaLight(
            ieee="00:17:88:01:00:00:00:02",
            nwk=0x1A2B,
            name="Philips LCA001",
            area="Den",
            model="LCT015",
        ),
        ZhaLight(
            ieee="00:17:88:01:00:00:00:01",
            nwk=6699,
            name="Couch lamp",
            area="Living room",
            model="LCT015",
        ),
    ]
    # All three queries share one authenticated connection.
    assert fake.commands == ["zha/devices", "get_states", "config/area_registry/list"]


async def test_ziggurat_url_reads_the_device_path(
    tmp_path: Path, fake_ha, caplog: pytest.LogCaptureFixture
) -> None:
    """The Ziggurat websocket path is returned, and the network key never reaches the logs."""
    _fake, url = fake_ha
    caplog.set_level(logging.DEBUG)
    client = _client(tmp_path, url)
    assert await client.ziggurat_url() == "ws://core-ziggurat:9999"
    await client.hue_lights()
    assert _NETWORK_KEY not in caplog.text
    assert _TOKEN not in caplog.text


async def test_stream_info_returns_url_and_addresses(tmp_path: Path, fake_ha) -> None:
    """One connection yields the Ziggurat URL and every device's network address."""
    fake, url = fake_ha
    ziggurat, addresses = await _client(tmp_path, url).stream_info()
    assert ziggurat == "ws://core-ziggurat:9999"
    assert addresses["00:17:88:01:00:00:00:01"] == 6699
    assert addresses["00:17:88:01:00:00:00:02"] == 0x1A2B
    assert fake.commands == ["zha/network/settings", "zha/devices"]


async def test_ziggurat_url_is_none_for_a_serial_radio(tmp_path: Path, fake_ha) -> None:
    """A non-websocket radio path (a serial Zigbee stick) means ZHA does not run on Ziggurat."""
    fake, url = fake_ha
    fake.results["zha/network/settings"] = {
        "radio_type": "ezsp",
        "device": {"path": "/dev/serial/by-id/usb-zigbee"},
        "settings": {"network_key": {"key": _NETWORK_KEY}},
    }
    assert await _client(tmp_path, url).ziggurat_url() is None


async def test_rejected_token_raises(tmp_path: Path, fake_ha) -> None:
    """A wrong access token fails the auth handshake with a readable error."""
    _fake, url = fake_ha
    with pytest.raises(HomeAssistantError, match="didn't accept the access token"):
        await _client(tmp_path, url, token="wrong").hue_lights()


async def test_missing_zha_raises_a_readable_error(tmp_path: Path, fake_ha) -> None:
    """Without ZHA (its commands are unknown), the error says ZHA is needed."""
    fake, url = fake_ha
    del fake.results["zha/network/settings"]
    with pytest.raises(
        HomeAssistantError, match=r"need Home Assistant's Zigbee \(ZHA\) integration"
    ):
        await _client(tmp_path, url).ziggurat_url()


async def test_unreachable_home_assistant_raises(
    tmp_path: Path, unused_tcp_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable Home Assistant raises a plain error; the connection error goes to the log."""
    client = _client(tmp_path, f"http://127.0.0.1:{unused_tcp_port}")
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await client.ziggurat_url()
    assert str(raised.value) == "Can't reach Home Assistant at this address."
    assert any(f"127.0.0.1:{unused_tcp_port}" in record.getMessage() for record in caplog.records)


async def test_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the add-on token or a configured URL and token there is no connection."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = _client(tmp_path, "http://homeassistant.local:8123", token="")
    assert not client.available
    assert not client.is_addon
    with pytest.raises(HomeAssistantError, match="Not connected to Home Assistant"):
        await client.hue_lights()


def test_addon_uses_the_supervisor_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As an add-on, the Supervisor's websocket proxy and token are used over manual settings."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    client = _client(tmp_path, "", token="")
    assert client.is_addon
    assert client.available
    assert client._connection() == ("ws://supervisor/core/websocket", "supervisor-token")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://homeassistant.local:8123", "ws://homeassistant.local:8123/api/websocket"),
        ("https://ha.example.com/", "wss://ha.example.com/api/websocket"),
        ("ws://10.0.0.2:8123/api/websocket", "ws://10.0.0.2:8123/api/websocket"),
    ],
)
def test_websocket_url(url: str, expected: str) -> None:
    """A Home Assistant base URL maps to its websocket API URL."""
    assert _websocket_url(url) == expected
