"""How the phone finds a desktop whose address has moved: ``<hostname>.local`` over mDNS.

No service is advertised and no library is pulled in, because neither is needed. Windows (the
``Dnscache`` service), macOS (``mDNSResponder``) and Linux (Avahi) all answer an mDNS query for the
machine's own ``<hostname>.local`` A record out of the box, and that record is the only thing the
phone is missing when a DHCP lease moves: it already holds the port, the certificate pin and the
token. DNS-SD browsing would find *unknown* desktops, which is not the problem; the phone is
looking for one it has already paired with.

The query is a *legacy unicast* one (RFC 6762 §6.7): sent to the multicast group from an ephemeral
port, so the responder unicasts the answer straight back to that port. That is what lets a phone
receive the reply without a multicast lock, and what lets this module be a plain UDP socket with
no membership in the group at all.

The address a responder returns is the address of the interface the query arrived on, which is
the right one by construction: a phone's query arrives on the LAN adapter. This machine's own
self-check has to send from that adapter explicitly (:func:`resolve`'s ``interface``), or the
loopback copy of the query is answered by whichever interface the OS picked, measured here as
Tailscale's link-local address rather than the LAN.

None of this is trusted. A LAN peer can answer the query with any address it likes, and the phone
will dial it; the certificate pin then refuses it, exactly as it refuses a wrong entry in the
stored host list. Discovery decides what to try, never what to believe.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import List, Optional

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

#: The optional pairing field the name travels in. See :mod:`hr_pairing`.
MDNS_SUFFIX = ".local"

_TYPE_A = 1
_CLASS_IN = 1
#: RFC 6762 §5.4: "QU" asks for a unicast response. Responders also unicast to a legacy source port
#: regardless, so this is belt and braces.
_UNICAST_RESPONSE = 0x8000
#: The response's class field carries the cache-flush bit in the same position; masked off below.
_CACHE_FLUSH = 0x8000

#: One LAN round trip is milliseconds. The wait only runs its course when nothing answers.
DEFAULT_TIMEOUT = 1.0
#: After the first answer, how long to keep listening for a second interface's, which arrives in
#: the same instant when it arrives at all.
_LINGER = 0.15


def local_name(hostname: Optional[str] = None) -> str:
    """The name the OS responder answers for: the host name, lower-cased, under ``.local``.

    mDNS names are case-insensitive but the responder replies with the lower-case form, and the
    phone compares owner names case-insensitively either way.
    """
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
    hostname = hostname.strip().lower().rstrip(".")
    if not hostname:
        return ""
    # A hostname the OS already qualified (``pc.lan``) is not an mDNS name; only the first label
    # is. ``.local`` itself is stripped and re-added so a bare label and a full name both work.
    if hostname.endswith(MDNS_SUFFIX):
        hostname = hostname[: -len(MDNS_SUFFIX)]
    label = hostname.split(".")[0]
    return f"{label}{MDNS_SUFFIX}" if label else ""


# ---- the packet ------------------------------------------------------------


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.strip(".").split(".")) + b"\x00"


def build_query(name: str) -> bytes:
    """A one-question DNS packet: ``A IN <name>``, id 0, QU bit set. The wire form the phone sends too."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(name) + struct.pack(">HH", _TYPE_A, _UNICAST_RESPONSE | _CLASS_IN)


class _Malformed(Exception):
    pass


def _read_name(packet: bytes, offset: int, depth: int = 0) -> tuple:
    """Decode a possibly compressed name. Returns ``(labels, offset after it)``."""
    if depth > 16:
        raise _Malformed("compression loop")
    labels: List[str] = []
    while True:
        if offset >= len(packet):
            raise _Malformed("truncated name")
        length = packet[offset]
        if length == 0:
            return labels, offset + 1
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise _Malformed("truncated pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            rest, _ = _read_name(packet, pointer, depth + 1)
            return labels + rest, offset + 2
        offset += 1
        labels.append(packet[offset : offset + length].decode("ascii", "replace"))
        offset += length


def parse_answers(packet: bytes, name: str) -> List[str]:
    """The IPv4 addresses a response carries for ``name``, in order, without duplicates.

    Anything that is not a response, or is malformed, yields nothing rather than an exception: the
    packet came off the network from whoever cared to send it.
    """
    want = [label.lower() for label in name.strip(".").split(".")]
    found: List[str] = []
    try:
        if len(packet) < 12:
            return found
        flags, questions, answers, authority, additional = struct.unpack(">HHHHH", packet[2:12])
        if not flags & 0x8000:
            return found
        offset = 12
        for _ in range(questions):
            _, offset = _read_name(packet, offset)
            offset += 4
        for _ in range(answers + authority + additional):
            labels, offset = _read_name(packet, offset)
            if offset + 10 > len(packet):
                break
            rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", packet[offset : offset + 10])
            offset += 10
            rdata = packet[offset : offset + rdlength]
            offset += rdlength
            if (
                rtype == _TYPE_A
                and (rclass & ~_CACHE_FLUSH) == _CLASS_IN
                and rdlength == 4
                and [label.lower() for label in labels] == want
            ):
                address = socket.inet_ntoa(rdata)
                if address not in found:
                    found.append(address)
    except (_Malformed, struct.error):
        pass
    return found


# ---- the query -------------------------------------------------------------


def resolve(name: str, *, timeout: float = DEFAULT_TIMEOUT, interface: Optional[str] = None) -> List[str]:
    """Ask the LAN for ``name`` and return the IPv4 addresses that came back, best effort.

    ``interface`` is the local IPv4 address to send from. Leave it unset on a client; set it on the
    desktop's self-check so the query leaves by the LAN adapter (see the module note).
    """
    if not name:
        return []
    query = build_query(name)
    found: List[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        sock.bind((interface or "0.0.0.0", 0))
        if interface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                packet, _peer = sock.recvfrom(4096)
            except socket.timeout:
                break
            for address in parse_answers(packet, name):
                if address not in found:
                    found.append(address)
            if found:
                deadline = min(deadline, time.monotonic() + _LINGER)
    except OSError:
        pass
    finally:
        sock.close()
    return found
