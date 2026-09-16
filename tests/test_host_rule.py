"""Which process holds 9443.

Two Hermes processes arm the listener - ``hermes dashboard`` and the desktop app's headless
``hermes serve`` - and a phone can stream into a live session only from inside the process that
owns it. So the desktop must win the port, and it must win it *after* the dashboard already has
it, which is what the claim file and the yield exist for. The decisions are pure functions and
tested as such; the ticks run against real sockets, with a second interpreter standing in for
the other process so that "live pid" means what it means in production.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys

import pytest


@pytest.fixture()
def listener(_plugin_on_path, monkeypatch, tmp_path):
    module = importlib.import_module("hr_listener")
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path)
    monkeypatch.setenv("HERMES_ODYSSEY_LISTENER", "1")
    monkeypatch.setenv("HERMES_ODYSSEY_HOST", "127.0.0.1")
    module.state.running = False
    module.state.error = ""
    yield module
    asyncio.run(module.stop())


@pytest.fixture()
def other_pid():
    """A process that is genuinely alive and genuinely not this one."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


DEAD_PID = 2_000_000_000


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def held_port():
    """A port some other server already holds."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        yield holder.getsockname()[1]
    finally:
        holder.close()


def _record(listener, name: str, **fields) -> None:
    path = listener.runtime_path() if name == "runtime" else listener.claim_path()
    path.write_text(json.dumps(fields), encoding="utf-8")


def _as(listener, monkeypatch, surface: str) -> None:
    monkeypatch.setattr(listener, "this_surface", lambda: surface)


# ---- the decisions ----------------------------------------------------------


def test_the_desktop_outranks_the_dashboard_and_nothing_outranks_itself(listener):
    assert listener.outranks("desktop", "dashboard")
    assert listener.outranks("dashboard", "gateway")
    assert not listener.outranks("dashboard", "desktop")
    assert not listener.outranks("desktop", "desktop")
    # An unknown surface never wins and never has to yield to another unknown.
    assert not listener.outranks("", "gateway")
    assert listener.outranks("gateway", "")


def test_this_surface_reads_the_headless_marker(listener, monkeypatch):
    monkeypatch.setenv("HERMES_SERVE_HEADLESS", "1")
    assert listener.this_surface() == "desktop"
    monkeypatch.delenv("HERMES_SERVE_HEADLESS")
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", object())
    assert listener.this_surface() == "dashboard"
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server")
    assert listener.this_surface() == "gateway"


def test_host_decision(listener, monkeypatch):
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", "9443")
    assert listener.host_decision("dashboard", None) == "bind"
    assert listener.host_decision("dashboard", {"surface": "desktop", "pid": 1, "port": 9443}) == "wait"
    assert listener.host_decision("dashboard", {"surface": "dashboard", "pid": 1, "port": 9443}) == "wait"
    assert listener.host_decision("desktop", {"surface": "dashboard", "pid": 1, "port": 9443}) == "claim"
    # A record from before the surface field existed cannot be ranked, so it is never displaced.
    assert listener.host_decision("desktop", {"pid": 1, "port": 9443}) == "wait"
    # A holder of some other port says nothing about ours.
    assert listener.host_decision("dashboard", {"surface": "desktop", "pid": 1, "port": 9444}) == "bind"


def test_should_yield(listener):
    assert listener.should_yield("dashboard", {"surface": "desktop", "pid": 1})
    assert not listener.should_yield("desktop", {"surface": "dashboard", "pid": 1})
    assert not listener.should_yield("dashboard", None)


def test_pid_alive_tells_a_running_process_from_a_dead_or_nonsense_one(listener, other_pid):
    assert listener.pid_alive(os.getpid())
    assert listener.pid_alive(other_pid)
    assert not listener.pid_alive(DEAD_PID)
    assert not listener.pid_alive("nope")
    assert not listener.pid_alive(None)


# ---- the ticks ---------------------------------------------------------------


def test_a_dashboard_waits_behind_a_live_desktop_host(listener, monkeypatch, other_pid, held_port):
    _as(listener, monkeypatch, "dashboard")
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(held_port))
    _record(listener, "runtime", pid=other_pid, surface="desktop", port=held_port)

    assert asyncio.run(listener.host_tick(1)) == "wait"
    assert not listener.state.running
    assert "hosted by desktop" in listener.state.error
    assert listener.read_claim() is None


def test_a_desktop_claims_a_port_a_dashboard_holds(listener, monkeypatch, other_pid, held_port):
    _as(listener, monkeypatch, "desktop")
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(held_port))
    _record(listener, "runtime", pid=other_pid, surface="dashboard", port=held_port)

    assert asyncio.run(listener.host_tick(1)) == "claim"
    assert not listener.state.running
    claim = listener.read_claim()
    assert claim["pid"] == os.getpid() and claim["surface"] == "desktop"


def test_a_record_from_a_dead_process_does_not_hold_the_port(listener, monkeypatch, held_port):
    _as(listener, monkeypatch, "dashboard")
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(held_port))
    _record(listener, "runtime", pid=DEAD_PID, surface="desktop", port=held_port)

    # Nobody live holds it, so this is a plain bind attempt - which fails on the held port and
    # says so, rather than deferring to a ghost.
    assert asyncio.run(listener.host_tick(1)) == "bind"
    assert "could not bind" in listener.state.error


def test_binding_records_the_surface_and_clears_the_claim(listener, monkeypatch):
    _as(listener, monkeypatch, "desktop")
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(_free_port()))
    _record(listener, "claim", pid=os.getpid(), surface="desktop")

    async def scenario():
        outcome = await listener.host_tick(1)
        recorded = listener.read_runtime()
        claim = listener.read_claim()
        await listener.stop()
        return outcome, recorded, claim

    outcome, recorded, claim = asyncio.run(scenario())
    assert outcome == "bound"
    assert recorded["surface"] == "desktop" and recorded["pid"] == os.getpid()
    assert claim is None


def test_a_host_yields_to_a_live_desktop_claim_and_holds_against_a_dead_one(
    listener, monkeypatch, other_pid
):
    _as(listener, monkeypatch, "dashboard")
    port = _free_port()
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(port))

    async def scenario():
        assert await listener.host_tick(1) == "bound"
        _record(listener, "claim", pid=DEAD_PID, surface="desktop")
        held = await listener.host_tick(1)
        _record(listener, "claim", pid=other_pid, surface="desktop")
        yielded = await listener.host_tick(1)
        with socket.socket() as probe:  # the port is free again for the claimant
            probe.bind(("127.0.0.1", port))
        return held, yielded, listener.read_runtime()

    held, yielded, recorded = asyncio.run(scenario())
    assert held == "hold"
    assert yielded == "yield"
    assert not listener.state.running
    assert "yielded" in listener.state.error and "desktop" in listener.state.error
    assert recorded is None
