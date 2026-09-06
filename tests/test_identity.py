"""The listener's certificate.

Two properties carry the whole pairing story and neither is visible by inspection: the certificate
must be **stable** across restarts, or every paired phone unpairs itself on a reboot, and its SAN
list must **not** name a LAN address, or the fingerprint rotates with the DHCP lease and does the
same thing more slowly.
"""

from __future__ import annotations

import base64
import hashlib
import importlib

import pytest


@pytest.fixture()
def hr_identity(_plugin_on_path, tmp_path, monkeypatch):
    """The identity module writing into this test's own directory."""
    module = importlib.import_module("hr_identity")
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path)
    return module


def test_generation_writes_both_files_and_they_parse(hr_identity, tmp_path):
    identity = hr_identity.ensure_identity()
    assert identity.cert_path == tmp_path / hr_identity.CERT_FILENAME
    assert identity.key_path == tmp_path / hr_identity.KEY_FILENAME
    assert identity.cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert identity.key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_a_second_call_returns_the_same_certificate(hr_identity):
    """The phone pins these bytes. Regenerating on restart would unpair every device."""
    first = hr_identity.ensure_identity()
    second = hr_identity.ensure_identity()
    assert first.der == second.der
    assert first.fingerprint_hex == second.fingerprint_hex


def test_forcing_a_rotation_changes_the_fingerprint(hr_identity):
    before = hr_identity.ensure_identity().fingerprint_hex
    after = hr_identity.ensure_identity(regenerate=True).fingerprint_hex
    assert before != after


def test_an_unparseable_certificate_is_replaced_rather_than_fatal(hr_identity):
    """A half-written pair must not leave the listener permanently down. Regeneration is safe to
    reach for precisely because the phone pins: it fails loudly, it does not downgrade."""
    original = hr_identity.ensure_identity()
    original.cert_path.write_text("not a certificate", encoding="utf-8")
    assert hr_identity.ensure_identity().der != original.der


def test_no_certificate_yet_reports_none_without_creating_one(hr_identity, tmp_path):
    assert hr_identity.existing_identity() is None
    assert hr_identity.fingerprints() is None
    assert not list(tmp_path.iterdir())


def test_the_fingerprints_are_the_digest_of_the_der(hr_identity):
    identity = hr_identity.ensure_identity()
    digest = hashlib.sha256(identity.der).digest()
    assert identity.fingerprint_hex == digest.hex()
    assert identity.fingerprint_b64 == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert len(identity.fingerprint_b64) == 43  # what the QR carries and the phone compares


def test_the_key_is_p256(hr_identity):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    certificate = x509.load_der_x509_certificate(hr_identity.ensure_identity().der)
    public_key = certificate.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert public_key.curve.name == "secp256r1"


def test_the_san_names_only_loopback(hr_identity):
    """A LAN address here would rotate the fingerprint with the DHCP lease. The phone does no
    hostname verification, so nothing is lost by leaving its address out."""
    import ipaddress

    from cryptography import x509

    certificate = x509.load_der_x509_certificate(hr_identity.ensure_identity().der)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"localhost"}
    addresses = set(san.get_values_for_type(x509.IPAddress))
    assert addresses == {ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv6Address("::1")}


def test_it_is_self_signed_and_long_lived(hr_identity):
    from cryptography import x509

    identity = hr_identity.ensure_identity()
    certificate = x509.load_der_x509_certificate(identity.der)
    assert certificate.issuer == certificate.subject
    assert (certificate.not_valid_after_utc - certificate.not_valid_before_utc).days >= 3650
    assert not identity.expired
    basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca is False  # it signs nothing but itself
