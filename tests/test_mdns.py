"""Tests for the mDNS ``_hue._tcp`` advertisement record and advertiser lifecycle."""

from __future__ import annotations

import socket

from ambilight_hue_bridge.discovery.mdns import (
    HUE_SERVICE_TYPE,
    MDNSAdvertiser,
    build_service_info,
)


def test_service_info_mirrors_a_bsb002_bridge() -> None:
    """The advertised record carries the bridge identity a Hue client looks for."""
    info = build_service_info(host_ip="192.168.1.50", port=443, bridge_id="AABBCCFFFEDDEEFF")
    assert info.type == HUE_SERVICE_TYPE
    assert info.port == 443
    assert socket.inet_aton("192.168.1.50") in info.addresses
    # Decode the TXT exactly as hue_entertainment.discover_bridges does (round-trip check).
    properties = {
        key.decode(): (value.decode() if value else "")
        for key, value in (info.properties or {}).items()
    }
    assert properties["modelid"] == "BSB002"
    assert properties["bridgeid"] == "AABBCCFFFEDDEEFF"
    assert info.server == "aabbccfffeddeeff.local."


async def test_advertiser_registers_then_unregisters(monkeypatch) -> None:
    """start() registers the service; stop() unregisters it and closes zeroconf."""
    created: list[_FakeZeroconf] = []
    monkeypatch.setattr(
        "ambilight_hue_bridge.discovery.mdns.AsyncZeroconf",
        lambda **_kwargs: _record(created, _FakeZeroconf()),
    )
    advertiser = MDNSAdvertiser(host_ip="1.2.3.4", port=443, bridge_id="AABBCCFFFEDDEEFF")
    await advertiser.start()
    assert created[0].registered is not None
    await advertiser.stop()
    assert created[0].unregistered is created[0].registered
    assert created[0].closed is True


async def test_advertiser_swallows_register_failure(monkeypatch) -> None:
    """A registration OSError is logged and swallowed (zeroconf is closed; stop() is a no-op)."""
    created: list[_FakeZeroconf] = []
    monkeypatch.setattr(
        "ambilight_hue_bridge.discovery.mdns.AsyncZeroconf",
        lambda **_kwargs: _record(created, _FakeZeroconf(fail=True)),
    )
    advertiser = MDNSAdvertiser(host_ip="1.2.3.4", port=443, bridge_id="AABBCCFFFEDDEEFF")
    await advertiser.start()  # must not raise
    assert created[0].closed is True
    await advertiser.stop()  # no-op, must not raise


def _record(bucket: list, item):  # noqa: ANN202 - tiny test helper
    """Append a constructed fake to a bucket and return it (so the test can inspect it)."""
    bucket.append(item)
    return item


class _FakeZeroconf:
    """AsyncZeroconf stand-in recording register/unregister/close (no real sockets)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.registered: object | None = None
        self.unregistered: object | None = None
        self.closed = False

    async def async_register_service(self, info: object) -> None:
        if self._fail:
            msg = "no multicast"
            raise OSError(msg)
        self.registered = info

    async def async_unregister_service(self, info: object) -> None:
        self.unregistered = info

    async def async_close(self) -> None:
        self.closed = True
