"""Builds the UPnP device descriptor (description.xml) served to clients."""

from __future__ import annotations

from ambilight_hue_bridge.const import BRIDGE_MODEL_ID
from ambilight_hue_bridge.identity import bridge_serial, bridge_udn


def build_description_xml(*, name: str, mac: str, host: str, port: int) -> str:
    """
    Build the UPnP ``description.xml`` for the virtual bridge.

    It mimics a real 2015 Hue bridge (BSB002), which Ambilight+Hue TVs validate.

    :param name: Friendly bridge name.
    :param mac: Host MAC address used to derive the UDN and serial number.
    :param host: IP address clients use to reach this bridge.
    :param port: TCP port the descriptor and v1 API are served on.
    """
    udn = bridge_udn(mac)
    serial = bridge_serial(mac)
    url_base = f"http://{host}:{port}/"
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
        "<specVersion><major>1</major><minor>0</minor></specVersion>\n"
        f"<URLBase>{url_base}</URLBase>\n"
        "<device>\n"
        "<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>\n"
        f"<friendlyName>{name} ({host})</friendlyName>\n"
        "<manufacturer>Signify</manufacturer>\n"
        "<manufacturerURL>http://www.meethue.com</manufacturerURL>\n"
        "<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>\n"
        "<modelName>Philips hue bridge 2015</modelName>\n"
        f"<modelNumber>{BRIDGE_MODEL_ID}</modelNumber>\n"
        "<modelURL>http://www.meethue.com</modelURL>\n"
        f"<serialNumber>{serial}</serialNumber>\n"
        f"<UDN>{udn}</UDN>\n"
        "<presentationURL>index.html</presentationURL>\n"
        "</device>\n"
        "</root>\n"
    )
