"""Decode inbound HueStream entertainment frames received from a TV."""

from __future__ import annotations

from dataclasses import dataclass

from ambilight_hue_bridge.color import xy_brightness_to_rgb16

_HEADER = b"HueStream"
_HEADER_LEN = 16
_UUID_LEN = 36
_VERSION_OFFSET = 9
_COLORSPACE_OFFSET = 14
_COLORSPACE_XY = 0x01
_V2_ENTRY = 7
_V1_ENTRY = 9
_V2_MIN_VERSION = 2
_RGB16_MAX = 65535


@dataclass
class FrameColor:
    """A single decoded colour from a HueStream frame."""

    target: int  # light id (HueStream v1) or channel id (HueStream v2)
    rgb: tuple[int, int, int]  # 16-bit per channel


@dataclass
class DecodedFrame:
    """A decoded HueStream frame."""

    is_v2: bool
    colors: list[FrameColor]


def decode_huestream(data: bytes) -> DecodedFrame | None:
    """
    Decode a HueStream entertainment frame, or return None if it is not valid.

    :param data: The decrypted frame payload.
    """
    if len(data) < _HEADER_LEN or data[0:9] != _HEADER:
        return None
    is_v2 = data[_VERSION_OFFSET] >= _V2_MIN_VERSION
    colorspace = data[_COLORSPACE_OFFSET]
    offset = _HEADER_LEN + _UUID_LEN if is_v2 else _HEADER_LEN
    entry = _V2_ENTRY if is_v2 else _V1_ENTRY
    colors: list[FrameColor] = []
    while offset + entry <= len(data):
        if is_v2:
            target = data[offset]
            color_at = offset + 1
        else:
            target = int.from_bytes(data[offset + 1 : offset + 3], "big")
            color_at = offset + 3
        first = int.from_bytes(data[color_at : color_at + 2], "big")
        second = int.from_bytes(data[color_at + 2 : color_at + 4], "big")
        third = int.from_bytes(data[color_at + 4 : color_at + 6], "big")
        colors.append(FrameColor(target=target, rgb=_to_rgb16(colorspace, first, second, third)))
        offset += entry
    return DecodedFrame(is_v2=is_v2, colors=colors)


def _to_rgb16(colorspace: int, first: int, second: int, third: int) -> tuple[int, int, int]:
    """Convert a frame's three 16-bit values to RGB according to the colour space."""
    if colorspace == _COLORSPACE_XY:
        return xy_brightness_to_rgb16(first / _RGB16_MAX, second / _RGB16_MAX, third / _RGB16_MAX)
    return (first, second, third)
