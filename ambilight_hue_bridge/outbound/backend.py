"""Output backends: the outbound streams the engine sends its light frames through."""

from __future__ import annotations

import logging
import time
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Protocol

from hue_entertainment import EntertainmentSession, ZigbeeEntertainmentSession, ZigbeeLight

from .controller import zigbee_channels
from .homeassistant import HomeAssistantError

if TYPE_CHECKING:
    from hue_entertainment import LightColorCommand

    from ambilight_hue_bridge.config.models import RealBridge, ZigbeeLightRef

    from .homeassistant import HomeAssistantClient

LOGGER = logging.getLogger(__name__)

# How long a lost Zigbee stream is restarted in place (e.g. while ZHA restarts its Zigbee stack)
# before it is reported as lost. Restarting the same session keeps the lamps' captured state.
_ZIGBEE_RECOVERY_S = 60.0


class BackendHealth(StrEnum):
    """
    The remote end's view of an active output stream.

    ``OK``: the stream is still ours.
    ``LOST``: the remote ended our stream.
    ``TAKEN_OVER``: another controller owns the target now.
    """

    OK = auto()
    LOST = auto()
    TAKEN_OVER = auto()


class OutputBackend(Protocol):
    """An outbound stream to one light target, opened on demand by the engine."""

    @property
    def is_streaming(self) -> bool:
        """Return True while the stream is active."""

    async def start(self) -> None:
        """Open the stream; raises when it cannot be established."""

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue one frame of channel colors; non-blocking.

        :param commands: One color command per channel to update this frame.
        """

    async def check_health(self) -> BackendHealth:
        """Return the remote end's view of the active stream; raises when it cannot be checked."""

    async def aclose(self) -> None:
        """Stop the stream and release its resources."""


