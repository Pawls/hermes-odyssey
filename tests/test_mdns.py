"""The mDNS query and answer, as bytes.

The phone builds the same query and reads the same answers (``Mdns.kt``), so the packets here are
the contract: a query that a real responder will not answer, or an answer this parser drops, is a
phone that cannot follow a moved address. ``WINDOWS_ANSWER`` is what this machine's ``Dnscache``
actually sent back to :func:`hr_mdns.build_query` on 2026-09-15, captured rather than composed.

Nothing here touches the network: ``conftest`` stubs :func:`hr_mdns.resolve` for the whole suite.
"""

from __future__ import annotations

import importlib
import socket
import struct

import pytest


@pytest.fixture()
def hr_mdns(_plugin_on_path):
    return importlib.import_module("hr_mdns")


def _name(labels):
    return b"".join(bytes([len(l)]) + l.encode() for l in labels) + b"\x00"


#: ``pawl-desktop.local``: one A record, cache-flush set, TTL 10, plus AAAA records in the additional
#: section, and the answer's owner name a compression pointer back to the question (0xC00C).
WINDOWS_ANSWER = (
    struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 1)
    + _name(["pawl-desktop", "local"])
    + struct.pack(">HH", 1, 0x8001)
    + b"\xc0\x0c"
    + struct.pack(">HHIH", 1, 0x8001, 10, 4)
    + socket.inet_aton("192.168.1.50")
    + b"\xc0\x0c"
    + struct.pack(">HHIH", 28, 0x8001, 10, 16)
    + bytes(16)
)


def test_the_query_is_one_legacy_unicast_a_question(hr_mdns):
    packet = hr_mdns.build_query("pawl-desktop.local")
    ident, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", packet[:12])
    assert (ident, flags, qd, an, ns, ar) == (0, 0, 1, 0, 0, 0)
    assert packet[12:] == _name(["pawl-desktop", "local"]) + struct.pack(">HH", 1, 0x8001)


def test_the_windows_answer_yields_the_lan_address(hr_mdns):
    assert hr_mdns.parse_answers(WINDOWS_ANSWER, "pawl-desktop.local") == ["192.168.1.50"]


def test_owner_names_compare_case_insensitively(hr_mdns):
    assert hr_mdns.parse_answers(WINDOWS_ANSWER, "PAWL-Desktop.local") == ["192.168.1.50"]


def test_an_answer_for_another_name_is_not_ours(hr_mdns):
    """Every responder on the LAN hears the query; only the one named gets to answer it."""
    assert hr_mdns.parse_answers(WINDOWS_ANSWER, "other-pc.local") == []


def test_a_query_is_not_an_answer(hr_mdns):
    assert hr_mdns.parse_answers(hr_mdns.build_query("pawl-desktop.local"), "pawl-desktop.local") == []


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        WINDOWS_ANSWER[:20],
        WINDOWS_ANSWER[:40],
        # A pointer that points at itself.
        struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c",
    ],
)
def test_garbage_off_the_wire_yields_nothing_rather_than_raising(hr_mdns, packet):
    assert hr_mdns.parse_answers(packet, "pawl-desktop.local") == []


def test_duplicate_records_collapse(hr_mdns):
    header = struct.pack(">HHHHHH", 0, 0x8400, 1, 2, 0, 0)
    body = (
        _name(["pawl-desktop", "local"])
        + struct.pack(">HH", 1, 1)
        + (b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("192.168.1.50")) * 2
    )
    assert hr_mdns.parse_answers(header + body, "pawl-desktop.local") == ["192.168.1.50"]


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("PAWL-DESKTOP", "pawl-desktop.local"),
        ("pawl-desktop.local", "pawl-desktop.local"),
        ("pc.corp.example", "pc.local"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_the_local_name_is_the_first_label_lower_cased(hr_mdns, hostname, expected):
    assert hr_mdns.local_name(hostname) == expected


def test_resolve_with_no_name_sends_nothing(hr_mdns, monkeypatch):
    """The stub in conftest is bypassed here to show the empty-name case never opens a socket."""
    monkeypatch.setattr(hr_mdns, "resolve", hr_mdns.__dict__["_real_resolve"])
    monkeypatch.setattr(hr_mdns.socket, "socket", lambda *a, **k: pytest.fail("socket opened"))
    assert hr_mdns.resolve("") == []
