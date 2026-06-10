"""Tests for the ingest buffer, channel mapping and the streaming engine."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import PairedUser, RealBridge, VirtualLight
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.engine.engine import Engine
from ambilight_hue_bridge.engine.ingest import ColorBuffer
from ambilight_hue_bridge.engine.mapping import map_to_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_buffer_latest_wins() -> None:
    """The buffer keeps the latest color per light and defaults to black."""
    buffer = ColorBuffer()
    assert buffer.get_color("1") == (0, 0, 0)
    buffer.set_color("1", (10, 20, 30))
    buffer.set_color("1", (40, 50, 60))
    assert buffer.get_color("1") == (40, 50, 60)


def test_mapping_fans_out_to_channels() -> None:
    """Each virtual light paints all of its mapped channels with its color."""
    buffer = ColorBuffer()
    buffer.set_color("1", (100, 0, 0))
    buffer.set_color("2", (0, 200, 0))
    lights = [
        VirtualLight(id="1", name="Left", channels=[0, 1]),
        VirtualLight(id="2", name="Right", channels=[2]),
        VirtualLight(id="3", name="Unmapped"),
    ]
    commands = map_to_commands(lights, buffer)
    assert len(commands) == 3
    by_channel = {command.channel_id: command for command in commands}
    assert by_channel[0].red == 100
    assert by_channel[1].red == 100
    assert by_channel[2].green == 200


class _FakeSession:
    """Stand-in for EntertainmentSession that records sent frames (no real I/O)."""

    def __init__(self, host: str, app_key: str, client_key: str, *, idle_timeout: float) -> None:
        """Record the construction args and start disconnected."""
        self.host = host
        self.app_key = app_key
        self.client_key = client_key
        self.idle_timeout = idle_timeout
        self.connected = False
        self.frames: list[object] = []

    @property
    def is_streaming(self) -> bool:
        """Return whether the fake stream is active."""
        return self.connected

    async def start(self, area_id: str) -> None:
        """Mark the stream active."""
        assert area_id
        self.connected = True

    def send(self, commands: object) -> None:
        """Record a frame."""
        self.frames.append(commands)

    async def aclose(self) -> None:
        """Mark the stream stopped."""
        self.connected = False


def _configured_store(tmp_path: Path) -> ConfigStore:
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="tv1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="Left", channels=[0])],
        ),
    ]
    store.config.real_bridges = [
        RealBridge(id="b", host="1.2.3.4", app_key="user", client_key="deadbeef"),
    ]
    store.config.active_real_bridge = "b"
    return store


async def _wait_until(predicate: Callable[[], bool], max_wait: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_engine_streams_on_submit(tmp_path: Path, monkeypatch) -> None:
    """Submitting a color lazily starts the stream and sends mapped frames."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.IDLE_TIMEOUT_S", 0.3)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: bool(created) and bool(created[0].frames))
    assert engine.is_streaming
    assert engine.stream_owner == "tv1"
    first_frame = created[0].frames[0]
    assert isinstance(first_frame, list)
    assert first_frame[0].channel_id == 0
    await engine.stop()
    assert not engine.is_streaming


def test_smoothing_eases_toward_target(tmp_path: Path) -> None:
    """Smoothing jumps to the target on first sight, then eases toward later targets."""
    store = _configured_store(tmp_path)
    store.config.virtual_bridge.stream_smoothing = 0.5
    engine = Engine(store)
    lights = store.config.users[0].lights
    engine._buffer.set_color("1", (100, 200, 40))
    engine._apply_smoothing(lights)
    # First sight: straight to the target (no fade-from-black flash).
    assert engine._smoothed_buffer.get_color("1") == (100, 200, 40)
    engine._buffer.set_color("1", (0, 0, 0))
    engine._apply_smoothing(lights)
    # Halfway eased toward the new target with smoothing 0.5.
    assert engine._smoothed_buffer.get_color("1") == (50, 100, 20)


def test_smoothing_off_is_instant(tmp_path: Path) -> None:
    """With smoothing 0.0 the streamed color tracks the target exactly each tick."""
    store = _configured_store(tmp_path)
    store.config.virtual_bridge.stream_smoothing = 0.0
    engine = Engine(store)
    lights = store.config.users[0].lights
    engine._buffer.set_color("1", (100, 200, 40))
    engine._apply_smoothing(lights)
    engine._buffer.set_color("1", (10, 20, 30))
    engine._apply_smoothing(lights)
    assert engine._smoothed_buffer.get_color("1") == (10, 20, 30)


async def test_engine_does_not_stream_without_credentials(tmp_path: Path) -> None:
    """With no configured bridge, submitting a color never starts a stream."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="tv1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="Left", channels=[0])],
        ),
    ]
    engine = Engine(store)
    engine.submit_color("tv1", "1", (1, 2, 3))
    assert not await _wait_until(lambda: engine.is_streaming, max_wait=0.3)


async def test_engine_does_not_stream_for_unassigned_tv(tmp_path: Path) -> None:
    """A paired TV with no entertainment area assigned never starts a stream."""
    store = _configured_store(tmp_path)
    store.config.users[0].entertainment_area = ""
    store.config.users[0].lights = []
    engine = Engine(store)
    engine.submit_color("tv1", "1", (1, 2, 3))
    assert not await _wait_until(lambda: engine.is_streaming, max_wait=0.3)


def test_submit_color_adopts_owner_only_once(tmp_path: Path) -> None:
    """submit_color adopts the first real owner and never lets a later TV steal it."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="tv1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="Left", channels=[0])],
        ),
    ]
    # No real bridge configured => no stream starts, so ownership is asserted in isolation.
    engine = Engine(store)
    engine.submit_color(None, "1", (1, 2, 3))
    assert engine.stream_owner is None
    engine.submit_color("tv1", "1", (1, 2, 3))
    assert engine.stream_owner == "tv1"
    engine.submit_color("tv2", "1", (1, 2, 3))
    assert engine.stream_owner == "tv1"


async def test_start_stream_sets_owner_then_clears_on_stop(tmp_path: Path, monkeypatch) -> None:
    """start_stream opens the stream as the given owner; stop clears it."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.start_stream("tv1")
    assert engine.stream_owner == "tv1"
    assert await _wait_until(lambda: engine.is_streaming)
    await engine.stop()
    assert engine.stream_owner is None
