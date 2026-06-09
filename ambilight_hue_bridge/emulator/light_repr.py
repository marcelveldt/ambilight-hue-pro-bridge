"""Builds the legacy Hue v1 JSON representation of lights."""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import VirtualLight


def default_light_state() -> dict[str, Any]:
    """Return the default mutable state for a freshly exposed light."""
    return {
        "on": True,
        "bri": 254,
        "hue": 0,
        "sat": 0,
        "xy": [0.0, 0.0],
        "ct": 366,
        "colormode": "xy",
    }


def build_v1_light(
    light: VirtualLight,
    state: dict[str, Any],
    *,
    reachable: bool = True,
) -> dict[str, Any]:
    """
    Build the Hue v1 ``/lights/<id>`` representation of a virtual light.

    Numeric state fields are emitted as integers, which Ambilight+Hue TVs require.

    :param light: The virtual light to represent.
    :param state: The light's current mutable state (on/bri/hue/sat/xy/ct/colormode).
    :param reachable: Whether the light reports as reachable.
    """
    return {
        "state": {
            "on": bool(state["on"]),
            "bri": int(state["bri"]),
            "hue": int(state["hue"]),
            "sat": int(state["sat"]),
            "effect": "none",
            "xy": list(state["xy"]),
            "ct": int(state["ct"]),
            "alert": "none",
            "colormode": state["colormode"],
            "mode": "homeautomation",
            "reachable": reachable,
        },
        "swupdate": {"state": "noupdates", "lastinstall": "2021-01-01T00:00:00"},
        "type": "Extended color light",
        "name": light.name,
        "modelid": light.modelid,
        "manufacturername": "Signify Netherlands B.V.",
        "productname": "Hue color lamp",
        "capabilities": {
            "certified": True,
            "control": {
                "mindimlevel": 200,
                "maxlumen": 800,
                "colorgamuttype": "C",
                "colorgamut": [[0.6915, 0.3083], [0.1700, 0.7000], [0.1532, 0.0475]],
                "ct": {"min": 153, "max": 500},
            },
            "streaming": {"renderer": True, "proxy": True},
        },
        "config": {
            "archetype": "sultanbulb",
            "function": "mixed",
            "direction": "omnidirectional",
            "startup": {"mode": "safety", "configured": True},
        },
        "uniqueid": _unique_id(light.id),
        "swversion": "1.65.11_hB798F2B",
    }


def _unique_id(light_id: str) -> str:
    """Build a stable zigbee-style uniqueid from a light id."""
    base = int(light_id) if light_id.isdigit() else zlib.crc32(light_id.encode())
    octet_a = (base >> 16) & 0xFF
    octet_b = (base >> 8) & 0xFF
    octet_c = base & 0xFF
    return f"00:17:88:01:00:{octet_a:02x}:{octet_b:02x}:{octet_c:02x}-0b"
