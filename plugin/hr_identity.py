"""The listener's TLS identity: one self-signed P-256 certificate, generated once and kept.

Ported from PawlRemote's ``vscode-pawl/src/remote/x509.ts``, but much shorter, because the
``cryptography`` package is already in the Hermes venv and it *does* have a certificate builder —
the TypeScript original had to assemble the DER by hand only because Node has no such API. The
decisions carried over verbatim are the ones that matter:

**The certificate is pinned, not validated.** The phone trusts it because its SHA-256 fingerprint
came off a QR code a person read, and for no other reason. There is no CA, and the client does no
hostname verification. That is what lets the certificate stay *stable* while the machine's LAN
address moves: a SAN list tracking the current DHCP lease would change the fingerprint on every
renewal and silently unpair every device. The SAN below names loopback only, which is true forever
and keeps a desktop browser usable for debugging.

**P-256 rather than RSA.** Generation is instant, the fingerprint is the same 32 bytes either way,
and every TLS stack a phone might use has supported it for a decade.

**Ten years.** A pairing is revoked by revoking the device, not by expiry; a certificate that
expired mid-run would break every paired phone at once with an error none of them can explain.

The private key is the most sensitive thing this repository writes. It never leaves
``$HERMES_HOME/odyssey/`` and is never logged, printed or put in a QR code.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import os
import threading
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

try:  # package import (``hermes_plugins.hermes_odyssey``)
    from . import hr_paths
except ImportError:  # standalone path load from dashboard/api.py
    import hr_paths  # type: ignore[no-redef]

#: Files under ``$HERMES_HOME/odyssey/``. PEM rather than DER so ``ssl_certfile``/``ssl_keyfile``
#: can be handed to uvicorn unchanged and a person can read them with any tool.
CERT_FILENAME = "listener-cert.pem"
KEY_FILENAME = "listener-key.pem"

#: Subject and issuer CN. Cosmetic — nothing checks it — but it is what a browser's certificate
#: viewer shows when someone debugs the listener by hand.
COMMON_NAME = "Odyssey"

LIFETIME_DAYS = 3653  # ten years, leap days included
_SKEW_HOURS = 24  # backdate notBefore so a phone with a slow clock still connects

_lock = threading.Lock()


@dataclass(frozen=True)
class Identity:
    """A loaded certificate and where its files live. Never carries the key material itself."""

    cert_path: Path
    key_path: Path
    der: bytes
    not_after: _dt.datetime

    @property
    def fingerprint_hex(self) -> str:
        """Lowercase hex SHA-256 of the DER, colon-free. What ``hermes odyssey status`` prints."""
        return hashlib.sha256(self.der).hexdigest()

    @property
    def fingerprint_b64(self) -> str:
        """The same digest base64url without padding: 43 characters against hex's 64.

        This is the form that rides in the QR payload, where every character is a module.
        """
        return urlsafe_b64encode(hashlib.sha256(self.der).digest()).decode("ascii").rstrip("=")

    @property
    def expired(self) -> bool:
        return self.not_after <= _dt.datetime.now(_dt.timezone.utc)


def cert_path() -> Path:
    return hr_paths.state_dir() / CERT_FILENAME


def key_path() -> Path:
    return hr_paths.state_dir() / KEY_FILENAME


# ---- generation ------------------------------------------------------------


def _generate(cert_file: Path, key_file: Path) -> None:
    """Write a fresh key pair and self-signed certificate. Caller holds the lock."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = _dt.datetime.now(_dt.timezone.utc)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed: the two are the same
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(hours=_SKEW_HOURS))
        .not_valid_after(now + _dt.timedelta(days=LIFETIME_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,  # meaningless for ECDSA; ECDHE derives the key
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.ObjectIdentifier("1.3.6.1.5.5.7.3.1")]),  # serverAuth
            critical=False,
        )
        .add_extension(
            # Loopback only, and deliberately so — see the module docstring. A LAN address here
            # would rotate the fingerprint with the DHCP lease and unpair every phone.
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    _write_private(
        key_file,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    _write_private(cert_file, certificate.public_bytes(serialization.Encoding.PEM))


def _write_private(path: Path, data: bytes) -> None:
    """Create-or-replace with mode 0600, atomically.

    Windows ignores the POSIX mode; there the file inherits the ACL of the user's LOCALAPPDATA,
    which is the same protection Hermes' own ``auth.json`` relies on. ``os.open`` with the mode is
    still worth doing: this repository is meant to run on a POSIX host too, and a key file written
    world-readable would be a silent failure everywhere but here.
    """
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


# ---- loading ---------------------------------------------------------------


def _load(cert_file: Path, key_file: Path) -> Optional[Identity]:
    """Parse an existing pair, or ``None`` when either file is missing or unusable.

    A malformed or half-written pair returns ``None`` rather than raising, so
    :func:`ensure_identity` regenerates instead of leaving the listener permanently down. That
    trade is safe *because the phone pins*: a regenerated certificate does not silently downgrade
    anything, it makes every paired phone refuse to connect until it is re-paired, which is loud.
    """
    from cryptography import x509

    try:
        cert_pem = cert_file.read_bytes()
        key_file.stat()  # presence only; the key is never read into this process
    except OSError:
        return None
    try:
        certificate = x509.load_pem_x509_certificate(cert_pem)
    except Exception:
        return None
    from cryptography.hazmat.primitives import serialization

    return Identity(
        cert_path=cert_file,
        key_path=key_file,
        der=certificate.public_bytes(serialization.Encoding.DER),
        not_after=certificate.not_valid_after_utc,
    )


def ensure_identity(*, regenerate: bool = False) -> Identity:
    """The listener's certificate, generating it on first use.

    Regeneration happens on three triggers and no others: the files are absent, they cannot be
    parsed, or the certificate has expired. ``regenerate=True`` forces it, which is what
    ``hermes odyssey rotate`` would use — and which unpairs every device by design.
    """
    cert_file, key_file = cert_path(), key_path()
    with _lock:
        identity = None if regenerate else _load(cert_file, key_file)
        if identity is None or identity.expired:
            _generate(cert_file, key_file)
            identity = _load(cert_file, key_file)
        if identity is None:  # pragma: no cover — a write that succeeded must parse
            raise RuntimeError(f"generated certificate at {cert_file} could not be read back")
        return identity


def existing_identity() -> Optional[Identity]:
    """The certificate if one is already on disk, else ``None``. Never generates.

    ``hermes odyssey status`` uses this: reporting "no certificate yet" is the honest answer, and
    generating one as a side effect of a status command would be a surprise.
    """
    return _load(cert_path(), key_path())


def fingerprints() -> Optional[Tuple[str, str]]:
    """``(hex, base64url)`` for the certificate on disk, or ``None``."""
    identity = existing_identity()
    return (identity.fingerprint_hex, identity.fingerprint_b64) if identity else None
