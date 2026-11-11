"""Tests for the web configuration UI API."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from hue_entertainment import DiscoveredBridge, EntertainmentArea, LightChannel
from hue_entertainment.zigbee import ZigguratError

from ambilight_hue_bridge.config.models import (
    CachedArea,
    CachedChannel,
    PairedUser,
    RealBridge,
    StreamTarget,
    VirtualLight,
    ZigbeeLightRef,
)
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.emulator.pairing import PairingManager
from ambilight_hue_bridge.emulator.rest_v1 import HueV1Emulator
from ambilight_hue_bridge.outbound.controller import (
    lights_from_area,
    lights_from_zigbee,
    zigbee_channels,
)
from ambilight_hue_bridge.outbound.homeassistant import HomeAssistantError, ZhaLight
from ambilight_hue_bridge.web.server import WebServer

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from aiohttp.typedefs import Handler


class _FakePolicyEngine:
    """Engine stand-in recording TV policy notifications."""

    def __init__(self) -> None:
        self.stream_owners: frozenset[str] = frozenset()
        self.is_streaming = False
        self.changed: list[str] = []
        self.restarted: list[str] = []

    def tv_settings_changed(self, owner: str) -> None:
        self.changed.append(owner)

    async def restart_stream(self, owner: str) -> None:
        self.restarted.append(owner)

    async def remove_tv(self, _owner: str) -> None:
        return


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
    assert tvs[0]["enabled"] is True
    assert tvs[0]["stream_priority"] == 0


async def test_tv_policy_get_and_update(aiohttp_client, tmp_path) -> None:
    """The per-TV REST resource exposes and applies enabled state and stream priority."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-10"),
    ]
    engine = _FakePolicyEngine()
    server = WebServer(
        store=store,
        engine=engine,  # type: ignore[arg-type]
        mac="aabbccddeeff",
        host_ip="1.2.3.4",
        http_port=8080,
    )
    app = web.Application()
    server.register(app)
    client = await aiohttp_client(app)

    updated = await (
        await client.put("/cfg/tvs/u1", json={"enabled": False, "stream_priority": 42})
    ).json()
    assert updated["enabled"] is False
    assert updated["stream_priority"] == 42
    assert engine.changed == ["u1"]

    fetched = await (await client.get("/cfg/tvs/u1")).json()
    assert fetched["enabled"] is False
    assert fetched["stream_priority"] == 42


async def test_tv_name_is_set_and_cleared(aiohttp_client, tmp_path) -> None:
    """A TV's name is trimmed and saved without touching its lights; an empty name clears it."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="55POS9002/12",
            created="2026-06-10",
            lights=[VirtualLight(id="1", name="Left")],
        ),
    ]
    engine = _FakePolicyEngine()
    server = WebServer(
        store=store,
        engine=engine,  # type: ignore[arg-type]
        mac="aabbccddeeff",
        host_ip="1.2.3.4",
        http_port=8080,
    )
    app = web.Application()
    server.register(app)
    client = await aiohttp_client(app)
    assert (await (await client.get("/cfg/tvs/u1")).json())["name"] == ""

    tv = await (await client.put("/cfg/tvs/u1", json={"name": "  Den TV  "})).json()
    assert tv["name"] == "Den TV"
    assert tv["devicetype"] == "55POS9002/12"
    assert tv["lights"] == ["Left"]
    assert engine.changed == []
    assert engine.restarted == []
    assert ConfigStore(tmp_path / "config.yaml").load().users[0].name == "Den TV"

    for cleared in ("", "   ", None):
        store.config.users[0].name = "Den TV"
        tv = await (await client.put("/cfg/tvs/u1", json={"name": cleared})).json()
        assert tv["name"] == ""


async def test_tv_policy_rejects_invalid_values(aiohttp_client, web_setup) -> None:
    """Policy updates reject non-boolean enablement and out-of-range priorities."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-10"),
    ]
    client = await aiohttp_client(app)
    assert (await client.put("/cfg/tvs/u1", json={"enabled": "false"})).status == 400
    assert (await client.put("/cfg/tvs/u1", json={"stream_priority": 101})).status == 400
    assert (
        await client.put(
            "/cfg/tvs/u1",
            json={"enabled": False, "stream_priority": 101},
        )
    ).status == 400
    assert store.config.users[0].enabled is True


