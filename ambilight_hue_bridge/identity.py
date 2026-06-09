"""Bridge identity derivation (bridge id, serial, UPnP UDN) from a MAC address."""

from __future__ import annotations

import uuid

from .const import UDN_PREFIX

_MAC_HEX_LEN = 12


def get_host_mac() -> str:
    """Return the host's primary MAC address as 12 lowercase hex characters."""
    return f"{uuid.getnode():012x}"


def normalize_mac(mac: str) -> str:
    """
    Normalize a MAC address to 12 lowercase hex characters without separators.

    :param mac: MAC address, with or without ``:`` / ``-`` separators.
    """
    cleaned = mac.replace(":", "").replace("-", "").lower()
    if len(cleaned) != _MAC_HEX_LEN:
        msg = f"Invalid MAC address: {mac!r}"
        raise ValueError(msg)
    return cleaned


def bridge_id(mac: str) -> str:
    """Return the Hue ``bridgeid`` for a MAC (first6 + ``FFFE`` + last6, uppercased)."""
    normalized = normalize_mac(mac)
    return (normalized[0:6] + "fffe" + normalized[6:12]).upper()


def bridge_serial(mac: str) -> str:
    """Return the bridge serial (the normalized 12-hex MAC) used in the UPnP UDN."""
    return normalize_mac(mac)


def bridge_udn(mac: str) -> str:
    """Return the UPnP UDN (``uuid:...``) derived from the MAC."""
    return f"uuid:{UDN_PREFIX}{bridge_serial(mac)}"


def mac_with_colons(mac: str) -> str:
    """Return the MAC formatted with colons, e.g. ``aa:bb:cc:dd:ee:ff``."""
    normalized = normalize_mac(mac)
    return ":".join(normalized[i : i + 2] for i in range(0, _MAC_HEX_LEN, 2))
