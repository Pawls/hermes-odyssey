"""``hermes remote pair | status | revoke``, driven through the argparse tree Hermes builds.

The parser is assembled here exactly as ``_attach_plugin_cli_command`` assembles it — a subparser
per command, ``handler_fn`` as ``func`` — so a change to :func:`hr_cli.setup` that would not parse
under Hermes fails here rather than at someone's terminal.

The assertion that matters most is not about output: it is that the token drawn into the QR is the
one the device store will accept, and that revoking it stops that being true. Everything the phone
does afterwards depends on those two facts.
"""

from __future__ import annotations

import argparse
import importlib
from typing import NamedTuple

import pytest


@pytest.fixture()
def state(_plugin_on_path, tmp_path, monkeypatch):
    """Redirect the whole state directory, so the certificate lands in tmp too."""
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(hr_paths, "devices_path", lambda: tmp_path / "devices.json")
    return tmp_path


@pytest.fixture()
def hr_cli(state):
    return importlib.import_module("hr_cli")


class Result(NamedTuple):
    """One command's exit code and everything it wrote."""

    code: int
    out: str
    err: str


@pytest.fixture()
def run(hr_cli, capsys):
    """Parse an argv tail the way Hermes does, and run it."""

    def _run(*argv) -> Result:
        parser = argparse.ArgumentParser(prog="hermes")
        sub = parser.add_subparsers()
        remote = sub.add_parser(hr_cli.COMMAND_NAME)
        hr_cli.setup(remote)
        remote.set_defaults(func=hr_cli.handler)
        args = parser.parse_args([hr_cli.COMMAND_NAME, *argv])
        code = args.func(args)
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return _run


# ---- pair ------------------------------------------------------------------


def test_pairing_prints_a_code_that_carries_a_working_token(run, hr_devices):
    hr_pairing = importlib.import_module("hr_pairing")

    drawn = run("pair", "--label", "Pixel 9")
    assert drawn.code == 0
    assert "█" in drawn.out  # the symbol itself

    piped = run("pair", "--label", "Pixel 9", "--uri-only")
    assert piped.code == 0
    parsed = hr_pairing.parse(piped.out.strip())
    assert parsed is not None

    device = hr_devices.verify_token(parsed.token)
    assert device is not None and device.label == "Pixel 9"


def test_the_code_pins_the_certificate_that_is_on_disk(run):
    hr_identity = importlib.import_module("hr_identity")
    hr_pairing = importlib.import_module("hr_pairing")

    parsed = hr_pairing.parse(run("pair", "--uri-only").out.strip())
    assert parsed.fingerprint == hr_identity.existing_identity().fingerprint_b64


def test_pairing_creates_the_certificate_on_first_use(run):
    hr_identity = importlib.import_module("hr_identity")
    assert hr_identity.existing_identity() is None
    run("pair", "--uri-only")
    assert hr_identity.existing_identity() is not None


def test_the_fingerprint_is_printed_for_the_eye_check(run):
    """Rule three of the three: a machine in the middle survives everything but this comparison."""
    hr_identity = importlib.import_module("hr_identity")
    out = run("pair").out
    assert hr_identity.existing_identity().fingerprint_hex in "".join(out.split())
    assert "revoke" in out  # and how to undo it


def test_the_token_is_never_printed_in_the_clear(run):
    """It is in the QR, because that is what pairing is. It is not in the human-readable block,
    where it would land in a scrollback buffer and in a screen-sharing recording."""
    hr_pairing = importlib.import_module("hr_pairing")
    drawn = run("pair").out
    piped = run("pair", "--uri-only").out
    assert hr_pairing.parse(piped.strip()).token not in drawn


def test_each_pairing_mints_a_distinct_device(run, hr_devices):
    run("pair", "--label", "one")
    run("pair", "--label", "two")
    devices = hr_devices.list_devices()
    assert {d.label for d in devices} == {"one", "two"}
    assert len({d.id for d in devices}) == 2


# ---- status ----------------------------------------------------------------


def test_status_before_anything_exists_says_so(run):
    result = run("status")
    assert result.code == 0
    assert "none yet" in result.out
    assert "No paired devices" in result.out


def test_status_lists_a_paired_device(run, hr_devices):
    run("pair", "--label", "Pixel 9")
    result = run("status")
    assert result.code == 0
    assert "Pixel 9" in result.out
    assert hr_devices.list_devices()[0].id in result.out


def test_status_hides_revoked_devices_unless_asked(run, hr_devices):
    run("pair", "--label", "Pixel 9")
    device_id = hr_devices.list_devices()[0].id
    run("revoke", device_id)

    assert device_id not in run("status").out
    verbose = run("status", "--all").out
    assert device_id in verbose
    assert "revoked" in verbose


def test_status_reports_that_no_listener_is_running(run):
    assert "not recorded as running" in run("status").out


# ---- revoke ----------------------------------------------------------------


def test_revoking_stops_the_token_working(run, hr_devices):
    hr_pairing = importlib.import_module("hr_pairing")
    token = hr_pairing.parse(run("pair", "--uri-only").out.strip()).token
    assert hr_devices.verify_token(token) is not None

    result = run("revoke", hr_devices.list_devices()[0].id)
    assert result.code == 0
    assert "Revoked" in result.out
    assert hr_devices.verify_token(token) is None


def test_a_unique_prefix_is_enough(run, hr_devices):
    run("pair", "--label", "Pixel 9")
    device_id = hr_devices.list_devices()[0].id
    assert run("revoke", device_id[:6]).code == 0
    assert hr_devices.list_devices()[0].revoked


def test_an_unknown_device_is_an_error_not_a_silent_success(run):
    assert run("revoke", "ffffffffffff").code == 1


def test_revoke_with_no_argument_says_what_to_do(run):
    result = run("revoke")
    assert result.code == 1
    assert "hermes remote status" in result.err


def test_revoke_all_ends_every_live_device(run, hr_devices):
    run("pair", "--label", "one")
    run("pair", "--label", "two")
    result = run("revoke", "--all")
    assert result.code == 0
    assert "2 device" in result.out
    assert all(d.revoked for d in hr_devices.list_devices())


def test_revoke_all_on_an_empty_store_is_not_an_error(run):
    result = run("revoke", "--all")
    assert result.code == 0
    assert "Nothing to revoke" in result.out


# ---- the bare command ------------------------------------------------------


def test_bare_hermes_remote_prints_the_usage_and_fails(run):
    result = run()
    assert result.code == 2
    for command in ("pair", "status", "revoke"):
        assert command in result.out