async def test_assign_tv_builds_lights_from_area(aiohttp_client, web_setup, monkeypatch) -> None:
    """Assigning an entertainment area to a TV rebuilds its lights from that area's channels."""
    app, store = web_setup
    _configure_area(store)
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-10"),
    ]
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    tv = await (await client.put("/cfg/tvs/u1", json={"entertainment_area": "area-1"})).json()
    assert tv["entertainment_area"] == "area-1"
    assert tv["lights"] == ["c0", "c1"]
    assert store.config.users[0].lights[0].channels == [0]


async def test_assign_tv_splits_gradient_zones(aiohttp_client, web_setup, monkeypatch) -> None:
    """Each zone of a gradient light becomes its own light, also when a client asks otherwise."""
    app, store = web_setup
    _configure_area(store)
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-10"),
    ]

    async def fake_areas(_host: str, _app_key: str) -> list[EntertainmentArea]:
        return [
            EntertainmentArea(
                id="area-1",
                name="Living",
                channels=[
                    LightChannel(
                        channel_id=0, service_id="grad", name="Strip", position=(-0.5, 0.0, 0.0)
                    ),
                    LightChannel(
                        channel_id=1, service_id="grad", name="Strip", position=(0.5, 0.0, 0.0)
                    ),
                ],
            ),
        ]

    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", fake_areas)
    client = await aiohttp_client(app)
    tv = await (
        await client.put(
            "/cfg/tvs/u1", json={"entertainment_area": "area-1", "split_gradients": False}
        )
    ).json()
    assert tv["lights"] == ["Strip (left)", "Strip (right)"]
    assert "split_gradients" not in tv


async def test_areas_list(aiohttp_client, web_setup, monkeypatch) -> None:
    """The areas endpoint lists the active bridge's entertainment areas."""
    app, store = web_setup
    _configure_area(store)
    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", _fake_areas_factory())
    client = await aiohttp_client(app)
    areas = await (await client.get("/cfg/areas")).json()
    assert areas[0]["id"] == "area-1"
    assert areas[0]["channels"] == 2
    # A successful live fetch also refreshes the persisted cache.
    assert store.config.real_bridges[0].cached_areas[0].id == "area-1"


async def test_delete_tv(aiohttp_client, web_setup) -> None:
    """A paired TV can be removed; deleting an unknown one returns 404."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-11"),
    ]
    client = await aiohttp_client(app)
    assert (await client.delete("/cfg/tvs/u1")).status == 200
    assert store.config.users == []
    assert (await client.delete("/cfg/tvs/u1")).status == 404


async def test_assign_smoothing_override_does_not_rebuild_lights(aiohttp_client, web_setup) -> None:
    """A smoothing-only PUT stores the per-TV override and leaves the TV's lights untouched."""
    app, store = web_setup
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-11",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="Left"), VirtualLight(id="2", name="Right")],
        ),
    ]
    client = await aiohttp_client(app)
    tv = await (await client.put("/cfg/tvs/u1", json={"stream_smoothing": 0.6})).json()
    assert tv["stream_smoothing"] == 0.6
    # Not rebuilt (no list_areas call needed; the existing light names are preserved).
    assert tv["lights"] == ["Left", "Right"]
    assert store.config.users[0].stream_smoothing == 0.6


