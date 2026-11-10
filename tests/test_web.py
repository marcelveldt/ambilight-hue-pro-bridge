"""Tests for the web configuration UI API."""

from __future__ import annotations

import logging
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


async def test_tv_smoothing_defaults_to_off(aiohttp_client, web_setup) -> None:
    """A TV with no smoothing set reports 0 (off) — there is no global default."""
    app, store = web_setup
    store.config.users = [
        PairedUser(username="u1", clientkey="k", devicetype="TV", created="2026-06-11"),
    ]
    client = await aiohttp_client(app)
    tvs = await (await client.get("/cfg/tvs")).json()
    assert tvs[0]["stream_smoothing"] == 0.0


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
    names = [light.name for light in lights_from_area(area, split_gradients=True)]
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
    tv = await (
        await client.put(
            "/cfg/tvs/u1", json={"entertainment_area": "area-1", "split_gradients": True}
        )
    ).json()
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
    names = [light.name for light in lights_from_area(area, split_gradients=True)]
    assert all(len(n) <= 32 for n in names)
    assert names[0].endswith("(far left)")
    assert names[1].endswith("(far right)")


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
    assert len(lights_from_area(area, split_gradients=True)) == 2
    merged = lights_from_area(area, split_gradients=False)
    assert len(merged) == 1
    assert merged[0].channels == [0, 1]


def test_zigbee_channels_split_gradient_lamps_into_zones() -> None:
    """With split on a gradient lamp gets up to 3 consecutive channels, any other lamp one."""
    lamps = [
        ZigbeeLightRef(ieee="a", segments=7),
        ZigbeeLightRef(ieee="b"),
        ZigbeeLightRef(ieee="c", segments=2),
    ]
    split = zigbee_channels(lamps, split_gradients=True)
    assert [lamp for lamp, _channels in split] == lamps
    assert [list(channels) for _lamp, channels in split] == [[0, 1, 2], [3], [4, 5]]
    merged = zigbee_channels(lamps, split_gradients=False)
    assert [list(channels) for _lamp, channels in merged] == [[0], [1], [2]]


def test_lights_from_zigbee_one_light_per_zone() -> None:
    """Zone lights follow the lamps' channels, named after the lamp and zone within 32 chars."""
    lamps = [
        ZigbeeLightRef(ieee="a", name="Signe floor lamp right of the TV", segments=7),
        ZigbeeLightRef(ieee="b", name="Signe left", segments=7),
        ZigbeeLightRef(ieee="c", name="Spot"),
        ZigbeeLightRef(ieee="d", segments=5),
    ]
    lights = lights_from_zigbee(lamps, split_gradients=True)
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
    merged = lights_from_zigbee(lamps, split_gradients=False)
    assert [(light.id, light.name, light.channels) for light in merged] == [
        ("1", "Signe floor lamp right of the TV", [0]),
        ("2", "Signe left", [1]),
        ("3", "Spot", [2]),
        ("4", "Light 4", [3]),
    ]


async def test_index_is_served(aiohttp_client, web_setup) -> None:
    """The single-page UI is served at the root with a root <base> for direct access."""
    app, _store = web_setup
    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "Ambilight" in body
    assert "Stream priority" in body
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
    error = HomeAssistantError("Home Assistant is not connected; set its URL and access token")
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

    # Removing and reordering lamps reads nothing; splitting off keeps the stored counts.
    tv = await (
        await client.put("/cfg/tvs/u1", json={"zigbee_lights": [lamps[2], lamps[0]]})
    ).json()
    assert reads == [[0x0B, 0x0C]]
    assert [lamp["zones"] for lamp in tv["zigbee_lights"]] == [2, 3]
    tv = await (await client.put("/cfg/tvs/u1", json={"split_gradients": False})).json()
    assert tv["lights"] == ["Strip", "Signe"]
    assert [lamp["zones"] for lamp in tv["zigbee_lights"]] == [1, 1]
    assert [lamp.segments for lamp in store.config.users[0].zigbee_lights] == [2, 7]


@pytest.mark.parametrize(
    ("home_assistant", "counts"),
    [
        pytest.param(
            _FakeHomeAssistant(error=HomeAssistantError("Home Assistant did not respond in time")),
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

    assert (await client.put("/cfg/homeassistant", json={"url": "ftp://x"})).status == 400
