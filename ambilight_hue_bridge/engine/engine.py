"""Engine: buffers inbound colors and streams them to the real bridge on demand."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from hue_entertainment import EntertainmentSession

from ambilight_hue_bridge.const import MAX_STREAM_SMOOTHING
from ambilight_hue_bridge.outbound.controller import active_bridge, tv_stream_target

from .ingest import ColorBuffer
from .mapping import map_to_commands

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import VirtualLight
    from ambilight_hue_bridge.config.store import ConfigStore

LOGGER = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 10.0


class Engine:
    """Owns the ingest buffer and the on-demand outbound entertainment stream."""

    def __init__(self, store: ConfigStore, *, rate_hz: int | None = None) -> None:
        """
        Initialize the engine (no stream until the first color is submitted).

        :param store: Config store providing the real bridges and virtual lights.
        :param rate_hz: Outbound frame rate in Hz (defaults to the configured rate).
        """
        self._store = store
        rate = rate_hz if rate_hz is not None else store.config.virtual_bridge.stream_rate_hz
        self._interval = 1.0 / max(1, rate)
        self._buffer = ColorBuffer()
        # Smoothed (eased) colors actually streamed to the bridge: float state for precision
        # plus an int buffer the mapper reads, so abrupt TV color jumps become fades.
        self._smoothed_float: dict[str, tuple[float, float, float]] = {}
        self._smoothed_buffer = ColorBuffer()
        self._session: EntertainmentSession | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._last_update = 0.0
        # Username (paired TV) that owns the active outbound stream, for the web UI + the
        # single-stream guard. Cleared on teardown.
        self._stream_owner: str | None = None
        # The lights mapped for the active stream (the owning TV's assigned lights), resolved
        # when the stream starts. Cleared on teardown.
        self._stream_lights: list[VirtualLight] = []
        # One-shot diagnostic flags so the streaming hot path logs its first event of each
        # kind (first color in, first frame out, why it can't start) without per-frame spam.
        self._logged_submit = False
        self._logged_no_stream = False
        self._logged_tick = False
        self._logged_empty = False

    @property
    def is_streaming(self) -> bool:
        """Return True while the outbound stream is active."""
        return self._session is not None and self._session.is_streaming

    @property
    def stream_owner(self) -> str | None:
        """Return the paired username that owns the active stream, or None."""
        return self._stream_owner

    def submit_color(self, owner: str | None, light_id: str, rgb: tuple[int, int, int]) -> None:
        """
        Record a color for a virtual light and ensure the stream is running.

        Safe to call from a request handler; never blocks.

        :param owner: The paired TV the color came from; adopted as the stream owner when an
            older v1-REST TV pushes colors without first activating an entertainment group.
        :param light_id: The virtual light id the color applies to.
        :param rgb: 16-bit RGB tuple (0-65535 per channel).
        """
        # Older TVs drive lights over plain v1 REST and never activate an entertainment group,
        # so they never call start_stream; adopt the owner from the first color they push.
        if owner and self._stream_owner is None:
            self._stream_owner = owner
        self._buffer.set_color(light_id, rgb)
        self._last_update = time.monotonic()
        if not self._logged_submit:
            self._logged_submit = True
            LOGGER.info("First inbound color submitted: light=%s rgb=%s", light_id, rgb)
        if not self.is_streaming and self._start_task is None:
            if self._can_stream():
                self._start_task = asyncio.create_task(self._start_stream())
            elif not self._logged_no_stream:
                self._logged_no_stream = True
                bridge = active_bridge(self._store)
                area, lights = tv_stream_target(self._store, self._stream_owner)
                LOGGER.warning(
                    "Not starting outbound stream: owner=%s bridge=%s client_key=%s "
                    "area=%r lights=%d (assign this TV an entertainment area in the web UI)",
                    self._stream_owner,
                    bridge.id if bridge else None,
                    bool(bridge and bridge.client_key),
                    area,
                    len(lights),
                )

    def start_stream(self, owner: str) -> None:
        """
        Proactively open the outbound stream (e.g. when the TV activates entertainment).

        Opening the DTLS stream to the real bridge ahead of the first frame hides the
        handshake latency, so the very first colors aren't lost.

        :param owner: The paired username activating the stream.
        """
        self._last_update = time.monotonic()
        self._stream_owner = owner
        if not self.is_streaming and self._start_task is None and self._can_stream():
            self._start_task = asyncio.create_task(self._start_stream())

    async def stop_stream(self) -> None:
        """Stop the outbound stream (e.g. when the TV deactivates entertainment)."""
        await self.stop()

    async def stop(self) -> None:
        """Stop the outbound stream and any background tasks."""
        if self._start_task is not None:
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
            self._start_task = None
        await self._teardown()

    def _can_stream(self) -> bool:
        """Return whether the active bridge has credentials and the owner resolves to an area."""
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.client_key:
            return False
        area, _ = tv_stream_target(self._store, self._stream_owner)
        return bool(area)

    def _apply_smoothing(self, lights: list[VirtualLight]) -> None:
        """
        Ease each light's streamed color toward its latest target color.

        Writes the result into the smoothed buffer the mapper reads, turning the TV's abrupt
        color steps into fades.
        """
        smoothing = max(
            0.0, min(MAX_STREAM_SMOOTHING, self._store.config.virtual_bridge.stream_smoothing)
        )
        for light in lights:
            target = self._buffer.get_color(light.id)
            current = self._smoothed_float.get(light.id)
            if current is None or smoothing <= 0.0:
                # First sight (or smoothing off): jump to the target, no fade-from-black.
                eased = (float(target[0]), float(target[1]), float(target[2]))
            else:
                eased = (
                    smoothing * current[0] + (1.0 - smoothing) * target[0],
                    smoothing * current[1] + (1.0 - smoothing) * target[1],
                    smoothing * current[2] + (1.0 - smoothing) * target[2],
                )
            self._smoothed_float[light.id] = eased
            self._smoothed_buffer.set_color(
                light.id, (round(eased[0]), round(eased[1]), round(eased[2]))
            )

    async def _start_stream(self) -> None:
        """Open the entertainment session for the owning TV's area and start the ticker."""
        bridge = active_bridge(self._store)
        if bridge is None:
            self._start_task = None
            return
        # The owner's assigned area + lights ("", [] when the TV is unassigned).
        area, self._stream_lights = tv_stream_target(self._store, self._stream_owner)
        try:
            LOGGER.info(
                "Starting outbound stream to %s area %s (owner=%s)",
                bridge.host,
                area,
                self._stream_owner,
            )
            session = EntertainmentSession(
                bridge.host,
                bridge.app_key,
                bridge.client_key,
                idle_timeout=0.0,
            )
            await session.start(area)
            self._session = session
            self._ticker = asyncio.create_task(self._run_ticker())
            LOGGER.info("Outbound stream started to %s area %s", bridge.host, area)
        except Exception:
            LOGGER.exception("Failed to start outbound stream to %s area %s", bridge.host, area)
        finally:
            self._start_task = None

    async def _run_ticker(self) -> None:
        """Send the buffered colors to the bridge at a fixed rate until inactivity."""
        try:
            while time.monotonic() - self._last_update < IDLE_TIMEOUT_S:
                if self._session is None or not self._session.is_streaming:
                    break
                self._apply_smoothing(self._stream_lights)
                commands = map_to_commands(self._stream_lights, self._smoothed_buffer)
                if commands:
                    if not self._logged_tick:
                        self._logged_tick = True
                        LOGGER.info(
                            "First outbound frame: %d command(s) channels=%s",
                            len(commands),
                            [command.channel_id for command in commands],
                        )
                    self._session.send(commands)
                elif not self._logged_empty:
                    self._logged_empty = True
                    LOGGER.warning(
                        "Ticker produced 0 commands (lights=%d) - nothing to stream",
                        len(self._stream_lights),
                    )
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
        self._stream_owner = None
        self._stream_lights = []
        self._smoothed_float.clear()
        if session is not None:
            await session.aclose()
