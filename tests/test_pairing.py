"""The pairing payload, which is the contract with an app in another language and another repo.

Every assertion here is a promise to code that cannot be changed in the same commit. A field
rename that passes these tests but breaks the Android client is exactly what the round trip below
is meant to catch before it ships.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def hr_pairing(_plugin_on_path):
    return importlib.import_module("hr_pairing")


FIELDS = {
    "hosts": ["192.168.1.50", "10.0.0.5", "127.0.0.1"],
    "port": 9443,
    "fingerprint": "A" * 43,
    "token": "hr1.0123456789ab.QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2",
    "name": "PAULS-PC",
}


def test_a_payload_survives_the_round_trip(hr_pairing):
    parsed = hr_pairing.parse(hr_pairing.build(**FIELDS))
    assert parsed is not None
    assert parsed.hosts == FIELDS["hosts"]
    assert parsed.port == FIELDS["port"]
    assert parsed.fingerprint == FIELDS["fingerprint"]
    assert parsed.token == FIELDS["token"]
    assert parsed.name == FIELDS["name"]
    assert parsed.version == hr_pairing.PAIRING_VERSION


def test_the_wire_format_is_the_one_the_app_parses(hr_pairing):
    """The scheme and the six single-letter keys are the contract. Changing one silently breaks a
    shipped client, so they are spelled out here rather than derived."""
    uri = hr_pairing.build(**FIELDS)
    assert uri.startswith("hermes-talaria://pair?")
    query = uri.split("?", 1)[1]
    keys = [pair.split("=", 1)[0] for pair in query.split("&")]
    assert keys == ["v", "h", "p", "f", "t", "n"]


def test_the_mdns_name_is_optional_and_does_not_bump_the_version(hr_pairing):
    """A v1 phone that predates ``m`` must still read the code, so it is a seventh key and not a
    v2. Absent means "not offered", never a refusal."""
    without = hr_pairing.build(**FIELDS)
    assert "m=" not in without and hr_pairing.parse(without).mdns_name == ""

    with_name = hr_pairing.build(**FIELDS, mdns_name="pawl-desktop.local")
    assert with_name.startswith(without + "&m=")
    assert hr_pairing.parse(with_name).mdns_name == "pawl-desktop.local"
    assert hr_pairing.parse(with_name).version == 1


def test_a_machine_name_with_awkward_characters_is_escaped(hr_pairing):
    uri = hr_pairing.build(**{**FIELDS, "name": "Paul's PC & laptop"})
    assert " " not in uri and "&laptop" not in uri
    assert hr_pairing.parse(uri).name == "Paul's PC & laptop"


@pytest.mark.parametrize(
    "override",
    [
        {"fingerprint": "A" * 42},  # truncated: pinning against this pins against nothing
        {"fingerprint": ""},
        {"hosts": []},
        {"port": 0},
        {"port": 70000},
        {"token": ""},
    ],
)
def test_an_unusable_payload_is_refused(hr_pairing, override):
    assert hr_pairing.parse(hr_pairing.build(**{**FIELDS, **override})) is None


def test_another_version_is_refused(hr_pairing):
    uri = hr_pairing.build(**FIELDS).replace("v=1", "v=2", 1)
    assert hr_pairing.parse(uri) is None


def test_something_that_is_not_a_pairing_uri_is_refused(hr_pairing):
    assert hr_pairing.parse("https://example.com/pair?v=1") is None
    assert hr_pairing.parse("") is None


# ---- address discovery ------------------------------------------------------


def test_the_candidate_list_ends_at_loopback(hr_pairing):
    """Loopback is useless to a phone and invaluable to whoever is debugging from this machine.
    Last means a client walking the list in order never reaches it from elsewhere."""
    hosts = hr_pairing.candidate_hosts()
    assert hosts[-1] == "127.0.0.1"
    assert len(set(hosts)) == len(hosts)


def test_link_local_and_loopback_are_not_offered_as_lan_addresses(hr_pairing, monkeypatch):
    monkeypatch.setattr(hr_pairing, "_route_address", lambda: "169.254.10.1")
    monkeypatch.setattr(hr_pairing, "_hostname_addresses", lambda: ["127.0.0.1", "192.168.1.50"])
    assert hr_pairing.candidate_hosts() == ["192.168.1.50", "127.0.0.1"]


def test_a_machine_with_no_route_still_pairs_over_loopback(hr_pairing, monkeypatch):
    monkeypatch.setattr(hr_pairing, "_route_address", lambda: None)
    monkeypatch.setattr(hr_pairing, "_hostname_addresses", lambda: [])
    assert hr_pairing.candidate_hosts() == ["127.0.0.1"]
