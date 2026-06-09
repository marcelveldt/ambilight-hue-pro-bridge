"""Tests for bridge identity derivation."""

from __future__ import annotations

import pytest

from ambilight_hue_bridge.identity import (
    bridge_id,
    bridge_serial,
    bridge_udn,
    mac_with_colons,
    normalize_mac,
)


def test_normalize_mac_strips_separators() -> None:
    """Separators are removed and the result is lowercased."""
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"


def test_normalize_mac_rejects_bad_length() -> None:
    """A MAC that is not 12 hex characters is rejected."""
    with pytest.raises(ValueError, match="Invalid MAC"):
        normalize_mac("aabbcc")


def test_bridge_id_inserts_fffe_and_uppercases() -> None:
    """The bridge id is first6 + FFFE + last6, uppercased."""
    assert bridge_id("aa:bb:cc:dd:ee:ff") == "AABBCCFFFEDDEEFF"


def test_bridge_serial_and_udn() -> None:
    """The serial is the normalized MAC and the UDN embeds it."""
    assert bridge_serial("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert bridge_udn("aabbccddeeff") == "uuid:2f402f80-da50-11e1-9b23-aabbccddeeff"


def test_mac_with_colons() -> None:
    """The MAC is formatted with colons."""
    assert mac_with_colons("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
