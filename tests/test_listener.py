"""The TLS listener's reverse proxy, driven against a real upstream over real sockets.

TLS itself is not exercised here — ``test_identity.py`` covers the certificate, and uvicorn's
``ssl_certfile`` wiring is one line — but everything the proxy *decides* is, because every one of
those decisions is a way to hand the dashboard to the LAN:

* an unauthenticated request must never reach 9119, since the dashboard trusts loopback and this
  proxy is loopback;
* the ``Host`` header must be rewritten, or ``host_header_middleware`` and
  ``_ws_host_origin_reason`` reject the request the phone just authenticated;
* the WebSocket credential must be injected server side, because in loopback mode it is the
  dashboard's process-lifetime master key and cannot cross the network.

The upstream is a bare ASGI app that reports the scope it was handed, so an assertion here is
about what the dashboard would actually see rather than about what the proxy meant to send.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, Dict, Tuple

import pytest


@pytest.fixture()
def hr_listener(_plugin_on_path, store_path):
    """The listener module, with the throttle counter cleared between tests."""
    module = importlib.import_module("hr_listener")
    module.state.failures.clear()
    return module


@pytest.fixture()
def paired(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    return token


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- the upstream ----------------------------------------------------------


async def _upstream(scope, receive, send) -> None:
    """A dashboard stand-in that answers with the request scope it was given."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            await send({"type": message["type"] + ".complete"})
            if message["type"] == "lifespan.shutdown":
                return

    headers = {key.decode(): value.decode() for key, value in scope["headers"]}

    if scope["type"] == "http":
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = json.dumps(
            {
                "method": scope["method"],
                "path": scope["path"],
                "query": scope["query_string"].decode(),
                "headers": headers,
                "body": body.decode(),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"x-from-upstream", b"1"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
        return

    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        offered = list(scope.get("subprotocols") or [])
        await send({"type": "websocket.accept", "subprotocol": offered[0] if offered else None})
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps({"query": scope["query_string"].decode(), "headers": headers}),
            }
        )
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await send({"type": "websocket.send", "text": "echo:" + message["text"]})
            elif message.get("bytes") is not None:
                await send({"type": "websocket.send", "bytes": b"echo:" + message["bytes"]})


# ---- running two servers on ephemeral ports --------------------------------


async def _serve(app) -> Tuple[Any, int]:
    """Start ``app`` on 127.0.0.1 with an OS-assigned port; return the server and that port.

    ``startup()`` rather than ``serve()``, for the same reason the listener itself avoids it:
    ``serve()`` installs signal handlers, which in a test process would trample pytest's.
    """
    import uvicorn

    # ``proxy_headers=False`` mirrors what ``hr_listener.start`` configures, and it is
    # load-bearing rather than cosmetic: with uvicorn's default the peer's own X-Forwarded-For
    # would rewrite ``scope["client"]``, and the proxy's forwarding header would then carry an
    # address the phone chose.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", lifespan="on", proxy_headers=False
    )
    server = uvicorn.Server(config)
    config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, port


class _Pair:
    """An upstream and a proxy in front of it, both listening, plus the proxy's port."""

    def __init__(self, upstream, proxy, port, app) -> None:
        self.upstream = upstream
        self.proxy = proxy
        self.port = port
        self.app = app

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def _running(hr_listener) -> _Pair:
    upstream_server, upstream_port = await _serve(_upstream)
    app = hr_listener.ReverseProxy(upstream_port)
    proxy_server, proxy_port = await _serve(app)
    return _Pair(upstream_server, proxy_server, proxy_port, app)


async def _teardown(pair: _Pair) -> None:
    await pair.proxy.shutdown()
    await pair.upstream.shutdown()
    await pair.app.aclose()


def _run(coro_factory):
    """Drive one async scenario. There is no pytest-asyncio in the Hermes venv and adding one to
    an environment ``hermes update`` owns is not on the table, so the loop is explicit."""

    async def main(hr_listener):
        pair = await _running(hr_listener)
        try:
            return await coro_factory(pair)
        finally:
            await _teardown(pair)

    return main


def _drive(hr_listener, scenario):
    return asyncio.run(_run(scenario)(hr_listener))


# ---- the gate --------------------------------------------------------------


