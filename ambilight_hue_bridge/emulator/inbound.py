"""Inbound entertainment streaming: receive a TV's DTLS HueStream and feed the engine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .dtls_server import HueDtlsServer
from .huestream import decode_huestream

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.models import VirtualLight
    from ambilight_hue_bridge.config.store import ConfigStore
    from ambilight_hue_bridge.engine.engine import Engine

    from .pairing import PairingManager

LOGGER = logging.getLogger(__name__)

_HUE_ENTERTAINMENT_PORT = 2100


class InboundStreamer:
    """Runs the inbound DTLS server and forwards decoded colours to the engine."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        pairing: PairingManager,
        engine: Engine,
        port: int = _HUE_ENTERTAINMENT_PORT,
    ) -> None:
        """
        Initialize the inbound streamer (no socket until :meth:`start`).

        :param store: Config store providing the virtual lights.
        :param pairing: Pairing manager used to look up the PSK by username.
        :param engine: Engine that receives the decoded colours.
        :param port: UDP port to listen on for the entertainment stream.
        """
        self._store = store
        self._pairing = pairing
        self._engine = engine
        self._port = port
        self._server: HueDtlsServer | None = None

    async def start(self) -> None:
        """Start listening for the TV's inbound entertainment stream."""
        server = HueDtlsServer(
            psk_provider=self._psk,
            on_frame=self._on_frame,
            loop=asyncio.get_running_loop(),
            port=self._port,
        )
        await server.start()
        self._server = server

    async def stop(self) -> None:
        """Stop the inbound DTLS server."""
        if self._server is not None:
            await self._server.stop()
            self._server = None

    def _psk(self, identity: str) -> bytes | None:
        """Return the PSK (client key bytes) for a username, or None if unknown."""
        clientkey = self._pairing.clientkey_for(identity)
        if not clientkey:
            return None
        try:
            return bytes.fromhex(clientkey)
        except ValueError:
            return None

    def _on_frame(self, data: bytes) -> None:
        """Decode a HueStream frame and submit each colour to the engine."""
        frame = decode_huestream(data)
        if frame is None:
            return
        lights = self._store.config.virtual_lights
        for color in frame.colors:
            light_id = _resolve_light(is_v2=frame.is_v2, target=color.target, lights=lights)
            if light_id is not None:
                self._engine.submit_color(light_id, color.rgb)


def _resolve_light(*, is_v2: bool, target: int, lights: list[VirtualLight]) -> str | None:
    """
    Resolve a frame target to a virtual light id.

    v1 frames address a light id directly; v2 frames address a channel, mapped here to the
    virtual light at that index (best-effort, pending verification against a real TV).

    :param is_v2: Whether the frame uses HueStream v2 (channel) addressing.
    :param target: The light id (v1) or channel id (v2) from the frame.
    :param lights: The configured virtual lights, in order.
    """
    if is_v2:
        return lights[target].id if 0 <= target < len(lights) else None
    return str(target)
