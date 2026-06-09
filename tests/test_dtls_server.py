"""Integration test: the library's DTLS client handshakes with the inbound server.

Both ends are our own pure-Python implementations, so this validates the full
handshake + key derivation + record encryption/decryption over loopback without a real TV.
"""

from __future__ import annotations

import asyncio
import uuid

from hue_entertainment import HueDtlsStreamer, LightColorCommand

from ambilight_hue_bridge.emulator.dtls_server import HueDtlsServer
from ambilight_hue_bridge.emulator.huestream import decode_huestream

_CLIENT_KEY = "00112233445566778899AABBCCDDEEFF"
_USERNAME = "test-user"


async def test_server_handshakes_with_lib_client() -> None:
    """The lib client pairs with the server and a sent frame is decrypted and decoded."""
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    def psk(identity: str) -> bytes | None:
        """Return the PSK for the known username."""
        return bytes.fromhex(_CLIENT_KEY) if identity == _USERNAME else None

    def on_frame(data: bytes) -> None:
        """Collect a decrypted application-data record."""
        received.append(data)

    server = HueDtlsServer(
        psk_provider=psk,
        on_frame=on_frame,
        loop=loop,
        host="127.0.0.1",
        port=2100,
    )
    await server.start()
    streamer = HueDtlsStreamer()
    area = str(uuid.UUID(int=0))
    try:
        await loop.run_in_executor(
            None,
            streamer.connect,
            "127.0.0.1",
            _USERNAME,
            _CLIENT_KEY,
            area,
        )
        streamer.send_colors([LightColorCommand(channel_id=0, red=65535, green=0, blue=0)])
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received, "server did not receive a decrypted frame"
        frame = decode_huestream(received[0])
        assert frame is not None
        assert frame.is_v2
        assert frame.colors[0].target == 0
        assert frame.colors[0].rgb[0] > 0
    finally:
        await loop.run_in_executor(None, streamer.disconnect)
        await server.stop()
