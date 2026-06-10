"""Hue-style self-signed TLS certificate for the virtual bridge's HTTPS listener."""

from __future__ import annotations

import logging
import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# A real Hue bridge cert is EC P-256, subject CN=<bridgeid>, OU=<modelid>, O="Philips Hue".
# Clients (the Hue app, Ambilight+Hue TVs) accept a self-signed cert with that CN - diyHue
# self-signs the same way and is accepted, so no real "root-bridge" CA chain is needed.
# The validity is backdated so the cert is never "not yet valid" on a bridge with a bad clock.
_NOT_BEFORE = datetime(2017, 1, 1, tzinfo=UTC)
_NOT_AFTER = datetime(2038, 1, 1, tzinfo=UTC)


def load_or_create_ssl_context(
    cert_path: Path,
    key_path: Path,
    *,
    bridge_id: str,
    model_id: str,
) -> ssl.SSLContext:
    """
    Return a TLS server context, generating a Hue-style cert on first use.

    :param cert_path: Where the PEM certificate is read from / written to.
    :param key_path: Where the PEM private key is read from / written to.
    :param bridge_id: The Hue bridgeid used as the certificate Common Name.
    :param model_id: The bridge model id used as the certificate Organizational Unit.
    """
    if not cert_path.exists() or not key_path.exists():
        _generate_cert(cert_path, key_path, bridge_id=bridge_id, model_id=model_id)
        LOGGER.info("Generated Hue-style bridge certificate (CN=%s) at %s", bridge_id, cert_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def _generate_cert(cert_path: Path, key_path: Path, *, bridge_id: str, model_id: str) -> None:
    """Generate and persist a self-signed EC P-256 certificate mimicking a real Hue bridge."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Philips Hue"),
            x509.NameAttribute(NameOID.COMMON_NAME, bridge_id),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, model_id),
        ],
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int(bridge_id, 16))
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
