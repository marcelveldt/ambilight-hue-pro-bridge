"""Engine: buffers inbound colors and streams them to the real bridge on demand."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from hue_entertainment import EntertainmentSession

from .ingest import ColorBuffer
from .mapping import map_to_commands

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import RealBridge
    from ambilight_hue_bridge.config.store import ConfigStore

LOGGER = logging.getLogger(__name__)

DEFAULT_RATE_HZ = 50
IDLE_TIMEOUT_S = 10.0


class Engine:
    """Owns the ingest buffer and the on-demand outbound entertainment stream."""

    def __init__(self, store: ConfigStore, *, rate_hz: int = DEFAULT_RATE_HZ) -> None:
        """
        Initialize the engine (no stream until the first color is submitted).

        :param store: Config store providing the real bridges and virtual lights.
        :param rate_hz: Outbound frame rate in Hz.
        """
        self._store = store
        self._interval = 1.0 / rate_hz
        self._buffer = ColorBuffer()
        self._session: EntertainmentSession | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._last_update = 0.0

    @property
    def is_streaming(self) -> bool:
        """Return True while the outbound stream is active."""
        return self._session is not None and self._session.is_streaming

    def submit_color(self, light_id: str, rgb: tuple[int, int, int]) -> None:
        """
        Record a color for a virtual light and ensure the stream is running.

        Safe to call from a request handler; never blocks.

        :param light_id: The virtual light id the color applies to.
        :param rgb: 16-bit RGB tuple (0-65535 per channel).
        """
        self._buffer.set_color(light_id, rgb)
        self._last_update = time.monotonic()
        if not self.is_streaming and self._start_task is None and self._can_stream():
            self._start_task = asyncio.create_task(self._start_stream())

    async def stop(self) -> None:
        """Stop the outbound stream and any background tasks."""
        if self._start_task is not None:
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
            self._start_task = None
        await self._teardown()

    def _can_stream(self) -> bool:
        """Return whether an active real bridge with credentials and an area is configured."""
        bridge = self._active_bridge()
        return bridge is not None and bool(bridge.client_key and bridge.entertainment_area)

    def _active_bridge(self) -> RealBridge | None:
        """Return the configured active real bridge (or the first one)."""
        bridges = self._store.config.real_bridges
        active_id = self._store.config.active_real_bridge
        if active_id:
            for bridge in bridges:
                if bridge.id == active_id:
                    return bridge
        return bridges[0] if bridges else None

    async def _start_stream(self) -> None:
        """Open the entertainment session and start the frame ticker."""
        try:
            bridge = self._active_bridge()
            if bridge is None:
                return
            session = EntertainmentSession(
                bridge.host,
                bridge.app_key,
                bridge.client_key,
                idle_timeout=0.0,
            )
            await session.start(bridge.entertainment_area)
            self._session = session
            self._ticker = asyncio.create_task(self._run_ticker())
            LOGGER.info(
                "Outbound stream started to %s area %s",
                bridge.host,
                bridge.entertainment_area,
            )
        except Exception:
            LOGGER.exception("Failed to start outbound entertainment stream")
        finally:
            self._start_task = None

    async def _run_ticker(self) -> None:
        """Send the buffered colors to the bridge at a fixed rate until inactivity."""
        try:
            while time.monotonic() - self._last_update < IDLE_TIMEOUT_S:
                if self._session is None or not self._session.is_streaming:
                    break
                commands = map_to_commands(self._store.config.virtual_lights, self._buffer)
                if commands:
                    self._session.send(commands)
                await asyncio.sleep(self._interval)
        finally:
            await self._teardown()
            LOGGER.info("Outbound stream stopped")

    async def _teardown(self) -> None:
        """Stop the ticker and close the entertainment session."""
        ticker = self._ticker
        self._ticker = None
        if ticker is not None and ticker is not asyncio.current_task():
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        session = self._session
        self._session = None
        if session is not None:
            await session.aclose()
