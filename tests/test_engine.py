"""Tests for the ingest buffer, channel mapping and the streaming engine."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import (
    PairedUser,
    RealBridge,
    StreamTarget,
    VirtualLight,
    ZigbeeLightRef,
)
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.engine.engine import Engine, _resource_key
from ambilight_hue_bridge.engine.ingest import ColorBuffer
from ambilight_hue_bridge.engine.mapping import map_to_commands
from ambilight_hue_bridge.outbound.backend import BackendHealth, HueBridgeBackend, ZigbeeBackend
from ambilight_hue_bridge.outbound.controller import lights_from_zigbee

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ambilight_hue_bridge.engine.engine import _StreamSlot


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


def _add_tv(
    store: ConfigStore,
    username: str,
    *,
    priority: int,
    enabled: bool = True,
) -> PairedUser:
    """Add an assigned TV to a configured test store."""
    user = PairedUser(
        username=username,
        clientkey="k",
        devicetype="TV",
        created="2026-06-10",
        enabled=enabled,
        stream_priority=priority,
        entertainment_area=f"area-{username}",
        lights=[VirtualLight(id="1", name=username, channels=[len(store.config.users)])],
    )
    store.config.users.append(user)
    return user


def _slot(engine: Engine, owner: str = "tv1") -> _StreamSlot:
    """Return the stream slot of a TV's output (created on first use)."""
    slot = engine._slot_for(owner)
    assert slot is not None
    return slot


def _captured_rid(engine: Engine) -> str:
    """Return the streamer rid the active Hue bridge backend captured ('' when none)."""
    backend = _slot(engine).backend
    return backend._our_streamer_rid if isinstance(backend, HueBridgeBackend) else ""


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

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine.IDLE_TIMEOUT_S", 0.3)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: bool(created) and bool(created[0].frames))
    assert engine.is_streaming
    assert engine.stream_owners == {"tv1"}
    first_frame = created[0].frames[0]
    assert isinstance(first_frame, list)
    assert first_frame[0].channel_id == 0
    assert await _wait_until(lambda: _slot(engine).transition_task is None)
    engine.submit_color("tv1", "1", (0, 65535, 0))
    assert _slot(engine).transition_task is None
    await engine.stop()
    assert not engine.is_streaming


def test_smoothing_eases_toward_target(tmp_path: Path) -> None:
    """Smoothing jumps to the target on first sight, then eases toward later targets."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_smoothing = 0.5
    engine = Engine(store)
    slot = _slot(engine)
    smoothing = engine._resolve_smoothing("tv1")  # resolve smoothing from this TV's per-TV setting
    lights = store.config.users[0].lights
    slot.buffer.set_color("1", (100, 200, 40))
    slot.apply_smoothing(lights, smoothing)
    # First sight: straight to the target (no fade-from-black flash).
    assert slot.smoothed_buffer.get_color("1") == (100, 200, 40)
    slot.buffer.set_color("1", (0, 0, 0))
    slot.apply_smoothing(lights, smoothing)
    # Halfway eased toward the new target with smoothing 0.5.
    assert slot.smoothed_buffer.get_color("1") == (50, 100, 20)


def test_smoothing_off_is_instant(tmp_path: Path) -> None:
    """With smoothing 0.0 (a TV's default) the streamed color tracks the target exactly."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_smoothing = 0.0
    engine = Engine(store)
    slot = _slot(engine)
    smoothing = engine._resolve_smoothing("tv1")
    lights = store.config.users[0].lights
    slot.buffer.set_color("1", (100, 200, 40))
    slot.apply_smoothing(lights, smoothing)
    slot.buffer.set_color("1", (10, 20, 30))
    slot.apply_smoothing(lights, smoothing)
    assert slot.smoothed_buffer.get_color("1") == (10, 20, 30)


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
    slot = _slot(engine)
    engine._identify["tv1"] = {"1": time.monotonic() + 5.0}
    engine._apply_identify(slot, "tv1")
    assert slot.smoothed_buffer.get_color("1") in {(65535, 65535, 65535), (0, 0, 0)}
    assert "1" in engine._identify["tv1"]
    engine._identify["tv1"]["1"] = time.monotonic() - 1.0  # already expired
    engine._apply_identify(slot, "tv1")
    assert "tv1" not in engine._identify


async def test_identify_opens_a_stream(tmp_path: Path, monkeypatch) -> None:
    """identify() adopts the owner and opens the outbound stream so the blink is visible."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.identify("tv1", "1")
    assert "1" in engine._identify["tv1"]
    assert await _wait_until(lambda: engine.is_streaming)
    assert engine.stream_owners == {"tv1"}
    await engine.stop()


def test_resolve_smoothing_is_per_tv_else_off(tmp_path: Path) -> None:
    """Smoothing resolves to the owning TV's value, defaulting to 0 (off) when unset."""
    store = _configured_store(tmp_path)
    engine = Engine(store)
    assert engine._resolve_smoothing("unknown") == 0.0  # no such TV -> off
    assert engine._resolve_smoothing("tv1") == 0.0  # tv1 has no value set -> off
    store.config.users[0].stream_smoothing = 0.6
    assert engine._resolve_smoothing("tv1") == 0.6  # the TV's own setting


def test_submit_color_keeps_tv_buffers_isolated(tmp_path: Path) -> None:
    """Colors from a waiting TV cannot overwrite the active TV's same-numbered lights."""
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
        PairedUser(
            username="tv2",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-2",
            lights=[VirtualLight(id="1", name="Right", channels=[1])],
        ),
    ]
    engine = Engine(store)
    engine.submit_color("tv1", "1", (1, 2, 3))
    engine.submit_color("tv2", "1", (4, 5, 6))
    assert engine._owner_buffers["tv1"].get_color("1") == (1, 2, 3)
    assert engine._owner_buffers["tv2"].get_color("1") == (4, 5, 6)


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

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
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

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming)
    assert engine.stream_owners == {"tv1"}
    await engine.stop()
    assert not engine.stream_owners


