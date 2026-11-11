"""Configuration data models for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro.mixins.yaml import DataClassYAMLMixin

from ambilight_hue_bridge.const import DEFAULT_STREAM_RATE_HZ, STREAMING_LIGHT_MODEL_ID


class StreamTarget(StrEnum):
    """Where a TV's colors are streamed to."""

    # An entertainment area on the real Hue bridge.
    HUE = "hue"
    # Zigbee lamps from Home Assistant's ZHA, streamed directly without a Hue bridge.
    ZIGBEE = "zigbee"


@dataclass
class VirtualLight:
    """A light the virtual bridge exposes to the TV."""

    id: str
    name: str
    modelid: str = STREAMING_LIGHT_MODEL_ID
    # Informational placement hint shown in the UI (left/right/center/top/bottom/behind).
    position: str = "center"
    # Real-bridge entertainment channel ids this light drives.
    channels: list[int] = field(default_factory=list)


@dataclass
class ZigbeeLightRef:
    """A Zigbee lamp a TV streams to, identified by its IEEE address."""

    ieee: str
    name: str = ""
    # Gradient segments the lamp reported when it was added to the TV (1 for a lamp without).
    segments: int = 1


@dataclass
class PairedUser:
    """A username created by a client (TV) through the pushlink pairing flow."""

    username: str
    clientkey: str
    devicetype: str
    created: str
    # Name given in the web UI ("" => the UI shows the devicetype).
    name: str = ""
    # Whether this TV may open an outbound Hue Entertainment session.
    enabled: bool = True
    # Higher values win when multiple TVs request the same output (a Hue bridge or Zigbee lamps).
    stream_priority: int = 0
    # Where this TV's colors go: a Hue bridge entertainment area or Zigbee lamps from ZHA.
    target: StreamTarget = StreamTarget.HUE
    # Per-TV assignment: the source-bridge entertainment area this TV drives and the resulting
    # lights exposed to this TV (one per channel, so each gradient zone is its own light).
    # Unassigned (empty area) => the TV sees no lights and does not stream until assigned.
    entertainment_area: str = ""
    lights: list[VirtualLight] = field(default_factory=list)
    # The ZHA lamps a Zigbee-target TV streams to; the TV sees one light per lamp, or per zone of a
    # gradient lamp. Stored by IEEE address only, as the network address can change.
    zigbee_lights: list[ZigbeeLightRef] = field(default_factory=list)
    # Per-TV temporal easing (0 => off, None => automatic). Fast DTLS-streaming TVs are already
    # smooth and want 0; the ~1 Hz REST TVs (older models) want easing to fill the gaps between
    # updates.
    stream_smoothing: float | None = None


@dataclass
class VirtualBridge:
    """Settings for the virtual Hue bridge presented to the TV."""

    name: str = "Ambilight Bridge"
    # None => auto-detect the MAC from the host on first run.
    mac: str | None = None
    # Listen for newer (Android) TVs that stream entertainment over inbound DTLS (UDP 2100).
    enable_inbound_dtls: bool = True
    # Advertise the bridge over mDNS (_hue._tcp) for newer Hue clients, in addition to SSDP.
    # Points at the TLS port when HTTPS is enabled, otherwise the HTTP port. LAN-only, additive.
    enable_mdns: bool = True
    # Outbound frame rate to the real bridge, in Hz (the bridge tops out around 50-60).
    stream_rate_hz: int = DEFAULT_STREAM_RATE_HZ


@dataclass
class CachedChannel:
    """A cached entertainment channel (mirrors hue_entertainment.LightChannel for offline use)."""

    channel_id: int
    service_id: str = ""
    name: str = ""
    position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


@dataclass
class CachedArea:
    """A cached entertainment area, so the bridge can serve areas while the real bridge is down."""

    id: str
    name: str
    channels: list[CachedChannel] = field(default_factory=list)


@dataclass
class RealBridge:
    """A real Hue bridge (V2 or Pro) the colors are streamed to via the Entertainment API."""

    id: str
    host: str
    app_key: str = ""
    client_key: str = ""
    # Last-seen entertainment areas, refreshed whenever we reach the bridge. Lets the web UI and
    # per-TV assignment keep working while the bridge is briefly unplugged (the discovery dance).
    cached_areas: list[CachedArea] = field(default_factory=list)


@dataclass
class HomeAssistantSettings:
    """The Home Assistant connection used when not running as a Home Assistant add-on."""

    # Base URL, e.g. http://homeassistant.local:8123.
    url: str = ""
    # Long-lived access token of an administrator (ZHA's network settings require one).
    token: str = ""


@dataclass
class Config(DataClassYAMLMixin):
    """Top-level persisted configuration."""

    virtual_bridge: VirtualBridge = field(default_factory=VirtualBridge)
    users: list[PairedUser] = field(default_factory=list)
    real_bridges: list[RealBridge] = field(default_factory=list)
    # id of the real bridge currently streamed to (empty => first configured bridge).
    active_real_bridge: str = ""
    home_assistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
