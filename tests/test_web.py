"""Tests for the web configuration UI API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hue_entertainment import DiscoveredBridge, EntertainmentArea, LightChannel

from ambilight_hue_bridge.config.models import RealBridge
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.web.server import WebServer

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import web


@pytest.fixture
def web_setup(tmp_path: Path) -> tuple[web.Application, ConfigStore]:
    """Build the web app over a temp config store."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    server = WebServer(store=store, engine=None, mac="aabbccddeeff", host_ip="1.2.3.4")
    return server.create_app(), store


async def test_status_reports_bridge_id(aiohttp_client, web_setup) -> None:
    """The status endpoint reports the derived bridge id and idle stream state."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    data = await (await client.get("/api/status")).json()
    assert data["bridge_id"] == "AABBCCFFFEDDEEFF"
    assert data["streaming"] is False


async def test_index_is_served(aiohttp_client, web_setup) -> None:
    """The single-page UI is served at the root."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 200
    assert "Ambilight" in await resp.text()


async def test_pair_adds_and_activates_bridge(aiohttp_client, web_setup, monkeypatch) -> None:
    """Pairing via the API stores the bridge and marks it active."""
    app, store = web_setup

    async def fake_pair(host: str) -> dict[str, str]:
        """Return fake credentials without touching the network."""
        return {"username": f"user-{host}", "clientkey": "KEY"}

    monkeypatch.setattr("ambilight_hue_bridge.outbound.controller.pair_bridge", fake_pair)
    client = await aiohttp_client(app)
    data = await (await client.post("/api/bridges/pair", json={"host": "192.168.1.5"})).json()
    assert data["paired"] is True
    assert data["active"] is True
    assert store.config.active_real_bridge == data["id"]
    assert any(bridge.host == "192.168.1.5" for bridge in store.config.real_bridges)


async def test_discover_lists_bridges(aiohttp_client, web_setup, monkeypatch) -> None:
    """The discover endpoint returns bridges found via mDNS."""
    app, _store = web_setup

    async def fake_discover() -> list[DiscoveredBridge]:
        """Return one fake discovered bridge without touching the network."""
        return [DiscoveredBridge(id="ABC", host="192.168.1.9", name="hue.local")]

    monkeypatch.setattr("ambilight_hue_bridge.web.server.discover_bridges", fake_discover)
    client = await aiohttp_client(app)
    found = await (await client.get("/api/discover")).json()
    assert found[0]["host"] == "192.168.1.9"
    assert found[0]["id"] == "ABC"


async def test_pair_requires_host(aiohttp_client, web_setup) -> None:
    """Pairing without a host returns a 400."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.post("/api/bridges/pair", json={})
    assert resp.status == 400


async def test_update_and_delete_bridge(aiohttp_client, web_setup, monkeypatch) -> None:
    """A bridge's entertainment area can be set and the bridge removed."""
    app, store = web_setup

    async def fake_pair(host: str) -> dict[str, str]:
        """Return fake credentials without touching the network."""
        assert host
        return {"username": "u", "clientkey": "k"}

    monkeypatch.setattr("ambilight_hue_bridge.outbound.controller.pair_bridge", fake_pair)
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    bridge = await (await client.post("/api/bridges/pair", json={"host": "1.1.1.1"})).json()
    bridge_id = bridge["id"]
    resp = await client.put(f"/api/bridges/{bridge_id}", json={"entertainment_area": "area-9"})
    updated = await resp.json()
    assert updated["entertainment_area"] == "area-9"
    await client.delete(f"/api/bridges/{bridge_id}")
    assert not store.config.real_bridges


def _configure_area(store: ConfigStore) -> None:
    """Give the store an active bridge with a selected entertainment area."""
    store.config.real_bridges = [
        RealBridge(
            id="b", host="1.2.3.4", app_key="u", client_key="k", entertainment_area="area-1"
        ),
    ]
    store.config.active_real_bridge = "b"


def _fake_areas_factory() -> object:
    """Return an async list_areas replacement with two channels (left and right)."""

    async def fake_areas(_host: str, _app_key: str) -> list[EntertainmentArea]:
        return [
            EntertainmentArea(
                id="area-1",
                name="Living",
                channels=[
                    LightChannel(
                        channel_id=0, service_id="s0", name="c0", position=(-0.9, 0.8, 0.0)
                    ),
                    LightChannel(
                        channel_id=1, service_id="s1", name="c1", position=(0.9, 0.8, 0.0)
                    ),
                ],
            ),
        ]

    return fake_areas


async def test_light_crud(aiohttp_client, web_setup) -> None:
    """Virtual lights can be created, updated (incl. channels) and deleted."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    created = await (
        await client.post("/api/lights", json={"name": "Left", "position": "left"})
    ).json()
    light_id = created["id"]
    assert created["name"] == "Left"
    updated = await (
        await client.put(f"/api/lights/{light_id}", json={"channels": [0, 2], "name": "Left edge"})
    ).json()
    assert updated["channels"] == [0, 2]
    assert updated["name"] == "Left edge"
    await client.delete(f"/api/lights/{light_id}")
    remaining = await (await client.get("/api/lights")).json()
    assert all(light["id"] != light_id for light in remaining)


async def test_channels_endpoint(aiohttp_client, web_setup, monkeypatch) -> None:
    """The channels endpoint returns the active area's channels."""
    app, store = web_setup
    _configure_area(store)
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    data = await (await client.get("/api/channels")).json()
    assert [channel["channel_id"] for channel in data["channels"]] == [0, 1]


async def test_auto_map_creates_one_light_per_channel(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """Auto-map replaces the lights with one per channel, positioned by x."""
    app, store = web_setup
    _configure_area(store)
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    lights = await (await client.post("/api/lights/auto-map")).json()
    assert len(lights) == 2
    assert lights[0]["channels"] == [0]
    assert lights[0]["position"] == "left"
    assert lights[1]["position"] == "right"
    assert len(store.config.virtual_lights) == 2
