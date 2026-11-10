"""Tests for the outbound stream backends."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from hue_entertainment import LightColorCommand

from ambilight_hue_bridge.config.models import RealBridge, ZigbeeLightRef
from ambilight_hue_bridge.outbound.backend import BackendHealth, HueBridgeBackend, ZigbeeBackend
from ambilight_hue_bridge.outbound.homeassistant import HomeAssistantError, ZhaLight

if TYPE_CHECKING:
    from hue_entertainment import ZigbeeLight


class _FakeSession:
    """Stand-in for EntertainmentSession with a scripted bridge status (no real I/O)."""

    def __init__(self, host: str, app_key: str, client_key: str, *, idle_timeout: float) -> None:
        """Record the construction args and start disconnected."""
        self.args = (host, app_key, client_key, idle_timeout)
        self.area_id: str | None = None
        self.closed = False
        self.frames: list[object] = []
        # Bridge-reported (status, active_streamer_rid), or the error the request raises.
        self.status: tuple[str, str] | Exception = ("active", "us")

    @property
    def is_streaming(self) -> bool:
        """Return whether the fake stream is active."""
        return self.area_id is not None and not self.closed

    async def start(self, area_id: str) -> None:
        """Mark the stream active for an area."""
        self.area_id = area_id

    def send(self, commands: object) -> None:
        """Record a frame."""
        self.frames.append(commands)

    async def remote_status(self) -> tuple[str, str]:
        """Return the scripted bridge status, or raise the scripted error."""
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    async def aclose(self) -> None:
        """Mark the stream stopped."""
        self.closed = True


def _make_backend(monkeypatch: pytest.MonkeyPatch) -> HueBridgeBackend:
    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend.EntertainmentSession", _FakeSession)
    bridge = RealBridge(id="b", host="1.2.3.4", app_key="user", client_key="deadbeef")
    return HueBridgeBackend(bridge, "area-1")


def _session(backend: HueBridgeBackend) -> _FakeSession:
    session = backend._session
    assert isinstance(session, _FakeSession)
    return session


async def test_backend_wraps_entertainment_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend streams to its area through a session without the library idle timeout."""
    backend = _make_backend(monkeypatch)
    session = _session(backend)
    assert session.args == ("1.2.3.4", "user", "deadbeef", 0.0)
    await backend.start()
    assert session.area_id == "area-1"
    assert backend.is_streaming
    backend.send([])
    assert session.frames == [[]]
    await backend.aclose()
    assert not backend.is_streaming


async def test_check_health_ok_captures_our_streamer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An active area naming a streamer is ours, and that streamer is remembered as us."""
    backend = _make_backend(monkeypatch)
    assert await backend.check_health() is BackendHealth.OK
    assert backend._our_streamer_rid == "us"


async def test_check_health_lost_when_area_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge deactivating the area means it ended our stream."""
    backend = _make_backend(monkeypatch)
    _session(backend).status = ("inactive", "")
    assert await backend.check_health() is BackendHealth.LOST


async def test_check_health_taken_over_by_foreign_streamer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once our streamer is known, a different active streamer is a takeover."""
    backend = _make_backend(monkeypatch)
    session = _session(backend)
    assert await backend.check_health() is BackendHealth.OK
    session.status = ("active", "other-app")
    assert await backend.check_health() is BackendHealth.TAKEN_OVER
    assert backend._our_streamer_rid == "us"


async def test_check_health_empty_rid_grace_until_captured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty streamer rid is trusted until ours is known; afterwards it means a drop."""
    backend = _make_backend(monkeypatch)
    session = _session(backend)
    session.status = ("active", "")
    assert await backend.check_health() is BackendHealth.OK
    assert not backend._streamer_rid_captured
    session.status = ("active", "us")
    assert await backend.check_health() is BackendHealth.OK
    session.status = ("active", "")
    assert await backend.check_health() is BackendHealth.LOST