async def test_higher_priority_tv_preempts_active_stream(tmp_path: Path, monkeypatch) -> None:
    """A higher-priority request closes the old area before opening the new TV's area."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_priority = 1
    _add_tv(store, "tv2", priority=10)
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(store)
    assert engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming and engine.stream_owners == {"tv1"})

    assert engine.start_stream("tv2")
    assert await _wait_until(
        lambda: len(created) == 2 and engine.is_streaming and engine.stream_owners == {"tv2"}
    )
    assert created[0].connected is False
    await engine.stop()


async def test_equal_priority_tv_does_not_preempt_current_owner(
    tmp_path: Path, monkeypatch
) -> None:
    """The current owner keeps the stream when another TV has the same priority."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_priority = 5
    _add_tv(store, "tv2", priority=5)
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(store)
    assert engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming)

    assert engine.start_stream("tv2") is False
    await asyncio.sleep(0.05)
    assert engine.stream_owners == {"tv1"}
    assert len(created) == 1
    await engine.stop()


async def test_enabling_pending_tv_starts_its_stream(tmp_path: Path, monkeypatch) -> None:
    """A disabled TV's accepted activation starts outbound when REST enables that TV."""
    store = _configured_store(tmp_path)
    store.config.users[0].enabled = False
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(store)
    assert engine.start_stream("tv1")
    await asyncio.sleep(0.05)
    assert not created
    assert not engine.stream_owners

    store.config.users[0].enabled = True
    engine.tv_settings_changed("tv1")
    assert await _wait_until(lambda: engine.is_streaming and engine.stream_owners == {"tv1"})
    await engine.stop()


async def test_disabling_owner_hands_stream_to_pending_tv(tmp_path: Path, monkeypatch) -> None:
    """Disabling the current TV stops it and starts the best pending eligible TV."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_priority = 10
    waiting = _add_tv(store, "tv2", priority=1)
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(store)
    assert engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming and engine.stream_owners == {"tv1"})
    assert engine.start_stream("tv2") is False

    store.config.users[0].enabled = False
    engine.tv_settings_changed("tv1")
    assert await _wait_until(
        lambda: (
            len(created) == 2 and engine.is_streaming and engine.stream_owners == {waiting.username}
        )
    )
    assert created[0].connected is False
    await engine.stop()


async def test_enabling_lower_priority_tv_does_not_preempt(tmp_path: Path, monkeypatch) -> None:
    """A pending TV enabled through REST waits while a higher-priority TV is streaming."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_priority = 10
    waiting = _add_tv(store, "tv2", priority=1, enabled=False)
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(store)
    assert engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming)
    assert engine.start_stream("tv2")

    waiting.enabled = True
    engine.tv_settings_changed("tv2")
    await asyncio.sleep(0.05)
    assert engine.stream_owners == {"tv1"}
    assert len(created) == 1
    await engine.stop()


