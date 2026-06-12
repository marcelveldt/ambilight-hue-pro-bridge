"""mDNS / DNS-SD advertisement so newer Hue clients find the virtual bridge via _hue._tcp."""

from __future__ import annotations

import logging
import socket
from contextlib import suppress

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from ambilight_hue_bridge.const import BRIDGE_MODEL_ID

LOGGER = logging.getLogger(__name__)

HUE_SERVICE_TYPE = "_hue._tcp.local."


def build_service_info(*, host_ip: str, port: int, bridge_id: str) -> AsyncServiceInfo:
    """
    Build the ``_hue._tcp.local`` service record advertised for the virtual bridge.

    Mirrors a real BSB002 bridge: the TXT record carries ``bridgeid`` (the same value we
    advertise over SSDP and return from the v1 ``/config``) and ``modelid=BSB002``, and the
    service points at the port the bridge serves the API on (the TLS port when HTTPS is enabled,
    otherwise the HTTP port). The Ambilight TVs ignore the SRV port and use the v1 API on :80.

    :param host_ip: LAN IPv4 address clients connect to.
    :param port: TCP port clients connect to (the TLS port if HTTPS is enabled, else HTTP).
    :param bridge_id: The 16-hex Hue ``bridgeid`` (uppercase, as advertised over SSDP).
    """
    instance = f"Philips Hue - {bridge_id[-6:]}.{HUE_SERVICE_TYPE}"
    return AsyncServiceInfo(
        HUE_SERVICE_TYPE,
        instance,
        addresses=[socket.inet_aton(host_ip)],
        port=port,
        properties={"bridgeid": bridge_id, "modelid": BRIDGE_MODEL_ID},
        server=f"{bridge_id.lower()}.local.",
    )


class MDNSAdvertiser:
    """Advertises the virtual bridge as a Hue bridge over mDNS (_hue._tcp on its API port)."""

    def __init__(self, *, host_ip: str, port: int, bridge_id: str) -> None:
        """
        Initialize the advertiser (no socket until :meth:`start`).

        :param host_ip: LAN IPv4 address clients connect to.
        :param port: TCP port clients connect to (the TLS port if HTTPS is enabled, else HTTP;
            real bridges use 443).
        :param bridge_id: The 16-hex Hue ``bridgeid`` published in the TXT record.
        """
        self._info = build_service_info(host_ip=host_ip, port=port, bridge_id=bridge_id)
        self._host_ip = host_ip
        self._port = port
        self._zeroconf: AsyncZeroconf | None = None

    async def start(self) -> None:
        """Register the ``_hue._tcp.local`` service on the LAN."""
        zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        try:
            await zeroconf.async_register_service(self._info)
        except OSError as err:
            await zeroconf.async_close()
            LOGGER.warning("Could not start mDNS advertisement (%s) - continuing without it", err)
            return
        self._zeroconf = zeroconf
        LOGGER.info("mDNS advertising %s on %s:%d", HUE_SERVICE_TYPE, self._host_ip, self._port)

    async def stop(self) -> None:
        """Unregister the service and close zeroconf."""
        if self._zeroconf is None:
            return
        with suppress(Exception):
            await self._zeroconf.async_unregister_service(self._info)
        await self._zeroconf.async_close()
        self._zeroconf = None
