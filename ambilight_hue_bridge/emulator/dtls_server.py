"""
Pure-Python DTLS 1.2 PSK server for the Hue Entertainment protocol.

Receives a TV's entertainment stream on UDP 2100: it performs the server side of the
DTLS 1.2 PSK handshake (only ``TLS_PSK_WITH_AES_128_GCM_SHA256``), then decrypts the
application-data records and hands the plaintext (HueStream frames) to a callback.

The PSK is the ``clientkey`` minted for the client during pairing, looked up by the PSK
identity (the username) the client presents. All socket I/O runs on a dedicated thread so
the asyncio event loop is never blocked; decoded frames are dispatched back onto the loop.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import socket
import struct
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

_DTLS_VERSION = b"\xfe\xfd"
_DTLS_VERSION_INT = 0xFEFD
_CIPHER_SUITE = b"\x00\xa8"  # TLS_PSK_WITH_AES_128_GCM_SHA256

_CT_CHANGE_CIPHER_SPEC = 0x14
_CT_HANDSHAKE = 0x16
_CT_APPLICATION_DATA = 0x17

_HT_CLIENT_HELLO = 0x01
_HT_SERVER_HELLO = 0x02
_HT_HELLO_VERIFY_REQUEST = 0x03
_HT_SERVER_HELLO_DONE = 0x0E
_HT_CLIENT_KEY_EXCHANGE = 0x10
_HT_FINISHED = 0x14

_RECORD_HEADER_LEN = 13
_HS_HEADER_LEN = 12
_RANDOM_LEN = 32
_VERIFY_DATA_LEN = 12
_COOKIE_LEN = 16
_SOCKET_TIMEOUT = 0.5


class HueDtlsServer:
    """Server side of the Hue Entertainment DTLS-PSK protocol (single client)."""

    def __init__(
        self,
        *,
        psk_provider: Callable[[str], bytes | None],
        on_frame: Callable[[str, bytes], None],
        loop: asyncio.AbstractEventLoop,
        host: str = "0.0.0.0",
        port: int = 2100,
    ) -> None:
        """
        Initialize the server (no socket until :meth:`start`).

        :param psk_provider: Maps a PSK identity (username) to its PSK bytes, or None.
        :param on_frame: Called on the event loop with the authenticated PSK identity (the
            client's username) and each decrypted application record.
        :param loop: The asyncio loop to dispatch decoded frames onto.
        :param host: Interface to bind to.
        :param port: UDP port to listen on (2100 for Hue Entertainment).
        """
        self._psk_provider = psk_provider
        self._on_frame = on_frame
        self._loop = loop
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._peer: tuple[str, int] | None = None
        self._aesgcm_in: AESGCM | None = None
        self._aesgcm_out: AESGCM | None = None
        self._reset()

    async def start(self) -> None:
        """Bind the UDP socket and start the receive thread."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        sock.settimeout(_SOCKET_TIMEOUT)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._run, name="hue-dtls-server", daemon=True)
        self._thread.start()
        LOGGER.info("Inbound DTLS server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the receive thread and close the socket."""
        self._running = False
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._thread is not None:
            await self._loop.run_in_executor(None, self._thread.join, 2.0)
            self._thread = None

    def _run(self) -> None:
        """Receive datagrams and drive the handshake / application-data state machine."""
        while self._running:
            # Snapshot the socket: stop() (on the event loop) nulls self._sock, which could
            # otherwise race this thread between the loop guard and the recvfrom call.
            sock = self._sock
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                self._handle_datagram(data, addr)
            except Exception:  # noqa: BLE001 - never let a malformed datagram kill the thread
                LOGGER.debug("Ignoring malformed DTLS datagram from %s", addr, exc_info=True)
        LOGGER.debug("Inbound DTLS server thread stopped")

    def _handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process every DTLS record contained in a datagram."""
        self._peer = addr
        for content_type, epoch, fragment in _parse_records(data):
            if content_type == _CT_HANDSHAKE and epoch == 0:
                for hs_type, body, full in _parse_handshake_messages(fragment):
                    self._handle_handshake(hs_type, body, full)
            elif content_type == _CT_HANDSHAKE and epoch == 1:
                self._handle_client_finished(fragment)
            elif content_type == _CT_APPLICATION_DATA and epoch == 1 and self._established:
                self._handle_application_data(fragment)

    def _handle_handshake(self, hs_type: int, body: bytes, full: bytes) -> None:
        """Dispatch an unencrypted (epoch 0) handshake message."""
        if hs_type == _HT_CLIENT_HELLO:
            self._handle_client_hello(body, full)
        elif hs_type == _HT_CLIENT_KEY_EXCHANGE:
            self._handle_key_exchange(body, full)

    def _handle_client_hello(self, body: bytes, full: bytes) -> None:
        """Handle a ClientHello: send a cookie first, then the server flight."""
        client_random, cookie = _parse_client_hello(body)
        if not cookie:
            self._reset()
            self._cookie = os.urandom(_COOKIE_LEN)
            self._send_handshake(self._build_hello_verify_request(), _HT_HELLO_VERIFY_REQUEST)
            return
        # Second ClientHello (with cookie): start the real handshake transcript here.
        self._client_random = client_random
        self._transcript = bytearray(full)
        self._server_random = _make_random()
        self._send_handshake(self._build_server_hello(), _HT_SERVER_HELLO, record=True)
        self._send_handshake(b"", _HT_SERVER_HELLO_DONE, record=True)

    def _handle_key_exchange(self, body: bytes, full: bytes) -> None:
        """Handle ClientKeyExchange: look up the PSK and derive the session keys."""
        identity_len = int.from_bytes(body[0:2], "big")
        identity = body[2 : 2 + identity_len].decode("utf-8", errors="ignore")
        psk = self._psk_provider(identity)
        if psk is None:
            LOGGER.warning("Inbound DTLS: unknown PSK identity %r", identity)
            return
        self._identity = identity
        self._transcript.extend(full)
        self._derive_keys(psk)

    def _handle_client_finished(self, fragment: bytes) -> None:
        """Handle the client's encrypted Finished, then send the server's Finished."""
        if self._aesgcm_in is None:
            return
        plaintext = self._decrypt(_CT_HANDSHAKE, fragment)
        self._transcript.extend(plaintext)  # the client's Finished is part of the transcript
        self._send_change_cipher_spec()
        self._send_server_finished()
        self._established = True
        LOGGER.info("Inbound DTLS handshake complete with %s", self._peer)

    def _handle_application_data(self, fragment: bytes) -> None:
        """Decrypt an application-data record and dispatch it to the loop."""
        plaintext = self._decrypt(_CT_APPLICATION_DATA, fragment)
        self._loop.call_soon_threadsafe(self._on_frame, self._identity, plaintext)

    # -- crypto / record layer --

    def _derive_keys(self, psk: bytes) -> None:
        """Derive the master secret and AES-GCM keys from the PSK."""
        n = len(psk)
        pre_master = struct.pack("!H", n) + b"\x00" * n + struct.pack("!H", n) + psk
        master_secret = _prf(
            pre_master,
            b"master secret",
            self._client_random + self._server_random,
            48,
        )
        key_block = _prf(
            master_secret,
            b"key expansion",
            self._server_random + self._client_random,
            40,
        )
        self._master_secret = master_secret
        client_write_key = key_block[0:16]
        server_write_key = key_block[16:32]
        self._client_write_iv = key_block[32:36]
        self._server_write_iv = key_block[36:40]
        self._aesgcm_in = AESGCM(client_write_key)
        self._aesgcm_out = AESGCM(server_write_key)

    def _decrypt(self, content_type: int, fragment: bytes) -> bytes:
        """Decrypt an AES-128-GCM record fragment sent by the client."""
        assert self._aesgcm_in is not None
        # A valid record is an 8-byte explicit nonce + ciphertext + 16-byte GCM tag; drop
        # anything shorter rather than computing a negative plaintext length and a bad AAD.
        if len(fragment) < 8 + 16:
            msg = "DTLS application-data record too short"
            raise ValueError(msg)
        explicit_nonce = fragment[0:8]
        ciphertext = fragment[8:]
        nonce = self._client_write_iv + explicit_nonce
        plaintext_len = len(ciphertext) - 16
        aad = explicit_nonce + struct.pack("!BHH", content_type, _DTLS_VERSION_INT, plaintext_len)
        return self._aesgcm_in.decrypt(nonce, ciphertext, aad)

    def _send_change_cipher_spec(self) -> None:
        """Send ChangeCipherSpec and switch the write epoch."""
        self._send_record(_CT_CHANGE_CIPHER_SPEC, b"\x01")
        self._epoch_out = 1
        self._send_seq = 0

    def _send_server_finished(self) -> None:
        """Compute and send the encrypted server Finished message."""
        digest = hashlib.sha256(self._transcript).digest()
        verify_data = _prf(self._master_secret, b"server finished", digest, _VERIFY_DATA_LEN)
        message = self._handshake_header(_HT_FINISHED, len(verify_data)) + verify_data
        self._server_msg_seq += 1
        self._send_encrypted(_CT_HANDSHAKE, message)

    def _send_handshake(self, body: bytes, msg_type: int, *, record: bool = False) -> None:
        """Send a plaintext handshake message, optionally recording it in the transcript."""
        message = self._handshake_header(msg_type, len(body)) + body
        if record:
            self._transcript.extend(message)
        self._server_msg_seq += 1
        self._send_record(_CT_HANDSHAKE, message)

    def _handshake_header(self, msg_type: int, length: int) -> bytes:
        """Build a DTLS handshake-message header (unfragmented)."""
        return struct.pack(
            "!B3sH3s3s",
            msg_type,
            length.to_bytes(3, "big"),
            self._server_msg_seq,
            (0).to_bytes(3, "big"),
            length.to_bytes(3, "big"),
        )

    def _send_record(self, content_type: int, fragment: bytes) -> None:
        """Send a single DTLS record to the connected peer."""
        if self._sock is None or self._peer is None:
            return
        header = struct.pack(
            "!BHH6sH",
            content_type,
            _DTLS_VERSION_INT,
            self._epoch_out,
            self._send_seq.to_bytes(6, "big"),
            len(fragment),
        )
        self._sock.sendto(header + fragment, self._peer)
        self._send_seq += 1

    def _send_encrypted(self, content_type: int, plaintext: bytes) -> None:
        """Encrypt and send an AES-128-GCM record using the server write key."""
        assert self._aesgcm_out is not None
        explicit_nonce = struct.pack("!H", self._epoch_out) + self._send_seq.to_bytes(6, "big")
        nonce = self._server_write_iv + explicit_nonce
        aad = explicit_nonce + struct.pack("!BHH", content_type, _DTLS_VERSION_INT, len(plaintext))
        ciphertext = self._aesgcm_out.encrypt(nonce, plaintext, aad)
        self._send_record(content_type, explicit_nonce + ciphertext)

    # -- message builders --

    def _build_hello_verify_request(self) -> bytes:
        """Build the HelloVerifyRequest body (server version + cookie)."""
        return _DTLS_VERSION + bytes([len(self._cookie)]) + self._cookie

    def _build_server_hello(self) -> bytes:
        """Build the ServerHello body (version, random, no session, chosen cipher)."""
        return (
            _DTLS_VERSION
            + self._server_random
            + b"\x00"  # session_id length
            + _CIPHER_SUITE
            + b"\x00"  # null compression
        )

    def _reset(self) -> None:
        """Reset all per-handshake state."""
        self._identity = ""
        self._cookie = b""
        self._client_random = b""
        self._server_random = b""
        self._master_secret = b""
        self._client_write_iv = b""
        self._server_write_iv = b""
        self._aesgcm_in = None
        self._aesgcm_out = None
        self._transcript = bytearray()
        self._server_msg_seq = 0
        self._epoch_out = 0
        self._send_seq = 0
        self._established = False


def _parse_records(data: bytes) -> list[tuple[int, int, bytes]]:
    """Split a datagram into ``(content_type, epoch, fragment)`` records."""
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset + _RECORD_HEADER_LEN <= len(data):
        content_type = data[offset]
        epoch = int.from_bytes(data[offset + 3 : offset + 5], "big")
        length = int.from_bytes(data[offset + 11 : offset + 13], "big")
        fragment = data[offset + _RECORD_HEADER_LEN : offset + _RECORD_HEADER_LEN + length]
        records.append((content_type, epoch, fragment))
        offset += _RECORD_HEADER_LEN + length
    return records


def _parse_handshake_messages(fragment: bytes) -> list[tuple[int, bytes, bytes]]:
    """Split a handshake fragment into ``(type, body, full_message)`` tuples."""
    messages: list[tuple[int, bytes, bytes]] = []
    offset = 0
    while offset + _HS_HEADER_LEN <= len(fragment):
        hs_type = fragment[offset]
        length = int.from_bytes(fragment[offset + 1 : offset + 4], "big")
        end = offset + _HS_HEADER_LEN + length
        messages.append((hs_type, fragment[offset + _HS_HEADER_LEN : end], fragment[offset:end]))
        offset = end
    return messages


def _parse_client_hello(body: bytes) -> tuple[bytes, bytes]:
    """Extract the client random and cookie from a ClientHello body."""
    offset = 2  # skip client_version
    client_random = body[offset : offset + _RANDOM_LEN]
    offset += _RANDOM_LEN
    session_id_len = body[offset]
    offset += 1 + session_id_len
    cookie_len = body[offset]
    offset += 1
    cookie = body[offset : offset + cookie_len]
    return client_random, cookie


def _make_random() -> bytes:
    """Generate a 32-byte TLS random (4-byte time + 28 random bytes)."""
    return struct.pack("!I", int(time.time())) + os.urandom(28)


def _prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2 PRF with SHA-256."""
    result = b""
    a = hmac.new(secret, label + seed, hashlib.sha256).digest()
    while len(result) < length:
        result += hmac.new(secret, a + label + seed, hashlib.sha256).digest()
        a = hmac.new(secret, a, hashlib.sha256).digest()
    return result[:length]