async def test_health_recovers_on_silent_bridge_drop(tmp_path: Path, monkeypatch) -> None:
    """When the bridge silently ends the session, the health monitor tears the stream down."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    # The bridge silently ends the session (status goes inactive) - sends would keep "succeeding".
    created[0].status = ("inactive", "")
    assert await _wait_until(lambda: not engine.is_streaming)
    # No re-grab backoff for a plain drop, so the next frame re-establishes it.
    assert _slot(engine).regrab_block_until == 0.0
    await engine.stop()


async def test_health_backs_off_on_takeover(tmp_path: Path, monkeypatch) -> None:
    """A foreign controller taking the area over makes the engine back off, not fight it."""
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    # Let the first poll capture our own streamer id before a different controller appears.
    assert await _wait_until(lambda: _captured_rid(engine) == "us")
    created[0].status = ("active", "other-app")
    assert await _wait_until(lambda: not engine.is_streaming)
    assert _slot(engine).regrab_block_until > time.monotonic()
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

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)

    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    assert await _wait_until(lambda: _captured_rid(engine) == "us")
    # Bridge keeps the config active but no longer names a streamer: our stream was dropped.
    created[0].status = ("active", "")
    assert await _wait_until(lambda: not engine.is_streaming)
    assert _slot(engine).regrab_block_until == 0.0  # a drop, not a takeover - re-grab is allowed
    await engine.stop()


class _ScriptedBackend:
    """Output backend whose health checks follow a script of results and errors to raise."""

    def __init__(self, script: list[BackendHealth | Exception]) -> None:
        """Store the script; once it runs out every check reports OK."""
        self.script = list(script)
        self.polls = 0
        self.streaming = False

    @property
    def is_streaming(self) -> bool:
        """Return whether the fake stream is active."""
        return self.streaming

    async def start(self) -> None:
        """Mark the stream active."""
        self.streaming = True

    def send(self, commands: object) -> None:
        """Ignore frames."""

    async def check_health(self) -> BackendHealth:
        """Return (or raise) the next scripted health result."""
        self.polls += 1
        if not self.script:
            return BackendHealth.OK
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self) -> None:
        """Mark the stream stopped."""
        self.streaming = False


async def _stream_with_health(
    tmp_path: Path, monkeypatch, script: list[BackendHealth | Exception]
) -> tuple[Engine, _ScriptedBackend]:
    backend = _ScriptedBackend(script)
    monkeypatch.setattr(Engine, "_create_backend", lambda _self, _user: (backend, "scripted"))
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)
    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    return engine, backend


async def test_health_failed_check_between_misses_still_tears_down(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed health check is skipped: it neither counts as a miss nor resets the count."""
    engine, backend = await _stream_with_health(
        tmp_path, monkeypatch, [BackendHealth.LOST, OSError("timeout"), BackendHealth.LOST]
    )
    assert await _wait_until(lambda: not engine.is_streaming)
    assert backend.polls == 3
    await engine.stop()


async def test_health_ok_between_misses_keeps_streaming(tmp_path: Path, monkeypatch) -> None:
    """An OK check resets the miss count, so isolated misses never tear the stream down."""
    engine, backend = await _stream_with_health(
        tmp_path,
        monkeypatch,
        [BackendHealth.LOST, BackendHealth.OK, BackendHealth.LOST, OSError("timeout")],
    )
    assert await _wait_until(lambda: backend.polls >= 6)
    assert engine.is_streaming
    await engine.stop()


class _SlowBackend(_ScriptedBackend):
    """Healthy backend whose start and close take a while, like a real one."""

    def __init__(self) -> None:
        super().__init__([])
        self.frames = 0
        self.closed = False
        self.started_at = 0.0
        self.closed_at = 0.0

    async def start(self) -> None:
        await asyncio.sleep(0.1)
        self.streaming = True
        self.started_at = time.monotonic()

    def send(self, _commands: object) -> None:
        self.frames += 1

    async def aclose(self) -> None:
        self.streaming = False
        await asyncio.sleep(0.2)
        self.closed = True
        self.closed_at = time.monotonic()


async def test_lost_stream_restarts_while_the_tv_keeps_sending(tmp_path: Path, monkeypatch) -> None:
    """A stream that dies under a busy TV is replaced cleanly: one owner, frames flowing."""
    backends: list[_SlowBackend] = []

    def create(_self: Engine, _user: object) -> tuple[_SlowBackend, str]:
        backends.append(_SlowBackend())
        return backends[-1], "slow"

    monkeypatch.setattr(Engine, "_create_backend", create)
    engine = Engine(_configured_store(tmp_path), rate_hz=50)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    backends[0].streaming = False  # the transport dropped
    for _ in range(40):
        engine.submit_color("tv1", "1", (65535, 0, 0))
        await asyncio.sleep(0.02)
    assert engine.stream_owners == {"tv1"}
    assert engine.is_streaming
    live = [backend for backend in backends if backend.streaming]
    assert len(live) == 1
    assert backends[0].closed
    frames = live[0].frames
    await asyncio.sleep(0.1)
    assert live[0].frames > frames
    await engine.stop_stream("tv1")
    assert not engine.is_streaming
    assert live[0].closed


