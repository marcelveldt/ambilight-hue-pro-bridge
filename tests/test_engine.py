"""Tests for the ingest buffer, channel mapping and the streaming engine."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
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
        # Bridge-reported (status, active_streamer_rid) for the health monitor; healthy + ours.
        self.status: tuple[str, str] = ("active", "us")

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

    async def remote_status(self) -> tuple[str, str]:
        """Return the bridge's view of the stream (drives the engine health monitor)."""
        return self.status

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
    store.config.users[0].stream_smoothing = 0.5
    engine = Engine(store)
    engine._stream_owner = "tv1"  # resolve smoothing from this TV's per-TV setting
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
    """With smoothing 0.0 (a TV's default) the streamed color tracks the target exactly."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_smoothing = 0.0
    engine = Engine(store)
    engine._stream_owner = "tv1"
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


def test_apply_identify_blinks_then_expires(tmp_path: Path) -> None:
    """An identified light is forced full-white/off, and the entry expires after its window."""
    store = _configured_store(tmp_path)
    engine = Engine(store)
    engine._identify["1"] = time.monotonic() + 5.0
    engine._apply_identify()
    assert engine._smoothed_buffer.get_color("1") in {(65535, 65535, 65535), (0, 0, 0)}
    assert "1" in engine._identify
    engine._identify["1"] = time.monotonic() - 1.0  # already expired
    engine._apply_identify()
    assert "1" not in engine._identify


async def test_identify_opens_a_stream(tmp_path: Path, monkeypatch) -> None:
    """identify() adopts the owner and opens the outbound stream so the blink is visible."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.identify("tv1", "1")
    assert "1" in engine._identify
    assert engine.stream_owner == "tv1"
    assert await _wait_until(lambda: engine.is_streaming)
    await engine.stop()


def test_resolve_smoothing_is_per_tv_else_off(tmp_path: Path) -> None:
    """Smoothing resolves to the owning TV's value, defaulting to 0 (off) when unset."""
    store = _configured_store(tmp_path)
    engine = Engine(store)
    assert engine._resolve_smoothing() == 0.0  # no owner -> off
    engine._stream_owner = "tv1"
    assert engine._resolve_smoothing() == 0.0  # tv1 has no value set -> off
    store.config.users[0].stream_smoothing = 0.6
    assert engine._resolve_smoothing() == 0.6  # the TV's own setting


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


async def test_stop_during_startup_discards_the_stream(tmp_path: Path, monkeypatch) -> None:
    """A stop while the (slow, uncancellable) connect is in flight discards the session."""
    gate = asyncio.Event()
    closed: list[bool] = []

    class _SlowSession(_FakeSession):
        """A session whose connect blocks and swallows cancellation (mimics the real DTLS)."""

        async def start(self, area_id: str) -> None:
            assert area_id
            # Swallow cancellation to mimic the real lib's non-cancellable DTLS connect.
            with suppress(asyncio.CancelledError):
                await gate.wait()
            self.connected = True

        async def aclose(self) -> None:
            await super().aclose()
            closed.append(True)

    def factory(*args: object, **kwargs: object) -> _SlowSession:
        return _SlowSession(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.start_stream("tv1")
    await asyncio.sleep(0.02)  # let _start_stream block in session.start
    await engine.stop()  # bumps the generation, then awaits the in-flight connect
    gate.set()
    await asyncio.sleep(0.02)
    assert not engine.is_streaming
    assert closed  # the racing session was closed, not left as a zombie


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


async def test_health_recovers_on_silent_bridge_drop(tmp_path: Path, monkeypatch) -> None:
    """When the bridge silently ends the session, the health monitor tears the stream down."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    # The bridge silently ends the session (status goes inactive) - sends would keep "succeeding".
    created[0].status = ("inactive", "")
    assert await _wait_until(lambda: not engine.is_streaming)
    # No re-grab backoff for a plain drop, so the next frame re-establishes it.
    assert engine._regrab_block_until == 0.0
    await engine.stop()


async def test_health_backs_off_on_takeover(tmp_path: Path, monkeypatch) -> None:
    """A foreign controller taking the area over makes the engine back off, not fight it."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    # Let the first poll capture our own streamer id before a different controller appears.
    assert await _wait_until(lambda: engine._our_streamer_rid == "us")
    created[0].status = ("active", "other-app")
    assert await _wait_until(lambda: not engine.is_streaming)
    assert engine._regrab_block_until > time.monotonic()
    # Within the backoff window a new frame must NOT re-grab the stream.
    engine.submit_color("tv1", "1", (0, 65535, 0))
    assert not await _wait_until(lambda: engine.is_streaming, max_wait=0.2)
    await engine.stop()


async def test_health_recovers_when_streamer_cleared(tmp_path: Path, monkeypatch) -> None:
    """Status stays 'active' but the bridge clears our streamer (rid '') - treated as a drop."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    assert await _wait_until(lambda: engine._our_streamer_rid == "us")
    # Bridge keeps the config active but no longer names a streamer: our stream was dropped.
    created[0].status = ("active", "")
    assert await _wait_until(lambda: not engine.is_streaming)
    assert engine._regrab_block_until == 0.0  # a drop, not a takeover - re-grab is allowed
    await engine.stop()


async def test_stop_during_cancellable_connect_closes_session(tmp_path: Path, monkeypatch) -> None:
    """A stop whose cancel lands on a cancellable await in start() still closes the session."""
    gate = asyncio.Event()
    closed: list[bool] = []

    class _PropagatingSession(_FakeSession):
        """Models the real lib activating the area on a pre-DTLS await, then propagating cancel."""

        async def start(self, area_id: str) -> None:
            assert area_id
            await gate.wait()  # cancellable; on stop() the CancelledError propagates out
            self.connected = True

        async def aclose(self) -> None:
            await super().aclose()
            closed.append(True)

    def factory(*args: object, **kwargs: object) -> _PropagatingSession:
        return _PropagatingSession(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.start_stream("tv1")
    await asyncio.sleep(0.02)  # let _start_stream block in session.start
    await engine.stop()
    assert not engine.is_streaming
    assert closed  # the orphaned mid-connect session was closed, not leaked
