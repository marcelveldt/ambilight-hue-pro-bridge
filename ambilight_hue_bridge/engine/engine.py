"""Engine: buffers inbound colors and streams them to the real bridge on demand."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import StreamTarget
from ambilight_hue_bridge.const import MAX_STREAM_SMOOTHING
from ambilight_hue_bridge.outbound.backend import BackendHealth, HueBridgeBackend, ZigbeeBackend
from ambilight_hue_bridge.outbound.controller import active_bridge, paired_user, tv_stream_target
from ambilight_hue_bridge.outbound.homeassistant import HomeAssistantClient

from .ingest import ColorBuffer
from .mapping import map_to_commands

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import PairedUser, VirtualLight
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.outbound.backend import OutputBackend

LOGGER = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 10.0
# Outbound liveness: a one-way DTLS stream can't tell when the bridge silently ends the session
# (a Pro reboot, an entertainment timeout, or another controller taking over) - sends keep
# "succeeding" into the void. So poll the bridge's view of our area; if it is no longer ours for
# this many consecutive polls, tear the stream down so the next inbound frame re-establishes it.
_HEALTH_POLL_S = 5.0
_HEALTH_FAILS_TO_RECOVER = 2
# After another controller takes the area over, wait this long before re-grabbing it, so we don't
# fight a deliberate Hue-app / Sync-box stream on every frame.
_FOREIGN_BACKOFF_S = 60.0
# After a failed start, wait this long before the next attempt instead of retrying on every frame.
_START_RETRY_BACKOFF_S = 3.0
# Identify ("blink this light", from the TV's Hue alert during setup): flash full white on/off
# over the stream for a short window ("select") or a longer one ("lselect").
_IDENTIFY_SELECT_S = 2.0
_IDENTIFY_LSELECT_S = 15.0
_IDENTIFY_BLINK_HZ = 1.5
_IDENTIFY_RGB = (65535, 65535, 65535)


class Engine:
    """Owns the ingest buffer and the on-demand outbound entertainment stream."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        rate_hz: int | None = None,
        home_assistant: HomeAssistantClient | None = None,
    ) -> None:
        """
        Initialize the engine (no stream until the first color is submitted).

        :param store: Config store providing the real bridges and virtual lights.
        :param rate_hz: Outbound frame rate in Hz (defaults to the configured rate).
        :param home_assistant: Client for the Zigbee target (defaults to one on ``store``).
        """
        self._store = store
        self._home_assistant = (
            home_assistant if home_assistant is not None else HomeAssistantClient(store)
        )
        rate = rate_hz if rate_hz is not None else store.config.virtual_bridge.stream_rate_hz
        self._interval = 1.0 / max(1, rate)
        self._buffer = ColorBuffer()
        self._owner_buffers: dict[str, ColorBuffer] = {}
        # Smoothed (eased) colors actually streamed to the bridge: float state for precision
        # plus an int buffer the mapper reads, so abrupt TV color jumps become fades.
        self._smoothed_float: dict[str, tuple[float, float, float]] = {}
        self._smoothed_buffer = ColorBuffer()
        self._backend: OutputBackend | None = None
        # A torn-down backend's close (for Zigbee: restoring the lamps) still in progress.
        self._closing: asyncio.Task[None] | None = None
        # True while restart_stream() replaces the transition; concurrent restarts coalesce.
        self._restarting = False
        self._ticker: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._transition_task: asyncio.Task[None] | None = None
        self._last_updates: dict[str, float] = {}
        self._active_owners: set[str] = set()
        self._request_order: dict[str, int] = {}
        self._next_request_order = 0
        # Monotonic time before which a new stream won't be opened (set when another controller
        # took the area over, so we back off rather than fight it, or after a failed start).
        self._regrab_block_until = 0.0
        # Set by the health monitor when the bridge has ended our stream; the ticker watches it
        # and tears down (keeping teardown a single-owner path).
        self._remote_dead = False
        # Username (paired TV) that owns the active or currently-opening outbound stream.
        self._stream_owner: str | None = None
        # The lights mapped for the active stream (the owning TV's assigned lights), resolved
        # when the stream starts. Cleared on teardown.
        self._stream_lights: list[VirtualLight] = []
        # Monotonic token bumped on every stop. The (slow, ~seconds) DTLS connect to the real
        # bridge is not reliably cancellable, so a start that completes after a stop checks this
        # to discard itself instead of leaving a zombie stream behind.
        self._stream_gen = 0
        # Lights each TV asked to identify, keyed by owner then light id -> blink expiry.
        self._identify: dict[str, dict[str, float]] = {}
        # One-shot diagnostic flags so the streaming hot path logs its first event of each
        # kind (first color in, first frame out, why it can't start) without per-frame spam.
        self._logged_submit = False
        self._logged_no_stream = False
        self._logged_tick = False
        self._logged_empty = False

    @property
    def is_streaming(self) -> bool:
        """Return True while the outbound stream is active."""
        return self._backend is not None and self._backend.is_streaming

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
        now = time.monotonic()
        if owner:
            # Keep colors isolated because every TV uses the same virtual light ids. A denied TV
            # may continue sending while another one owns the real bridge.
            self._owner_buffers.setdefault(owner, ColorBuffer()).set_color(light_id, rgb)
            self._last_updates[owner] = now
            self._remember_request(owner)
        else:
            self._buffer.set_color(light_id, rgb)
        if not self._logged_submit:
            self._logged_submit = True
            LOGGER.info("First inbound color submitted: light=%s rgb=%s", light_id, rgb)
        if owner and self._preferred_owner() == owner:
            self._schedule_reconcile()
        elif (
            not self.is_streaming
            and self._transition_task is None
            and not self._logged_no_stream
            and time.monotonic() >= self._regrab_block_until
        ):
            self._logged_no_stream = True
            user = paired_user(self._store, owner)
            if user is not None and user.target is StreamTarget.ZIGBEE:
                LOGGER.warning(
                    "Not starting outbound stream: owner=%s enabled=%s target=zigbee "
                    "home_assistant=%s zigbee_lights=%d",
                    owner,
                    user.enabled,
                    self._home_assistant.available,
                    len(user.zigbee_lights),
                )
            else:
                bridge = active_bridge(self._store)
                area, lights = tv_stream_target(self._store, owner)
                LOGGER.warning(
                    "Not starting outbound stream: owner=%s enabled=%s bridge=%s client_key=%s "
                    "area=%r lights=%d",
                    owner,
                    user.enabled if user is not None else None,
                    bridge.id if bridge else None,
                    bool(bridge and bridge.client_key),
                    area,
                    len(lights),
                )

    def start_stream(self, owner: str) -> bool:
        """
        Register a TV's activation request and open the stream when it wins arbitration.

        Opening the DTLS stream to the real bridge ahead of the first frame hides the
        handshake latency. Disabled or not-yet-configured TVs remain pending so enabling or
        assigning them later can start the real stream without making the TV reconnect.

        :param owner: The paired username activating the stream.
        :return: True when the virtual bridge can accept the request; False when another active
            TV has equal or higher priority.
        """
        user = paired_user(self._store, owner)
        if user is None:
            return False
        self._last_updates[owner] = time.monotonic()
        self._remember_request(owner)
        if not self._owner_ready(owner):
            return True
        preferred = self._preferred_owner()
        if preferred != owner:
            LOGGER.warning(
                "Denying entertainment stream for %s: %s has equal or higher priority",
                owner,
                preferred,
            )
            return False
        self._schedule_reconcile()
        return True

    def release_stream(self, owner: str) -> None:
        """Record that a TV no longer requests streaming, keeping its current session warm."""
        self._active_owners.discard(owner)
        self._request_order.pop(owner, None)
        self._identify.pop(owner, None)
        if self._stream_owner == owner and self._preferred_owner() is not None:
            self._schedule_reconcile()

    def tv_settings_changed(self, owner: str) -> None:
        """Re-evaluate ownership after a TV's enabled state or priority changes."""
        now = time.monotonic()
        if (
            owner in self._active_owners
            and now - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S
        ):
            self._last_updates[owner] = now
        self._schedule_reconcile()

    async def remove_tv(self, owner: str) -> None:
        """Forget a removed TV and stop or hand off its outbound stream."""
        self._active_owners.discard(owner)
        self._request_order.pop(owner, None)
        self._last_updates.pop(owner, None)
        self._owner_buffers.pop(owner, None)
        self._identify.pop(owner, None)
        if self._stream_owner == owner:
            await self.stop_stream(owner)
        else:
            self._schedule_reconcile()

    async def stop_stream(self, owner: str | None = None) -> None:
        """Stop one TV's outbound stream, or all streaming when owner is omitted."""
        if owner is None:
            await self.stop()
            return
        self.release_stream(owner)
        if self._stream_owner != owner:
            return
        self._stream_gen += 1
        await self._cancel_transition()
        await self._teardown()
        self._schedule_reconcile()

    async def restart_stream(self, owner: str) -> None:
        """Restart a TV's active stream so changed stream settings (target, lights) apply now."""
        if self._stream_owner != owner or self._restarting:
            return
        self._restarting = True
        try:
            self._stream_gen += 1
            await self._cancel_transition()
            # The teardown runs as the ownership transition, so frames arriving while the old
            # backend closes (restoring Zigbee lamps) can't start a replacement alongside it.
            self._transition_task = asyncio.create_task(self._restart())
        finally:
            self._restarting = False

    def identify(self, owner: str, light_id: str, *, sustained: bool = False) -> None:
        """
        Blink a light so the user can locate it (the TV's 'alert' during light assignment).

        Opens the outbound stream if needed and overlays a flash on the light's channels for a
        short window, or a longer one for a sustained ('lselect') identify.

        :param owner: The paired TV requesting the identify.
        :param light_id: The virtual light id to flash.
        :param sustained: Whether this is a long 'lselect' identify rather than a single blink.
        """
        now = time.monotonic()
        self._last_updates[owner] = now
        self._remember_request(owner)
        if self._preferred_owner() != owner:
            return
        self._identify.setdefault(owner, {})[light_id] = now + (
            _IDENTIFY_LSELECT_S if sustained else _IDENTIFY_SELECT_S
        )
        self._schedule_reconcile()

    def stop_identify(self, light_id: str) -> None:
        """Cancel a light's identify blink (the TV's alert 'none')."""
        for lights in self._identify.values():
            lights.pop(light_id, None)

    async def stop(self) -> None:
        """Stop the outbound stream and any background tasks."""
        self._active_owners.clear()
        self._request_order.clear()
        self._stream_gen += 1
        await self._cancel_transition()
        await self._teardown()

    def _remember_request(self, owner: str) -> None:
        """Track a TV's active stream intent without changing the order on every frame."""
        if paired_user(self._store, owner) is not None and owner not in self._active_owners:
            self._active_owners.add(owner)
            self._request_order[owner] = self._next_request_order
            self._next_request_order += 1

    def _owner_ready(self, owner: str) -> bool:
        """Return whether a TV is enabled and has a usable outbound stream target."""
        user = paired_user(self._store, owner)
        if user is None or not user.enabled:
            return False
        if user.target is StreamTarget.ZIGBEE:
            return bool(user.zigbee_lights) and self._home_assistant.available
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.client_key:
            return False
        area, _ = tv_stream_target(self._store, owner)
        return bool(area)

    def _preferred_owner(self) -> str | None:
        """Return the highest-priority active, ready TV; preserve the current owner on ties."""
        now = time.monotonic()
        candidates = [
            owner
            for owner in self._active_owners
            if now - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S
            and self._owner_ready(owner)
        ]
        if not candidates:
            return None
        highest = max(self._priority(owner) for owner in candidates)
        current = self._stream_owner
        if current in candidates and self._priority(current) == highest:
            return current
        return min(
            (owner for owner in candidates if self._priority(owner) == highest),
            key=lambda owner: self._request_order[owner],
        )

    def _priority(self, owner: str) -> int:
        """Return a paired TV's configured stream priority."""
        user = paired_user(self._store, owner)
        return user.stream_priority if user is not None else 0

    def _schedule_reconcile(self) -> None:
        """Schedule serialized ownership reconciliation without blocking request handlers."""
        desired = self._preferred_owner()
        current = self._stream_owner
        if self._transition_task is not None:
            if current is not None and (
                not self._owner_ready(current) or (desired is not None and desired != current)
            ):
                self._stream_gen += 1
            return
        if self.is_streaming and current is not None and current == desired:
            return
        if current is None and time.monotonic() < self._regrab_block_until:
            return
        self._transition_task = asyncio.create_task(self._reconcile())

    async def _cancel_transition(self) -> None:
        """Cancel and await an in-flight ownership transition."""
        task = self._transition_task
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._transition_task = None

    async def _reconcile(self) -> None:
        """Stop, start, or hand off the single outbound stream to the preferred TV."""
        try:
            while True:
                desired = self._preferred_owner()
                current = self._stream_owner
                if current is not None and not self._owner_ready(current):
                    self._stream_gen += 1
                    await self._teardown()
                    continue
                if desired is None:
                    return
                if current is not None and current != desired:
                    self._stream_gen += 1
                    await self._teardown()
                    continue
                if current is not None and self._backend is not None and not self.is_streaming:
                    # The stream died before its ticker noticed: tear it down here first, so
                    # the restart below never races the ticker's own teardown.
                    self._stream_gen += 1
                    await self._teardown()
                    continue
                if self.is_streaming:
                    return
                if time.monotonic() < self._regrab_block_until:
                    return
                if current is None:
                    self._stream_owner = desired
                self._buffer = self._owner_buffers.setdefault(desired, ColorBuffer())
                started = await self._open_stream(desired)
                if started:
                    continue
                if self._preferred_owner() != desired:
                    continue
                return
        finally:
            self._transition_task = None

    async def _restart(self) -> None:
        """Tear the active stream down, then reconcile; runs as the transition task."""
        try:
            await self._teardown()
        except BaseException:
            self._transition_task = None
            raise
        await self._reconcile()

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
        user = paired_user(self._store, self._stream_owner)
        if user is not None and user.stream_smoothing is not None:
            return user.stream_smoothing
        return 0.0

    def _apply_identify(self, owner: str) -> None:
        """Overlay a full-white blink on lights the TV asked to identify; expire finished ones."""
        identified = self._identify.get(owner)
        if not identified:
            return
        now = time.monotonic()
        for light_id in [lid for lid, expiry in identified.items() if now >= expiry]:
            del identified[light_id]
        if not identified:
            self._identify.pop(owner, None)
            return
        # Keep the stream alive while identifying, then flash full white on/off.
        self._last_updates[owner] = now
        rgb = _IDENTIFY_RGB if int(now * 2 * _IDENTIFY_BLINK_HZ) % 2 == 0 else (0, 0, 0)
        for light_id in identified:
            self._smoothed_buffer.set_color(light_id, rgb)

    def _create_backend(self, user: PairedUser) -> tuple[OutputBackend, str] | None:
        """
        Return the output backend for a TV's stream target, with a target label for logs.

        None when a Hue-target TV has no real bridge to stream to.
        """
        if user.target is StreamTarget.ZIGBEE:
            lamps = user.zigbee_lights
            return ZigbeeBackend(self._home_assistant, lamps), f"{len(lamps)} Zigbee lamp(s)"
        bridge = active_bridge(self._store)
        if bridge is None:
            return None
        area = user.entertainment_area
        return HueBridgeBackend(bridge, area), f"{bridge.host} area {area}"

    async def _open_stream(self, owner: str) -> bool:
        """Open the output stream for one TV and start its ticker."""
        # Never overlap a previous backend that is still closing (restoring its lamps).
        with suppress(Exception):
            await self._wait_for_close()
        user = paired_user(self._store, owner)
        created = self._create_backend(user) if user is not None else None
        if user is None or created is None:
            if self._stream_owner == owner:
                self._stream_owner = None
            return False
        gen = self._stream_gen
        backend, target = created
        lights = user.lights
        try:
            LOGGER.info("Starting outbound stream to %s (owner=%s)", target, owner)
            await backend.start()
        except asyncio.CancelledError:
            # stop() cancelled us mid-connect. If the cancel landed after backend.start() had
            # already activated the area on the bridge (its pre-DTLS HTTP PUT), the backend is
            # orphaned here - close it so the area is deactivated and the HTTP session released.
            self._closing = asyncio.create_task(backend.aclose())
            with suppress(Exception):
                await self._wait_for_close()
            raise
        except Exception:
            LOGGER.exception("Failed to start outbound stream to %s", target)
            self._regrab_block_until = time.monotonic() + _START_RETRY_BACKOFF_S
            with suppress(Exception):
                await backend.aclose()
            if self._stream_owner == owner:
                self._stream_owner = None
            return False
        else:
            if gen != self._stream_gen:
                LOGGER.info(
                    "Discarding outbound stream to %s: ownership changed mid-connect", target
                )
                await backend.aclose()
                if self._stream_owner == owner:
                    self._stream_owner = None
                return False
            self._backend = backend
            self._stream_lights = lights
            self._remote_dead = False
            self._ticker = asyncio.create_task(self._run_ticker(owner))
            self._health_task = asyncio.create_task(self._run_health(owner, backend))
            LOGGER.info("Outbound stream started to %s", target)
            return True

    async def _run_ticker(self, owner: str) -> None:
        """Send the buffered colors to the bridge at a fixed rate until inactivity."""
        try:
            while time.monotonic() - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S:
                backend = self._backend
                if (
                    self._stream_owner != owner
                    or self._remote_dead
                    or backend is None
                    or not backend.is_streaming
                ):
                    break
                self._apply_smoothing(self._stream_lights)
                self._apply_identify(owner)
                commands = map_to_commands(self._stream_lights, self._smoothed_buffer)
                if commands:
                    if not self._logged_tick:
                        self._logged_tick = True
                        LOGGER.info(
                            "First outbound frame: %d command(s) channels=%s",
                            len(commands),
                            [command.channel_id for command in commands],
                        )
                    backend.send(commands)
                elif not self._logged_empty:
                    self._logged_empty = True
                    LOGGER.warning(
                        "Ticker produced 0 commands (lights=%d) - nothing to stream",
                        len(self._stream_lights),
                    )
                await asyncio.sleep(self._interval)
        finally:
            if self._stream_owner == owner:
                # A start already in flight for this owner must discard itself, not adopt the
                # slot this teardown is clearing.
                self._stream_gen += 1
                await self._teardown()
                LOGGER.info("Outbound stream stopped")

    async def _run_health(self, owner: str, tracked_backend: OutputBackend) -> None:
        """
        Poll the bridge's view of our stream and flag it dead if the bridge silently ended it.

        The bridge can drop our session (a Pro reboot, an entertainment timeout, another
        controller taking over) without our one-way DTLS sender noticing. This polls the
        backend's health and, on a confirmed loss, sets the flag the ticker watches so the
        stream tears down and re-establishes on the next inbound frame.
        """
        fails = 0
        while True:
            await asyncio.sleep(_HEALTH_POLL_S)
            backend = self._backend
            if (
                self._stream_owner != owner
                or backend is not tracked_backend
                or not backend.is_streaming
                or self._remote_dead
            ):
                return
            try:
                health = await backend.check_health()
            except Exception as err:  # noqa: BLE001 - a transient API error is not a stream loss
                LOGGER.debug("Stream health check failed (%s); ignoring", err)
                continue
            if health is BackendHealth.OK:
                fails = 0
                continue
            fails += 1
            if fails < _HEALTH_FAILS_TO_RECOVER:
                continue
            if health is BackendHealth.TAKEN_OVER:
                # A different controller owns the area now - back off, don't fight it.
                self._regrab_block_until = time.monotonic() + _FOREIGN_BACKOFF_S
                LOGGER.warning(
                    "Bridge entertainment taken over by another streamer; backing off %.0fs",
                    _FOREIGN_BACKOFF_S,
                )
            else:
                LOGGER.warning("Bridge ended the entertainment session; tearing down to recover")
            self._remote_dead = True
            return

    async def _teardown(self) -> None:
        """Stop the ticker, the health monitor, and close the output backend."""
        ticker = self._ticker
        self._ticker = None
        if ticker is not None and ticker is not asyncio.current_task():
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        health = self._health_task
        self._health_task = None
        if health is not None and health is not asyncio.current_task():
            health.cancel()
            with suppress(asyncio.CancelledError):
                await health
        backend = self._backend
        self._backend = None
        self._stream_owner = None
        self._stream_lights = []
        self._smoothed_float.clear()
        self._identify.clear()
        # Reset per-stream state so the next stream starts clean. The re-grab backoff persists
        # (it intentionally outlives the torn-down session it was set for).
        self._remote_dead = False
        self._logged_tick = False
        self._logged_empty = False
        self._logged_no_stream = False
        if backend is not None:
            # Closing restores Zigbee lamps: it runs on even when this teardown is cancelled, and
            # the next stream (or shutdown) waits for it.
            self._closing = asyncio.create_task(backend.aclose())
        await self._wait_for_close()

    async def _wait_for_close(self) -> None:
        """Wait for a backend close still in progress; it keeps running if we are cancelled."""
        closing = self._closing
        if closing is None:
            return
        try:
            await asyncio.shield(closing)
        finally:
            if closing.done() and self._closing is closing:
                self._closing = None
