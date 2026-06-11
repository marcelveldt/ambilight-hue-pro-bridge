"""Tests for the v1 REST emulator using an aiohttp test client."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from ambilight_hue_bridge.config.models import PairedUser, VirtualLight
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.emulator.pairing import PairingManager
from ambilight_hue_bridge.emulator.rest_v1 import HueV1Emulator, log_requests

if TYPE_CHECKING:
    from pathlib import Path

_USERNAME_LEN = 32


def _build_emulator(tmp_path: Path) -> tuple[web.Application, ConfigStore]:
    """Build a v1 emulator app over a fresh temp config store."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    emulator = HueV1Emulator(
        store=store,
        pairing=PairingManager(store),
        host_ip="1.2.3.4",
        mac="aabbccddeeff",
        http_port=80,
    )
    app = web.Application(middlewares=[log_requests])
    emulator.register(app)
    return app, store


@pytest.fixture
def emulator_setup(tmp_path: Path) -> tuple[web.Application, ConfigStore]:
    """Build a v1 emulator app and expose its config store."""
    return _build_emulator(tmp_path)


def _two_lights() -> list[VirtualLight]:
    """Return the two virtual lights an assigned TV exposes in these tests."""
    return [VirtualLight(id="1", name="Left"), VirtualLight(id="2", name="Right")]


class _FakeEngine:
    """Engine stand-in recording the stream lifecycle and submitted colors."""

    def __init__(self) -> None:
        self.stream_owner: str | None = None
        self.is_streaming = False
        self.started: list[str] = []
        self.stopped = 0
        self.colors: list[tuple[str, str]] = []
        self.identified: list[tuple[str, str, bool]] = []

    def start_stream(self, owner: str) -> None:
        self.stream_owner = owner
        self.is_streaming = True
        self.started.append(owner)

    async def stop_stream(self) -> None:
        self.is_streaming = False
        self.stream_owner = None
        self.stopped += 1

    def submit_color(self, owner: str, light_id: str, _rgb: tuple[int, int, int]) -> None:
        self.colors.append((owner, light_id))

    def identify(self, owner: str, light_id: str, *, sustained: bool = False) -> None:
        self.identified.append((owner, light_id, sustained))

    def stop_identify(self, light_id: str) -> None:
        self.identified.append(("", light_id, False))


def _build_emulator_with_engine(tmp_path: Path) -> tuple[web.Application, ConfigStore, _FakeEngine]:
    """Build a v1 emulator wired to a fake engine, exposing the store and engine."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    engine = _FakeEngine()
    emulator = HueV1Emulator(
        store=store,
        pairing=PairingManager(store),
        host_ip="1.2.3.4",
        mac="aabbccddeeff",
        http_port=80,
        engine=engine,  # type: ignore[arg-type]
    )
    app = web.Application(middlewares=[log_requests])
    emulator.register(app)
    return app, store, engine


def _assigned_user(username: str) -> PairedUser:
    """Return a paired TV with two assigned lights."""
    return PairedUser(
        username=username,
        clientkey="k",
        devicetype="TV",
        created="2026-06-10",
        entertainment_area="area-1",
        lights=_two_lights(),
    )


async def test_public_config_identifies_as_bsb002(aiohttp_client, emulator_setup) -> None:
    """The unauthenticated short config identifies as a BSB002 bridge."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    resp = await client.get("/api/config")
    config = await resp.json()
    assert config["modelid"] == "BSB002"
    assert config["bridgeid"] == "AABBCCFFFEDDEEFF"


async def test_description_served(aiohttp_client, emulator_setup) -> None:
    """The descriptor is served over HTTP."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    resp = await client.get("/description.xml")
    assert "Philips hue bridge 2015" in await resp.text()


async def test_pairing_returns_credentials(aiohttp_client, emulator_setup) -> None:
    """Pushlink pairing returns a username and (when requested) a clientkey."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    resp = await client.post("/api", json={"devicetype": "unit#test", "generateclientkey": True})
    success = (await resp.json())[0]["success"]
    assert len(success["username"]) == _USERNAME_LEN
    assert len(success["clientkey"]) == _USERNAME_LEN