class HueBridgeBackend:
    """Streams to an entertainment area on a real Hue bridge."""

    def __init__(self, bridge: RealBridge, area_id: str) -> None:
        """
        Initialize the backend (no network I/O until :meth:`start`).

        :param bridge: The paired real bridge to stream to.
        :param area_id: Id of the entertainment area to stream to.
        """
        self._area_id = area_id
        self._session = EntertainmentSession(
            bridge.host,
            bridge.app_key,
            bridge.client_key,
            idle_timeout=0.0,
        )
        # active_streamer rid the bridge reports for us, captured (once, positively) the first
        # time the bridge names our streamer, so the health check can tell our own stream from
        # a foreign takeover. The boolean distinguishes "captured" from the transient empty rid
        # the bridge returns before it has named our streamer.
        self._our_streamer_rid = ""
        self._streamer_rid_captured = False

    @property
    def is_streaming(self) -> bool:
        """Return True while the DTLS stream is active."""
        return self._session.is_streaming

    async def start(self) -> None:
        """Activate the entertainment area and open the DTLS stream."""
        await self._session.start(self._area_id)

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue one frame of channel colors; non-blocking.

        :param commands: One color command per entertainment channel to update this frame.
        """
        self._session.send(commands)

    async def check_health(self) -> BackendHealth:
        """Return whether the bridge still streams our area, ended it, or handed it to another."""
        status, rid = await self._session.remote_status()
        if status == "active" and rid and not self._streamer_rid_captured:
            # First time the bridge names our streamer: that rid is us. (A non-empty rid is
            # required, so the transient empty rid right after start isn't mistaken for us.)
            self._our_streamer_rid = rid
            self._streamer_rid_captured = True
        # Before our streamer is named, trust status alone (startup grace); after, the rid
        # must match us - a cleared rid then means the bridge dropped us, not that we're fine.
        ours = status == "active" and (
            rid == self._our_streamer_rid if self._streamer_rid_captured else True
        )
        if ours:
            return BackendHealth.OK
        if status == "active" and rid and rid != self._our_streamer_rid:
            return BackendHealth.TAKEN_OVER
        LOGGER.debug(
            "Bridge does not report our area as streaming (status=%r)", status or "unknown"
        )
        return BackendHealth.LOST

    async def aclose(self) -> None:
        """Stop streaming, deactivate the area and release the HTTP session."""
        await self._session.aclose()


class ZigbeeBackend:
    """Streams straight to Zigbee lamps through ZHA's Ziggurat server, without a Hue bridge."""

    def __init__(
        self,
        home_assistant: HomeAssistantClient,
        lamps: list[ZigbeeLightRef],
        *,
        max_rate_hz: float = 25.0,
    ) -> None:
        """
        Initialize the backend (no network I/O until :meth:`start`).

        :param home_assistant: Client resolving the Ziggurat server and the lamps' addresses.
        :param lamps: The lamps to stream to, on the channels of
            :func:`~ambilight_hue_bridge.outbound.controller.zigbee_channels`.
        :param max_rate_hz: Most frames per second to send; the lamps fade in between.
        """
        self._home_assistant = home_assistant
        self._lamps = lamps
        self._max_rate_hz = max_rate_hz
        self._session: ZigbeeEntertainmentSession | None = None
        self._lost_since: float | None = None

    @property
    def is_streaming(self) -> bool:
        """Return True from a successful start until closed, also while recovering a lost stream."""
        return self._session is not None

    @property
    def max_rate_hz(self) -> float:
        """Return the most frames per second to send; changing it applies to a running stream."""
        return self._max_rate_hz

    @max_rate_hz.setter
    def max_rate_hz(self, value: float) -> None:
        self._max_rate_hz = value
        if self._session is not None:
            self._session.max_rate_hz = value

    async def start(self) -> None:
        """
        Look up the Ziggurat server and the lamps in ZHA, then open the stream.

        Lamps that are no longer in ZHA are skipped.

        :raises HomeAssistantError: When ZHA is unavailable, not on Ziggurat, or has none of the
            lamps.
        :raises ZigguratError: When the Ziggurat server can't be reached.
        """
        # The network address can change (a lamp rejoining), so resolve it fresh on every start.
        url, addresses = await self._home_assistant.stream_info()
        if url is None:
            msg = "ZHA does not run on a Ziggurat server"
            raise HomeAssistantError(msg)
        lights: list[ZigbeeLight] = []
        for lamp, channels in zigbee_channels(self._lamps):
            nwk = addresses.get(lamp.ieee.lower())
            if nwk is None:
                LOGGER.warning(
                    "Zigbee lamp %s (%s) not found in ZHA; skipping", lamp.name, lamp.ieee
                )
                continue
            lights.extend(
                ZigbeeLight(channel_id, lamp.ieee, nwk, lamp.name, zone)
                for zone, channel_id in enumerate(channels)
            )
        if not lights:
            msg = "None of the selected Zigbee lamps are in ZHA"
            raise HomeAssistantError(msg)
        self._session = ZigbeeEntertainmentSession(
            url, lights, idle_timeout=0.0, max_rate_hz=self._max_rate_hz
        )
        await self._session.start()

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue one frame of lamp colors; non-blocking.

        :param commands: One color command per channel (a lamp or a zone) to update this frame.
        """
        if self._session is not None:
            self._session.send(commands)

    async def check_health(self) -> BackendHealth:
        """Restart a lost stream in place; return LOST once it stayed down for too long."""
        session = self._session
        if session is None or session.is_streaming:
            self._lost_since = None
            return BackendHealth.OK
        now = time.monotonic()
        if self._lost_since is None:
            self._lost_since = now
            LOGGER.warning("Zigbee stream lost; restarting it")
        try:
            await session.start()
        except Exception as err:  # noqa: BLE001 - any failure here means "still down"
            if now - self._lost_since < _ZIGBEE_RECOVERY_S:
                LOGGER.debug("Zigbee stream not back yet (%s)", err)
                return BackendHealth.OK
            return BackendHealth.LOST
        LOGGER.info("Zigbee stream restarted")
        self._lost_since = None
        return BackendHealth.OK

    async def aclose(self) -> None:
        """Stop streaming and restore the lamps' state from before the stream."""
        session, self._session = self._session, None
        if session is not None:
            await session.aclose()
