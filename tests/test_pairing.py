"""Tests for the pushlink pairing manager and its area auto-assignment."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import CachedArea, CachedChannel, RealBridge
from ambilight_hue_bridge.config.store import ConfigStore
from ambilight_hue_bridge.emulator.pairing import PairingManager

if TYPE_CHECKING:
    from pathlib import Path


def _store_with_cached_bridge(tmp_path: Path) -> ConfigStore:
    """Build a store with an active bridge whose entertainment areas are already cached."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    store.config.real_bridges = [
        RealBridge(
            id="b",
            host="1.2.3.4",
            app_key="u",
            client_key="k",
            cached_areas=[
                CachedArea(
                    id="area-1",
                    name="Living",
                    channels=[
                        CachedChannel(channel_id=0, name="Left", position=[-0.9, 0.8, 0.0]),
                        CachedChannel(channel_id=1, name="Right", position=[0.9, 0.8, 0.0]),
                    ],
                ),
            ],
        ),
    ]
    store.config.active_real_bridge = "b"
    return store


def test_create_user_auto_assigns_first_area(tmp_path: Path) -> None:
    """A new TV gets the active bridge's first cached area (and its lights) by default."""
    store = _store_with_cached_bridge(tmp_path)
    pairing = PairingManager(store)
    user = pairing.create_user("TV", generate_clientkey=True)
    assert user.entertainment_area == "area-1"
    assert [light.name for light in user.lights] == ["Left", "Right"]
    assert store.config.users[0].entertainment_area == "area-1"


def test_create_user_without_bridge_has_no_lights(tmp_path: Path) -> None:
    """With no bridge configured, a new TV is left unassigned (no lights, no stream)."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    pairing = PairingManager(store)
    user = pairing.create_user("TV", generate_clientkey=True)
    assert user.entertainment_area == ""
    assert user.lights == []