async def test_check_health_transient_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing status request raises, so the engine can skip that poll."""
    backend = _make_backend(monkeypatch)
    _session(backend).status = OSError("timeout")
    with pytest.raises(OSError, match="timeout"):
        await backend.check_health()
    assert not backend._streamer_rid_captured


class _FakeHomeAssistant:
    """Stand-in for HomeAssistantClient with a fixed Ziggurat URL and ZHA lamps."""

    def __init__(self, url: str | None, lights: list[ZhaLight]) -> None:
        self.url = url
        self.lights = lights

    async def stream_info(self) -> tuple[str | None, dict[str, int]]:
        return self.url, {light.ieee: light.nwk for light in self.lights}


class _FakeZigbeeSession:
    """Stand-in for ZigbeeEntertainmentSession that records its lights and frames."""

    def __init__(
        self, url: str, lights: list[ZigbeeLight], *, idle_timeout: float, max_rate_hz: float = 25.0
    ) -> None:
        self.url = url
        self.lights = lights
        self.idle_timeout = idle_timeout
        self.max_rate_hz = max_rate_hz
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


_LAMPS = [
    ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Left"),
    ZigbeeLightRef(ieee="00:17:88:01:00:00:00:02", name="Gone"),
    # Stored upper case, as a hand-edited config might.
    ZigbeeLightRef(ieee="00:17:88:01:0C:BF:17:C8", name="Right"),
]


def _make_zigbee_backend(
    monkeypatch: pytest.MonkeyPatch,
    url: str | None = "ws://core-ziggurat:9999",
    lamps: list[ZigbeeLightRef] = _LAMPS,
    *,
    split_gradients: bool = True,
) -> ZigbeeBackend:
    monkeypatch.setattr(
        "ambilight_hue_bridge.outbound.backend.ZigbeeEntertainmentSession", _FakeZigbeeSession
    )
    home_assistant = _FakeHomeAssistant(
        url,
        [
            # Out of order on purpose: lamps are matched by IEEE address, not position.
            ZhaLight(ieee="00:17:88:01:0c:bf:17:c8", nwk=0x3333, name="R"),
            ZhaLight(ieee="00:17:88:01:00:00:00:01", nwk=0x1111, name="L"),
        ],
    )
    return ZigbeeBackend(
        home_assistant,  # type: ignore[arg-type]
        lamps,
        split_gradients=split_gradients,
    )


def _zigbee_session(backend: ZigbeeBackend) -> _FakeZigbeeSession:
    session = backend._session
    assert isinstance(session, _FakeZigbeeSession)
    return session


async def test_zigbee_start_resolves_addresses_and_skips_missing_lamps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Each lamp gets its current ZHA address; a lamp gone from ZHA is skipped with a warning."""
    backend = _make_zigbee_backend(monkeypatch)
    with caplog.at_level(logging.WARNING):
        await backend.start()
    session = _zigbee_session(backend)
    assert session.url == "ws://core-ziggurat:9999"
    assert session.idle_timeout == 0.0
    # Channel ids stay the lamps' configured positions, so the TV's lights keep mapping right.
    assert [(light.channel_id, light.nwk, light.name) for light in session.lights] == [
        (0, 0x1111, "Left"),
        (2, 0x3333, "Right"),
    ]
    assert any("Gone" in record.getMessage() for record in caplog.records)
    assert backend.is_streaming


_GRADIENT_LAMPS = [
    ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Signe", segments=7),
    # Not in ZHA: skipped, but its channels stay reserved.
    ZigbeeLightRef(ieee="00:17:88:01:00:00:00:02", name="Gone", segments=7),
    ZigbeeLightRef(ieee="00:17:88:01:0c:bf:17:c8", name="Spot"),
]


