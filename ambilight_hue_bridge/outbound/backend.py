"""Output backends: the outbound streams the engine sends its light frames through."""

from __future__ import annotations

import logging
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Protocol

from hue_entertainment import EntertainmentSession

if TYPE_CHECKING:
    from hue_entertainment import LightColorCommand

    from ambilight_hue_bridge.config.models import RealBridge

LOGGER = logging.getLogger(__name__)


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
