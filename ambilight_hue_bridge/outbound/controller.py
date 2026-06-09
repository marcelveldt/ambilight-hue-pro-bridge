"""Pairing with and inspecting real Hue bridges, and resolving the active bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hue_entertainment import HueEntertainmentAPI

from ambilight_hue_bridge.config.models import RealBridge

if TYPE_CHECKING:
    from hue_entertainment import EntertainmentArea

    from ambilight_hue_bridge.config.store import ConfigStore


async def pair_bridge(host: str) -> dict[str, str]:
    """
    Pair with a real Hue bridge; the link button must be pressed first.

    :param host: Bridge IP address or hostname.
    """
    api = HueEntertainmentAPI(host)
    try:
        credentials: dict[str, str] = await api.pair()
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


def active_bridge(store: ConfigStore) -> RealBridge | None:
    """Return the configured active real bridge (or the first one, or None)."""
    bridges = store.config.real_bridges
    active = store.config.active_real_bridge
    if active:
        for bridge in bridges:
            if bridge.id == active:
                return bridge
    return bridges[0] if bridges else None


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
