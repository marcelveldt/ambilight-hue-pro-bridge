"""Tests for Hue v1 state to RGB conversion."""

from __future__ import annotations

from ambilight_hue_bridge.color import state_to_rgb16

_RGB16_MAX = 65535


def test_off_is_black() -> None:
    """An off light maps to black regardless of other fields."""
    assert state_to_rgb16({"on": False, "bri": 254, "xy": [0.7, 0.3]}) == (0, 0, 0)


def test_red_via_xy_is_red_dominant() -> None:
    """A red xy point at full brightness yields a dominant red channel within range."""
    red, green, blue = state_to_rgb16({"on": True, "bri": 254, "colormode": "xy", "xy": [0.7, 0.3]})
    assert red > green
    assert red > blue
    assert red <= _RGB16_MAX


def test_green_via_hs_is_green_dominant() -> None:
    """A green hue maps to a dominant green channel."""
    red, green, blue = state_to_rgb16(
        {"on": True, "bri": 254, "colormode": "hs", "hue": 21845, "sat": 254},
    )
    assert green >= red
    assert green >= blue


def test_brightness_scales_channels() -> None:
    """Lower brightness produces smaller channel values."""
    full = state_to_rgb16({"on": True, "bri": 254, "colormode": "hs", "hue": 0, "sat": 254})
    half = state_to_rgb16({"on": True, "bri": 127, "colormode": "hs", "hue": 0, "sat": 254})
    assert 0 < half[0] < full[0]
