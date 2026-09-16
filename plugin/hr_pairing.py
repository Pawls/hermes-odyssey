"""What the QR code says, and which addresses the phone should try.

Everything here is pure apart from the socket probing in :func:`candidate_hosts`, which matters
more than usual: this payload is the contract with an Android client written in a different
language, in a different repository, by someone reading the plan rather than this file. Renaming a
field silently breaks a shipped app, so the payload carries a version and :func:`parse` refuses
anything else.

**The QR carries the device token itself, and that is a deliberate difference from PawlRemote.**
There the code was a short-lived offer redeemed over the network for a token, because it was drawn
in an editor panel that anyone walking past could photograph. Here pairing happens at a terminal
that a person is already sitting at, and a redemption round trip would mean an unauthenticated
route on a listener whose whole point is that it has none. The cost is that the code on screen *is*
the credential for as long as it is on screen, and a screenshot of it stays valid until the device
is revoked. ``hermes odyssey revoke`` is the remedy, and ``hermes odyssey pair`` says so.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qs, urlencode

#: Bumped only for a change a v1 client cannot read. New OPTIONAL fields do not bump it.
PAIRING_VERSION = 1

#: The URI scheme. Registered as an Android deep link later, so a scan can open the app directly.
SCHEME = "hermes-odyssey"
_PREFIX = f"{SCHEME}://pair?"

#: 32 bytes of SHA-256 is 43 base64url characters with the padding stripped. A shorter one means a
#: truncated payload, and pinning against a truncated digest is pinning against nothing.
FINGERPRINT_CHARS = 43


@dataclass(frozen=True)
class PairingUri:
    """The decoded payload. ``hosts`` is in the order the client should try them."""

    hosts: List[str]
    port: int
    fingerprint: str
    token: str
    name: str
    version: int = PAIRING_VERSION
    #: ``<hostname>.local``, for the phone to resolve over mDNS when every host is silent. Optional
    #: (``m``): a v1 client that predates it ignores it, and an empty one means "not offered".
    mdns_name: str = ""


def build(
    *, hosts: List[str], port: int, fingerprint: str, token: str, name: str, mdns_name: str = ""
) -> str:
    """The string the QR encodes.

    A URI rather than JSON or a colon-separated list: it is self-describing, a generic scanner app
    shows something meaningful, and ``Uri.parse`` on Android reads it with no custom code.

    The host list is the first answer to DHCP, and ``mdns_name`` the second. The machine may have
    several interfaces and its address will change, so the client gets every candidate and tries
    them in order; when none answers it asks the LAN for the name (:mod:`hr_mdns`) and tries what
    comes back. Neither is a security boundary — the certificate pin is — so nothing is lost by
    being generous with either.
    """
    fields = {
        "v": str(PAIRING_VERSION),
        "h": ",".join(hosts),
        "p": str(port),
        "f": fingerprint,
        "t": token,
        "n": name,
    }
    if mdns_name:
        fields["m"] = mdns_name
    return _PREFIX + urlencode(fields)


def parse(text: str) -> Optional[PairingUri]:
    """Read one back. ``None`` for anything this version cannot honour."""
    if not text.startswith(_PREFIX):
        return None
    fields = parse_qs(text[len(_PREFIX) :], keep_blank_values=True)

    def one(key: str) -> str:
        values = fields.get(key) or [""]
        return values[0]

    try:
        version = int(one("v"))
        port = int(one("p"))
    except ValueError:
        return None
    hosts = [h for h in one("h").split(",") if h]
    fingerprint, token = one("f"), one("t")
    if (
        version != PAIRING_VERSION
        or not 0 < port < 65536
        or not hosts
        or len(fingerprint) != FINGERPRINT_CHARS
        or not token
    ):
        return None
    return PairingUri(
        hosts=hosts,
        port=port,
        fingerprint=fingerprint,
        token=token,
        name=one("n"),
        mdns_name=one("m"),
    )


# ---- address discovery -----------------------------------------------------


def _route_address() -> Optional[str]:
    """The address the OS would use to reach the internet, which is the LAN address in practice.

    A UDP socket is *connected* to a public address and its local end read back. Nothing is sent
    and no packet leaves the machine — connecting a datagram socket only fixes the local route — so
    this works with the cable unplugged as long as a default route exists.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 53))  # TEST-NET-1: reserved, never routed anywhere
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _hostname_addresses() -> List[str]:
    """Every IPv4 address the machine's own name resolves to."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def candidate_hosts() -> List[str]:
    """LAN addresses for the phone to try, best first, loopback last.

    Loopback is included and last on purpose: it is useless to a phone and invaluable to whoever is
    debugging the listener from the same machine, and a client that walks the list in order never
    reaches it from elsewhere.
    """
    ordered: List[str] = []

    def add(address: Optional[str]) -> None:
        if not address or address in ordered:
            return
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            return
        if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast:
            return
        ordered.append(address)

    add(_route_address())
    for address in _hostname_addresses():
        add(address)
    ordered.append("127.0.0.1")
    return ordered


def machine_name() -> str:
    """What the phone lists this desktop as. Display text; nothing depends on it."""
    try:
        return socket.gethostname() or "hermes"
    except OSError:
        return "hermes"