async def test_restart_stream_reopens_the_active_stream(tmp_path: Path, monkeypatch) -> None:
    """restart_stream() replaces the owner's backend so new stream settings apply now."""
    backends: list[_SlowBackend] = []

    def create(_self: Engine, _user: object) -> tuple[_SlowBackend, str]:
        backends.append(_SlowBackend())
        return backends[-1], "slow"

    monkeypatch.setattr(Engine, "_create_backend", create)
    engine = Engine(_configured_store(tmp_path), rate_hz=50)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    restart = asyncio.create_task(engine.restart_stream("tv1"))
    # The TV keeps sending while the old backend closes: no replacement may start before that.
    for _ in range(20):
        engine.submit_color("tv1", "1", (65535, 0, 0))
        await asyncio.sleep(0.02)
    await restart
    assert await _wait_until(lambda: len(backends) == 2 and engine.is_streaming)
    assert backends[0].closed
    assert backends[1].started_at >= backends[0].closed_at
    assert engine.stream_owners == {"tv1"}
    await engine.restart_stream("someone-else")  # not the owner: nothing happens
    assert len(backends) == 2
    await engine.stop()


async def test_stop_during_a_restart_lets_the_close_finish(tmp_path: Path, monkeypatch) -> None:
    """Shutting down while a restart closes the old backend still lets that close complete."""
    backends: list[_SlowBackend] = []

    def create(_self: Engine, _user: object) -> tuple[_SlowBackend, str]:
        backends.append(_SlowBackend())
        return backends[-1], "slow"

    monkeypatch.setattr(Engine, "_create_backend", create)
    engine = Engine(_configured_store(tmp_path), rate_hz=50)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: engine.is_streaming)
    await engine.restart_stream("tv1")
    await asyncio.sleep(0.05)  # the old backend is now closing
    await engine.stop()
    assert backends[0].closed


async def test_concurrent_restarts_during_startup_coalesce(tmp_path: Path, monkeypatch) -> None:
    """Two restarts racing a stream start close the starting backend once and open one stream."""
    backends: list[_SlowBackend] = []

    def create(_self: Engine, _user: object) -> tuple[_SlowBackend, str]:
        backends.append(_SlowBackend())
        return backends[-1], "slow"

    monkeypatch.setattr(Engine, "_create_backend", create)
    engine = Engine(_configured_store(tmp_path), rate_hz=50)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: len(backends) == 1)  # its start() is now in progress
    await asyncio.gather(engine.restart_stream("tv1"), engine.restart_stream("tv1"))
    for _ in range(25):
        engine.submit_color("tv1", "1", (65535, 0, 0))
        await asyncio.sleep(0.02)
    assert await _wait_until(lambda: engine.is_streaming)
    assert await _wait_until(lambda: backends[0].closed)
    live = [backend for backend in backends if backend.streaming]
    assert len(live) == 1
    assert live[0].started_at >= backends[0].closed_at
    await engine.stop()


async def test_open_rechecks_ownership_after_waiting_for_a_close(
    tmp_path: Path, monkeypatch
) -> None:
    """A TV disabled while its stream waits for an earlier close never gets a backend."""
    backends: list[_SlowBackend] = []

    def create(_self: Engine, _user: object) -> tuple[_SlowBackend, str]:
        backends.append(_SlowBackend())
        return backends[-1], "slow"

    monkeypatch.setattr(Engine, "_create_backend", create)
    store = _configured_store(tmp_path)
    engine = Engine(store, rate_hz=50)
    release = asyncio.Event()
    _slot(engine).closing = asyncio.create_task(release.wait())  # an earlier backend still closing
    engine.submit_color("tv1", "1", (65535, 0, 0))
    await asyncio.sleep(0.05)  # the new stream now waits for that close
    store.config.users[0].enabled = False
    engine.tv_settings_changed("tv1")
    release.set()
    await asyncio.sleep(0.1)
    assert backends == []
    assert not engine.stream_owners
    await engine.stop()


async def test_failed_start_backs_off_before_retrying(tmp_path: Path, monkeypatch) -> None:
    """A start that fails is retried after a pause, not on every inbound frame."""
    starts: list[float] = []

    class _FailingBackend(_ScriptedBackend):
        async def start(self) -> None:
            starts.append(time.monotonic())
            msg = "lamps unreachable"
            raise OSError(msg)

    monkeypatch.setattr(Engine, "_create_backend", lambda _self, _user: (_FailingBackend([]), "x"))
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._START_RETRY_BACKOFF_S", 0.3)
    engine = Engine(_configured_store(tmp_path), rate_hz=100)
    for _ in range(10):
        engine.submit_color("tv1", "1", (65535, 0, 0))
        await asyncio.sleep(0.02)
    assert len(starts) == 1
    await asyncio.sleep(0.3)
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: len(starts) == 2)
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

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", factory)
    engine = Engine(_configured_store(tmp_path))
    engine.start_stream("tv1")
    await asyncio.sleep(0.02)  # let _start_stream block in session.start
    await engine.stop()
    assert not engine.is_streaming
    assert closed  # the orphaned mid-connect session was closed, not leaked


