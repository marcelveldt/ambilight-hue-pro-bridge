"""Constants for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

from typing import Final

PACKAGE_NAME: Final = "ambilight_hue_bridge"
DISPLAY_NAME: Final = "Ambilight+Hue Pro Bridge"

# Directory for persistent configuration and state.
DEFAULT_DATA_DIR: Final = "data"
CONFIG_FILENAME: Final = "config.yaml"

# The virtual bridge serves the legacy Hue v1 REST API and the UPnP descriptor on this
# TCP port. A real Hue bridge always uses port 80, and some TVs assume it, so 80 is the
# faithful default (running on it needs privileges; the HA add-on runs as root).
DEFAULT_HTTP_PORT: Final = 80

# SSDP / UPnP discovery.
SSDP_MCAST_ADDR: Final = "239.255.255.250"
SSDP_PORT: Final = 1900
SSDP_NOTIFY_INTERVAL: Final = 60.0  # seconds between ssdp:alive NOTIFY broadcasts

# Bridge identity advertised to clients. These mimic a real "2015" Hue bridge (BSB002);
# the version values match what diyHue advertises so TVs that enforce a minimum
# apiversion accept us.
BRIDGE_MODEL_ID: Final = "BSB002"
BRIDGE_SW_VERSION: Final = "1967054020"
BRIDGE_API_VERSION: Final = "1.67.0"
BRIDGE_DATASTORE_VERSION: Final = "126"
# UPnP UDN/USN are built as uuid:<UDN_PREFIX><serial>, matching a real Hue bridge.
UDN_PREFIX: Final = "2f402f80-da50-11e1-9b23-"
UPNP_SERVER: Final = "Linux/3.14.0 UPnP/1.0 IpBridge/1.67.0"

# Default modelid used for synthesized virtual lights. It must be a model the TV firmware
# recognizes as Entertainment-streaming-capable, otherwise the TV falls back to the slow
# ~1 Hz v1 path for that lamp. This is configurable per virtual light and likely needs
# tuning against real TV firmware (see docs/ARCHITECTURE.md, risk #1 / M0 capture).
STREAMING_LIGHT_MODEL_ID: Final = "LCX004"
