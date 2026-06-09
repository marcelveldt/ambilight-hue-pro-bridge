"""Tests for the v1 REST emulator using an aiohttp test client."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ambilight_hue_bridge.config.models import VirtualLight
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.emulator.pairing import PairingManager
from ambilight_hue_bridge.emulator.rest_v1 import HueV1Emulator

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import web

_USERNAME_LEN = 32


@pytest.fixture
def emulator_app(tmp_path: Path) -> web.Application:
    """Build a v1 emulator app backed by a temp config with two virtual lights."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.virtual_lights = [
        VirtualLight(id="1", name="Left"),
        VirtualLight(id="2", name="Right"),
    ]
    emulator = HueV1Emulator(
        store=store,
        pairing=PairingManager(store),
        host_ip="1.2.3.4",
        mac="aabbccddeeff",
    )
    return emulator.create_app()


async def test_public_config_identifies_as_bsb002(aiohttp_client, emulator_app) -> None:
    """The unauthenticated short config identifies as a BSB002 bridge."""
    client = await aiohttp_client(emulator_app)
    resp = await client.get("/api/config")
    config = await resp.json()
    assert config["modelid"] == "BSB002"
    assert config["bridgeid"] == "AABBCCFFFEDDEEFF"


async def test_description_served(aiohttp_client, emulator_app) -> None:
    """The descriptor is served over HTTP."""
    client = await aiohttp_client(emulator_app)
    resp = await client.get("/description.xml")
    assert "Philips hue bridge 2015" in await resp.text()


async def test_pairing_returns_credentials(aiohttp_client, emulator_app) -> None:
    """Pushlink pairing returns a username and (when requested) a clientkey."""
    client = await aiohttp_client(emulator_app)
    resp = await client.post("/api", json={"devicetype": "unit#test", "generateclientkey": True})
    success = (await resp.json())[0]["success"]
    assert len(success["username"]) == _USERNAME_LEN
    assert len(success["clientkey"]) == _USERNAME_LEN


async def test_lights_require_authorization(aiohttp_client, emulator_app) -> None:
    """An unknown username gets an unauthorized error."""
    client = await aiohttp_client(emulator_app)
    resp = await client.get("/api/nope/lights")
    assert (await resp.json())[0]["error"]["type"] == 1


async def test_paired_user_can_list_lights(aiohttp_client, emulator_app) -> None:
    """After pairing, lights are listed with integer state and streaming capability."""
    client = await aiohttp_client(emulator_app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    lights = await (await client.get(f"/api/{user}/lights")).json()
    assert set(lights) == {"1", "2"}
    assert isinstance(lights["1"]["state"]["bri"], int)
    assert lights["1"]["capabilities"]["streaming"]["renderer"] is True


async def test_put_light_state_echoes_success(aiohttp_client, emulator_app) -> None:
    """A light state PUT echoes a success entry per key."""
    client = await aiohttp_client(emulator_app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    resp = await client.put(f"/api/{user}/lights/1/state", json={"on": True, "bri": 100})
    keys = {next(iter(entry["success"])) for entry in await resp.json()}
    assert keys == {"/lights/1/state/on", "/lights/1/state/bri"}