class _FakeHomeAssistant:
    """Stand-in for HomeAssistantClient: a configured connection with one Ziggurat lamp."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def stream_info(self) -> tuple[str, dict[str, int]]:
        return "ws://core-ziggurat:9999", {"00:17:88:01:00:00:00:01": 0x1111}


class _FakeZigbeeSession:
    """Stand-in for ZigbeeEntertainmentSession that records sent frames (no real I/O)."""

    def __init__(self, url: str, lights: list[object], *, idle_timeout: float) -> None:
        self.url = url
        self.lights = lights
        self.idle_timeout = idle_timeout
        self.streaming = False
        self.closed = False
        self.frames: list[object] = []

    @property
    def is_streaming(self) -> bool:
        return self.streaming

    async def start(self) -> None:
        self.streaming = True

    def send(self, commands: object) -> None:
        self.frames.append(commands)

    async def aclose(self) -> None:
        self.streaming = False
        self.closed = True


def _zigbee_store(tmp_path: Path) -> ConfigStore:
    """Build a store with one Zigbee-target TV and no Hue bridge at all."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    lamps = [ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Lamp")]
    store.config.users = [
        PairedUser(
            username="tv1",
            clientkey="k",
            devicetype="TV",
            created="2026-11-10",
            target=StreamTarget.ZIGBEE,
            zigbee_lights=lamps,
            lights=lights_from_zigbee(lamps),
        ),
    ]
    return store


