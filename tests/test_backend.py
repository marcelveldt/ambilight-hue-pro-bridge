"""Tests for the outbound stream backends."""

from __future__ import annotations

import pytest

from ambilight_hue_bridge.config.models import RealBridge
from ambilight_hue_bridge.outbound.backend import BackendHealth, HueBridgeBackend


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
