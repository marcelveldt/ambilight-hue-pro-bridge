"""Tests for inbound HueStream frame ingestion (owner -> lights resolution, v1/v2 mapping)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import PairedUser, VirtualLight
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.emulator.inbound import InboundStreamer
from ambilight_hue_bridge.emulator.pairing import PairingManager

if TYPE_CHECKING:
    from pathlib import Path


class _FakeColor:
    """A decoded HueStream color entry."""

    def __init__(self, target: int, rgb: tuple[int, int, int]) -> None:
        self.target = target
        self.rgb = rgb


class _FakeFrame:
    """A decoded HueStream frame."""

    def __init__(self, *, is_v2: bool, colors: list[_FakeColor]) -> None:
        self.is_v2 = is_v2
        self.colors = colors


class _RecordingEngine:
    """Engine stand-in recording (owner, light_id, rgb) submissions."""

    def __init__(self, owner: str | None) -> None:
        self.stream_owner = owner
        self.colors: list[tuple[str | None, str, tuple[int, int, int]]] = []

    def submit_color(self, owner: str | None, light_id: str, rgb: tuple[int, int, int]) -> None:
        self.colors.append((owner, light_id, rgb))


def _streamer(tmp_path: Path, engine: _RecordingEngine) -> InboundStreamer:
    """Build an InboundStreamer over a store whose owner 'u1' has two lights."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.users = [
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="1", name="Left"), VirtualLight(id="2", name="Right")],
        ),
    ]
    return InboundStreamer(store=store, pairing=PairingManager(store), engine=engine)  # type: ignore[arg-type]


def test_v1_frame_forwards_each_color_by_light_id(tmp_path: Path, monkeypatch) -> None:
    """A v1 frame addresses light ids directly; every color is forwarded under the owner."""
    engine = _RecordingEngine("u1")
    streamer = _streamer(tmp_path, engine)
    frame = _FakeFrame(is_v2=False, colors=[_FakeColor(1, (10, 20, 30)), _FakeColor(2, (1, 2, 3))])
    monkeypatch.setattr(
        "ambilight_hue_bridge.emulator.inbound.decode_huestream", lambda _data: frame
    )
    streamer._on_frame(b"x")
    assert engine.colors == [("u1", "1", (10, 20, 30)), ("u1", "2", (1, 2, 3))]


def test_v2_frame_maps_channel_index_and_drops_out_of_range(tmp_path: Path, monkeypatch) -> None:
    """A v2 frame addresses channels by index into the TV's lights; out-of-range is dropped."""
    engine = _RecordingEngine("u1")
    streamer = _streamer(tmp_path, engine)
    frame = _FakeFrame(is_v2=True, colors=[_FakeColor(0, (1, 1, 1)), _FakeColor(5, (9, 9, 9))])
    monkeypatch.setattr(
        "ambilight_hue_bridge.emulator.inbound.decode_huestream", lambda _data: frame
    )
    streamer._on_frame(b"x")
    assert engine.colors == [("u1", "1", (1, 1, 1))]


def test_undecodable_frame_is_a_noop(tmp_path: Path, monkeypatch) -> None:
    """A frame that fails to decode submits nothing."""
    engine = _RecordingEngine("u1")
    streamer = _streamer(tmp_path, engine)
    monkeypatch.setattr(
        "ambilight_hue_bridge.emulator.inbound.decode_huestream", lambda _data: None
    )
    streamer._on_frame(b"garbage")
    assert engine.colors == []
