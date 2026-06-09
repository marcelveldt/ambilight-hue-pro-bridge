"""Tests for the inbound HueStream frame decoder."""

from __future__ import annotations

from ambilight_hue_bridge.emulator.huestream import decode_huestream

_HEADER_V2 = b"HueStream" + bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
_HEADER_V1 = b"HueStream" + bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
_UUID = b"00000000-0000-0000-0000-000000000000"


def _color(value: int) -> bytes:
    return value.to_bytes(2, "big")


def test_decode_v2_channels() -> None:
    """A v2 frame yields channel-addressed colours after the 36-byte area UUID."""
    body = (
        bytes([0])
        + _color(65535)
        + _color(0)
        + _color(0)
        + bytes([1])
        + _color(0)
        + _color(65535)
        + _color(0)
    )
    frame = decode_huestream(_HEADER_V2 + _UUID + body)
    assert frame is not None
    assert frame.is_v2
    assert frame.colors[0].target == 0
    assert frame.colors[0].rgb == (65535, 0, 0)
    assert frame.colors[1].target == 1
    assert frame.colors[1].rgb == (0, 65535, 0)


def test_decode_v1_lights() -> None:
    """A v1 frame yields light-id-addressed colours (no UUID block)."""
    body = bytes([0x00]) + (5).to_bytes(2, "big") + _color(100) + _color(200) + _color(50)
    frame = decode_huestream(_HEADER_V1 + body)
    assert frame is not None
    assert not frame.is_v2
    assert frame.colors[0].target == 5
    assert frame.colors[0].rgb == (100, 200, 50)


def test_decode_rejects_non_huestream() -> None:
    """Data without the HueStream header is rejected."""
    assert decode_huestream(b"not a hue frame at all") is None
