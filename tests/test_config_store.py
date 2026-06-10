"""Tests for configuration loading and persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import PairedUser, VirtualLight
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
    assert config.users == []


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    """Saved configuration (incl. a TV's per-area assignment) round-trips through YAML."""
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    store.load()
    store.config.users.append(
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-06-10",
            entertainment_area="area-1",
            lights=[VirtualLight(id="7", name="Strip", position="behind")],
        ),
    )
    store.config.virtual_bridge.name = "Living Room Bridge"
    store.save()

    reloaded = ConfigStore(path)
    config = reloaded.load()
    assert config.virtual_bridge.name == "Living Room Bridge"
    assert len(config.users) == 1
    assert config.users[0].entertainment_area == "area-1"
    assert config.users[0].lights[0].id == "7"
    assert config.users[0].lights[0].position == "behind"
