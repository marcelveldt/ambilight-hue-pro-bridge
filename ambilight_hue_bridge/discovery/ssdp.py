"""SSDP/UPnP discovery responder so Ambilight+Hue TVs find the virtual bridge."""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import suppress
from typing import cast

from ambilight_hue_bridge.const import (
    SSDP_MCAST_ADDR,
    SSDP_NOTIFY_INTERVAL,
    SSDP_PORT,
    UPNP_SERVER,
    VERBOSE,
)

LOGGER = logging.getLogger(__name__)

_MULTICAST_TTL = 2


class SSDPServer(asyncio.DatagramProtocol):
    """Answers SSDP M-SEARCH requests and periodically advertises the bridge."""

    def __init__(self, *, host_ip: str, http_port: int, bridge_id: str, udn: str) -> None:
        """
        Initialize the responder (no socket until :meth:`start`).

        :param host_ip: LAN IP address advertised in the descriptor LOCATION.
        :param http_port: TCP port the descriptor and v1 API are served on.
        :param bridge_id: The Hue ``bridgeid`` sent in the ``hue-bridgeid`` header.
        :param udn: The UPnP UDN (``uuid:...``) used to build the ST/USN values.
        """
        self._host_ip = host_ip
        self._http_port = http_port
        self._bridge_id = bridge_id
        self._udn = udn
        self._transport: asyncio.DatagramTransport | None = None
        self._notify_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bind the multicast socket and begin responding and advertising."""
        sock = self._create_socket()
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, sock=sock)
        self._notify_task = asyncio.create_task(self._notify_loop())
        LOGGER.info(
            "SSDP responder listening on %s:%d, advertising %s",
            SSDP_MCAST_ADDR,
            SSDP_PORT,
            self._location(),
        )

    async def stop(self) -> None:
        """Stop advertising and close the socket."""
        if self._notify_task is not None:
            self._notify_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._notify_task
            self._notify_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the datagram transport once the socket is ready."""
        self._transport = cast("asyncio.DatagramTransport", transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Reply to SSDP ``M-SEARCH`` discovery requests with our device descriptors."""
        message = data.decode("utf-8", errors="ignore")
        if "M-SEARCH" not in message:
            return
        if "ssdp:discover" not in message:
            LOGGER.log(VERBOSE, "SSDP ignored non-discover datagram from %s:%d", addr[0], addr[1])
            return
        search_target = _header_value(message, "ST") or "?"
        # VERBOSE: every UPnP device on the LAN M-SEARCHes constantly, so this is firehose noise.
        # The startup "listening/joined" lines stay at INFO.
        LOGGER.log(
            VERBOSE,
            "SSDP M-SEARCH from %s:%d (ST=%s) - responding",
            addr[0],
            addr[1],
            search_target,
        )
        if self._transport is None:
            return
        responses = self._search_responses()
        for response in responses:
            self._transport.sendto(response, addr)
        LOGGER.log(
            VERBOSE,
            "SSDP sent %d response variant(s) to %s:%d, LOCATION=%s",
            len(responses),
            addr[0],
            addr[1],
            self._location(),
        )

    def error_received(self, exc: Exception) -> None:
        """Log transport errors without crashing the responder."""
        LOGGER.warning("SSDP transport error: %s", exc)

    def _create_socket(self) -> socket.socket:
        """Create the bound, multicast-joined UDP socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with suppress(AttributeError, OSError):
            # Not available on every platform; harmless when missing.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", SSDP_PORT))
        except OSError as err:
            sock.close()
            msg = (
                f"Failed to bind SSDP UDP port {SSDP_PORT} - another SSDP service "
                "(Home Assistant, miniupnpd, another bridge instance) may already own it"
            )
            raise OSError(msg) from err
        # Join the discovery group on the advertised interface AND on INADDR_ANY, so the
        # TV's M-SEARCH is received regardless of which NIC the host auto-detection picked
        # (a wrong single-interface join is a classic "the bridge never hears the TV" bug).
        joined: list[str] = []
        for interface in (self._host_ip, "0.0.0.0"):
            try:
                mreq = socket.inet_aton(SSDP_MCAST_ADDR) + socket.inet_aton(interface)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined.append(interface)
            except OSError as err:
                LOGGER.debug("SSDP could not join %s on %s: %s", SSDP_MCAST_ADDR, interface, err)
        if not joined:
            sock.close()
            msg = f"Failed to join SSDP multicast group {SSDP_MCAST_ADDR} on any interface"
            raise OSError(msg)
        # Pin outbound multicast (NOTIFY) to the advertised LAN interface.
        with suppress(OSError):
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self._host_ip)
            )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, _MULTICAST_TTL)
        LOGGER.info("SSDP joined %s on interface(s): %s", SSDP_MCAST_ADDR, ", ".join(joined))
        return sock

    async def _notify_loop(self) -> None:
        """Broadcast ssdp:alive NOTIFY messages on an interval (helps some TVs discover us)."""
        while True:
            # A transient multicast send error (e.g. an interface flap) must not kill the
            # advertising loop for the rest of the process lifetime.
            with suppress(OSError):
                self._send_notify()
            await asyncio.sleep(SSDP_NOTIFY_INTERVAL)

    def _send_notify(self) -> None:
        """Send one round of ssdp:alive NOTIFY datagrams to the multicast group."""
        if self._transport is None:
            return
        messages = self._notify_messages()
        for message in messages:
            self._transport.sendto(message, (SSDP_MCAST_ADDR, SSDP_PORT))
        LOGGER.log(
            VERBOSE,
            "SSDP broadcast %d ssdp:alive NOTIFY to %s:%d",
            len(messages),
            SSDP_MCAST_ADDR,
            SSDP_PORT,
        )

    def _variants(self) -> list[tuple[str, str]]:
        """Return the (ST/NT, USN) pairs a real Hue bridge advertises."""
        return [
            ("upnp:rootdevice", f"{self._udn}::upnp:rootdevice"),
            (self._udn, self._udn),
            (
                "urn:schemas-upnp-org:device:basic:1",
                f"{self._udn}::urn:schemas-upnp-org:device:basic:1",
            ),
        ]

    def _location(self) -> str:
        """Return the descriptor LOCATION URL."""
        return f"http://{self._host_ip}:{self._http_port}/description.xml"

    def _search_responses(self) -> list[bytes]:
        """Build the M-SEARCH 200 OK responses (one per ST variant)."""
        responses: list[bytes] = []
        for search_target, usn in self._variants():
            message = (
                "HTTP/1.1 200 OK\r\n"
                f"HOST: {SSDP_MCAST_ADDR}:{SSDP_PORT}\r\n"
                "EXT:\r\n"
                "CACHE-CONTROL: max-age=100\r\n"
                f"LOCATION: {self._location()}\r\n"
                f"SERVER: {UPNP_SERVER}\r\n"
                f"hue-bridgeid: {self._bridge_id}\r\n"
                f"ST: {search_target}\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            responses.append(message.encode("utf-8"))
        return responses

    def _notify_messages(self) -> list[bytes]:
        """Build the ssdp:alive NOTIFY messages (one per NT variant)."""
        messages: list[bytes] = []
        for notification_type, usn in self._variants():
            message = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_MCAST_ADDR}:{SSDP_PORT}\r\n"
                "CACHE-CONTROL: max-age=100\r\n"
                f"LOCATION: {self._location()}\r\n"
                f"SERVER: {UPNP_SERVER}\r\n"
                "NTS: ssdp:alive\r\n"
                f"hue-bridgeid: {self._bridge_id}\r\n"
                f"NT: {notification_type}\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            )
            messages.append(message.encode("utf-8"))
        return messages


def _header_value(message: str, name: str) -> str | None:
    """
    Return an HTTP header value from a raw SSDP message, matched case-insensitively.

    :param message: The decoded SSDP request text.
    :param name: The header name to look up (e.g. ``ST``).
    """
    prefix = f"{name.lower()}:"
    for line in message.splitlines():
        if line.lower().startswith(prefix):
            return line[len(prefix) :].strip()
    return None
