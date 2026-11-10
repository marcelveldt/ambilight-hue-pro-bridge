"""Engine: buffers inbound colors and streams them to each TV's output on demand."""

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
# Zigbee airtime is shared with the whole mesh, so ZHA runs at most one fast entertainment stream
# (a TV streaming entertainment) and one slow one (a TV pushing v1 REST colors about once a
# second, where 5 frames/s with the lamps fading in between look the same).
_ZIGBEE_FAST = "fast"
_ZIGBEE_SLOW = "slow"
_ZIGBEE_RATE_HZ = {_ZIGBEE_FAST: 25.0, _ZIGBEE_SLOW: 5.0}
# A TV that is not streaming re-checks whether it can start at most this often; a stream that
# closes wakes the waiting outputs right away.
_RECHECK_S = 0.2
# Identify ("blink this light", from the TV's Hue alert during setup): flash full white on/off
# over the stream for a short window ("select") or a longer one ("lselect").
_IDENTIFY_SELECT_S = 2.0
_IDENTIFY_LSELECT_S = 15.0
_IDENTIFY_BLINK_HZ = 1.5
_IDENTIFY_RGB = (65535, 65535, 65535)


class Engine:
    """Owns the TVs' color buffers and one on-demand outbound stream per output."""

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
        self._owner_buffers: dict[str, ColorBuffer] = {}
        self._last_updates: dict[str, float] = {}
        self._active_owners: set[str] = set()
        self._request_order: dict[str, int] = {}
        self._next_request_order = 0
        self._last_recheck: dict[str, float] = {}
        # TVs that stream entertainment (newer, DTLS) rather than only pushing v1 REST colors.
        self._fast_tvs: set[str] = set()
        # Restarts started from synchronous calls (kept so they are not garbage collected).
        self._background: set[asyncio.Task[None]] = set()
        # One stream slot per exclusive output (a Hue bridge, or a group of Zigbee lamps), keyed
        # by _resource_key() and created on first use. TVs on different outputs stream at once.
        self._slots: dict[str, _StreamSlot] = {}
        # Lights each TV asked to identify, keyed by owner then light id -> blink expiry.
        self._identify: dict[str, dict[str, float]] = {}
        # One-shot diagnostic flags so the streaming hot path logs its first event of each
        # kind (first color in, why it can't start) without per-frame spam.
        self._logged_submit = False
        self._logged_no_stream = False

    @property
    def is_streaming(self) -> bool:
        """Return True while any outbound stream is active."""
        return any(slot.is_streaming for slot in self._slots.values())

    @property
    def stream_owners(self) -> frozenset[str]:
        """Return the paired usernames that own an active (or opening) outbound stream."""
        return frozenset(slot.owner for slot in self._slots.values() if slot.owner is not None)

    def submit_color(self, owner: str, light_id: str, rgb: tuple[int, int, int]) -> None:
        """
        Record a color for a virtual light and ensure the stream is running.

        Safe to call from a request handler; never blocks.

        :param owner: The paired TV the color came from; adopted as the stream owner when an
            older v1-REST TV pushes colors without first activating an entertainment group.
        :param light_id: The virtual light id the color applies to.
        :param rgb: 16-bit RGB tuple (0-65535 per channel).
        """
        now = time.monotonic()
        # Keep colors isolated because every TV uses the same virtual light ids. A denied TV
        # may continue sending while another one owns the real bridge.
        self._owner_buffers.setdefault(owner, ColorBuffer()).set_color(light_id, rgb)
        self._last_updates[owner] = now
        self._remember_request(owner)
        if not self._logged_submit:
            self._logged_submit = True
            LOGGER.info("First inbound color submitted: light=%s rgb=%s", light_id, rgb)
        owned = self._owned_slot(owner)
        if owned is not None and owned.is_streaming and owned.transition_task is None:
            # The streaming TV's own frames: nothing to arbitrate (setting changes arrive through
            # tv_settings_changed / restart_stream).
            return
        if now - self._last_recheck.get(owner, 0.0) < _RECHECK_S:
            return
        self._last_recheck[owner] = now
        slot = self._slot_for(owner)
        if slot is not None and self._preferred_owner(slot) == owner:
            self._schedule_reconcile(slot)
        elif not self._logged_no_stream and (
            slot is None
            or (
                not slot.is_streaming
                and slot.transition_task is None
                and now >= slot.regrab_block_until
            )
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
            TV on the same output has equal or higher priority.
        """
        user = paired_user(self._store, owner)
        if user is None:
            return False
        self._last_updates[owner] = time.monotonic()
        self._remember_request(owner)
        if owner not in self._fast_tvs:
            self._fast_tvs.add(owner)
            if self._owned_slot(owner) is not None and user.target is StreamTarget.ZIGBEE:
                # Its stream so far came from REST colors: reopen it at the fast rate.
                task = asyncio.create_task(self.restart_stream(owner))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        slot = self._slot_for(owner)
        if slot is None or not self._owner_ready(owner):
            return True
        # Denied only for a TV on its own output; waiting for Zigbee capacity is accepted quietly.
        preferred = self._preferred_owner(slot, class_aware=False)
        if preferred != owner:
            LOGGER.warning(
                "Denying entertainment stream for %s: %s has equal or higher priority",
                owner,
                preferred,
            )
            return False
        self._schedule_reconcile(slot)
        return True

    def release_stream(self, owner: str) -> None:
        """Record that a TV no longer requests streaming, keeping its current session warm."""
        self._active_owners.discard(owner)
        self._request_order.pop(owner, None)
        self._identify.pop(owner, None)
        slot = self._owned_slot(owner)
        if slot is not None and self._preferred_owner(slot) is not None:
            self._schedule_reconcile(slot)

    def tv_settings_changed(self, owner: str) -> None:
        """Re-evaluate ownership after a TV's enabled state, priority, or target changes."""
        now = time.monotonic()
        if (
            owner in self._active_owners
            and now - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S
        ):
            self._last_updates[owner] = now
        self._schedule_reconcile_all()

    async def remove_tv(self, owner: str) -> None:
        """Forget a removed TV and stop or hand off its outbound stream."""
        self._active_owners.discard(owner)
        self._request_order.pop(owner, None)
        self._last_updates.pop(owner, None)
        self._owner_buffers.pop(owner, None)
        self._identify.pop(owner, None)
        self._fast_tvs.discard(owner)
        self._last_recheck.pop(owner, None)
        if self._owned_slot(owner) is not None:
            await self.stop_stream(owner)
        # The removed TV may have linked other Zigbee TVs' lamps into one output.
        self._schedule_reconcile_all()

    async def stop_stream(self, owner: str | None = None) -> None:
        """Stop one TV's outbound stream, or all streaming when owner is omitted."""
        if owner is None:
            await self.stop()
            return
        self.release_stream(owner)
        slot = self._owned_slot(owner)
        if slot is None:
            return
        await self._stop_slot(slot)
        self._schedule_reconcile(slot)

    async def restart_stream(self, owner: str) -> None:
        """Restart a TV's active stream so changed stream settings (target, lights) apply now."""
        slot = self._owned_slot(owner)
        if slot is None or slot.restarting:
            return
        slot.restarting = True
        try:
            slot.gen += 1
            await slot.cancel_transition()
            # The teardown runs as the ownership transition, so frames arriving while the old
            # backend closes (restoring Zigbee lamps) can't start a replacement alongside it.
            slot.transition_task = asyncio.create_task(self._restart(slot))
        finally:
            slot.restarting = False

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
        slot = self._slot_for(owner)
        if slot is None or self._preferred_owner(slot) != owner:
            return
        self._identify.setdefault(owner, {})[light_id] = now + (
            _IDENTIFY_LSELECT_S if sustained else _IDENTIFY_SELECT_S
        )
        self._schedule_reconcile(slot)

    def stop_identify(self, light_id: str) -> None:
        """Cancel a light's identify blink (the TV's alert 'none')."""
        for lights in self._identify.values():
            lights.pop(light_id, None)

    async def stop(self) -> None:
        """Stop every outbound stream and any background tasks."""
        self._active_owners.clear()
        self._request_order.clear()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await asyncio.gather(*(self._stop_slot(slot) for slot in self._slots.values()))

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

    def _owner_ready_for(self, owner: str, slot: _StreamSlot, *, class_aware: bool = True) -> bool:
        """
        Return whether a TV is ready to stream and its output is the slot's.

        :param class_aware: Also apply the one-fast/one-slow Zigbee stream limit.
        """
        return (
            self._key_for(owner) == slot.key
            and self._owner_ready(owner)
            and not (class_aware and self._outranked(owner, slot))
        )

    def _preferred_owner(self, slot: _StreamSlot, *, class_aware: bool = True) -> str | None:
        """
        Return the slot's highest-priority active, ready TV; preserve its owner on ties.

        :param class_aware: Also apply the one-fast/one-slow Zigbee stream limit.
        """
        now = time.monotonic()
        candidates = [
            owner
            for owner in self._active_owners
            if now - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S
            and self._owner_ready_for(owner, slot, class_aware=class_aware)
        ]
        if not candidates:
            return None
        highest = max(self._priority(owner) for owner in candidates)
        current = slot.owner
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

    def _slot_for(self, owner: str) -> _StreamSlot | None:
        """Return the stream slot of a TV's output (created on first use), or None without one."""
        key = self._key_for(owner)
        if key is None:
            return None
        slot = self._slots.get(key)
        if slot is None:
            slot = self._slots[key] = _StreamSlot(key)
        return slot

    def _key_for(self, owner: str) -> str | None:
        """Return the output (slot key) a TV streams to now, or None when it has none."""
        key = _resource_key(self._store, owner)
        if key is None or not key.startswith("zigbee:"):
            return key
        # A group keeps the slot one of its TVs already streams on, so linking an idle TV into
        # the group (or unlinking one) does not restart that stream.
        group = _zigbee_group(self._store, owner)
        owned = [
            slot.key
            for slot in self._slots.values()
            if slot.owner in group and slot.key.startswith("zigbee:")
        ]
        return min(owned) if owned else key

    def _owned_slot(self, owner: str) -> _StreamSlot | None:
        """Return the slot whose active or opening stream a TV owns, or None."""
        return next((slot for slot in self._slots.values() if slot.owner == owner), None)

    def _held_elsewhere(self, owner: str, slot: _StreamSlot) -> bool:
        """Return whether another output still holds what a TV needs to start on this one."""
        return bool(self._blocking_slots(owner, slot))

    def _blocking_slots(self, owner: str, slot: _StreamSlot) -> list[_StreamSlot]:
        """
        Return the other outputs holding what a TV needs to start on this one.

        That is the TV's own stream there (active, opening or still closing), a lamp of the TV
        that another output's stream drives or is still restoring, or the Zigbee capacity of
        the TV's class (one fast and one slow stream).
        """
        lamps = _zigbee_lamps(self._store, owner)
        zigbee_class = self._zigbee_class(owner)
        return [
            other
            for other in self._slots.values()
            if other is not slot
            and (
                other.owner == owner
                or (other.closing_owner == owner and other.is_closing)
                or not lamps.isdisjoint(other.busy_lamps)
                or (zigbee_class and zigbee_class in other.busy_classes)
            )
        ]

    def _outranked(self, owner: str, slot: _StreamSlot) -> bool:
        """
        Return whether a Zigbee TV must yield its kind of stream to a TV on another output.

        That TV must be of the same class (fast or slow) and win its own output, and have a
        higher priority, unless this TV no longer requests streaming at all.
        """
        zigbee_class = self._zigbee_class(owner)
        if not zigbee_class:
            return False
        released = owner not in self._active_owners
        priority = self._priority(owner)
        for rival in self._active_owners:
            if rival == owner or self._zigbee_class(rival) != zigbee_class:
                continue
            if not released and self._priority(rival) <= priority:
                continue
            rival_slot = self._slot_for(rival)
            if rival_slot is None or rival_slot is slot:
                continue
            if self._preferred_owner(rival_slot, class_aware=False) == rival:
                return True
        return False

    def _zigbee_class(self, owner: str) -> str:
        """Return a Zigbee-target TV's stream class (fast or slow), or "" for other TVs."""
        user = paired_user(self._store, owner)
        if user is None or user.target is not StreamTarget.ZIGBEE:
            return ""
        return _ZIGBEE_FAST if owner in self._fast_tvs else _ZIGBEE_SLOW

    def _schedule_reconcile_all(self, *, skip: _StreamSlot | None = None) -> None:
        """
        Schedule reconciliation of every output, including any an active TV just moved to.

        :param skip: An output to leave out (one that reconciles itself).
        """
        for owner in self._active_owners:
            self._slot_for(owner)
        for slot in list(self._slots.values()):
            if slot is not skip:
                self._schedule_reconcile(slot)

    def _schedule_reconcile(self, slot: _StreamSlot) -> None:
        """Schedule serialized ownership reconciliation without blocking request handlers."""
        desired = self._preferred_owner(slot)
        current = slot.owner
        if slot.transition_task is not None:
            if current is not None and (
                not self._owner_ready_for(current, slot)
                or (desired is not None and desired != current)
            ):
                slot.gen += 1
            return
        if slot.is_streaming and current is not None and current == desired:
            return
        if current is None:
            if desired is None or time.monotonic() < slot.regrab_block_until:
                return
            blocking = self._blocking_slots(desired, slot)
            if blocking:
                # The output in the way may now be streaming for an outranked TV: re-check it.
                for other in blocking:
                    if other.owner not in (None, desired):
                        self._schedule_reconcile(other)
                return
        slot.transition_task = asyncio.create_task(self._reconcile(slot))

    async def _reconcile(self, slot: _StreamSlot) -> None:
        """Stop, start, or hand off one output's stream to its preferred TV."""
        try:
            while True:
                desired = self._preferred_owner(slot)
                current = slot.owner
                if current is not None and not self._owner_ready_for(current, slot):
                    slot.gen += 1
                    await self._teardown(slot)
                    continue
                if desired is None:
                    return
                if current is not None and current != desired:
                    slot.gen += 1
                    await self._teardown(slot)
                    continue
                if current is not None and slot.backend is not None and not slot.is_streaming:
                    # The stream died before its ticker noticed: tear it down here first, so
                    # the restart below never races the ticker's own teardown.
                    slot.gen += 1
                    await self._teardown(slot)
                    continue
                if slot.is_streaming:
                    return
                if time.monotonic() < slot.regrab_block_until:
                    return
                if current is None:
                    # A TV moving here from another output waits until its stream there has closed.
                    if self._held_elsewhere(desired, slot):
                        return
                    slot.owner = desired
                    slot.lamps = _zigbee_lamps(self._store, desired)
                    slot.stream_class = self._zigbee_class(desired)
                slot.buffer = self._owner_buffers.setdefault(desired, ColorBuffer())
                started = await self._open_stream(slot, desired)
                if started:
                    continue
                if self._preferred_owner(slot) != desired:
                    continue
                return
        finally:
            slot.transition_task = None

    async def _restart(self, slot: _StreamSlot) -> None:
        """Tear the slot's active stream down, then reconcile; runs as its transition task."""
        try:
            await self._teardown(slot)
        except BaseException:
            slot.transition_task = None
            raise
        await self._reconcile(slot)

    async def _stop_slot(self, slot: _StreamSlot) -> None:
        """Stop a slot's stream, discarding any start still in flight."""
        slot.gen += 1
        await slot.cancel_transition()
        await self._teardown(slot)

    def _resolve_smoothing(self, owner: str) -> float:
        """Return a TV's smoothing setting, or 0.0 (off) when it has none."""
        user = paired_user(self._store, owner)
        if user is not None and user.stream_smoothing is not None:
            return user.stream_smoothing
        return 0.0

    def _apply_identify(self, slot: _StreamSlot, owner: str) -> None:
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
            slot.smoothed_buffer.set_color(light_id, rgb)

    def _create_backend(self, user: PairedUser) -> tuple[OutputBackend, str] | None:
        """
        Return the output backend for a TV's stream target, with a target label for logs.

        None when a Hue-target TV has no real bridge to stream to.
        """
        if user.target is StreamTarget.ZIGBEE:
            lamps = user.zigbee_lights
            zigbee_class = self._zigbee_class(user.username)
            backend = ZigbeeBackend(
                self._home_assistant, lamps, max_rate_hz=_ZIGBEE_RATE_HZ[zigbee_class]
            )
            return backend, f"{len(lamps)} Zigbee lamp(s), {zigbee_class}"
        bridge = active_bridge(self._store)
        if bridge is None:
            return None
        area = user.entertainment_area
        return HueBridgeBackend(bridge, area), f"{bridge.host} area {area}"

    async def _open_stream(self, slot: _StreamSlot, owner: str) -> bool:
        """Open the output stream for one TV and start its ticker."""
        gen = slot.gen
        # Never overlap a previous backend that is still closing (restoring its lamps).
        with suppress(Exception):
            await slot.wait_for_close()
        user = paired_user(self._store, owner)
        # The TV may have been disabled or preempted while we waited (that bumps the gen).
        created = self._create_backend(user) if user is not None and gen == slot.gen else None
        if user is None or created is None:
            if slot.owner == owner:
                slot.owner = None
            return False
        backend, target = created
        lights = user.lights
        try:
            LOGGER.info("Starting outbound stream to %s (owner=%s)", target, owner)
            await backend.start()
        except asyncio.CancelledError:
            # stop() cancelled us mid-connect. If the cancel landed after backend.start() had
            # already activated the area on the bridge (its pre-DTLS HTTP PUT), the backend is
            # orphaned here - close it so the area is deactivated and the HTTP session released.
            slot.closing = asyncio.create_task(backend.aclose())
            slot.closing_owner = owner
            with suppress(Exception):
                await slot.wait_for_close()
            raise
        except Exception:
            LOGGER.exception("Failed to start outbound stream to %s", target)
            slot.regrab_block_until = time.monotonic() + _START_RETRY_BACKOFF_S
            with suppress(Exception):
                await backend.aclose()
            if slot.owner == owner:
                slot.owner = None
            return False
        else:
            if gen != slot.gen:
                LOGGER.info(
                    "Discarding outbound stream to %s: ownership changed mid-connect", target
                )
                await backend.aclose()
                if slot.owner == owner:
                    slot.owner = None
                return False
            slot.backend = backend
            slot.lights = lights
            slot.remote_dead = False
            slot.ticker = asyncio.create_task(self._run_ticker(slot, owner))
            slot.health_task = asyncio.create_task(slot.run_health(owner, backend))
            LOGGER.info("Outbound stream started to %s", target)
            return True

    async def _run_ticker(self, slot: _StreamSlot, owner: str) -> None:
        """Send the buffered colors to the output at a fixed rate until inactivity."""
        try:
            while time.monotonic() - self._last_updates.get(owner, 0.0) < IDLE_TIMEOUT_S:
                backend = slot.backend
                if (
                    slot.owner != owner
                    or slot.remote_dead
                    or backend is None
                    or not backend.is_streaming
                ):
                    break
                slot.apply_smoothing(slot.lights, self._resolve_smoothing(owner))
                self._apply_identify(slot, owner)
                commands = map_to_commands(slot.lights, slot.smoothed_buffer)
                if commands:
                    if not slot.logged_tick:
                        slot.logged_tick = True
                        LOGGER.info(
                            "First outbound frame: %d command(s) channels=%s",
                            len(commands),
                            [command.channel_id for command in commands],
                        )
                    backend.send(commands)
                elif not slot.logged_empty:
                    slot.logged_empty = True
                    LOGGER.warning(
                        "Ticker produced 0 commands (lights=%d) - nothing to stream",
                        len(slot.lights),
                    )
                await asyncio.sleep(self._interval)
        finally:
            if slot.owner == owner:
                # A start already in flight for this owner must discard itself, not adopt the
                # slot this teardown is clearing.
                slot.gen += 1
                await self._teardown(slot)
                LOGGER.info("Outbound stream stopped")

    async def _teardown(self, slot: _StreamSlot) -> None:
        """Stop the slot's ticker, the health monitor, and close the output backend."""
        ticker = slot.ticker
        slot.ticker = None
        if ticker is not None and ticker is not asyncio.current_task():
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        health = slot.health_task
        slot.health_task = None
        if health is not None and health is not asyncio.current_task():
            health.cancel()
            with suppress(asyncio.CancelledError):
                await health
        backend = slot.backend
        owner = slot.owner
        slot.backend = None
        slot.owner = None
        slot.lights = []
        slot.smoothed_float.clear()
        if owner is not None:
            self._identify.pop(owner, None)
        # Reset per-stream state so the next stream starts clean. The re-grab backoff persists
        # (it intentionally outlives the torn-down session it was set for).
        slot.remote_dead = False
        slot.logged_tick = False
        slot.logged_empty = False
        self._logged_no_stream = False
        if backend is not None:
            # Closing restores Zigbee lamps: it runs on even when this teardown is cancelled, and
            # the next stream (or shutdown) waits for it.
            slot.closing = asyncio.create_task(backend.aclose())
            slot.closing_owner = owner
            slot.closing_lamps = slot.lamps
            slot.closing_class = slot.stream_class
        await slot.wait_for_close()
        # A TV waiting on another output for this one's TV or lamps (a target switch, a
        # regrouping) resumes now; this output itself restarts on its next frame as before.
        self._schedule_reconcile_all(skip=slot)


class _StreamSlot:
    """The outbound stream to one exclusive output, shared by the TVs that target it."""

    def __init__(self, key: str) -> None:
        """
        Initialize an idle slot.

        :param key: The output's resource key (see ``_resource_key``).
        """
        self.key = key
        # The owning TV's inbound colors, which the smoothing eases toward.
        self.buffer = ColorBuffer()
        # Smoothed (eased) colors actually streamed to the bridge: float state for precision
        # plus an int buffer the mapper reads, so abrupt TV color jumps become fades.
        self.smoothed_float: dict[str, tuple[float, float, float]] = {}
        self.smoothed_buffer = ColorBuffer()
        self.backend: OutputBackend | None = None
        # A torn-down backend's close (for Zigbee: restoring the lamps) still in progress, and
        # the TV it streamed for.
        self.closing: asyncio.Task[None] | None = None
        self.closing_owner: str | None = None
        # The Zigbee lamps (IEEE) of the owning TV's stream, and of the stream still closing.
        self.lamps: frozenset[str] = frozenset()
        self.closing_lamps: frozenset[str] = frozenset()
        # The Zigbee stream class (fast/slow) of the owning TV's stream and of the one closing.
        self.stream_class = ""
        self.closing_class = ""
        # True while restart_stream() replaces the transition; concurrent restarts coalesce.
        self.restarting = False
        self.ticker: asyncio.Task[None] | None = None
        self.health_task: asyncio.Task[None] | None = None
        self.transition_task: asyncio.Task[None] | None = None
        # Monotonic time before which a new stream won't be opened (set when another controller
        # took the area over, so we back off rather than fight it, or after a failed start).
        self.regrab_block_until = 0.0
        # Set by the health monitor when the bridge has ended our stream; the ticker watches it
        # and tears down (keeping teardown a single-owner path).
        self.remote_dead = False
        # Username (paired TV) that owns the active or currently-opening outbound stream.
        self.owner: str | None = None
        # The lights mapped for the active stream (the owning TV's assigned lights), resolved
        # when the stream starts. Cleared on teardown.
        self.lights: list[VirtualLight] = []
        # Monotonic token bumped on every stop. The (slow, ~seconds) DTLS connect to the real
        # bridge is not reliably cancellable, so a start that completes after a stop checks this
        # to discard itself instead of leaving a zombie stream behind.
        self.gen = 0
        # One-shot diagnostic flags so the ticker logs its first frame out (or that it has
        # nothing to send) without per-frame spam.
        self.logged_tick = False
        self.logged_empty = False

    @property
    def is_streaming(self) -> bool:
        """Return True while the slot's outbound stream is active."""
        return self.backend is not None and self.backend.is_streaming

    @property
    def is_closing(self) -> bool:
        """Return True while a torn-down backend is still closing."""
        return self.closing is not None and not self.closing.done()

    @property
    def busy_classes(self) -> frozenset[str]:
        """Return the Zigbee stream classes this output's active, opening or closing stream uses."""
        classes = {self.stream_class} if self.owner is not None else set()
        if self.is_closing:
            classes.add(self.closing_class)
        return frozenset(classes - {""})

    @property
    def busy_lamps(self) -> frozenset[str]:
        """Return the Zigbee lamps this output's active, opening or closing stream drives."""
        lamps = self.lamps if self.owner is not None else frozenset()
        return lamps | self.closing_lamps if self.is_closing else lamps

    async def cancel_transition(self) -> None:
        """Cancel and await an in-flight ownership transition."""
        task = self.transition_task
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.transition_task = None

    def apply_smoothing(self, lights: list[VirtualLight], smoothing: float) -> None:
        """
        Ease each light's streamed color toward its latest target color.

        Writes the result into the smoothed buffer the mapper reads, turning the TV's abrupt
        color steps into fades.

        :param lights: The lights to ease.
        :param smoothing: The owning TV's smoothing setting (0.0 = off).
        """
        smoothing = max(0.0, min(MAX_STREAM_SMOOTHING, smoothing))
        for light in lights:
            target = self.buffer.get_color(light.id)
            current = self.smoothed_float.get(light.id)
            if current is None or smoothing <= 0.0:
                # First sight (or smoothing off): jump to the target, no fade-from-black.
                eased = (float(target[0]), float(target[1]), float(target[2]))
            else:
                eased = (
                    smoothing * current[0] + (1.0 - smoothing) * target[0],
                    smoothing * current[1] + (1.0 - smoothing) * target[1],
                    smoothing * current[2] + (1.0 - smoothing) * target[2],
                )
            self.smoothed_float[light.id] = eased
            self.smoothed_buffer.set_color(
                light.id, (round(eased[0]), round(eased[1]), round(eased[2]))
            )

    async def run_health(self, owner: str, tracked_backend: OutputBackend) -> None:
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
            backend = self.backend
            if (
                self.owner != owner
                or backend is not tracked_backend
                or not backend.is_streaming
                or self.remote_dead
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
                self.regrab_block_until = time.monotonic() + _FOREIGN_BACKOFF_S
                LOGGER.warning(
                    "Bridge entertainment taken over by another streamer; backing off %.0fs",
                    _FOREIGN_BACKOFF_S,
                )
            else:
                LOGGER.warning("Bridge ended the entertainment session; tearing down to recover")
            self.remote_dead = True
            return

    async def wait_for_close(self) -> None:
        """Wait for a backend close still in progress; it keeps running if we are cancelled."""
        closing = self.closing
        if closing is None:
            return
        try:
            await asyncio.shield(closing)
        finally:
            if closing.done() and self.closing is closing:
                self.closing = None


def _resource_key(store: ConfigStore, owner: str) -> str | None:
    """
    Return the exclusive output a TV streams to, or None when it has no usable target.

    A Hue bridge runs one entertainment stream at a time, so all its TVs share one key. A Zigbee
    TV's lamps are its own output, unless they overlap another Zigbee TV's lamps: TVs linked by
    a shared lamp (directly or through other TVs) share one key.
    """
    user = paired_user(store, owner)
    if user is None:
        return None
    if user.target is not StreamTarget.ZIGBEE:
        bridge = active_bridge(store)
        return f"hue:{bridge.id}" if bridge is not None else None
    group = _zigbee_group(store, owner)
    # Named after the group's TVs rather than its lamps, so editing lamps within a group keeps
    # its slot, and a group never takes the name a live stream of an earlier group still holds.
    return f"zigbee:{','.join(sorted(group))}" if group else None


def _zigbee_lamps(store: ConfigStore, owner: str) -> frozenset[str]:
    """Return the IEEE addresses of a Zigbee-target TV's lamps (empty for other TVs)."""
    user = paired_user(store, owner)
    if user is None or user.target is not StreamTarget.ZIGBEE:
        return frozenset()
    return frozenset(lamp.ieee.lower() for lamp in user.zigbee_lights)


def _zigbee_group(store: ConfigStore, owner: str) -> frozenset[str]:
    """Return the Zigbee TVs linked to a TV by shared lamps, directly or through others."""
    lamps = set(_zigbee_lamps(store, owner))
    if not lamps:
        return frozenset()
    others = [
        (other.username, _zigbee_lamps(store, other.username))
        for other in store.config.users
        if other.username != owner
    ]
    group = {owner}
    grown = True
    while grown:
        grown = False
        for username, other_lamps in others:
            if username not in group and not lamps.isdisjoint(other_lamps):
                group.add(username)
                lamps |= other_lamps
                grown = True
    return frozenset(group)