def test_an_unauthenticated_request_never_reaches_the_dashboard(hr_listener):
    """The dashboard trusts loopback and this proxy *is* loopback. Forwarding an anonymous
    request would hand the whole dashboard to anyone on the Wi-Fi."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/api/sessions")

    response = _drive(hr_listener, scenario)
    assert response.status_code == 401
    assert "x-from-upstream" not in response.headers


def test_an_unknown_bearer_is_refused(hr_listener):
    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(
                pair.base_url + "/api/sessions", headers=_auth("hr1.aaaaaaaaaaaa.nope")
            )

    assert _drive(hr_listener, scenario).status_code == 401


def test_a_revoked_device_is_refused(hr_listener, hr_devices, store_path, paired):
    hr_devices.revoke_device(paired.split(".")[1], store_path)

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/api/sessions", headers=_auth(paired))

    assert _drive(hr_listener, scenario).status_code == 401


def test_an_unreadable_store_is_503_not_401(hr_listener, store_path, paired):
    """503 says nothing was checked. A phone told 401 would discard a credential that is fine."""
    store_path.write_text("{ truncated", encoding="utf-8")

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/api/sessions", headers=_auth(paired))

    assert _drive(hr_listener, scenario).status_code == 503


def test_repeated_failures_stop_being_answered(hr_listener, paired):
    """A brute-force brake, not a firewall: it keeps a mistyped-token loop off the device store."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            for _ in range(hr_listener._MAX_FAILURES):
                await client.get(pair.base_url + "/x", headers=_auth("hr1.aaaaaaaaaaaa.nope"))
            blocked = await client.get(pair.base_url + "/x", headers=_auth("hr1.aaaaaaaaaaaa.no"))
            # A live device is caught by the brake too, which is the accepted cost of a brake
            # that cannot be probed with valid credentials.
            good = await client.get(pair.base_url + "/x", headers=_auth(paired))
            return blocked, good

    blocked, good = _drive(hr_listener, scenario)
    assert blocked.status_code == 429
    assert good.status_code == 429


def test_no_cors_headers_are_ever_sent(hr_listener, paired):
    """A page on the LAN must not be able to reach this from a browser. The bearer forces a
    preflight, and the preflight is never answered."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/x", headers=_auth(paired))

    headers = _drive(hr_listener, scenario).headers
    assert not any(key.lower().startswith("access-control-") for key in headers)


# ---- proxying --------------------------------------------------------------


def test_an_authenticated_request_arrives_with_a_rewritten_host(hr_listener, paired):
    """``host_header_middleware`` measures Host against the dashboard's bound host. The phone's
    Host names the listener, which the dashboard has never heard of."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(
                pair.base_url + "/api/sessions?limit=5", headers=_auth(paired)
            )

    response = _drive(hr_listener, scenario)
    assert response.status_code == 201
    seen = response.json()
    assert seen["path"] == "/api/sessions"
    assert seen["query"] == "limit=5"
    assert seen["headers"]["host"].startswith("127.0.0.1:")
    assert seen["headers"]["authorization"].startswith("Bearer hr1.")


def test_a_client_supplied_forwarding_header_is_not_relayed(hr_listener, paired):
    """A proxy that forwards what the peer claimed about itself lets the peer forge it."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(
                pair.base_url + "/x",
                headers={**_auth(paired), "X-Forwarded-For": "10.9.9.9", "X-Forwarded-Proto": "http"},
            )

    seen = _drive(hr_listener, scenario).json()
    assert seen["headers"]["x-forwarded-for"] == "127.0.0.1"
    assert seen["headers"]["x-forwarded-proto"] == "https"


def test_a_request_body_and_the_response_are_relayed_intact(hr_listener, paired):
    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.post(
                pair.base_url + "/api/plugins/hermes-remote/ws-ticket",
                headers=_auth(paired),
                content=b'{"hello":"world"}',
            )

    response = _drive(hr_listener, scenario)
    assert response.json()["body"] == '{"hello":"world"}'
    assert response.json()["method"] == "POST"
    assert response.headers["x-from-upstream"] == "1"


def test_a_get_carries_no_body_framing(hr_listener, paired):
    """Streaming unconditionally would put ``Transfer-Encoding: chunked`` on every GET."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/x", headers=_auth(paired))

    seen = _drive(hr_listener, scenario).json()
    assert "transfer-encoding" not in seen["headers"]


def test_an_upstream_that_is_gone_is_a_502(hr_listener, paired):
    """Not a 401. The phone's credential was fine; the dashboard was not there."""

    async def scenario(pair):
        import httpx

        await pair.upstream.shutdown()
        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + "/x", headers=_auth(paired))

    assert _drive(hr_listener, scenario).status_code == 502


