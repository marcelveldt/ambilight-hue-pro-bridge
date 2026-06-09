"""Tests for configuration loading and persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import VirtualLight
from ambilight_hue_bridge.config.store import ConfigStore

if TYPE_CHECKING:
    from pathlib import Path


def test_load_creates_default_file(tmp_path: Path) -> None:
    """Loading when no file exists writes a default config to disk."""
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    config = store.load()
    assert path.exists()
    assert config.virtual_bridge.name == "Ambilight Bridge"
    assert config.virtual_lights == []


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    """Saved configuration round-trips through YAML."""
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    store.load()
    store.config.virtual_lights.append(VirtualLight(id="7", name="Strip", position="behind"))
    store.config.virtual_bridge.name = "Living Room Bridge"
    store.save()

    reloaded = ConfigStore(path)
    config = reloaded.load()
    assert config.virtual_bridge.name == "Living Room Bridge"
    assert len(config.virtual_lights) == 1
    assert config.virtual_lights[0].id == "7"
    assert config.virtual_lights[0].position == "behind"