async def test_tv_smoothing_defaults_to_automatic(aiohttp_client, web_setup) -> None:
    """A TV with no smoothing set reports null (automatic); setting null goes back to automatic."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-11"),
    ]
    client = await aiohttp_client(app)
    tvs = await (await client.get("/cfg/tvs")).json()
    assert tvs[0]["stream_smoothing"] is None
    await client.put("/cfg/tvs/u1", json={"stream_smoothing": 0.0})
    assert store.config.users[0].stream_smoothing == 0.0
    tv = await (await client.put("/cfg/tvs/u1", json={"stream_smoothing": None})).json()
    assert tv["stream_smoothing"] is None
    assert store.config.users[0].stream_smoothing is None


def test_mirror_labels_gradient_zones_by_position() -> None:
    """Same-named gradient zones are disambiguated by their on-screen position."""
    area = EntertainmentArea(
        id="a",
        name="A",
        channels=[
            LightChannel(channel_id=0, service_id="g", name="Strip", position=(-0.9, 0.0, 0.0)),
            LightChannel(channel_id=1, service_id="g", name="Strip", position=(0.0, 0.0, 0.0)),
            LightChannel(channel_id=2, service_id="g", name="Strip", position=(0.9, 0.0, 0.0)),
        ],
    )
    names = [light.name for light in lights_from_area(area)]
    assert names == ["Strip (far left)", "Strip (center)", "Strip (far right)"]


def _cached_bridge(store: ConfigStore) -> None:
    """Give the store an active bridge whose areas are already cached (real bridge offline)."""
    store.config.real_bridges = [
        RealBridge(
            id="b",
            host="1.2.3.4",
            app_key="u",
            client_key="k",
            cached_areas=[
                CachedArea(
                    id="area-1",
                    name="Living",
                    channels=[
                        CachedChannel(channel_id=0, name="c0", position=[-0.9, 0.8, 0.0]),
                        CachedChannel(channel_id=1, name="c1", position=[0.9, 0.8, 0.0]),
                    ],
                ),
            ],
        ),
    ]
    store.config.active_real_bridge = "b"


async def test_areas_served_from_cache_when_bridge_unreachable(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """When the bridge is unplugged, /cfg/areas serves the cached areas so the TV setup works."""
    app, store = web_setup
    _cached_bridge(store)

    async def boom(_host: str, _app_key: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", boom)
    client = await aiohttp_client(app)
    areas = await (await client.get("/cfg/areas")).json()
    assert areas[0]["id"] == "area-1"
    assert areas[0]["channels"] == 2


async def test_assign_tv_uses_cached_area_when_bridge_unreachable(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """A TV can be assigned an area (and get lights) from the cache while the bridge is offline."""
    app, store = web_setup
    _cached_bridge(store)
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-11"),
    ]

    async def boom(_host: str, _app_key: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", boom)
    client = await aiohttp_client(app)
    tv = await (await client.put("/cfg/tvs/u1", json={"entertainment_area": "area-1"})).json()
    assert tv["entertainment_area"] == "area-1"
    assert tv["lights"] == ["c0", "c1"]


def test_mirror_caps_long_names_to_32_chars() -> None:
    """Long source names are truncated to Hue's 32-char limit, keeping the position suffix."""
    area = EntertainmentArea(
        id="a",
        name="A",
        channels=[
            LightChannel(
                channel_id=0,
                service_id="g",
                name="Woonkamer Gradient lichtstrip",
                position=(-0.9, 0.0, 0.0),
            ),
            LightChannel(
                channel_id=1,
                service_id="g",
                name="Woonkamer Gradient lichtstrip",
                position=(0.9, 0.0, 0.0),
            ),
        ],
    )
    names = [light.name for light in lights_from_area(area)]
    assert all(len(n) <= 32 for n in names)
    assert names[0].endswith("(far left)")
    assert names[1].endswith("(far right)")


def test_zigbee_channels_split_gradient_lamps_into_zones() -> None:
    """A gradient lamp gets up to 3 consecutive channels, any other lamp one."""
    lamps = [
        ZigbeeLightRef(ieee="a", segments=7),
        ZigbeeLightRef(ieee="b"),
        ZigbeeLightRef(ieee="c", segments=2),
    ]
    split = zigbee_channels(lamps)
    assert [lamp for lamp, _channels in split] == lamps
    assert [list(channels) for _lamp, channels in split] == [[0, 1, 2], [3], [4, 5]]


