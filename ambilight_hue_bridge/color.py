"""Convert Hue v1 light state into RGB for the Entertainment stream."""

from __future__ import annotations

import colorsys
import math
from typing import Any

_RGB_MAX = 65535
_BRI_MAX = 254
_HUE_MAX = 65535
_SAT_MAX = 254
_CT_NEUTRAL = 366


def state_to_rgb16(state: dict[str, Any]) -> tuple[int, int, int]:
    """
    Convert a Hue v1 light state to 16-bit RGB (0-65535 per channel).

    :param state: A v1 light state dict (on/bri/hue/sat/xy/ct/colormode).
    """
    if not state.get("on", True):
        return (0, 0, 0)
    brightness = _clamp01(float(state.get("bri", _BRI_MAX)) / _BRI_MAX)
    mode = state.get("colormode", "xy")
    if mode == "hs":
        red, green, blue = colorsys.hsv_to_rgb(
            float(state.get("hue", 0)) / _HUE_MAX,
            _clamp01(float(state.get("sat", 0)) / _SAT_MAX),
            1.0,
        )
    elif mode == "ct":
        red, green, blue = _ct_to_rgb(int(state.get("ct", _CT_NEUTRAL)))
    else:
        xy = state.get("xy") or [0.0, 0.0]
        red, green, blue = _xy_to_rgb(float(xy[0]), float(xy[1]))
    return (
        round(red * brightness * _RGB_MAX),
        round(green * brightness * _RGB_MAX),
        round(blue * brightness * _RGB_MAX),
    )


def _clamp01(value: float) -> float:
    """Clamp a value to the 0.0-1.0 range."""
    return max(0.0, min(1.0, value))


def _gamma(component: float) -> float:
    """Apply the sRGB gamma curve to a linear colour component."""
    if component <= 0.0031308:
        return 12.92 * component
    # math.pow keeps the result a float (the `**` operator is typed as Any for float bases).
    return 1.055 * math.pow(component, 1.0 / 2.4) - 0.055


def _xy_to_rgb(x: float, y: float) -> tuple[float, float, float]:
    """Convert CIE xy chromaticity (at full brightness) to gamma-encoded sRGB (0-1)."""
    if y <= 0.0:
        return (0.0, 0.0, 0.0)
    z = 1.0 - x - y
    big_x = x / y
    big_z = z / y
    red = big_x * 1.656492 - 0.354851 - big_z * 0.255038
    green = -big_x * 0.707196 + 1.655397 + big_z * 0.036152
    blue = big_x * 0.051713 - 0.121364 + big_z * 1.011530
    red, green, blue = (_gamma(max(0.0, component)) for component in (red, green, blue))
    largest = max(red, green, blue)
    if largest > 1.0:
        red, green, blue = (component / largest for component in (red, green, blue))
    return (_clamp01(red), _clamp01(green), _clamp01(blue))


def _ct_to_rgb(mireds: int) -> tuple[float, float, float]:
    """Convert a colour temperature in mireds to an approximate sRGB (0-1)."""
    # Tanner Helland's colour-temperature approximation (boundaries 66 and 19 are his).
    temp = (1_000_000 / max(1, mireds)) / 100.0
    if temp <= 66:
        red = 1.0
        green = _clamp01((99.4708025861 * math.log(temp) - 161.1195681661) / 255.0)
        if temp <= 19:
            blue = 0.0
        else:
            blue = _clamp01((138.5177312231 * math.log(temp - 10) - 305.0447927307) / 255.0)
    else:
        red = _clamp01((329.698727446 * ((temp - 60) ** -0.1332047592)) / 255.0)
        green = _clamp01((288.1221695283 * ((temp - 60) ** -0.0755148492)) / 255.0)
        blue = 1.0
    return (red, green, blue)
