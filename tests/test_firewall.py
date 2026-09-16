"""``hermes talaria firewall``: a command per platform that admits the listener's port and nothing
wider, printed for a person to run with elevation.

The Windows case carries the finding that makes this module worth having: the runtime interpreter
is reached through a junction, and an allow rule written against the junction path matches no
socket, because Windows keys the rule on the resolved image path.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture()
def hr_firewall(_plugin_on_path):
    return importlib.import_module("hr_firewall")


def _no_tools(_name):
    return None


def test_windows_scopes_the_rule_to_the_port_the_program_and_the_private_profile(hr_firewall):
    steps = hr_firewall.instructions(9443, system="Windows", program=r"C:\rt\python.exe")
    assert "-LocalPort 9443" in steps.command
    assert r"-Program 'C:\rt\python.exe'" in steps.command
    assert "-Profile Private" in steps.command
    assert "hermes update" in steps.note  # the thing that silently orphans it


def test_macos_allows_the_interpreter_because_its_firewall_has_no_ports(hr_firewall):
    steps = hr_firewall.instructions(9443, system="Darwin", program="/opt/py/bin/python3.11")
    assert "--unblockapp '/opt/py/bin/python3.11'" in steps.command
    assert "9443" not in steps.command


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ("ufw", "ufw allow 9500/tcp"),
        ("firewall-cmd", "--add-port=9500/tcp"),
        (None, "tcp dport 9500 accept"),
    ],
)
def test_linux_uses_whichever_firewall_tool_is_installed(hr_firewall, present, expected):
    which = (lambda name: f"/usr/sbin/{name}" if name == present else None)
    steps = hr_firewall.instructions(9500, system="Linux", program="/x", which=which)
    assert expected in steps.command


def test_an_unknown_platform_gets_no_invented_command(hr_firewall):
    assert hr_firewall.instructions(9443, system="Plan9", program="/x", which=_no_tools) is None


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_the_interpreter_path_is_resolved_through_junctions(hr_firewall, tmp_path, monkeypatch):
    import subprocess

    real = tmp_path / "cpython-3.11.15"
    real.mkdir()
    (real / "python.exe").write_bytes(b"")
    link = tmp_path / "cpython-3.11"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)

    monkeypatch.setattr(sys, "_base_executable", str(link / "python.exe"), raising=False)
    assert os.path.normcase(hr_firewall.runtime_interpreter()) == os.path.normcase(
        os.path.realpath(real / "python.exe")
    )
