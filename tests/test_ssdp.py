"""Tests for the SSDP discovery responder logic (no socket binding)."""

from __future__ import annotations

from ambilight_hue_bridge.discovery.ssdp import SSDPServer, _header_value


class _FakeTransport:
    """Records datagrams passed to ``sendto`` so responses can be asserted."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        """Record an outgoing datagram."""
        self.sent.append((data, addr))


def _server_with_transport() -> tuple[SSDPServer, _FakeTransport]:
    """Build an SSDPServer wired to a fake transport (no real socket)."""
    server = SSDPServer(
        host_ip="1.2.3.4",
        http_port=80,
        bridge_id="AABBCCFFFEDDEEFF",
        udn="uuid:2f402f80-da50-11e1-9b23-aabbccddeeff",
    )
    transport = _FakeTransport()
    server._transport = transport  # type: ignore[assignment]
    return server, transport


def test_header_value_is_case_insensitive() -> None:
    """A header value is parsed regardless of header-name casing, else None."""
    message = 'M-SEARCH * HTTP/1.1\r\nST: upnp:rootdevice\r\nMAN: "ssdp:discover"\r\n\r\n'
    assert _header_value(message, "ST") == "upnp:rootdevice"
    assert _header_value(message, "st") == "upnp:rootdevice"
    assert _header_value(message, "Missing") is None


def test_msearch_gets_one_response_per_variant() -> None:
    """An M-SEARCH with ssdp:discover is answered, one 200 OK per ST variant, to the sender."""
    server, transport = _server_with_transport()
    msearch = b'M-SEARCH * HTTP/1.1\r\nMAN: "ssdp:discover"\r\nST: ssdp:all\r\n\r\n'
    server.datagram_received(msearch, ("9.9.9.9", 1900))
    assert len(transport.sent) == 3
    first = transport.sent[0][0].decode()
    assert first.startswith("HTTP/1.1 200 OK")
    assert "LOCATION: http://1.2.3.4:80/description.xml" in first
    assert "hue-bridgeid: AABBCCFFFEDDEEFF" in first
    assert all(addr == ("9.9.9.9", 1900) for _data, addr in transport.sent)


def test_non_discover_datagram_is_ignored() -> None:
    """A datagram without ssdp:discover produces no response."""
    server, transport = _server_with_transport()
    server.datagram_received(b"NOTIFY * HTTP/1.1\r\nNTS: ssdp:alive\r\n\r\n", ("9.9.9.9", 1900))
    assert transport.sent == []
