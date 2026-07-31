"""
Pure-Python DTLS 1.2 PSK server for the Hue Entertainment protocol.

Receives a TV's entertainment stream on UDP 2100: it performs the server side of the
DTLS 1.2 PSK handshake (only ``TLS_PSK_WITH_AES_128_GCM_SHA256``), then decrypts the
application-data records and hands the plaintext (HueStream frames) to a callback.

The PSK is the ``clientkey`` minted for the client during pairing, looked up by the PSK
identity (the username) the client presents. All socket I/O runs on a dedicated thread so
the asyncio event loop is never blocked; decoded frames are dispatched back onto the loop.
Handshake and encryption state is isolated per TV UDP endpoint; stream ownership and priority
remain the engine's responsibility after authentication.
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
from dataclasses import dataclass, field
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
_SESSION_IDLE_TIMEOUT_S = 300.0
# Pause after an unexpected socket error before retrying, so a persistent error can't spin the
# receive thread in a hot loop (while still keeping the listener alive across transient ones).
_ERROR_BACKOFF_S = 1.0

type _Peer = tuple[str, int]


@dataclass(slots=True)
class _DtlsSession:
    """DTLS handshake and encryption state for one TV UDP endpoint."""

    peer: _Peer
    last_seen: float = field(default_factory=time.monotonic)
    identity: str = ""
    cookie: bytes = b""
    client_random: bytes = b""
    server_random: bytes = b""
    master_secret: bytes = b""
    client_write_iv: bytes = b""
    server_write_iv: bytes = b""
    aesgcm_in: AESGCM | None = None
    aesgcm_out: AESGCM | None = None
    transcript: bytearray = field(default_factory=bytearray)
    server_msg_seq: int = 0
    epoch_out: int = 0
    send_seq: int = 0
    established: bool = False

    def reset(self) -> None:
        """Reset this peer's state for a new handshake."""
        self.identity = ""
        self.cookie = b""
        self.client_random = b""
        self.server_random = b""
        self.master_secret = b""
        self.client_write_iv = b""
        self.server_write_iv = b""
        self.aesgcm_in = None
        self.aesgcm_out = None
        self.transcript = bytearray()
        self.server_msg_seq = 0
        self.epoch_out = 0
        self.send_seq = 0
        self.established = False