async def test_zigbee_start_streams_gradient_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gradient lamp streams its zones on consecutive channels; a skipped lamp keeps its own."""
    backend = _make_zigbee_backend(monkeypatch, lamps=_GRADIENT_LAMPS)
    await backend.start()
    session = _zigbee_session(backend)
    assert [(light.channel_id, light.ieee, light.nwk, light.zone) for light in session.lights] == [
        (0, "00:17:88:01:00:00:00:01", 0x1111, 0),
        (1, "00:17:88:01:00:00:00:01", 0x1111, 1),
        (2, "00:17:88:01:00:00:00:01", 0x1111, 2),
        (6, "00:17:88:01:0c:bf:17:c8", 0x3333, 0),
    ]


async def test_zigbee_start_without_split_streams_one_channel_per_lamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without split gradients every lamp, gradient or not, streams one colour."""
    backend = _make_zigbee_backend(monkeypatch, lamps=_GRADIENT_LAMPS, split_gradients=False)
    await backend.start()
    session = _zigbee_session(backend)
    assert [(light.channel_id, light.nwk, light.zone) for light in session.lights] == [
        (0, 0x1111, 0),
        (2, 0x3333, 0),
    ]


async def test_zigbee_start_without_any_lamp_in_zha_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When none of the lamps are in ZHA, start fails and no session is created."""
    backend = _make_zigbee_backend(monkeypatch)
    backend._lamps = [ZigbeeLightRef(ieee="00:17:88:01:00:00:00:02", name="Gone")]
    with pytest.raises(HomeAssistantError, match="None of the selected Zigbee lamps"):
        await backend.start()
    assert backend._session is None
    assert not backend.is_streaming


async def test_zigbee_start_without_ziggurat_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZHA on another radio than Ziggurat can't be streamed to."""
    backend = _make_zigbee_backend(monkeypatch, url=None)
    with pytest.raises(HomeAssistantError, match="Ziggurat"):
        await backend.start()
    assert backend._session is None


async def test_zigbee_backend_wraps_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames, health and close go to the Zigbee session."""
    backend = _make_zigbee_backend(monkeypatch)
    backend.send([LightColorCommand(channel_id=0, red=1)])  # before start: dropped, no error
    await backend.start()
    session = _zigbee_session(backend)
    backend.send([LightColorCommand(channel_id=0, red=1)])
    assert len(session.frames) == 1
    assert await backend.check_health() is BackendHealth.OK
    await backend.aclose()
    assert session.closed
    assert not backend.is_streaming


async def test_zigbee_rate_change_reaches_the_running_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new max_rate_hz opens the stream at that rate, and changes a running one in place."""
    backend = _make_zigbee_backend(monkeypatch)
    backend.max_rate_hz = 10.0
    await backend.start()
    session = _zigbee_session(backend)
    assert session.max_rate_hz == 10.0
    backend.max_rate_hz = 5.0
    assert backend.max_rate_hz == 5.0
    assert _zigbee_session(backend) is session
    assert session.max_rate_hz == 5.0


async def test_zigbee_lost_stream_restarts_the_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost stream is restarted in place, so the lamps' captured state is kept."""
    backend = _make_zigbee_backend(monkeypatch)
    await backend.start()
    session = _zigbee_session(backend)
    session.streaming = False  # e.g. ZHA restarting its Zigbee stack
    assert backend.is_streaming  # still "ours" while it recovers
    assert await backend.check_health() is BackendHealth.OK
    assert _zigbee_session(backend) is session
    assert session.streaming


async def test_zigbee_stream_down_too_long_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that cannot be restarted stays OK during the recovery window, then is LOST."""
    backend = _make_zigbee_backend(monkeypatch)
    await backend.start()
    session = _zigbee_session(backend)

    async def still_down() -> None:
        msg = "Ziggurat unreachable"
        raise OSError(msg)

    session.start = still_down  # type: ignore[method-assign]
    session.streaming = False
    assert await backend.check_health() is BackendHealth.OK
    monkeypatch.setattr("ambilight_hue_bridge.outbound.backend._ZIGBEE_RECOVERY_S", 0.0)
    assert await backend.check_health() is BackendHealth.LOST
