"""The gateway host: the listener answering for itself inside ``hermes gateway run``.

Two things are on trial. Arming must refuse every process that is not the gateway, because
``register`` runs everywhere and the marker it would naively read is inherited by the gateway's
children. And the in-process app must gate, answer the two routes, and terminate ``/api/ws`` on a
Starlette socket the real handler can drive - proven here with a stub handler that echoes, since
the real one needs a gateway in the process. The live gateway run is the plan's verification.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import sys
import time
from typing import Any, Dict, Tuple

import pytest


@pytest.fixture()
def host(_plugin_on_path, store_path, monkeypatch, tmp_path):
    module = importlib.import_module("hr_gateway_host")
    listener = importlib.import_module("hr_listener")
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path)
    listener.state.failures.clear()
    listener.state.running = False
    listener.state.error = ""
    monkeypatch.setattr(module, "HANDLE_WS", None)
    yield module
    module.disarm()
    asyncio.run(listener.stop())


@pytest.fixture()
def paired(hr_devices, store_path):
    device, token = hr_devices.create_device("Pixel 8", store_path)
    return device, token


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _serve(app) -> Tuple[Any, int]:
    """``app`` on an ephemeral loopback port, without ``serve()``'s signal handlers."""
    import uvicorn

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", lifespan="on", proxy_headers=False
    )
    server = uvicorn.Server(config)
    config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    return server, server.servers[0].sockets[0].getsockname()[1]


def _drive(host, scenario):
    async def main():
        server, port = await _serve(host.GatewayHost())
        try:
            return await scenario(port)
        finally:
            await server.shutdown()

    return asyncio.run(main())


async def _echo(ws, *, auth_identity=None, subprotocol=None) -> None:
    """Stands in for ``tui_gateway.ws.handle_ws``: same surface, no gateway behind it."""
    from starlette.websockets import WebSocketDisconnect

    await ws.accept()
    await ws.send_text(json.dumps({"identity": auth_identity, "peer": ws.client.host}))
    try:
        while True:
            await ws.send_text("echo:" + await ws.receive_text())
    except WebSocketDisconnect:
        pass


# ---- is this the gateway? ----------------------------------------------------


def test_a_test_process_is_not_the_gateway(host):
    assert not host.is_gateway_process()
    assert not host.arm()
    assert not host.armed()


def test_argv_decides_and_the_environment_marker_does_not(host, monkeypatch):
    """The gateway discovers plugins before ``gateway.run`` sets ``_HERMES_GATEWAY``, and its
    children inherit the marker afterwards, so it says nothing either way."""
    monkeypatch.setattr(sys, "argv", ["C:/hermes/hermes_cli/main.py", "gateway", "status"])
    assert not host.is_gateway_process()
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    assert not host.is_gateway_process()
    monkeypatch.delenv("_HERMES_GATEWAY")
    monkeypatch.setattr(sys, "argv", ["C:/hermes/hermes_cli/main.py", "gateway", "run"])
    assert host.is_gateway_process()
    monkeypatch.setattr(sys, "argv", ["C:/hermes/hermes_cli/main.py", "--profile", "ops", "gateway", "run"])
    assert host.is_gateway_process()


# ---- the routes ---------------------------------------------------------------


def test_an_unauthenticated_request_is_refused(host):
    async def scenario(port):
        import httpx

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            return await client.get(host._HEALTH_PATH)

    response = _drive(host, scenario)
    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


def test_health_and_ws_ticket_are_answered_in_direct_mode(host, paired):
    device, token = paired

    async def scenario(port):
        import httpx

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            health = await client.get(host._HEALTH_PATH, headers=_auth(token))
            ticket = await client.post(host._WS_TICKET_PATH, headers=_auth(token))
            wrong_method = await client.post(host._HEALTH_PATH, headers=_auth(token))
            unknown = await client.get("/api/sessions", headers=_auth(token))
            return health, ticket, wrong_method, unknown

    health, ticket, wrong_method, unknown = _drive(host, scenario)
    assert health.status_code == 200
    assert health.json() == {
        "ok": True,
        "plugin": "hermes-odyssey",
        "version": importlib.import_module("hr_routes").PLUGIN_VERSION,
        "mode": "direct",
        "device": {"id": device.id, "label": "Pixel 8"},
    }
    assert ticket.status_code == 200
    assert ticket.json() == {"mode": "direct", "ws_path": "/api/ws", "ticket": None, "expires_in": None}
    # Nothing else is served: there is no dashboard behind this host to forward to.
    assert wrong_method.status_code == 404
    assert unknown.status_code == 404


# ---- the socket ---------------------------------------------------------------


def test_an_unauthenticated_upgrade_is_refused_before_it_is_accepted(host, monkeypatch):
    monkeypatch.setattr(host, "HANDLE_WS", _echo)

    async def scenario(port):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            async with connect(f"ws://127.0.0.1:{port}/api/ws", proxy=None):
                return "accepted"
        except InvalidStatus as exc:
            return exc.response.status_code

    assert _drive(host, scenario) == 403


