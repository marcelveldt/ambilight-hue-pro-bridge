"""Constants for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

import logging
from typing import Final

PACKAGE_NAME: Final = "ambilight_hue_bridge"
DISPLAY_NAME: Final = "Ambilight+Hue Pro Bridge"

# Directory for persistent configuration and state.
DEFAULT_DATA_DIR: Final = "data"
CONFIG_FILENAME: Final = "config.yaml"
# Home Assistant writes the add-on's UI options here, inside the data dir; read as a config
# source when present (a no-op for plain Docker / source runs).
ADDON_OPTIONS_FILENAME: Final = "options.json"

# Custom log level below DEBUG (10) for the high-frequency trace noise - SSDP datagrams and the
# web UI's own polling. At INFO you see lifecycle events; DEBUG adds the TV's requests; VERBOSE
# adds the firehose. Registered here (next to the definition, idempotently) so the name resolves
# and renders regardless of which module is imported first.
VERBOSE: Final = 5
logging.addLevelName(VERBOSE, "VERBOSE")
# Rotating log file written under the data dir (in addition to the console) by default.
LOG_FILENAME: Final = "bridge.log"

# Single HTTP port serving the TV-facing Hue API + descriptor AND the web UI. This is a
# command-line option only (never persisted). 8080 is a convenient default; older
# Ambilight+Hue TVs assume the Hue bridge is on port 80, so run with --http-port 80 for them.
DEFAULT_HTTP_PORT: Final = 8080

# Optional HTTPS port serving the same app with a Hue-style self-signed certificate. Off by
# default (0): every Ambilight+Hue TV tested so far connects over plain HTTP and never uses the
# cert, and binding 443 needs privileges. Set a port (e.g. 443) to enable TLS for a future
# client that requires it. Command-line only.
DEFAULT_HTTPS_PORT: Final = 0

# Filenames for the persisted bridge TLS certificate (generated once, then pinned by clients).
CERT_FILENAME: Final = "bridge_cert.pem"
CERT_KEY_FILENAME: Final = "bridge_key.pem"

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

# Outbound streaming bounds. The Hue bridge tops out around 50-60 Hz; per-TV smoothing is
# capped below 1.0 because 1.0 would mean the eased color never converges on its target (a
# frozen output). The frame rate is a config-only field (no UI), defaulting to 50 Hz.
DEFAULT_STREAM_RATE_HZ: Final = 50
MAX_STREAM_SMOOTHING: Final = 0.95

# devicetype registered with the real bridge when pairing; shown in the Hue app's list of
# connected apps (format "appname#devicename").
PAIR_DEVICE_TYPE: Final = "ambilight_hue_bridge#bridge"

# Default modelid used for synthesized virtual lights. It must be a model the TV firmware
# recognizes as Entertainment-streaming-capable, otherwise the TV falls back to the slow
# ~1 Hz v1 path for that lamp. This is configurable per virtual light and likely needs
# tuning against real TV firmware (see docs/ARCHITECTURE.md, risk #1 / M0 capture).
STREAMING_LIGHT_MODEL_ID: Final = "LCX004"
