"""Pairing with and inspecting real Hue bridges, and resolving the active bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hue_entertainment import HueEntertainmentAPI

from ambilight_hue_bridge.config.models import RealBridge, VirtualLight
from ambilight_hue_bridge.const import PAIR_DEVICE_TYPE

if TYPE_CHECKING:
    from hue_entertainment import EntertainmentArea

    from ambilight_hue_bridge.config.models import PairedUser, ZigbeeLightRef
    from ambilight_hue_bridge.config.store import ConfigStore

# Hue v1 light names are capped at 32 chars; longer names are rejected and some TVs then show
# a generic "light N" instead of the real name.
_MAX_LIGHT_NAME = 32
# Zones a gradient lamp of a Zigbee-target TV is split into.
_ZIGBEE_GRADIENT_ZONES = 3


async def pair_bridge(host: str) -> dict[str, str]:
    """
    Pair with a real Hue bridge; the link button must be pressed first.

    :param host: Bridge IP address or hostname.
    """
    api = HueEntertainmentAPI(host)
    try:
        credentials: dict[str, str] = await api.pair(device_type=PAIR_DEVICE_TYPE)
        return credentials
    finally:
        await api.close()


async def pair_and_store(store: ConfigStore, host: str, *, set_active: bool = True) -> RealBridge:
    """
    Pair with a bridge and persist its credentials in the configuration.

    :param store: The config store to update and save.
    :param host: Bridge IP address or hostname.
    :param set_active: Whether to mark the paired bridge as the active one.
    """
    credentials = await pair_bridge(host)
    if "username" not in credentials or "clientkey" not in credentials:
        msg = "the bridge returned an unexpected pairing response"
        raise OSError(msg)
    bridge = _upsert_bridge(store, host, credentials)
    if set_active:
        store.config.active_real_bridge = bridge.id
    store.save()
    return bridge


async def list_areas(host: str, app_key: str) -> list[EntertainmentArea]:
    """
    List the entertainment configurations available on a real bridge.

    :param host: Bridge IP address or hostname.
    :param app_key: The bridge application key obtained from pairing.
    """
    api = HueEntertainmentAPI(host, app_key)
    try:
        areas: list[EntertainmentArea] = await api.get_entertainment_areas()
        return areas
    finally:
        await api.close()


def tv_lights(store: ConfigStore, username: str | None) -> list[VirtualLight]:
    """
    Return the virtual lights exposed to a TV.

    Each TV gets only the lights of its assigned entertainment area; an unassigned TV sees
    no lights until an area is assigned to it in the web UI.
    """
    user = paired_user(store, username)
    return user.lights if user is not None else []


def tv_stream_target(
    store: ConfigStore,
    username: str | None,
) -> tuple[str, list[VirtualLight]]:
    """
    Return the (entertainment_area, lights) a TV streams to.

    Resolved purely from the TV's own assignment; an unassigned TV returns ("", []) and does
    not stream.
    """
    user = paired_user(store, username)
    return (user.entertainment_area, user.lights) if user is not None else ("", [])


def paired_user(store: ConfigStore, username: str | None) -> PairedUser | None:
    """Return the paired TV for a username, or None."""
    if username:
        for user in store.config.users:
            if user.username == username:
                return user
    return None


def active_bridge(store: ConfigStore) -> RealBridge | None:
    """Return the configured active real bridge (or the first one, or None)."""
    bridges = store.config.real_bridges
    active = store.config.active_real_bridge
    if active:
        for bridge in bridges:
            if bridge.id == active:
                return bridge
    return bridges[0] if bridges else None


def lights_from_area(area: Any, *, split_gradients: bool = True) -> list[VirtualLight]:
    """
    Build the virtual lights a TV sees from a source entertainment area.

    With ``split_gradients`` each channel becomes its own light (so the TV can drive each
    gradient zone separately, disambiguated by on-screen position when they share a name).
    Otherwise channels of the same light (e.g. a gradient strip's zones) are merged into one
    light driving all its channels.

    Works on both a live ``EntertainmentArea`` and a persisted ``CachedArea`` (both expose
    ``channels`` with ``channel_id``/``service_id``/``name``/``position``).

    :param area: An EntertainmentArea (live) or CachedArea (persisted) to mirror.
    :param split_gradients: Expose each channel as a separate light (vs one light per device).
    """
    if not split_gradients:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for channel in area.channels:
            key = channel.service_id or channel.name
            if key not in merged:
                merged[key] = {"name": channel.name, "x": channel.position[0], "channels": []}
                order.append(key)
            merged[key]["channels"].append(channel.channel_id)
        return [
            VirtualLight(
                id=str(index + 1),
                name=_fit_name(merged[key]["name"] or f"Light {index + 1}"),
                position=_position_from_x(merged[key]["x"]),
                channels=merged[key]["channels"],
            )
            for index, key in enumerate(order)
        ]
    name_counts: dict[str, int] = {}
    for channel in area.channels:
        name_counts[channel.name] = name_counts.get(channel.name, 0) + 1
    seen: dict[str, int] = {}
    lights: list[VirtualLight] = []
    for index, channel in enumerate(area.channels):
        base = channel.name or f"Zone {index + 1}"
        if name_counts.get(channel.name, 0) > 1:
            # Same-named gradient zones: disambiguate by on-screen position so the user can tell
            # them apart (e.g. "Strip (left)") instead of an opaque "Strip 1".
            label = _zone_label(channel.position)
            seen[label] = seen.get(label, 0) + 1
            suffix = label if seen[label] == 1 else f"{label} {seen[label]}"
            name = _fit_name(base, suffix)
        else:
            name = _fit_name(base)
        lights.append(
            VirtualLight(
                id=str(index + 1),
                name=name,
                position=_position_from_x(channel.position[0]),
                channels=[channel.channel_id],
            ),
        )
    return lights


def lights_from_zigbee(lamps: list[ZigbeeLightRef], *, split_gradients: bool) -> list[VirtualLight]:
    """
    Build the virtual lights a Zigbee-target TV sees: one per lamp or gradient zone, in order.

    Each light drives one channel of :func:`zigbee_channels`; its id is that channel id + 1.

    :param lamps: The TV's selected ZHA lamps.
    :param split_gradients: Expose each zone of a gradient lamp as a separate light.
    """
    lights: list[VirtualLight] = []
    for index, (lamp, channels) in enumerate(
        zigbee_channels(lamps, split_gradients=split_gradients)
    ):
        base = lamp.name or f"Light {index + 1}"
        for zone, channel in enumerate(channels):
            suffix = f"zone {zone + 1}" if len(channels) > 1 else ""
            lights.append(
                VirtualLight(id=str(channel + 1), name=_fit_name(base, suffix), channels=[channel])
            )
    return lights


def zigbee_channels(
    lamps: list[ZigbeeLightRef], *, split_gradients: bool
) -> list[tuple[ZigbeeLightRef, range]]:
    """
    Return each of a Zigbee-target TV's lamps with the channel ids it streams on, in order.

    A gradient lamp gets one channel per zone (up to 3) when ``split_gradients`` is on; any other
    lamp gets one channel. The channels are numbered consecutively from 0.

    :param lamps: The TV's selected ZHA lamps.
    :param split_gradients: Whether the TV splits gradient lamps into zones.
    """
    layout: list[tuple[ZigbeeLightRef, range]] = []
    start = 0
    for lamp in lamps:
        zones = min(_ZIGBEE_GRADIENT_ZONES, lamp.segments) if split_gradients else 1
        layout.append((lamp, range(start, start + zones)))
        start += zones
    return layout


def _upsert_bridge(store: ConfigStore, host: str, credentials: dict[str, str]) -> RealBridge:
    """Add or update a real bridge entry with freshly paired credentials."""
    for bridge in store.config.real_bridges:
        if bridge.host == host:
            bridge.app_key = credentials["username"]
            bridge.client_key = credentials["clientkey"]
            return bridge
    bridge = RealBridge(
        id=host.replace(".", "-"),
        host=host,
        app_key=credentials["username"],
        client_key=credentials["clientkey"],
    )
    store.config.real_bridges.append(bridge)
    return bridge


def _position_from_x(x: float) -> str:
    """Map an entertainment channel's x position to a coarse screen position."""
    if x < -0.3:
        return "left"
    if x > 0.3:
        return "right"
    return "center"


def _fit_name(base: str, position: str = "") -> str:
    """
    Build a light name within Hue's 32-char limit, keeping the position suffix readable.

    The base (which repeats across a strip's zones) is truncated first; the position suffix
    (which is what distinguishes the zones) is always preserved.

    :param base: The source light/zone name.
    :param position: Optional on-screen position appended as " (position)".
    """
    if not position:
        return base[:_MAX_LIGHT_NAME].rstrip()
    tail = f" ({position})"
    return f"{base[: _MAX_LIGHT_NAME - len(tail)].rstrip()}{tail}"


def _zone_label(position: Any) -> str:
    """Describe a channel's horizontal screen position from its x coordinate (left..right)."""
    x = position[0]
    if x < -0.6:
        return "far left"
    if x < -0.2:
        return "left"
    if x > 0.6:
        return "far right"
    if x > 0.2:
        return "right"
    return "center"
