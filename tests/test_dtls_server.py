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


def test_inbound_recv_error_keeps_listening(monkeypatch) -> None:
    """A transient socket error logs and keeps the receive thread alive (it must not break out)."""
    monkeypatch.setattr("ambilight_hue_bridge.emulator.dtls_server._ERROR_BACKOFF_S", 0.0)
    loop = asyncio.new_event_loop()
    server = HueDtlsServer(
        psk_provider=lambda _identity: None,
        on_frame=lambda _identity, _data: None,
        loop=loop,
        port=0,
    )
    calls = {"n": 0}

    class _FlakySock:
        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("connection refused")  # transient ICMP-style error
            server._running = False  # let the loop exit cleanly on the next guard check
            raise TimeoutError

        def close(self) -> None:
            """No-op close for the injected socket."""

    server._sock = _FlakySock()  # type: ignore[assignment]
    server._running = True
    try:
        server._run()  # returns only because the flaky socket flips _running on the 3rd call
    finally:
        loop.close()
    # Without the fix the first OSError would break out after a single recvfrom; with it the
    # thread rides through both transient errors and keeps listening.
    assert calls["n"] == 3


async def test_server_handshakes_with_lib_client() -> None:
    """The lib client pairs with the server and a sent frame is decrypted and decoded."""
    loop = asyncio.get_running_loop()
    received: list[tuple[str, bytes]] = []

    def psk(identity: str) -> bytes | None:
        """Return the PSK for the known username."""
        return bytes.fromhex(_CLIENT_KEY) if identity == _USERNAME else None

    def on_frame(identity: str, data: bytes) -> None:
        """Collect the authenticated identity and a decrypted application-data record."""
        received.append((identity, data))

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
        identity, payload = received[0]
        assert identity == _USERNAME
        frame = decode_huestream(payload)
        assert frame is not None
        assert frame.is_v2
        assert frame.colors[0].target == 0
        assert frame.colors[0].rgb[0] > 0
    finally:
        await loop.run_in_executor(None, streamer.disconnect)
        await server.stop()