# ---- the WebSocket ---------------------------------------------------------


def _ws_url(pair: _Pair, path: str = "/api/ws") -> str:
    return f"ws://127.0.0.1:{pair.port}{path}"


def test_an_unauthenticated_upgrade_is_refused_before_it_is_accepted(hr_listener):
    async def scenario(pair):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            async with connect(_ws_url(pair), proxy=None):
                return None
        except InvalidStatus as exc:
            return exc.response.status_code

    assert _drive(hr_listener, scenario) == 403


def test_the_listener_supplies_the_credential_the_phone_cannot_have(hr_listener, paired, monkeypatch):
    """In loopback mode the only credential the WS gate accepts is the dashboard's session token,
    a process-lifetime master key. It is attached here and never sent to the phone."""
    import hr_wsauth  # noqa: PLC0415 — the standalone copy the listener will import

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", lambda: {"token": "session-master"})

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(
            _ws_url(pair) + "?resume=abc", additional_headers=_auth(paired), proxy=None
        ) as socket:
            return json.loads(await socket.recv())

    seen = _drive(hr_listener, scenario)
    assert seen["query"] == "resume=abc&token=session-master"
    assert seen["headers"]["host"].startswith("127.0.0.1:")
    assert "origin" not in seen["headers"]


def test_a_client_supplied_token_cannot_override_the_injected_one(hr_listener, paired, monkeypatch):
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", lambda: {"token": "session-master"})

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(
            _ws_url(pair) + "?token=guessed", additional_headers=_auth(paired), proxy=None
        ) as socket:
            return json.loads(await socket.recv())

    assert _drive(hr_listener, scenario)["query"] == "token=session-master"


def test_an_upgrade_cannot_carry_a_forged_forwarding_header(hr_listener, paired, monkeypatch):
    """Same rule as the HTTP path: the phone does not get to describe where it came from."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(
            _ws_url(pair),
            additional_headers={**_auth(paired), "X-Forwarded-For": "10.9.9.9"},
            proxy=None,
        ) as socket:
            return json.loads(await socket.recv())

    assert "x-forwarded-for" not in _drive(hr_listener, scenario)["headers"]


def test_gated_mode_lets_the_phones_own_ticket_through(hr_listener, paired, monkeypatch):
    """The phone got that ticket from ``/ws-ticket`` and it dies in 30 seconds; the listener adds
    nothing of its own."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(
            _ws_url(pair) + "?ticket=mine", additional_headers=_auth(paired), proxy=None
        ) as socket:
            return json.loads(await socket.recv())

    assert _drive(hr_listener, scenario)["query"] == "ticket=mine"


def test_an_upgrade_may_carry_the_device_token_in_its_query(hr_listener, paired, monkeypatch):
    """The attached TUI connects with Node's own ``WebSocket``, which takes a URL and no headers.
    Its token rides in the query, is checked by the same gate, and never reaches the upstream."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", lambda: {"token": "session-master"})

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(_ws_url(pair) + f"?device={paired}&resume=abc", proxy=None) as socket:
            return json.loads(await socket.recv())

    seen = _drive(hr_listener, scenario)
    assert seen["query"] == "resume=abc&token=session-master"
    assert paired not in json.dumps(seen)


def test_a_wrong_device_token_in_the_query_is_refused(hr_listener, paired):
    async def scenario(pair):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            async with connect(_ws_url(pair) + "?device=hr1.000000000000.nope", proxy=None):
                return "accepted"
        except InvalidStatus as exc:
            return exc.response.status_code

    assert _drive(hr_listener, scenario) == 403


def test_the_query_credential_is_not_honoured_on_http(hr_listener, paired):
    """A token in a URL lands in access logs and browser history; only the upgrade, which has no
    other way to carry one, gets to use it."""

    async def scenario(pair):
        import httpx

        async with httpx.AsyncClient() as client:
            return await client.get(pair.base_url + f"/x?device={paired}")

    assert _drive(hr_listener, scenario).status_code == 401


def test_frames_flow_both_ways(hr_listener, paired, monkeypatch):
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(_ws_url(pair), additional_headers=_auth(paired), proxy=None) as socket:
            await socket.recv()  # the upstream's opening report
            await socket.send('{"method":"prompt.submit"}')
            text = await socket.recv()
            await socket.send(b"\x00\x01")
            binary = await socket.recv()
            return text, binary

    text, binary = _drive(hr_listener, scenario)
    assert text == 'echo:{"method":"prompt.submit"}'
    assert binary == b"echo:\x00\x01"


def test_the_upstreams_subprotocol_is_what_reaches_the_phone(hr_listener, paired, monkeypatch):
    """The dashboard selects one deliberately — never the ticket-bearing protocol value it took
    care not to reflect — so the accepted protocol is relayed, not the client's request."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(
            _ws_url(pair),
            additional_headers=_auth(paired),
            subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket.secret"],
            proxy=None,
        ) as socket:
            return socket.subprotocol

    assert _drive(hr_listener, scenario) == "hermes-gateway-v1"


