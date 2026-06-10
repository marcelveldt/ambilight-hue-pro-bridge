"""Tests for the web configuration UI API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from hue_entertainment import DiscoveredBridge, EntertainmentArea, LightChannel

from ambilight_hue_bridge.config.models import PairedUser, RealBridge
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.web.server import WebServer, _mirror_from_area

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def web_setup(tmp_path: Path) -> tuple[web.Application, ConfigStore]:
    """Build the web app over a temp config store."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    server = WebServer(
        store=store, engine=None, mac="aabbccddeeff", host_ip="1.2.3.4", http_port=8080
    )
    app = web.Application()
    server.register(app)
    return app, store


async def test_status_reports_bridge_id(aiohttp_client, web_setup) -> None:
    """The status endpoint reports the derived bridge id and idle stream state."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    data = await (await client.get("/cfg/status")).json()
    assert data["bridge_id"] == "AABBCCFFFEDDEEFF"
    assert data["streaming"] is False


async def test_settings_get_and_put_clamps(aiohttp_client, web_setup) -> None:
    """Streaming settings are read and updated, with out-of-range values clamped."""
    app, store = web_setup
    client = await aiohttp_client(app)
    current = await (await client.get("/cfg/settings")).json()
    assert current["stream_smoothing"] == 0.5
    assert current["stream_rate_hz"] == 50
    updated = await (
        await client.put("/cfg/settings", json={"stream_smoothing": 1.5, "stream_rate_hz": 999})
    ).json()
    assert updated["stream_smoothing"] == 0.95
    assert updated["stream_rate_hz"] == 60
    assert store.config.virtual_bridge.stream_smoothing == 0.95


async def test_tvs_lists_paired_users(aiohttp_client, web_setup) -> None:
    """The TVs endpoint lists paired devices with a streaming flag."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="55POS9002/12", created="2026-06-10"),
    ]
    client = await aiohttp_client(app)
    tvs = await (await client.get("/cfg/tvs")).json()
    assert tvs[0]["devicetype"] == "55POS9002/12"
    assert tvs[0]["streaming"] is False


async def test_assign_tv_builds_lights_from_area(aiohttp_client, web_setup, monkeypatch) -> None:
    """Assigning an entertainment area to a TV rebuilds its lights from that area's channels."""
    app, store = web_setup
    _configure_area(store)
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-10"),
    ]
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    tv = await (
        await client.put(
            "/cfg/tvs/u1", json={"entertainment_area": "area-1", "split_gradients": True}
        )
    ).json()
    assert tv["entertainment_area"] == "area-1"
    assert tv["lights"] == ["c0", "c1"]
    assert store.config.users[0].lights[0].channels == [0]


async def test_areas_list(aiohttp_client, web_setup, monkeypatch) -> None:
    """The areas endpoint lists the active bridge's entertainment areas."""
    app, store = web_setup
    _configure_area(store)
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    areas = await (await client.get("/cfg/areas")).json()
    assert areas[0]["id"] == "area-1"
    assert areas[0]["channels"] == 2


def test_mirror_merges_gradient_when_not_split() -> None:
    """With split off, channels of the same light merge into one multi-channel light."""
    area = EntertainmentArea(
        id="a",
        name="A",
        channels=[
            LightChannel(channel_id=0, service_id="grad", name="Strip", position=(-0.5, 0.0, 0.0)),
            LightChannel(channel_id=1, service_id="grad", name="Strip", position=(0.5, 0.0, 0.0)),
        ],
    )
    assert len(_mirror_from_area(area, split_gradients=True)) == 2
    merged = _mirror_from_area(area, split_gradients=False)
    assert len(merged) == 1
    assert merged[0].channels == [0, 1]


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
    data = await (await client.post("/cfg/bridges/pair", json={"host": "192.168.1.5"})).json()
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
    found = await (await client.get("/cfg/discover")).json()
    assert found[0]["host"] == "192.168.1.9"
    assert found[0]["id"] == "ABC"


async def test_pair_requires_host(aiohttp_client, web_setup) -> None:
    """Pairing without a host returns a 400."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.post("/cfg/bridges/pair", json={})
    assert resp.status == 400


async def test_update_and_delete_bridge(aiohttp_client, web_setup, monkeypatch) -> None:
    """A bridge can be marked active and then removed."""
    app, store = web_setup

    async def fake_pair(host: str) -> dict[str, str]:
        """Return fake credentials without touching the network."""
        assert host
        return {"username": "u", "clientkey": "k"}

    monkeypatch.setattr("ambilight_hue_bridge.outbound.controller.pair_bridge", fake_pair)
    client = await aiohttp_client(app)
    bridge = await (await client.post("/cfg/bridges/pair", json={"host": "1.1.1.1"})).json()
    bridge_id = bridge["id"]
    resp = await client.put(f"/cfg/bridges/{bridge_id}", json={"active": True})
    updated = await resp.json()
    assert updated["active"] is True
    assert store.config.active_real_bridge == bridge_id
    await client.delete(f"/cfg/bridges/{bridge_id}")
    assert not store.config.real_bridges


def _configure_area(store: ConfigStore) -> None:
    """Give the store an active bridge (its areas are listed live via list_areas)."""
    store.config.real_bridges = [
        RealBridge(id="b", host="1.2.3.4", app_key="u", client_key="k"),
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
