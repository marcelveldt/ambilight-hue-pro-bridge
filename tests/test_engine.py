"""Tests for the ingest buffer, channel mapping and the streaming engine."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import RealBridge, VirtualLight
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
    store.config.virtual_lights = [VirtualLight(id="1", name="Left", channels=[0])]
    store.config.real_bridges = [
        RealBridge(
            id="b",
            host="1.2.3.4",
            app_key="user",
            client_key="deadbeef",
            entertainment_area="area-1",
        ),
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
    engine.submit_color("1", (65535, 0, 0))
    assert await _wait_until(lambda: bool(created) and bool(created[0].frames))
    assert engine.is_streaming
    first_frame = created[0].frames[0]
    assert isinstance(first_frame, list)
    assert first_frame[0].channel_id == 0
    await engine.stop()
    assert not engine.is_streaming


async def test_engine_does_not_stream_without_credentials(tmp_path: Path) -> None:
    """With no configured bridge, submitting a color never starts a stream."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.virtual_lights = [VirtualLight(id="1", name="Left", channels=[0])]
    engine = Engine(store)
    engine.submit_color("1", (1, 2, 3))
    assert not await _wait_until(lambda: engine.is_streaming, max_wait=0.3)
