"""Tests for the v1 light representation."""

from __future__ import annotations

from ambilight_hue_bridge.config.models import VirtualLight
from ambilight_hue_bridge.emulator.light_repr import build_v1_light, default_light_state


def test_state_fields_are_integers() -> None:
    """Numeric state fields are emitted as ints (Ambilight+Hue TVs reject floats)."""
    light = VirtualLight(id="1", name="Left")
    state = default_light_state()
    state["bri"] = 200.7  # a float should be coerced
    repr_ = build_v1_light(light, state)
    assert isinstance(repr_["state"]["bri"], int)
    assert isinstance(repr_["state"]["hue"], int)
    assert isinstance(repr_["state"]["sat"], int)
    assert isinstance(repr_["state"]["ct"], int)


def test_advertises_streaming_capability() -> None:
    """Exposed lights advertise entertainment streaming so capable TVs use the fast path."""
    repr_ = build_v1_light(VirtualLight(id="2", name="Right"), default_light_state())
    assert repr_["capabilities"]["streaming"] == {"renderer": True, "proxy": True}
    assert repr_["type"] == "Extended color light"


def test_unique_id_is_stable_and_formatted() -> None:
    """The uniqueid is deterministic and zigbee-formatted."""
    light = VirtualLight(id="3", name="Center")
    first = build_v1_light(light, default_light_state())["uniqueid"]
    second = build_v1_light(light, default_light_state())["uniqueid"]
    assert first == second
    assert first.startswith("00:17:88:01:")
    assert first.endswith("-0b")