def test_lights_from_zigbee_one_light_per_zone() -> None:
    """Zone lights follow the lamps' channels, named after the lamp and zone within 32 chars."""
    lamps = [
        ZigbeeLightRef(ieee="a", name="Signe floor lamp right of the TV", segments=7),
        ZigbeeLightRef(ieee="b", name="Signe left", segments=7),
        ZigbeeLightRef(ieee="c", name="Spot"),
        ZigbeeLightRef(ieee="d", segments=5),
    ]
    lights = lights_from_zigbee(lamps)
    assert [(light.id, light.channels) for light in lights] == [
        (str(channel + 1), [channel]) for channel in range(10)
    ]
    assert [light.name for light in lights] == [
        "Signe floor lamp right (zone 1)",
        "Signe floor lamp right (zone 2)",
        "Signe floor lamp right (zone 3)",
        "Signe left (zone 1)",
        "Signe left (zone 2)",
        "Signe left (zone 3)",
        "Spot",
        "Light 4 (zone 1)",
        "Light 4 (zone 2)",
        "Light 4 (zone 3)",
    ]


async def test_index_is_served(aiohttp_client, web_setup) -> None:
    """The single-page UI is served at the root with a root <base> for direct access."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "Ambilight" in body
    assert "data-priority" in body
    assert "data-enabled" in body
    assert '<base href="/" />' in body


async def test_index_rewrites_base_for_ingress(aiohttp_client, web_setup) -> None:
    """Behind HA ingress the <base> is rewritten to the X-Ingress-Path prefix."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/" />' in body
    assert '<base href="/" />' not in body


_LAN_PEER = "192.0.2.10"
_SUPERVISOR_PEER = "172.30.32.2"