async def test_zigbee_tv_streams_without_a_hue_bridge(tmp_path: Path, monkeypatch) -> None:
    """A Zigbee-target TV streams through the Zigbee backend with no Hue bridge configured."""
    created: list[_FakeZigbeeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeZigbeeSession:
        session = _FakeZigbeeSession(*args, **kwargs)  # type: ignore[arg-type]
        created.append(session)
        return session

    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.ZigbeeEntertainmentSession", factory)
    engine = Engine(
        _zigbee_store(tmp_path),
        rate_hz=100,
        home_assistant=_FakeHomeAssistant(),  # type: ignore[arg-type]
    )
    engine.submit_color("tv1", "1", (65535, 0, 0))
    assert await _wait_until(lambda: bool(created) and bool(created[0].frames))
    assert isinstance(_slot(engine).backend, ZigbeeBackend)
    assert engine.stream_owners == {"tv1"}
    first_frame = created[0].frames[0]
    assert isinstance(first_frame, list)
    assert first_frame[0].channel_id == 0
    assert first_frame[0].red == 65535
    await engine.stop()
    assert created[0].closed


def test_zigbee_tv_readiness_needs_lamps_and_home_assistant(tmp_path: Path) -> None:
    """A Zigbee TV is ready when enabled with lamps and a Home Assistant connection."""
    store = _zigbee_store(tmp_path)
    home_assistant = _FakeHomeAssistant()
    engine = Engine(store, home_assistant=home_assistant)  # type: ignore[arg-type]
    assert engine._owner_ready("tv1")
    home_assistant.available = False
    assert not engine._owner_ready("tv1")
    home_assistant.available = True
    store.config.users[0].zigbee_lights = []
    assert not engine._owner_ready("tv1")


def test_create_backend_follows_the_tv_target(tmp_path: Path, monkeypatch) -> None:
    """A Hue-target TV gets the Hue bridge backend, a Zigbee-target TV the Zigbee backend."""
    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", _FakeSession)
    store = _configured_store(tmp_path)
    engine = Engine(store, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    user = store.config.users[0]
    created = engine._create_backend(user)
    assert created is not None
    assert isinstance(created[0], HueBridgeBackend)
    assert created[1] == "1.2.3.4 area area-1"
    user.target = StreamTarget.ZIGBEE
    user.zigbee_lights = [ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Lamp")]
    store.config.real_bridges = []
    created = engine._create_backend(user)
    assert created is not None
    assert isinstance(created[0], ZigbeeBackend)
    assert created[1] == "1 Zigbee lamp(s)"


_LAMP_A = "00:17:88:01:00:00:00:0a"
_LAMP_B = "00:17:88:01:00:00:00:0b"
_LAMP_C = "00:17:88:01:00:00:00:0c"
_RED = (65535, 0, 0)


def _add_zigbee_tv(
    store: ConfigStore, username: str, ieees: list[str], *, priority: int = 0
) -> PairedUser:
    """Add a Zigbee-target TV streaming to the given lamps to a test store."""
    lamps = [ZigbeeLightRef(ieee=ieee, name=ieee) for ieee in ieees]
    user = PairedUser(
        username=username,
        clientkey="k",
        devicetype="TV",
        created="2026-11-10",
        stream_priority=priority,
        target=StreamTarget.ZIGBEE,
        zigbee_lights=lamps,
        lights=lights_from_zigbee(lamps),
    )
    store.config.users.append(user)
    return user


class _CountingBackend(_ScriptedBackend):
    """Instant backend that counts frames and records its close."""

    def __init__(self, script: list[BackendHealth | Exception] | None = None) -> None:
        super().__init__(script or [])
        self.frames = 0
        self.closed = False

    def send(self, _commands: object) -> None:
        self.frames += 1

    async def aclose(self) -> None:
        self.streaming = False
        self.closed = True


def _backends_by_tv[BackendT: _ScriptedBackend](
    monkeypatch, make: Callable[[PairedUser], BackendT]
) -> dict[str, list[BackendT]]:
    """Patch backend creation to ``make``; return the backends created, per TV."""
    created: dict[str, list[BackendT]] = {}

    def create(_self: Engine, user: PairedUser) -> tuple[BackendT, str]:
        backend = make(user)
        created.setdefault(user.username, []).append(backend)
        return backend, user.username

    monkeypatch.setattr(Engine, "_create_backend", create)
    return created


def test_resource_key_groups_zigbee_tvs_linked_by_lamps(tmp_path: Path) -> None:
    """Hue TVs share their bridge's key; Zigbee TVs share one only when lamps link them."""
    store = _configured_store(tmp_path)
    _add_tv(store, "tv2", priority=0)
    _add_zigbee_tv(store, "z1", [_LAMP_A])
    _add_zigbee_tv(store, "z2", [_LAMP_B])
    _add_zigbee_tv(store, "z3", [_LAMP_C.upper()])
    assert _resource_key(store, "tv1") == _resource_key(store, "tv2") == "hue:b"
    assert len({_resource_key(store, tv) for tv in ("z1", "z2", "z3")}) == 3
    # z4 shares a lamp with z1 and with z3, linking all three into one output.
    linker = _add_zigbee_tv(store, "z4", [_LAMP_A, _LAMP_C])
    assert _resource_key(store, "z1") == _resource_key(store, "z3") == _resource_key(store, "z4")
    assert _resource_key(store, "z2") not in {_resource_key(store, "z1"), None}
    linker.zigbee_lights = []
    assert _resource_key(store, "z4") is None
    assert _resource_key(store, "z1") != _resource_key(store, "z3")
    store.config.real_bridges = []
    assert _resource_key(store, "tv1") is None
    assert _resource_key(store, "unknown") is None


async def test_hue_and_zigbee_tvs_stream_at_the_same_time(tmp_path: Path, monkeypatch) -> None:
    """A Hue TV and a Zigbee TV each get their own stream; neither denies the other."""
    store = _configured_store(tmp_path)
    _add_zigbee_tv(store, "tvz", [_LAMP_A])
    backends = _backends_by_tv(monkeypatch, lambda _user: _CountingBackend())
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    assert engine.start_stream("tv1")
    assert engine.start_stream("tvz")
    engine.submit_color("tv1", "1", _RED)
    engine.submit_color("tvz", "1", _RED)
    assert await _wait_until(
        lambda: all(tv in backends and backends[tv][0].frames for tv in ("tv1", "tvz"))
    )
    assert engine.stream_owners == {"tv1", "tvz"}
    await engine.stop()


async def test_hue_tvs_arbitrate_without_touching_a_zigbee_stream(
    tmp_path: Path, monkeypatch
) -> None:
    """A higher-priority Hue TV takes over the bridge; the Zigbee TV's stream carries on."""
    store = _configured_store(tmp_path)
    store.config.users[0].stream_priority = 1
    _add_tv(store, "tv2", priority=10)
    _add_tv(store, "tv3", priority=10)
    _add_zigbee_tv(store, "tvz", [_LAMP_A])
    backends = _backends_by_tv(monkeypatch, lambda _user: _CountingBackend())
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    assert engine.start_stream("tv1")
    assert engine.start_stream("tvz")
    assert await _wait_until(lambda: engine.stream_owners == {"tv1", "tvz"} and "tvz" in backends)
    zigbee = backends["tvz"][0]

    assert engine.start_stream("tv2")
    assert await _wait_until(lambda: engine.stream_owners == {"tv2", "tvz"} and "tv2" in backends)
    assert backends["tv1"][0].closed
    assert engine.start_stream("tv3") is False  # equal priority: tv2 keeps the bridge
    await asyncio.sleep(0.05)
    assert "tv3" not in backends
    assert backends["tvz"] == [zigbee]
    assert zigbee.streaming
    await engine.stop()


async def test_zigbee_tvs_with_separate_lamps_stream_at_the_same_time(
    tmp_path: Path, monkeypatch
) -> None:
    """Two Zigbee TVs without a shared lamp both stream, whatever their priorities."""
    store = _zigbee_store(tmp_path)
    store.config.users[0].stream_priority = 10
    _add_zigbee_tv(store, "tv2", [_LAMP_A])
    backends = _backends_by_tv(monkeypatch, lambda _user: _CountingBackend())
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    assert engine.start_stream("tv1")
    assert engine.start_stream("tv2")
    assert await _wait_until(
        lambda: all(tv in backends and backends[tv][0].frames for tv in ("tv1", "tv2"))
    )
    assert engine.stream_owners == {"tv1", "tv2"}
    await engine.stop()


async def test_zigbee_tvs_sharing_a_lamp_arbitrate(tmp_path: Path, monkeypatch) -> None:
    """Zigbee TVs with a lamp in common compete for one stream, by priority."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    _add_zigbee_tv(store, "tv1", [_LAMP_A, _LAMP_B])
    second = _add_zigbee_tv(store, "tv2", [_LAMP_B, _LAMP_C])
    backends = _backends_by_tv(monkeypatch, lambda _user: _CountingBackend())
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    assert engine.start_stream("tv1")
    assert await _wait_until(lambda: engine.is_streaming)
    assert engine.start_stream("tv2") is False
    await asyncio.sleep(0.05)
    assert engine.stream_owners == {"tv1"}
    assert "tv2" not in backends

    second.stream_priority = 10
    engine.tv_settings_changed("tv2")
    assert await _wait_until(lambda: engine.stream_owners == {"tv2"} and engine.is_streaming)
    assert backends["tv1"][0].closed
    await engine.stop()


async def test_zigbee_tvs_linked_by_a_new_shared_lamp_arbitrate(
    tmp_path: Path, monkeypatch
) -> None:
    """Giving a streaming Zigbee TV another TV's lamp leaves only the higher-priority one."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    _add_zigbee_tv(store, "tvb", [_LAMP_A])
    winner = _add_zigbee_tv(store, "tva", [_LAMP_B], priority=10)
    backends = _backends_by_tv(monkeypatch, lambda _user: _CountingBackend())
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    assert engine.start_stream("tvb")
    assert engine.start_stream("tva")
    assert await _wait_until(lambda: engine.stream_owners == {"tva", "tvb"} and engine.is_streaming)

    # As the web UI does: update the lamps, then restart the TV and re-evaluate ownership.
    winner.zigbee_lights = [ZigbeeLightRef(ieee=_LAMP_A), ZigbeeLightRef(ieee=_LAMP_B)]
    winner.lights = lights_from_zigbee(winner.zigbee_lights)
    await engine.restart_stream("tva")
    engine.tv_settings_changed("tva")
    assert await _wait_until(lambda: len(backends["tva"]) == 2 and backends["tva"][1].streaming)
    assert await _wait_until(lambda: backends["tvb"][0].closed)
    assert engine.stream_owners == {"tva"}
    await engine.stop()


async def test_failed_zigbee_start_does_not_hold_back_a_hue_tv(tmp_path: Path, monkeypatch) -> None:
    """A Zigbee TV backing off after a failed start leaves the Hue bridge free to stream."""
    starts: list[str] = []

    class _FailingBackend(_CountingBackend):
        async def start(self) -> None:
            starts.append("zigbee")
            msg = "lamps unreachable"
            raise OSError(msg)

    store = _configured_store(tmp_path)
    _add_zigbee_tv(store, "tvz", [_LAMP_A])
    backends = _backends_by_tv(
        monkeypatch,
        lambda user: (
            _FailingBackend() if user.target is StreamTarget.ZIGBEE else _CountingBackend()
        ),
    )
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._START_RETRY_BACKOFF_S", 60.0)
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    engine.submit_color("tvz", "1", _RED)
    assert await _wait_until(lambda: _slot(engine, "tvz").regrab_block_until > time.monotonic())
    engine.submit_color("tv1", "1", _RED)
    assert await _wait_until(lambda: "tv1" in backends and backends["tv1"][0].frames > 0)
    engine.submit_color("tvz", "1", _RED)
    await asyncio.sleep(0.05)
    assert starts == ["zigbee"]  # the Zigbee TV itself still backs off
    await engine.stop()


async def test_hue_takeover_backs_off_only_the_hue_bridge(tmp_path: Path, monkeypatch) -> None:
    """Another controller taking the Hue area over doesn't keep a Zigbee TV from streaming."""
    store = _configured_store(tmp_path)
    _add_zigbee_tv(store, "tvz", [_LAMP_A])
    backends = _backends_by_tv(
        monkeypatch,
        lambda user: (
            _CountingBackend()
            if user.target is StreamTarget.ZIGBEE
            else _CountingBackend([BackendHealth.TAKEN_OVER, BackendHealth.TAKEN_OVER])
        ),
    )
    monkeypatch.setattr("ambilight_hue_bridge.engine.engine._HEALTH_POLL_S", 0.02)
    engine = Engine(store, rate_hz=100, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    engine.submit_color("tv1", "1", _RED)
    assert await _wait_until(lambda: "tv1" in backends and backends["tv1"][0].closed)
    assert _slot(engine).regrab_block_until > time.monotonic()
    engine.submit_color("tvz", "1", _RED)
    assert await _wait_until(lambda: _slot(engine, "tvz").is_streaming)
    engine.submit_color("tv1", "1", _RED)
    assert not await _wait_until(lambda: _slot(engine).is_streaming, max_wait=0.2)
    assert len(backends["tv1"]) == 1
    await engine.stop()


def _switch_to_zigbee(user: PairedUser, ieees: list[str]) -> None:
    """Point a TV at Zigbee lamps, as the web UI does before restarting its stream."""
    user.target = StreamTarget.ZIGBEE
    user.zigbee_lights = [ZigbeeLightRef(ieee=ieee) for ieee in ieees]
    user.lights = lights_from_zigbee(user.zigbee_lights)


async def test_target_switch_moves_the_stream_to_the_new_output(
    tmp_path: Path, monkeypatch
) -> None:
    """A TV switched to Zigbee lamps while streaming streams there only, after the Hue close."""
    store = _configured_store(tmp_path)
    backends = _backends_by_tv(monkeypatch, lambda _user: _SlowBackend())
    engine = Engine(store, rate_hz=50, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    engine.submit_color("tv1", "1", _RED)
    assert await _wait_until(lambda: engine.is_streaming)
    hue_slot = _slot(engine)

    _switch_to_zigbee(store.config.users[0], [_LAMP_A])
    await engine.restart_stream("tv1")
    engine.tv_settings_changed("tv1")
    # The TV keeps sending while the Hue stream closes: no Zigbee stream may start before that.
    for _ in range(20):
        engine.submit_color("tv1", "1", _RED)
        await asyncio.sleep(0.02)
    assert await _wait_until(lambda: len(backends["tv1"]) == 2 and engine.is_streaming)
    hue, zigbee = backends["tv1"]
    assert hue.closed
    assert zigbee.started_at >= hue.closed_at
    assert _slot(engine) is not hue_slot
    assert _slot(engine).owner == "tv1"
    assert hue_slot.owner is None
    assert engine.stream_owners == {"tv1"}
    await engine.stop()


async def test_target_switch_resumes_without_a_new_frame(tmp_path: Path, monkeypatch) -> None:
    """A TV switched to another output resumes there once the old stream closed, unprompted."""
    store = _configured_store(tmp_path)
    backends = _backends_by_tv(monkeypatch, lambda _user: _SlowBackend())
    engine = Engine(store, rate_hz=50, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    engine.submit_color("tv1", "1", _RED)
    assert await _wait_until(lambda: engine.is_streaming)

    _switch_to_zigbee(store.config.users[0], [_LAMP_A])
    await engine.restart_stream("tv1")
    engine.tv_settings_changed("tv1")
    assert await _wait_until(lambda: len(backends["tv1"]) == 2 and engine.is_streaming)
    assert backends["tv1"][0].closed
    assert engine.stream_owners == {"tv1"}
    await engine.stop()


async def test_stop_closes_every_output(tmp_path: Path, monkeypatch) -> None:
    """stop() tears down every output's stream and waits for each backend to finish closing."""
    store = _configured_store(tmp_path)
    _add_zigbee_tv(store, "tvz", [_LAMP_A])
    backends = _backends_by_tv(monkeypatch, lambda _user: _SlowBackend())
    engine = Engine(store, rate_hz=50, home_assistant=_FakeHomeAssistant())  # type: ignore[arg-type]
    engine.submit_color("tv1", "1", _RED)
    engine.submit_color("tvz", "1", _RED)
    assert await _wait_until(lambda: engine.stream_owners == {"tv1", "tvz"} and len(backends) == 2)
    assert await _wait_until(lambda: all(bs[0].streaming for bs in backends.values()))
    await engine.stop()
    assert all(backend.closed for bs in backends.values() for backend in bs)
    assert not engine.is_streaming
    assert not engine.stream_owners
