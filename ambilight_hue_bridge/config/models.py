"""Configuration data models for the Ambilight+Hue Pro Bridge."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro.mixins.yaml import DataClassYAMLMixin

from ambilight_hue_bridge.const import DEFAULT_HTTP_PORT, STREAMING_LIGHT_MODEL_ID


@dataclass
class VirtualLight:
    """A light the virtual bridge exposes to the TV."""

    id: str
    name: str
    modelid: str = STREAMING_LIGHT_MODEL_ID
    # Informational placement hint shown in the UI (left/right/center/top/bottom/behind).
    position: str = "center"
    # Real-bridge entertainment channel ids this light drives (wired in later milestones).
    channels: list[int] = field(default_factory=list)


@dataclass
class PairedUser:
    """A username created by a client through the pushlink pairing flow."""

    username: str
    clientkey: str
    devicetype: str
    created: str


@dataclass
class VirtualBridge:
    """Settings for the virtual Hue bridge presented to the TV."""

    name: str = "Ambilight Bridge"
    # None => auto-detect the MAC from the host on first run.
    mac: str | None = None
    http_port: int = DEFAULT_HTTP_PORT
    enable_inbound_dtls: bool = False


@dataclass
class Config(DataClassYAMLMixin):
    """Top-level persisted configuration."""

    virtual_bridge: VirtualBridge = field(default_factory=VirtualBridge)
    virtual_lights: list[VirtualLight] = field(default_factory=list)
    users: list[PairedUser] = field(default_factory=list)