class HueDtlsServer:
    """Server side of the Hue Entertainment DTLS-PSK protocol."""

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
        self._sessions: dict[_Peer, _DtlsSession] = {}

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
        self._sessions.clear()

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
            except OSError as err:
                if not self._running:
                    break  # socket closed by stop() - expected shutdown
                # A transient socket error (e.g. an ICMP ECONNREFUSED after a peer blip, or an
                # interface flap) must NOT kill the listener for the rest of the process life -
                # that silently breaks all entertainment reception while the web UI keeps working.
                LOGGER.warning("Inbound DTLS socket error: %s; continuing to listen", err)
                time.sleep(_ERROR_BACKOFF_S)
                continue
            try:
                self._handle_datagram(data, addr)
            except Exception:  # noqa: BLE001 - never let a malformed datagram kill the thread
                LOGGER.debug("Ignoring malformed DTLS datagram from %s", addr, exc_info=True)
        LOGGER.debug("Inbound DTLS server thread stopped")

    def _handle_datagram(self, data: bytes, addr: _Peer) -> None:
        """Process every DTLS record contained in a datagram."""
        records = _parse_records(data)
        session = self._sessions.get(addr)
        if session is None:
            if not _contains_client_hello(records):
                return
            now = time.monotonic()
            self._prune_sessions(now)
            session = _DtlsSession(peer=addr, last_seen=now)
            self._sessions[addr] = session
        else:
            session.last_seen = time.monotonic()

        for content_type, epoch, fragment in records:
            if content_type == _CT_HANDSHAKE and epoch == 0:
                for hs_type, body, full in _parse_handshake_messages(fragment):
                    self._handle_handshake(session, hs_type, body, full)
            elif content_type == _CT_HANDSHAKE and epoch == 1:
                self._handle_client_finished(session, fragment)
            elif content_type == _CT_APPLICATION_DATA and epoch == 1 and session.established:
                self._handle_application_data(session, fragment)

    def _handle_handshake(
        self,
        session: _DtlsSession,
        hs_type: int,
        body: bytes,
        full: bytes,
    ) -> None:
        """Dispatch an unencrypted (epoch 0) handshake message."""
        if hs_type == _HT_CLIENT_HELLO:
            self._handle_client_hello(session, body, full)
        elif hs_type == _HT_CLIENT_KEY_EXCHANGE:
            self._handle_key_exchange(session, body, full)

    def _handle_client_hello(
        self,
        session: _DtlsSession,
        body: bytes,
        full: bytes,
    ) -> None:
        """Handle a ClientHello: send a cookie first, then the server flight."""
        client_random, cookie = _parse_client_hello(body)
        if not cookie:
            session.reset()
            session.cookie = os.urandom(_COOKIE_LEN)
            self._send_handshake(
                session,
                self._build_hello_verify_request(session),
                _HT_HELLO_VERIFY_REQUEST,
            )
            return
        # Second ClientHello (with cookie): start the real handshake transcript here.
        session.client_random = client_random
        session.transcript = bytearray(full)
        session.server_random = _make_random()
        self._send_handshake(
            session,
            self._build_server_hello(session),
            _HT_SERVER_HELLO,
            record=True,
        )
        self._send_handshake(session, b"", _HT_SERVER_HELLO_DONE, record=True)

    def _handle_key_exchange(
        self,
        session: _DtlsSession,
        body: bytes,
        full: bytes,
    ) -> None:
        """Handle ClientKeyExchange: look up the PSK and derive the session keys."""
        identity_len = int.from_bytes(body[0:2], "big")
        identity = body[2 : 2 + identity_len].decode("utf-8", errors="ignore")
        psk = self._psk_provider(identity)
        if psk is None:
            LOGGER.warning("Inbound DTLS: unknown PSK identity %r", identity)
            return
        session.identity = identity
        session.transcript.extend(full)
        self._derive_keys(session, psk)

    def _handle_client_finished(self, session: _DtlsSession, fragment: bytes) -> None:
        """Handle the client's encrypted Finished, then send the server's Finished."""
        if session.aesgcm_in is None:
            return
        plaintext = self._decrypt(session, _CT_HANDSHAKE, fragment)
        session.transcript.extend(plaintext)  # the client's Finished is part of the transcript
        self._send_change_cipher_spec(session)
        self._send_server_finished(session)
        session.established = True
        self._remove_replaced_sessions(session)
        LOGGER.info(
            "Inbound DTLS handshake complete with %s (identity=%s)",
            session.peer,
            session.identity,
        )

    def _handle_application_data(self, session: _DtlsSession, fragment: bytes) -> None:
        """Decrypt an application-data record and dispatch it to the loop."""
        plaintext = self._decrypt(session, _CT_APPLICATION_DATA, fragment)
        self._loop.call_soon_threadsafe(self._on_frame, session.identity, plaintext)

    # -- crypto / record layer --

    def _derive_keys(self, session: _DtlsSession, psk: bytes) -> None:
        """Derive the master secret and AES-GCM keys from the PSK."""
        n = len(psk)
        pre_master = struct.pack("!H", n) + b"\x00" * n + struct.pack("!H", n) + psk
        master_secret = _prf(
            pre_master,
            b"master secret",
            session.client_random + session.server_random,
            48,
        )
        key_block = _prf(
            master_secret,
            b"key expansion",
            session.server_random + session.client_random,
            40,
        )
        session.master_secret = master_secret
        client_write_key = key_block[0:16]
        server_write_key = key_block[16:32]
        session.client_write_iv = key_block[32:36]
        session.server_write_iv = key_block[36:40]
        session.aesgcm_in = AESGCM(client_write_key)
        session.aesgcm_out = AESGCM(server_write_key)

    def _decrypt(self, session: _DtlsSession, content_type: int, fragment: bytes) -> bytes:
        """Decrypt an AES-128-GCM record fragment sent by the client."""
        assert session.aesgcm_in is not None
        # A valid record is an 8-byte explicit nonce + ciphertext + 16-byte GCM tag; drop
        # anything shorter rather than computing a negative plaintext length and a bad AAD.
        if len(fragment) < 8 + 16:
            msg = "DTLS application-data record too short"
            raise ValueError(msg)
        explicit_nonce = fragment[0:8]
        ciphertext = fragment[8:]
        nonce = session.client_write_iv + explicit_nonce
        plaintext_len = len(ciphertext) - 16
        aad = explicit_nonce + struct.pack("!BHH", content_type, _DTLS_VERSION_INT, plaintext_len)
        return session.aesgcm_in.decrypt(nonce, ciphertext, aad)

    def _send_change_cipher_spec(self, session: _DtlsSession) -> None:
        """Send ChangeCipherSpec and switch the write epoch."""
        self._send_record(session, _CT_CHANGE_CIPHER_SPEC, b"\x01")
        session.epoch_out = 1
        session.send_seq = 0

    def _send_server_finished(self, session: _DtlsSession) -> None:
        """Compute and send the encrypted server Finished message."""
        digest = hashlib.sha256(session.transcript).digest()
        verify_data = _prf(
            session.master_secret,
            b"server finished",
            digest,
            _VERIFY_DATA_LEN,
        )
        message = self._handshake_header(session, _HT_FINISHED, len(verify_data)) + verify_data
        session.server_msg_seq += 1
        self._send_encrypted(session, _CT_HANDSHAKE, message)

    def _send_handshake(
        self,
        session: _DtlsSession,
        body: bytes,
        msg_type: int,
        *,
        record: bool = False,
    ) -> None:
        """Send a plaintext handshake message, optionally recording it in the transcript."""
        message = self._handshake_header(session, msg_type, len(body)) + body
        if record:
            session.transcript.extend(message)
        session.server_msg_seq += 1
        self._send_record(session, _CT_HANDSHAKE, message)

    def _handshake_header(self, session: _DtlsSession, msg_type: int, length: int) -> bytes:
        """Build a DTLS handshake-message header (unfragmented)."""
        return struct.pack(
            "!B3sH3s3s",
            msg_type,
            length.to_bytes(3, "big"),
            session.server_msg_seq,
            (0).to_bytes(3, "big"),
            length.to_bytes(3, "big"),
        )

    def _send_record(
        self,
        session: _DtlsSession,
        content_type: int,
        fragment: bytes,
    ) -> None:
        """Send a single DTLS record to one peer."""
        if self._sock is None:
            return
        header = struct.pack(
            "!BHH6sH",
            content_type,
            _DTLS_VERSION_INT,
            session.epoch_out,
            session.send_seq.to_bytes(6, "big"),
            len(fragment),
        )
        self._sock.sendto(header + fragment, session.peer)
        session.send_seq += 1

    def _send_encrypted(
        self,
        session: _DtlsSession,
        content_type: int,
        plaintext: bytes,
    ) -> None:
        """Encrypt and send an AES-128-GCM record using the server write key."""
        assert session.aesgcm_out is not None
        explicit_nonce = struct.pack("!H", session.epoch_out) + session.send_seq.to_bytes(6, "big")
        nonce = session.server_write_iv + explicit_nonce
        aad = explicit_nonce + struct.pack("!BHH", content_type, _DTLS_VERSION_INT, len(plaintext))
        ciphertext = session.aesgcm_out.encrypt(nonce, plaintext, aad)
        self._send_record(session, content_type, explicit_nonce + ciphertext)

    # -- message builders --

    def _build_hello_verify_request(self, session: _DtlsSession) -> bytes:
        """Build the HelloVerifyRequest body (server version + cookie)."""
        return _DTLS_VERSION + bytes([len(session.cookie)]) + session.cookie

    def _build_server_hello(self, session: _DtlsSession) -> bytes:
        """Build the ServerHello body (version, random, no session, chosen cipher)."""
        return (
            _DTLS_VERSION
            + session.server_random
            + b"\x00"  # session_id length
            + _CIPHER_SUITE
            + b"\x00"  # null compression
        )

    def _prune_sessions(self, now: float) -> None:
        """Drop inactive peer state before accepting another client."""
        stale = [
            peer
            for peer, session in self._sessions.items()
            if now - session.last_seen >= _SESSION_IDLE_TIMEOUT_S
        ]
        for peer in stale:
            del self._sessions[peer]

    def _remove_replaced_sessions(self, established: _DtlsSession) -> None:
        """Keep only the newest UDP session for one authenticated TV identity."""
        replaced = [
            peer
            for peer, session in self._sessions.items()
            if peer != established.peer and session.identity == established.identity
        ]
        for peer in replaced:
            del self._sessions[peer]


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


def _contains_client_hello(records: list[tuple[int, int, bytes]]) -> bool:
    """Return whether records contain an unencrypted ClientHello."""
    for content_type, epoch, fragment in records:
        if content_type != _CT_HANDSHAKE or epoch != 0:
            continue
        if any(
            hs_type == _HT_CLIENT_HELLO
            for hs_type, _body, _full in _parse_handshake_messages(fragment)
        ):
            return True
    return False


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
