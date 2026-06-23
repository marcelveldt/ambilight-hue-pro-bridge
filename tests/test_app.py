"""Tests for app-level helpers (stable bridge identity)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambilight_hue_bridge.app import resolve_mac
from ambilight_hue_bridge.config.store import ConfigStore

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_mac_persists_for_stable_identity(tmp_path: Path, monkeypatch) -> None:
    """The MAC is detected once, persisted, and stays constant even if detection would differ."""
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    assert store.config.virtual_bridge.mac is None

    # Simulate uuid.getnode() returning a fresh random MAC on each call (the container behaviour).
    detected = iter(["aabbccddeeff", "001122334455"])
    monkeypatch.setattr("ambilight_hue_bridge.app.get_host_mac", lambda: next(detected))

    first = resolve_mac(store)
    assert first == "aabbccddeeff"
    assert store.config.virtual_bridge.mac == "aabbccddeeff"

    # A second resolution (e.g. the next restart) must reuse the persisted value, not re-detect -
    # if it re-detected it would get the next (different) MAC from the iterator.
    assert resolve_mac(store) == "aabbccddeeff"


def test_resolve_mac_survives_a_fresh_store_load(tmp_path: Path, monkeypatch) -> None:
    """The persisted MAC is read back from disk by a new store (a real process restart)."""
    monkeypatch.setattr("ambilight_hue_bridge.app.get_host_mac", lambda: "aabbccddeeff")
    store = ConfigStore(tmp_path / "config.yaml")
    store.load()
    resolve_mac(store)

    reloaded = ConfigStore(tmp_path / "config.yaml")
    reloaded.load()
    assert reloaded.config.virtual_bridge.mac == "aabbccddeeff"
    # Even if detection would now yield something else, the stored identity wins.
    monkeypatch.setattr("ambilight_hue_bridge.app.get_host_mac", lambda: "ffffffffffff")
    assert resolve_mac(reloaded) == "aabbccddeeff"
