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
# Identify ("blink this light", from the TV's Hue alert during setup): flash full white on/off
# over the stream for a short window ("select") or a longer one ("lselect").
_IDENTIFY_SELECT_S = 2.0
_IDENTIFY_LSELECT_S = 15.0
_IDENTIFY_BLINK_HZ = 1.5
_IDENTIFY_RGB = (65535, 65535, 65535)


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
        # Monotonic token bumped on every stop. The (slow, ~seconds) DTLS connect to the real
        # bridge is not reliably cancellable, so a start that completes after a stop checks this
        # to discard itself instead of leaving a zombie stream behind.
        self._stream_gen = 0
        # Lights the TV asked to identify (blink), keyed by light id -> blink expiry (monotonic).
        self._identify: dict[str, float] = {}
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

    def identify(self, owner: str, light_id: str, *, sustained: bool = False) -> None:
        """
        Blink a light so the user can locate it (the TV's 'alert' during light assignment).

        Opens the outbound stream if needed and overlays a flash on the light's channels for a
        short window, or a longer one for a sustained ('lselect') identify.

        :param owner: The paired TV requesting the identify.
        :param light_id: The virtual light id to flash.
        :param sustained: Whether this is a long 'lselect' identify rather than a single blink.
        """
        self._last_update = time.monotonic()
        self._identify[light_id] = self._last_update + (
            _IDENTIFY_LSELECT_S if sustained else _IDENTIFY_SELECT_S
        )
        if owner and self._stream_owner is None:
            self._stream_owner = owner
        if not self.is_streaming and self._start_task is None and self._can_stream():
            self._start_task = asyncio.create_task(self._start_stream())

    def stop_identify(self, light_id: str) -> None:
        """Cancel a light's identify blink (the TV's alert 'none')."""
        self._identify.pop(light_id, None)

    async def stop(self) -> None:
        """Stop the outbound stream and any background tasks."""
        # Invalidate any in-flight startup before we wait on it, so a connect that finishes
        # during the await discards itself rather than starting a ticker we just tore down.
        self._stream_gen += 1
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
        smoothing = max(0.0, min(MAX_STREAM_SMOOTHING, self._resolve_smoothing()))
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

    def _resolve_smoothing(self) -> float:
        """Return the owning TV's smoothing setting, or 0.0 (off) when it has none."""
        owner = self._stream_owner
        if owner:
            for user in self._store.config.users:
                if user.username == owner and user.stream_smoothing is not None:
                    return user.stream_smoothing
        return 0.0

    def _apply_identify(self) -> None:
        """Overlay a full-white blink on lights the TV asked to identify; expire finished ones."""
        if not self._identify:
            return
        now = time.monotonic()
        for light_id in [lid for lid, expiry in self._identify.items() if now >= expiry]:
            del self._identify[light_id]
        if not self._identify:
            return
        # Keep the stream alive while identifying, then flash full white on/off.
        self._last_update = now
        rgb = _IDENTIFY_RGB if int(now * 2 * _IDENTIFY_BLINK_HZ) % 2 == 0 else (0, 0, 0)
        for light_id in self._identify:
            self._smoothed_buffer.set_color(light_id, rgb)

    async def _start_stream(self) -> None:
        """Open the entertainment session for the owning TV's area and start the ticker."""
        bridge = active_bridge(self._store)
        if bridge is None:
            self._start_task = None
            return
        # The owner's assigned area + lights ("", [] when the TV is unassigned).
        gen = self._stream_gen
        area, lights = tv_stream_target(self._store, self._stream_owner)
        session = EntertainmentSession(
            bridge.host,
            bridge.app_key,
            bridge.client_key,
            idle_timeout=0.0,
        )
        try:
            LOGGER.info(
                "Starting outbound stream to %s area %s (owner=%s)",
                bridge.host,
                area,
                self._stream_owner,
            )
            await session.start(area)
            if gen != self._stream_gen:
                # The TV deactivated while we were connecting; abandon this session.
                LOGGER.info(
                    "Discarding outbound stream to %s area %s: stopped mid-connect",
                    bridge.host,
                    area,
                )
                await session.aclose()
                return
            self._session = session
            self._stream_lights = lights
            self._ticker = asyncio.create_task(self._run_ticker())
            LOGGER.info("Outbound stream started to %s area %s", bridge.host, area)
        except Exception:
            LOGGER.exception("Failed to start outbound stream to %s area %s", bridge.host, area)
            with suppress(Exception):
                await session.aclose()
        finally:
            self._start_task = None

    async def _run_ticker(self) -> None:
        """Send the buffered colors to the bridge at a fixed rate until inactivity."""
        try:
            while time.monotonic() - self._last_update < IDLE_TIMEOUT_S:
                if self._session is None or not self._session.is_streaming:
                    break
                self._apply_smoothing(self._stream_lights)
                self._apply_identify()
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
        self._identify.clear()
        # Reset the per-stream one-shot diagnostics so the next stream logs its own first frame.
        self._logged_tick = False
        self._logged_empty = False
        self._logged_no_stream = False
        if session is not None:
            await session.aclose()