async def test_lights_require_authorization(aiohttp_client, emulator_setup) -> None:
    """An unknown username gets an unauthorized error."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    resp = await client.get("/api/nope/lights")
    assert (await resp.json())[0]["error"]["type"] == 1


async def test_nupnp_lists_this_bridge(aiohttp_client, emulator_setup) -> None:
    """The local N-UPnP endpoint returns this bridge's id and LAN address."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    for path in ("/api/nupnp", "/nupnp"):
        found = await (await client.get(path)).json()
        # The nupnp list-of-dicts shape (not the per-user datastore) proves /api/nupnp is
        # registered before the dynamic /api/{user} route and isn't captured as user="nupnp".
        assert found[0]["id"] == "aabbccfffeddeeff"
        assert found[0]["internalipaddress"] == "1.2.3.4"
        assert found[0]["port"] == 80


async def test_config_probe_returns_bridge_identity_for_unknown_user(
    aiohttp_client, emulator_setup
) -> None:
    """GET /api/<unknown>/config returns the public bridge config (IP-scan probe), not an error."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    config = await (await client.get("/api/nouser/config")).json()
    assert config["modelid"] == "BSB002"
    assert config["bridgeid"] == "AABBCCFFFEDDEEFF"
    # It must look like a bridge: swversion/apiversion/name are what clients check for.
    assert {"swversion", "apiversion", "name"} <= config.keys()
    assert "error" not in config


async def test_unassigned_tv_has_no_lights(aiohttp_client, emulator_setup) -> None:
    """A freshly paired TV exposes no lights until an entertainment area is assigned."""
    app, _store = emulator_setup
    client = await aiohttp_client(app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    assert await (await client.get(f"/api/{user}/lights")).json() == {}


async def test_paired_user_can_list_lights(aiohttp_client, emulator_setup) -> None:
    """After pairing and being assigned lights, the TV lists them with integer state."""
    app, store = emulator_setup
    client = await aiohttp_client(app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    store.config.users[-1].lights = _two_lights()
    lights = await (await client.get(f"/api/{user}/lights")).json()
    assert set(lights) == {"1", "2"}
    assert isinstance(lights["1"]["state"]["bri"], int)
    assert lights["1"]["capabilities"]["streaming"]["renderer"] is True


def test_ensure_entertainment_group_tracks_the_tv_group(tmp_path: Path) -> None:
    """A stream the TV activates is lazily tracked as a resolvable Entertainment group."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=_two_lights(),
        ),
    ]
    emulator = HueV1Emulator(
        store=store,
        pairing=PairingManager(store),
        host_ip="1.2.3.4",
        mac="aabbccddeeff",
        http_port=80,
    )
    lights = store.config.users[0].lights
    group = emulator._ensure_entertainment_group("200", lights)
    assert group["type"] == "Entertainment"
    assert group["lights"] == ["1", "2"]
    assert "stream" in group
    # Idempotent: a second activation resolves to the same tracked group, not a duplicate.
    assert emulator._ensure_entertainment_group("200", lights) is group