@web.middleware
async def _peer_from_header(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Use the ``X-Peer`` header as the client address, so a test can call from any host."""
    peer = request.headers.get("X-Peer")
    return await handler(request.clone(remote=peer) if peer else request)


def _service_app(
    store: ConfigStore, home_assistant: _FakeHomeAssistant | None = None
) -> tuple[web.Application, WebServer]:
    """Build the service's app: the TV-facing Hue v1 API and the web UI on one app."""
    app = web.Application(middlewares=[_peer_from_header])
    HueV1Emulator(
        store=store,
        pairing=PairingManager(store),
        host_ip="1.2.3.4",
        mac="aabbccddeeff",
        http_port=80,
    ).register(app)
    server = WebServer(
        store=store,
        engine=None,
        mac="aabbccddeeff",
        host_ip="1.2.3.4",
        http_port=80,
        home_assistant=home_assistant,  # type: ignore[arg-type]
    )
    server.register(app)
    return app, server


async def test_addon_serves_ui_only_to_home_assistant(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """As an add-on the UI and /cfg answer the Supervisor's ingress and this host, not the LAN."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    _app, store = web_setup
    app, _server = _service_app(store)
    client = await aiohttp_client(app)
    lan = {"X-Peer": _LAN_PEER}

    resp = await client.get("/", headers=lan)
    assert resp.status == 403
    assert "Home Assistant sidebar" in await resp.text()
    assert (await client.head("/cfg/status", headers=lan)).status == 403
    resp = await client.put(
        "/cfg/homeassistant", json={"url": "http://192.0.2.99", "token": "x"}, headers=lan
    )
    assert resp.status == 403
    assert store.config.home_assistant.url == ""

    ingress = {"X-Peer": _SUPERVISOR_PEER, "X-Ingress-Path": "/api/hassio_ingress/abc123"}
    resp = await client.get("/", headers=ingress)
    assert resp.status == 200
    assert '<base href="/api/hassio_ingress/abc123/" />' in await resp.text()
    assert (await client.get("/cfg/status", headers=ingress)).status == 200
    # Home Assistant itself (on the host network) calls from loopback, e.g. a REST command.
    assert (await client.get("/cfg/tvs")).status == 200


async def test_addon_blocks_every_ui_route_from_the_lan(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """As an add-on every web UI route, whatever its method, answers a LAN client with a 403."""

    async def no_discovery() -> list[DiscoveredBridge]:
        return []

    # Keeps a request that wrongly reaches a handler off the network.
    monkeypatch.setattr("ambilight_hue_bridge.web.server.discover_bridges", no_discovery)
    home_assistant = _FakeHomeAssistant()
    home_assistant.is_addon = True
    _app, store = web_setup
    app, server = _service_app(store, home_assistant)
    client = await aiohttp_client(app)
    routes = [route for route in app.router.routes() if route.resource in server._resources]
    assert len(routes) > 15

    served = []
    for route in routes:
        assert route.resource is not None
        path = re.sub(r"\{\w+\}", "x", route.resource.canonical)
        resp = await client.request(route.method, path, headers={"X-Peer": _LAN_PEER})
        if resp.status != 403:
            served.append(f"{route.method} {path} -> {resp.status}")
    assert not served


async def test_addon_keeps_the_hue_api_on_the_lan(aiohttp_client, web_setup, monkeypatch) -> None:
    """As an add-on the TVs still pair and discover the bridge from the LAN."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    _app, store = web_setup
    app, _server = _service_app(store)
    client = await aiohttp_client(app)
    lan = {"X-Peer": _LAN_PEER}

    assert (await client.get("/description.xml", headers=lan)).status == 200
    assert (await client.get("/api/config", headers=lan)).status == 200
    assert (await client.get("/api/nupnp", headers=lan)).status == 200
    resp = await client.post("/api", json={"devicetype": "tv#ambilight"}, headers=lan)
    username = (await resp.json())[0]["success"]["username"]
    assert (await client.get(f"/api/{username}/lights", headers=lan)).status == 200


async def test_standalone_serves_ui_to_the_lan(aiohttp_client, web_setup, monkeypatch) -> None:
    """Outside the add-on (no ingress) the UI and /cfg stay reachable from the LAN."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    _app, store = web_setup
    app, _server = _service_app(store)
    client = await aiohttp_client(app)
    lan = {"X-Peer": _LAN_PEER}

    assert (await client.get("/", headers=lan)).status == 200
    assert (await client.get("/cfg/status", headers=lan)).status == 200


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


async def test_failed_discovery_is_logged(
    aiohttp_client, web_setup, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed bridge search answers 502 and logs the reason the UI no longer shows."""
    app, _store = web_setup

    async def failing_discover() -> list[DiscoveredBridge]:
        """Fail like a host without a usable network interface."""
        raise OSError("No usable interface")

    monkeypatch.setattr("ambilight_hue_bridge.web.server.discover_bridges", failing_discover)
    client = await aiohttp_client(app)
    assert (await client.get("/cfg/discover")).status == 502
    assert "Bridge discovery failed: No usable interface" in caplog.text


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


class _FakeHomeAssistant:
    """Stand-in for HomeAssistantClient with a fixed Ziggurat URL and ZHA lamps, or an error."""

    def __init__(
        self,
        url: str | None = "ws://core-ziggurat:9999",
        error: HomeAssistantError | None = None,
        addresses: dict[str, int] | None = None,
    ) -> None:
        self.url = url
        self.error = error
        self.addresses = addresses or {}
        self.is_addon = False
        self.available = error is None

    async def ziggurat_url(self) -> str | None:
        if self.error is not None:
            raise self.error
        return self.url

    async def stream_info(self) -> tuple[str | None, dict[str, int]]:
        if self.error is not None:
            raise self.error
        return self.url, self.addresses

    async def hue_lights(self) -> list[ZhaLight]:
        return [
            ZhaLight(
                ieee="00:17:88:01:00:00:00:01", nwk=1, name="Couch", area="Living", model="LCT015"
            ),
        ]


def _patch_segment_counts(
    monkeypatch: pytest.MonkeyPatch, counts: dict[int, int] | Exception
) -> list[list[int]]:
    """Answer the Ziggurat segment reads from ``counts`` (or raise it); return each read's nwks."""
    reads: list[list[int]] = []

    async def fake_read(_url: str, nwks: Iterable[int]) -> dict[int, int]:
        requested = list(nwks)
        reads.append(requested)
        if isinstance(counts, Exception):
            raise counts
        return {nwk: counts[nwk] for nwk in requested if nwk in counts}

    monkeypatch.setattr("ambilight_hue_bridge.web.server.read_segment_counts", fake_read)
    return reads


def _zigbee_app(
    store: ConfigStore, home_assistant: _FakeHomeAssistant, engine: object = None
) -> web.Application:
    server = WebServer(
        store=store,
        engine=engine,  # type: ignore[arg-type]
        mac="aabbccddeeff",
        host_ip="1.2.3.4",
        http_port=8080,
        home_assistant=home_assistant,  # type: ignore[arg-type]
    )
    app = web.Application()
    server.register(app)
    return app


async def test_zigbee_lights_lists_hue_lamps(aiohttp_client, web_setup) -> None:
    """The Zigbee lights endpoint lists ZHA's Hue color lamps (name, area) when available."""
    _app, store = web_setup
    client = await aiohttp_client(_zigbee_app(store, _FakeHomeAssistant()))
    data = await (await client.get("/cfg/zigbee/lights")).json()
    assert data == {
        "available": True,
        "lights": [
            {
                "ieee": "00:17:88:01:00:00:00:01",
                "name": "Couch",
                "area": "Living",
                "model": "LCT015",
            },
        ],
    }


async def test_zigbee_lights_unavailable_without_ziggurat(aiohttp_client, web_setup) -> None:
    """ZHA on another radio makes the Zigbee target unavailable, with the reason."""
    _app, store = web_setup
    client = await aiohttp_client(_zigbee_app(store, _FakeHomeAssistant(url=None)))
    data = await (await client.get("/cfg/zigbee/lights")).json()
    assert data["available"] is False
    assert "Ziggurat" in data["reason"]
    assert data["lights"] == []


async def test_zigbee_lights_reports_home_assistant_errors(aiohttp_client, web_setup) -> None:
    """A Home Assistant error becomes the unavailable reason instead of an HTTP error."""
    _app, store = web_setup
    error = HomeAssistantError("Not connected to Home Assistant yet.")
    client = await aiohttp_client(_zigbee_app(store, _FakeHomeAssistant(error=error)))
    resp = await client.get("/cfg/zigbee/lights")
    assert resp.status == 200
    data = await resp.json()
    assert data["available"] is False
    assert data["reason"] == str(error)


async def test_assign_zigbee_target_rebuilds_lights(aiohttp_client, web_setup, monkeypatch) -> None:
    """Picking Zigbee lamps gives the TV one light per lamp; switching back rebuilds the area."""
    _app, store = web_setup
    _cached_bridge(store)
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-11-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="c0", channels=[0])],
        ),
    ]

    async def boom(_host: str, _app_key: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr("ambilight_hue_bridge.web.server.list_areas", boom)
    _patch_segment_counts(monkeypatch, {})
    engine = _FakePolicyEngine()
    client = await aiohttp_client(_zigbee_app(store, _FakeHomeAssistant(), engine))
    lamps = [
        {"ieee": "00:17:88:01:00:00:00:0A", "name": "A lamp with a name far beyond 32 chars"},
        {"ieee": "00:17:88:01:00:00:00:0b", "name": "Couch"},
    ]
    tv = await (
        await client.put("/cfg/tvs/u1", json={"target": "zigbee", "zigbee_lights": lamps})
    ).json()
    assert tv["target"] == "zigbee"
    assert tv["zigbee_lights"] == [
        {"ieee": "00:17:88:01:00:00:00:0a", "name": lamps[0]["name"], "zones": 1},
        {"ieee": "00:17:88:01:00:00:00:0b", "name": "Couch", "zones": 1},
    ]
    assert tv["lights"] == ["A lamp with a name far beyond 32", "Couch"]
    user = store.config.users[0]
    assert user.target is StreamTarget.ZIGBEE
    assert [(light.id, light.channels) for light in user.lights] == [("1", [0]), ("2", [1])]
    assert engine.changed == ["u1"]
    assert engine.restarted == ["u1"]

    tv = await (await client.put("/cfg/tvs/u1", json={"target": "hue"})).json()
    assert tv["target"] == "hue"
    assert tv["lights"] == ["c0", "c1"]
    # The lamp selection is kept for switching back.
    assert len(tv["zigbee_lights"]) == 2


async def test_assign_zigbee_rejects_invalid_values(aiohttp_client, web_setup) -> None:
    """An unknown target, too many lamps, or a lamp without an IEEE address is rejected."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-11-10"),
    ]
    client = await aiohttp_client(app)
    too_many = [{"ieee": f"00:17:88:01:00:00:00:{index:02x}"} for index in range(11)]
    assert (await client.put("/cfg/tvs/u1", json={"target": "wled"})).status == 400
    assert (await client.put("/cfg/tvs/u1", json={"zigbee_lights": too_many})).status == 400
    assert (await client.put("/cfg/tvs/u1", json={"zigbee_lights": [{"name": "x"}]})).status == 400
    assert (await client.put("/cfg/tvs/u1", json={"zigbee_lights": "lamp"})).status == 400
    user = store.config.users[0]
    assert user.target is StreamTarget.HUE
    assert user.zigbee_lights == []


_SIGNE = "00:17:88:01:00:00:00:0a"
_SPOT = "00:17:88:01:00:00:00:0b"
_STRIP = "00:17:88:01:00:00:00:0c"


async def test_assign_zigbee_lamps_reads_segments_of_new_lamps_only(
    aiohttp_client, web_setup, monkeypatch, tmp_path
) -> None:
    """Newly added lamps are asked for their gradient segments; selected lamps keep theirs."""
    _app, store = web_setup
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-11-10",
            target=StreamTarget.ZIGBEE,
            zigbee_lights=[ZigbeeLightRef(ieee=_SIGNE, name="Signe", segments=7)],
        ),
    ]
    # The Signe would answer 1 now: only its stored count may be used.
    reads = _patch_segment_counts(monkeypatch, {0x0A: 1, 0x0B: 1, 0x0C: 2})
    home_assistant = _FakeHomeAssistant(addresses={_SIGNE: 0x0A, _SPOT: 0x0B, _STRIP: 0x0C})
    client = await aiohttp_client(_zigbee_app(store, home_assistant))
    lamps = [
        {"ieee": _SIGNE, "name": "Signe"},
        {"ieee": _SPOT, "name": "Spot"},
        {"ieee": _STRIP, "name": "Strip"},
    ]
    tv = await (await client.put("/cfg/tvs/u1", json={"zigbee_lights": lamps})).json()
    assert reads == [[0x0B, 0x0C]]
    assert [lamp["zones"] for lamp in tv["zigbee_lights"]] == [3, 1, 2]
    assert tv["lights"] == [
        "Signe (zone 1)",
        "Signe (zone 2)",
        "Signe (zone 3)",
        "Spot",
        "Strip (zone 1)",
        "Strip (zone 2)",
    ]
    saved = ConfigStore(tmp_path / "config.yaml").load().users[0].zigbee_lights
    assert [lamp.segments for lamp in saved] == [7, 1, 2]

    # Removing and reordering lamps reads nothing and keeps the stored counts.
    tv = await (
        await client.put("/cfg/tvs/u1", json={"zigbee_lights": [lamps[2], lamps[0]]})
    ).json()
    assert reads == [[0x0B, 0x0C]]
    assert [lamp["zones"] for lamp in tv["zigbee_lights"]] == [2, 3]
    assert [lamp.segments for lamp in store.config.users[0].zigbee_lights] == [2, 7]


async def test_assign_zigbee_lamps_in_order_while_a_read_is_slow(
    aiohttp_client, web_setup, monkeypatch
) -> None:
    """A quick second lamp edit is applied after the first, not overtaken by it."""
    _app, store = web_setup
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-11-10",
            target=StreamTarget.ZIGBEE,
        ),
    ]
    delays = iter([0.3, 0.05])  # the first edit's read is slower than the second's

    async def slow_read(_url: str, nwks: Iterable[int]) -> dict[int, int]:
        await asyncio.sleep(next(delays))
        return dict.fromkeys(nwks, 1)

    monkeypatch.setattr("ambilight_hue_bridge.web.server.read_segment_counts", slow_read)
    home_assistant = _FakeHomeAssistant(addresses={_SIGNE: 0x0A, _SPOT: 0x0B})
    client = await aiohttp_client(_zigbee_app(store, home_assistant))
    # Tick the Signe, then the spot before the first answer arrived.
    first = asyncio.ensure_future(
        client.put("/cfg/tvs/u1", json={"zigbee_lights": [{"ieee": _SIGNE}]})
    )
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(
        client.put("/cfg/tvs/u1", json={"zigbee_lights": [{"ieee": _SIGNE}, {"ieee": _SPOT}]})
    )
    await asyncio.gather(first, second)
    assert [lamp.ieee for lamp in store.config.users[0].zigbee_lights] == [_SIGNE, _SPOT]


@pytest.mark.parametrize(
    ("home_assistant", "counts"),
    [
        pytest.param(
            _FakeHomeAssistant(error=HomeAssistantError("Home Assistant didn't answer in time.")),
            {0x0A: 7},
            id="home-assistant-error",
        ),
        pytest.param(
            _FakeHomeAssistant(addresses={_SIGNE: 0x0A}),
            ZigguratError("Cannot connect to Ziggurat"),
            id="ziggurat-unreachable",
        ),
        pytest.param(_FakeHomeAssistant(addresses={_SIGNE: 0x0A}), {}, id="no-answer"),
        pytest.param(_FakeHomeAssistant(), {0x0A: 7}, id="not-in-zha"),
    ],
)
async def test_assign_zigbee_lamp_stays_one_segment_when_reading_fails(
    aiohttp_client, web_setup, monkeypatch, caplog, home_assistant, counts
) -> None:
    """A new lamp whose segments cannot be read streams as one light; the PUT still succeeds."""
    _app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-11-10"),
    ]
    _patch_segment_counts(monkeypatch, counts)
    client = await aiohttp_client(_zigbee_app(store, home_assistant))
    with caplog.at_level(logging.WARNING):
        resp = await client.put(
            "/cfg/tvs/u1",
            json={"target": "zigbee", "zigbee_lights": [{"ieee": _SIGNE, "name": "Signe"}]},
        )
    assert resp.status == 200
    tv = await resp.json()
    assert tv["lights"] == ["Signe"]
    assert store.config.users[0].zigbee_lights[0].segments == 1
    assert any("re-add" in record.getMessage() for record in caplog.records)


async def test_home_assistant_token_is_write_only(aiohttp_client, web_setup, monkeypatch) -> None:
    """The Home Assistant token can be set and kept, but no response ever contains it."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app, store = web_setup
    client = await aiohttp_client(app)
    resp = await client.put(
        "/cfg/homeassistant", json={"url": "http://homeassistant.local:8123", "token": "s3cret"}
    )
    assert "s3cret" not in await resp.text()
    resp = await client.get("/cfg/homeassistant")
    assert "s3cret" not in await resp.text()
    assert await resp.json() == {
        "addon": False,
        "available": True,
        "url": "http://homeassistant.local:8123",
        "token_set": True,
    }
    assert store.config.home_assistant.token == "s3cret"

    # Re-sending the same URL keeps the token; a different URL drops it (the token is never
    # sent to a host it was not entered for); an empty URL disconnects.
    await client.put("/cfg/homeassistant", json={"url": "http://homeassistant.local:8123"})
    assert store.config.home_assistant.token == "s3cret"
    await client.put("/cfg/homeassistant", json={"url": "https://ha.example.com"})
    assert store.config.home_assistant.token == ""
    await client.put("/cfg/homeassistant", json={"url": "https://ha.example.com", "token": "new"})
    assert store.config.home_assistant.token == "new"
    data = await (await client.put("/cfg/homeassistant", json={"url": ""})).json()
    assert data["available"] is False
    assert store.config.home_assistant.token == ""

    resp = await client.put("/cfg/homeassistant", json={"url": "ftp://x"})
    assert resp.status == 400
    assert (await resp.json())["error"] == (
        "Enter the full address, starting with http:// or https://."
    )
