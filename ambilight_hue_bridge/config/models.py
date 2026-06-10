"""Configuration data models for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro.mixins.yaml import DataClassYAMLMixin

from ambilight_hue_bridge.const import DEFAULT_STREAM_RATE_HZ, STREAMING_LIGHT_MODEL_ID


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
class PairedUser:
    """A username created by a client (TV) through the pushlink pairing flow."""

    username: str
    clientkey: str
    devicetype: str
    created: str
    # Per-TV assignment: the source-bridge entertainment area this TV drives, whether to split
    # gradient lights into per-zone virtual lights, and the resulting lights exposed to this TV.
    # Unassigned (empty area) => the TV sees no lights and does not stream until assigned.
    entertainment_area: str = ""
    split_gradients: bool = True
    lights: list[VirtualLight] = field(default_factory=list)
    # Per-TV smoothing override (None => use the global default). DTLS-streaming TVs send a fast,
    # already-smooth stream and want ~0; ~1 Hz REST TVs want easing to fill the gaps.
    stream_smoothing: float | None = None


@dataclass
class VirtualBridge:
    """Settings for the virtual Hue bridge presented to the TV."""

    name: str = "Ambilight Bridge"
    # None => auto-detect the MAC from the host on first run.
    mac: str | None = None
    # Listen for newer (Android) TVs that stream entertainment over inbound DTLS (UDP 2100).
    enable_inbound_dtls: bool = True
    # Advertise the bridge over mDNS (_hue._tcp on the TLS port) for newer Hue clients, in
    # addition to SSDP. Only takes effect when the HTTPS listener is up. LAN-only, additive.
    enable_mdns: bool = True
    # Outbound frame rate to the real bridge, in Hz (the bridge tops out around 50-60).
    stream_rate_hz: int = DEFAULT_STREAM_RATE_HZ
    # Temporal easing applied to the TV's colors before forwarding: the fraction of the
    # previous frame retained each tick (0.0 = off/instant but abrupt, higher = smoother but
    # laggier). Smooths the TV's stepwise Ambilight into fades; ~0.5 is a good balance.
    stream_smoothing: float = 0.5


@dataclass
class RealBridge:
    """A real Hue bridge (V2 or Pro) the colors are streamed to via the Entertainment API."""

    id: str
    host: str
    app_key: str = ""
    client_key: str = ""
    # "v2" (square) or "pro" - informational; both use the same CLIP v2 + DTLS path.
    model: str = "v2"


@dataclass
class Config(DataClassYAMLMixin):
    """Top-level persisted configuration."""

    virtual_bridge: VirtualBridge = field(default_factory=VirtualBridge)
    users: list[PairedUser] = field(default_factory=list)
    real_bridges: list[RealBridge] = field(default_factory=list)
    # id of the real bridge currently streamed to (empty => first configured bridge).
    active_real_bridge: str = ""