async def test_put_light_state_echoes_success(aiohttp_client, emulator_setup) -> None:
    """A light state PUT echoes a success entry per key."""
    app, store = emulator_setup
    client = await aiohttp_client(app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    store.config.users[-1].lights = _two_lights()
    resp = await client.put(f"/api/{user}/lights/1/state", json={"on": True, "bri": 100})
    keys = {next(iter(entry["success"])) for entry in await resp.json()}
    assert keys == {"/lights/1/state/on", "/lights/1/state/bri"}


async def test_malformed_light_state_does_not_500(aiohttp_client, emulator_setup) -> None:
    """Garbage state values from a flaky TV are coerced, not 500'd, and survive a later GET."""
    app, store = emulator_setup
    client = await aiohttp_client(app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    store.config.users[-1].lights = _two_lights()
    resp = await client.put(f"/api/{user}/lights/1/state", json={"bri": "oops", "xy": [0.1]})
    assert resp.status == 200
    # A later GET must not re-raise on the poisoned state - it was coerced on write.
    got = await (await client.get(f"/api/{user}/lights/1")).json()
    assert isinstance(got["state"]["bri"], int)
    assert isinstance(got["state"]["xy"], list)
    assert len(got["state"]["xy"]) == 2


async def test_create_group_accepts_trailing_slash(aiohttp_client, emulator_setup) -> None:
    """Newer TVs POST to /groups/ with a trailing slash; it must create a group, not 404."""
    app, store = emulator_setup
    client = await aiohttp_client(app)
    pair = await (await client.post("/api", json={"devicetype": "unit"})).json()
    user = pair[0]["success"]["username"]
    store.config.users[-1].lights = _two_lights()
    resp = await client.post(
        f"/api/{user}/groups/",
        json={"lights": ["1", "2"], "type": "Entertainment", "class": "TV"},
    )
    assert resp.status == 200
    assert "id" in (await resp.json())[0]["success"]


async def test_single_stream_guard_denies_a_second_tv(aiohttp_client, tmp_path) -> None:
    """While one TV streams, a second is denied (307); the same owner may re-activate."""
    app, store, engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1"), _assigned_user("u2")]
    client = await aiohttp_client(app)

    ok = await (await client.put("/api/u1/groups/200", json={"stream": {"active": True}})).json()
    assert ok[0]["success"]["/groups/200/stream/active"] is True
    assert engine.started == ["u1"]

    denied = await (
        await client.put("/api/u2/groups/200", json={"stream": {"active": True}})
    ).json()
    assert denied[0]["error"]["type"] == 307

    # The same owner re-activating is not denied.
    again = await (await client.put("/api/u1/groups/200", json={"stream": {"active": True}})).json()
    assert again[0]["success"]["/groups/200/stream/active"] is True


async def test_deactivate_keeps_the_stream_warm(aiohttp_client, tmp_path) -> None:
    """A stream active=false does NOT tear down the outbound stream (it idle-times-out instead).

    The TV toggles the stream rapidly in its configure menu; a full reconnect per toggle thrashes
    the real bridge, so we keep it warm and let the engine idle it.
    """
    app, store, engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1")]
    client = await aiohttp_client(app)
    await client.put("/api/u1/groups/200", json={"stream": {"active": True}})
    await client.put("/api/u1/groups/200", json={"stream": {"active": False}})
    assert engine.started == ["u1"]
    assert engine.stopped == 0  # not torn down on deactivate


async def test_non_dict_stream_body_does_not_500(aiohttp_client, tmp_path) -> None:
    """A malformed stream value (not an object) is ignored, not crashed on."""
    app, store, _engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1")]
    client = await aiohttp_client(app)
    resp = await client.put("/api/u1/groups/200", json={"stream": "on"})
    assert resp.status == 200


async def test_light_state_alert_triggers_identify(aiohttp_client, tmp_path) -> None:
    """An alert=lselect on a light asks the engine to identify (blink) it."""
    app, store, engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1")]
    client = await aiohttp_client(app)
    await client.put("/api/u1/lights/1/state", json={"alert": "lselect"})
    assert engine.identified == [("u1", "1", True)]


async def test_delete_group(aiohttp_client, tmp_path) -> None:
    """A group can be deleted (TVs do this to re-configure entertainment)."""
    app, store, _engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1")]
    client = await aiohttp_client(app)
    await client.post("/api/u1/groups/", json={"lights": ["1", "2"], "type": "Entertainment"})
    resp = await client.delete("/api/u1/groups/1")
    assert resp.status == 200
    assert "deleted" in (await resp.json())[0]["success"]
    groups = await (await client.get("/api/u1/groups")).json()
    assert "1" not in groups


async def test_group_action_fans_out_to_engine(aiohttp_client, tmp_path) -> None:
    """A group-0 action pushes every one of the TV's lights to the engine under its owner."""
    app, store, engine = _build_emulator_with_engine(tmp_path)
    store.config.users = [_assigned_user("u1")]
    client = await aiohttp_client(app)
    resp = await client.put("/api/u1/groups/0/action", json={"on": True, "bri": 200})
    keys = {next(iter(entry["success"])) for entry in await resp.json()}
    assert keys == {"/groups/0/action/on", "/groups/0/action/bri"}
    assert {light_id for _owner, light_id in engine.colors} == {"1", "2"}
    assert all(owner == "u1" for owner, _light_id in engine.colors)
