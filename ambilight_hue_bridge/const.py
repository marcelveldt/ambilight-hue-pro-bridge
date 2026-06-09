"""Constants for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

from typing import Final

PACKAGE_NAME: Final = "ambilight_hue_bridge"
DISPLAY_NAME: Final = "Ambilight+Hue Pro Bridge"

# Default directory for persistent configuration and state.
DEFAULT_DATA_DIR: Final = "data"

# Default TCP port for the web configuration interface.
# NOTE: the virtual Hue bridge HTTP/SSDP endpoints are a separate concern and
# their ports are decided during the architecture phase (see README).
DEFAULT_WEB_PORT: Final = 8080