# ---- revocation reaching a socket that is already open ---------------------


def test_revoking_a_device_closes_the_socket_it_already_had(
    hr_listener, hr_devices, store_path, paired, monkeypatch
):
    """The hole this closes: the bearer is only on the upgrade, and the phone's heartbeat keeps
    the socket open for as long as the listener lives, so an upgrade-only check let a revoked
    device go on reading a session indefinitely."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        async with connect(_ws_url(pair), additional_headers=_auth(paired), proxy=None) as socket:
            await socket.recv()  # the upstream's opening report; the socket is live
            assert len(hr_listener._live) == 1
            hr_devices.revoke_device(paired.split(".")[1], store_path)
            hr_listener._sweep_revoked()
            try:
                await asyncio.wait_for(socket.recv(), timeout=5)
            except ConnectionClosed as exc:
                return exc.rcvd.code
            return None

    # 1008 rather than 1000: the phone shows "Pair again" instead of retrying a reconnect that can
    # only ever be refused.
    assert _drive(hr_listener, scenario) == 1008
    assert not hr_listener._live


def test_an_unreadable_store_does_not_close_anything(hr_listener, store_path, paired, monkeypatch):
    """The store says which devices are live. A disk error that answered "none" would close every
    session on the machine — the same reason :func:`_authenticate` answers 503 and not 401."""
    import hr_wsauth

    monkeypatch.setattr(hr_wsauth, "listener_upgrade_query", dict)

    async def scenario(pair):
        from websockets.asyncio.client import connect

        async with connect(_ws_url(pair), additional_headers=_auth(paired), proxy=None) as socket:
            await socket.recv()
            store_path.write_text("{ truncated", encoding="utf-8")
            hr_listener._sweep_revoked()
            await socket.send("still here")
            return await socket.recv()

    assert _drive(hr_listener, scenario) == "echo:still here"


def test_a_sweep_with_nothing_open_reads_no_store(hr_listener, monkeypatch):
    """The common case is no phone attached, and it must not cost a file read every five seconds."""
    import hr_devices as devices

    def boom(*args, **kwargs):
        raise AssertionError("the store was read with no live socket")

    monkeypatch.setattr(devices, "list_devices", boom)
    hr_listener._sweep_revoked()


# ---- configuration ---------------------------------------------------------


def test_the_kill_switch_stops_it_opening_a_socket(hr_listener, monkeypatch):
    monkeypatch.setenv(hr_listener.ENV_ENABLED, "off")
    assert hr_listener.enabled() is False
    monkeypatch.setenv(hr_listener.ENV_ENABLED, "1")
    assert hr_listener.enabled() is True


def test_a_nonsense_port_falls_back_to_the_default(hr_listener, monkeypatch):
    monkeypatch.setenv(hr_listener.ENV_PORT, "not-a-port")
    assert hr_listener.configured_port() == hr_listener.DEFAULT_PORT
    monkeypatch.setenv(hr_listener.ENV_PORT, "70000")
    assert hr_listener.configured_port() == hr_listener.DEFAULT_PORT
    monkeypatch.setenv(hr_listener.ENV_PORT, "9444")
    assert hr_listener.configured_port() == 9444


def test_it_does_not_start_where_there_is_no_dashboard_to_proxy(hr_listener, monkeypatch):
    """This is what keeps a bare ``include_router`` — in a test, or any other host — from opening
    a LAN socket as a side effect of mounting the plugin's routes."""
    monkeypatch.delenv(hr_listener.ENV_ENABLED, raising=False)
    monkeypatch.delitem(__import__("sys").modules, "hermes_cli.web_server", raising=False)

    async def scenario():
        hr_listener.schedule_start()
        return hr_listener._task

    assert asyncio.run(scenario()) is None
