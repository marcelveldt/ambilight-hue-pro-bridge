"""Tests for the UPnP descriptor and SSDP message building."""

from __future__ import annotations

from ambilight_hue_bridge.discovery.description import build_description_xml
from ambilight_hue_bridge.discovery.ssdp import SSDPServer
from ambilight_hue_bridge.identity import bridge_id, bridge_udn

_MAC = "aabbccddeeff"


def _server() -> SSDPServer:
    return SSDPServer(
        host_ip="1.2.3.4",
        http_port=80,
        bridge_id=bridge_id(_MAC),
        udn=bridge_udn(_MAC),
    )


def test_description_contains_bridge_identity() -> None:
    """The descriptor identifies as a 2015 BSB002 bridge with the derived UDN."""
    xml = build_description_xml(name="Test Bridge", mac=_MAC, host="1.2.3.4", port=80)
    assert "Philips hue bridge 2015" in xml
    assert "<modelNumber>BSB002</modelNumber>" in xml
    # Serial/UDN embed the bridgeid (with FFFE), matching the advertised hue-bridgeid.
    assert "uuid:2f402f80-da50-11e1-9b23-aabbccfffeddeeff" in xml
    assert "<serialNumber>aabbccfffeddeeff</serialNumber>" in xml
    assert "http://1.2.3.4:80/" in xml


def test_search_responses_carry_all_variants() -> None:
    """M-SEARCH responses carry the bridge id, LOCATION and all three ST variants."""
    text = "".join(response.decode() for response in _server()._search_responses())
    assert "hue-bridgeid: AABBCCFFFEDDEEFF" in text
    assert "LOCATION: http://1.2.3.4:80/description.xml" in text
    assert "ST: upnp:rootdevice" in text
    assert "ST: uuid:2f402f80-da50-11e1-9b23-aabbccfffeddeeff" in text
    assert "ST: urn:schemas-upnp-org:device:basic:1" in text


def test_notify_messages_advertise_alive() -> None:
    """NOTIFY messages advertise ssdp:alive for each variant."""
    text = "".join(message.decode() for message in _server()._notify_messages())
    assert "NOTIFY * HTTP/1.1" in text
    assert "NTS: ssdp:alive" in text
    assert text.count("hue-bridgeid: AABBCCFFFEDDEEFF") == 3