def test_the_handler_gets_a_starlette_socket_and_the_devices_identity(host, paired, monkeypatch):
    device, token = paired
    monkeypatch.setattr(host, "HANDLE_WS", _echo)

    async def scenario(port):
        from websockets.asyncio.client import connect

        async with connect(
            f"ws://127.0.0.1:{port}/api/ws", additional_headers=_auth(token), proxy=None
        ) as ws:
            opening = json.loads(await ws.recv())
            await ws.send('{"method":"gateway.ping"}')
            return opening, await ws.recv()

    opening, echoed = _drive(host, scenario)
    assert opening["identity"] == {"user_id": f"device:{device.id}", "provider": "hermes-odyssey-device"}
    assert opening["peer"] == "127.0.0.1"
    assert echoed == 'echo:{"method":"gateway.ping"}'


def test_the_terminals_query_credential_is_admitted_here_too(host, paired, monkeypatch):
    """``hermes odyssey attach`` against a gateway host: same URL shape, same gate, same identity."""
    device, token = paired
    monkeypatch.setattr(host, "HANDLE_WS", _echo)

    async def scenario(port):
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{port}/api/ws?device={token}", proxy=None) as ws:
            return json.loads(await ws.recv())

    opening = _drive(host, scenario)
    assert opening["identity"] == {"user_id": f"device:{device.id}", "provider": "hermes-odyssey-device"}


def test_only_the_gateway_path_is_a_socket(host, paired, monkeypatch):
    _device, token = paired
    monkeypatch.setattr(host, "HANDLE_WS", _echo)

    async def scenario(port):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            async with connect(
                f"ws://127.0.0.1:{port}/api/other", additional_headers=_auth(token), proxy=None
            ):
                return "accepted"
        except InvalidStatus as exc:
            return exc.response.status_code

    assert _drive(host, scenario) == 403


def test_revoking_a_device_closes_its_socket_with_1008(host, paired, hr_devices, store_path, monkeypatch):
    device, token = paired
    monkeypatch.setattr(host, "HANDLE_WS", _echo)
    listener = importlib.import_module("hr_listener")

    async def scenario(port):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        async with connect(
            f"ws://127.0.0.1:{port}/api/ws", additional_headers=_auth(token), proxy=None
        ) as ws:
            await ws.recv()
            assert len(listener._live) == 1
            hr_devices.revoke_device(device.id, store_path)
            listener._sweep_revoked()
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except ConnectionClosed as exc:
                return exc.rcvd.code if exc.rcvd else None
            return "still open"

    assert _drive(host, scenario) == 1008
    assert not listener._live


# ---- arming ----------------------------------------------------------------------


def test_armed_it_hosts_on_its_own_thread_and_disarms_cleanly(host, paired, monkeypatch):
    """The whole path a real gateway takes, with the two identity checks answered for it: a thread
    with a loop of its own, the host rule binding TLS on the configured port, the record naming
    the gateway with no upstream, and ``disarm`` releasing all of it."""
    _device, token = paired
    listener = importlib.import_module("hr_listener")
    port = _free_port()
    monkeypatch.setenv("HERMES_ODYSSEY_LISTENER", "1")
    monkeypatch.setenv("HERMES_ODYSSEY_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_ODYSSEY_PORT", str(port))
    monkeypatch.setattr(host, "is_gateway_process", lambda: True)
    monkeypatch.setattr(host, "_pid_file_names_this_process", lambda: True)
    monkeypatch.setattr(host, "_PID_POLL_SECONDS", 0.05)

    assert host.arm()
    assert host.armed()
    deadline = time.monotonic() + 10.0
    while not listener.state.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert listener.state.running, listener.state.error

    recorded = listener.read_runtime()
    assert recorded["pid"] == os.getpid()
    assert recorded["surface"] == "gateway"
    assert recorded["upstream_port"] == 0
    assert listener.live_runtime() == recorded

    import httpx

    with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=False) as client:
        assert client.get(host._HEALTH_PATH).status_code == 401
        health = client.get(host._HEALTH_PATH, headers=_auth(token))
    assert health.status_code == 200 and health.json()["mode"] == "direct"

    host.disarm()
    assert not host.armed()
    assert not listener.state.running
    assert listener.read_runtime() is None
    with socket.socket() as probe:  # the port is free again
        probe.bind(("127.0.0.1", port))


def test_a_process_the_pid_file_never_names_does_not_host(host, monkeypatch):
    listener = importlib.import_module("hr_listener")
    monkeypatch.setenv("HERMES_ODYSSEY_LISTENER", "1")
    monkeypatch.setattr(host, "is_gateway_process", lambda: True)
    monkeypatch.setattr(host, "_pid_file_names_this_process", lambda: False)
    monkeypatch.setattr(host, "_PID_POLL_SECONDS", 0.01)
    monkeypatch.setattr(host, "_PID_CLAIM_TIMEOUT_SECONDS", 0.1)

    assert host.arm()
    host._thread.join(5.0)
    assert not host.armed()
    assert not listener.state.running
    assert "never claimed" in listener.state.error
