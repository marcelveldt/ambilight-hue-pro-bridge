"""Tests for configuration loading and persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import (
    PairedUser,
    StreamTarget,
    VirtualLight,
    ZigbeeLightRef,
)
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
            enabled=False,
            stream_priority=42,
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
    assert config.users[0].enabled is False
    assert config.users[0].stream_priority == 42
    assert config.users[0].entertainment_area == "area-1"
    assert config.users[0].lights[0].id == "7"
    assert config.users[0].lights[0].position == "behind"


def test_load_old_tv_config_uses_policy_defaults(tmp_path: Path) -> None:
    """Existing configs without policy fields keep TVs enabled at equal priority."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
users:
  - username: u1
    clientkey: k
    devicetype: TV
    created: "2026-06-10"
""".lstrip(),
        encoding="utf-8",
    )
    config = ConfigStore(path).load()
    assert config.users[0].enabled is True
    assert config.users[0].stream_priority == 0


def test_zigbee_target_roundtrip(tmp_path: Path) -> None:
    """A TV's Zigbee target and lamps and the Home Assistant connection round-trip."""
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    store.load()
    store.config.users.append(
        PairedUser(
            username="u1",
            clientkey="k",
            devicetype="TV",
            created="2026-11-10",
            target=StreamTarget.ZIGBEE,
            zigbee_lights=[
                ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Couch"),
                ZigbeeLightRef(ieee="00:17:88:01:00:00:00:02", name="Signe", segments=7),
            ],
        ),
    )
    store.config.home_assistant.url = "http://homeassistant.local:8123"
    store.config.home_assistant.token = "token"
    store.save()
    assert "target: zigbee" in path.read_text(encoding="utf-8")

    config = ConfigStore(path).load()
    assert config.users[0].target is StreamTarget.ZIGBEE
    assert config.users[0].zigbee_lights == [
        ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Couch"),
        ZigbeeLightRef(ieee="00:17:88:01:00:00:00:02", name="Signe", segments=7),
    ]
    assert config.home_assistant.url == "http://homeassistant.local:8123"
    assert config.home_assistant.token == "token"


def test_load_old_config_defaults_to_the_hue_target(tmp_path: Path) -> None:
    """Configs from before the Zigbee target keep TVs on the Hue bridge, with no HA connection."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
users:
  - username: u1
    clientkey: k
    devicetype: TV
    created: "2026-06-10"
    entertainment_area: area-1
""".lstrip(),
        encoding="utf-8",
    )
    config = ConfigStore(path).load()
    assert config.users[0].target is StreamTarget.HUE
    assert config.users[0].zigbee_lights == []
    assert config.home_assistant.url == ""
    assert config.home_assistant.token == ""


def test_zigbee_lamp_from_before_gradient_zones_loads_as_one_segment(tmp_path: Path) -> None:
    """A lamp saved without a segment count loads as a lamp without segments."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
users:
  - username: u1
    clientkey: k
    devicetype: TV
    created: "2026-11-10"
    target: zigbee
    zigbee_lights:
      - ieee: "00:17:88:01:00:00:00:01"
        name: Signe
""".lstrip(),
        encoding="utf-8",
    )
    config = ConfigStore(path).load()
    assert config.users[0].zigbee_lights == [
        ZigbeeLightRef(ieee="00:17:88:01:00:00:00:01", name="Signe", segments=1)
    ]
