"""Tests for the Hue-style self-signed bridge certificate."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.x509.oid import NameOID

from ambilight_hue_bridge.discovery.cert import load_or_create_ssl_context

if TYPE_CHECKING:
    from pathlib import Path

_BRIDGE_ID = "AABBCCFFFEDDEEFF"


def test_generates_hue_style_cert(tmp_path: Path) -> None:
    """The generated cert is self-signed with CN=bridgeid, OU=modelid, O=Philips Hue."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    context = load_or_create_ssl_context(
        cert_path, key_path, bridge_id=_BRIDGE_ID, model_id="BSB002"
    )
    assert isinstance(context, ssl.SSLContext)

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == _BRIDGE_ID
    assert cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value == "Philips Hue"
    assert (
        cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value == "BSB002"
    )
    # Self-signed: issuer equals subject (no external "root-bridge" chain required).
    assert cert.issuer == cert.subject
    assert cert.serial_number == int(_BRIDGE_ID, 16)


def test_cert_is_reused_across_calls(tmp_path: Path) -> None:
    """A second call reuses the persisted cert rather than regenerating it."""
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    load_or_create_ssl_context(cert_path, key_path, bridge_id=_BRIDGE_ID, model_id="BSB002")
    first = cert_path.read_bytes()
    load_or_create_ssl_context(cert_path, key_path, bridge_id=_BRIDGE_ID, model_id="BSB002")
    assert cert_path.read_bytes() == first
