"""The TLS listener: the security boundary between the LAN and a loopback dashboard.

The dashboard has no TLS of its own and expects a reverse proxy — ``web_server.py`` names Caddy in
its own notes. Owning that proxy inside the plugin is what buys certificate pinning without a fork
and without asking anyone to run Caddy. It is the third of the three things §3 of the plan says
Phase 1 registers, and §5.6 explains why proxying *from loopback* makes it simpler than the plan
assumed: the dashboard sees a loopback peer, which is exactly what ``_ws_client_reason`` demands in
ungated mode, and this process controls the ``Host`` header, so ``_ws_host_origin_reason`` is
satisfied by construction. The dashboard stays bound to 127.0.0.1 and never has to trust the LAN.

**Everything through here is authenticated by this file, not by the dashboard.** That is the part
worth being loud about. On a loopback bind the dashboard trusts its peer, and every proxied
request arrives from loopback because that is what a proxy is — so if this listener forwarded an
unauthenticated request, it would hand the whole dashboard to anyone on the Wi-Fi. So
:func:`_authenticate` runs before anything is forwarded, on HTTP requests and WebSocket upgrades
alike, and a request without a live paired device's bearer never reaches 9119 at all. The plugin's
own routes are additionally gated by the dashboard's token seam; every other path is gated only
here.

A WebSocket needs one thing more, because the bearer is on the upgrade and the frames after it
carry nothing: :func:`_sweep_revoked` re-reads the device store every few seconds and closes the
sockets whose device has gone. Without it ``hermes remote revoke`` ended a device's *requests* and
left its open session running, which a phone's heartbeat then kept alive indefinitely.

The bearer *is* forwarded upstream, because ``/api/plugins/hermes-remote/…`` needs it to clear that
second gate. The dashboard session token travels the other way and only in loopback mode: it is
attached to the WebSocket upgrade query by :func:`hr_wsauth.listener_upgrade_query`, server side,
and never appears in a response, a log or a QR code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlencode

# ``hr_wsauth`` is imported where it is used rather than here: it pulls ``hr_provider``, which
# imports ``hermes_cli.dashboard_auth`` and through it FastAPI. ``hermes remote`` reads this
# module's configuration in the plain CLI process, and that cost would land on every invocation.
try:  # package import (``hermes_plugins.hermes_remote``)
    from . import hr_devices, hr_identity, hr_paths
except ImportError:  # standalone path load from dashboard/api.py
    import hr_devices  # type: ignore[no-redef]
    import hr_identity  # type: ignore[no-redef]
    import hr_paths  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

#: Nothing standard claims it, and it is stable so a paired phone keeps working across restarts
#: without re-scanning.
DEFAULT_PORT = 9443

ENV_PORT = "HERMES_REMOTE_PORT"
ENV_HOST = "HERMES_REMOTE_HOST"
#: Set to ``0``/``off``/``false``/``no`` to load the plugin without opening a LAN socket.
ENV_ENABLED = "HERMES_REMOTE_LISTENER"

#: Where a running listener records itself, so ``hermes remote status`` in another process can say
#: something true. Removed on clean shutdown; a stale one is detected by probing the port.
RUNTIME_FILENAME = "listener.json"

#: Rejected bearers tolerated in a window before the listener stops answering at all. This is a
#: brute-force brake, not a firewall: the secret is 256 bits, so the real work is keeping a
#: mistyped-token loop from spinning the device store.
_MAX_FAILURES = 20
_FAILURE_WINDOW_SECONDS = 60.0

#: Hop-by-hop headers, which belong to one connection and must not be relayed (RFC 9110 §7.6.1).
#: ``host`` is not hop-by-hop but is rewritten rather than copied, so it is in the drop set too.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        # Never relayed from the client. The listener sets its own below; a proxy that forwards
        # what the peer claimed about itself is a proxy that lets the peer forge it.
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "forwarded",
    }
)
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Dropped from a proxied WebSocket upgrade: the handshake headers belong to the leg the client
#: terminated, and ``origin`` would be the phone's, which ``_ws_host_origin_reason`` would then
#: measure against a bound host it has never heard of. The forwarding headers go for the same
#: reason they do on the HTTP path — a peer must not get to describe itself.
_DROP_WS_HEADERS = frozenset(
    {
        "host",
        "origin",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "forwarded",
    }
)

#: Matches the dashboard's own ``ws_max_size``: a phone sending an attachment must not be cut off
#: by the proxy when the server behind it would have accepted the frame.
_WS_MAX_BYTES = 64 * 1024 * 1024

#: How often a live socket's device is re-checked against the store. ``hermes remote revoke`` runs
#: in a *different process* — the plain CLI — so there is no in-process signal to hook and the file
#: is the only channel. Five seconds is the delay between revoking and the phone dropping; it is
#: bounded work no matter how many phones are attached, because one read answers for all of them.
_REVOCATION_SWEEP_SECONDS = 5.0


# ---- configuration ---------------------------------------------------------


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "off", "false", "no"}


def enabled() -> bool:
    """Whether the listener should open a socket at all."""
    return _env_flag(ENV_ENABLED, True)


def configured_port() -> int:
    try:
        port = int(os.environ.get(ENV_PORT, "").strip() or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def configured_host() -> str:
    """The bind address. Every interface by default — the phone is on one of them and which one
    changes with the network. TLS plus the bearer, not the bind, is what keeps this closed."""
    return os.environ.get(ENV_HOST, "").strip() or "0.0.0.0"


def runtime_path() -> Path:
    return hr_paths.state_dir() / RUNTIME_FILENAME


# ---- state -----------------------------------------------------------------


@dataclass
class ListenerState:
    """What this process's listener is doing. Read by ``dashboard/api.py`` and the shutdown hook."""

    running: bool = False
    host: str = ""
    port: int = 0
    upstream_port: int = 0
    error: str = ""
    started_at: Optional[int] = None
    failures: Deque[float] = field(default_factory=deque)


state = ListenerState()
_server: Any = None
_task: Optional[asyncio.Task] = None


# ---- live sockets ----------------------------------------------------------


@dataclass
class _LiveSocket:
    """One proxied WebSocket, and the device whose bearer opened it.

    Authenticating the upgrade is not enough on its own: the frames after it carry no bearer, and
    a phone's heartbeat keeps the socket open indefinitely, so a revoked device would go on reading
    a session until the listener process itself stopped. Holding the device id beside the socket is
    what lets :func:`_sweep_revoked` end it.
    """

    device_id: str
    revoked: asyncio.Event


_live: Dict[int, _LiveSocket] = {}
_live_seq = 0
_sweeper: Optional[asyncio.Task] = None


def _register_live(device_id: str) -> Tuple[int, _LiveSocket]:
    global _live_seq
    _live_seq += 1
    handle = _LiveSocket(device_id=device_id, revoked=asyncio.Event())
    _live[_live_seq] = handle
    return _live_seq, handle


def _sweep_revoked() -> None:
    """Signal every live socket whose device is no longer paired.

    An unreadable store is a reason to do *nothing*: it says which devices are live, and a disk
    error that answered "none" would close every session on the machine. Same reasoning as
    :func:`_authenticate` answering 503 rather than 401.
    """
    if not _live:
        return
    try:
        paired = {d.id for d in hr_devices.list_devices() if not d.revoked}
    except hr_devices.DeviceStoreUnavailable as exc:
        _log.warning("hermes-remote listener: revocation sweep skipped: %s", exc)
        return
    for handle in list(_live.values()):
        if handle.device_id not in paired and not handle.revoked.is_set():
            _log.info(
                "hermes-remote listener: closing live socket for revoked device %s",
                handle.device_id[:8],
            )
            handle.revoked.set()


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(_REVOCATION_SWEEP_SECONDS)
        try:
            _sweep_revoked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sweep must not stop the listener serving
            _log.debug("hermes-remote listener: revocation sweep failed: %s", exc)


def _write_runtime() -> None:
    """Record the live endpoint for other processes. Best effort: it is a convenience for
    ``hermes remote status``, and failing to write it must not stop the listener serving."""
    try:
        path = runtime_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": state.host,
                    "port": state.port,
                    "upstream_port": state.upstream_port,
                    "started_at": state.started_at,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        _log.debug("hermes-remote: could not write %s: %s", RUNTIME_FILENAME, exc)


def _clear_runtime() -> None:
    try:
        runtime_path().unlink(missing_ok=True)
    except OSError:
        pass


def read_runtime() -> Optional[Dict[str, Any]]:
    """What a listener last recorded, or ``None``. May be stale — probe the port to be sure."""
    try:
        return json.loads(runtime_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- authentication --------------------------------------------------------


class _Rejected(Exception):
    """A request that must not be proxied. ``status`` is what the client is told."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _header(headers: List[Tuple[bytes, bytes]], name: str) -> str:
    wanted = name.encode("latin-1")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


def _bearer(headers: List[Tuple[bytes, bytes]]) -> str:
    scheme, _, value = _header(headers, "authorization").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _throttled() -> bool:
    now = time.monotonic()
    while state.failures and now - state.failures[0] > _FAILURE_WINDOW_SECONDS:
        state.failures.popleft()
    return len(state.failures) >= _MAX_FAILURES


def _authenticate(scope: Dict[str, Any]):
    """The paired device behind this request, or raise :class:`_Rejected`.

    Every proxied path goes through here, including the WebSocket upgrade. An unreadable store is
    503 and never 401: answering 401 would tell a phone its token was rejected when in fact nothing
    was checked, and a paired device would wipe its credential over a transient disk error.
    """
    if _throttled():
        raise _Rejected(429, "too many failed attempts")
    try:
        device = hr_devices.verify_token(_bearer(scope.get("headers") or []))
    except hr_devices.DeviceStoreUnavailable as exc:
        _log.warning("hermes-remote listener: device store unavailable: %s", exc)
        raise _Rejected(503, "device store unavailable") from exc
    if device is None:
        state.failures.append(time.monotonic())
        raise _Rejected(401, "unauthorized")
    return device


# ---- the proxy -------------------------------------------------------------


class ReverseProxy:
    """A bare ASGI app forwarding an authenticated request to the loopback dashboard.

    Bare ASGI rather than a FastAPI app because there is nothing to route: every path is either
    forwarded verbatim or refused, and a framework here would add a second place for a path to be
    matched differently from the way the dashboard matches it.
    """

    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1") -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.authority = f"{upstream_host}:{upstream_port}"
        self._client: Any = None

    # -- lifecycle --

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self):
        if self._client is None:
            import httpx

            # No timeout on reads: the dashboard streams SSE (``/api/events``) and long polls, and
            # a read deadline would sever them. Connect keeps a short one because a loopback
            # connect that does not complete immediately is not going to.
            self._client = httpx.AsyncClient(
                base_url=f"http://{self.authority}",
                timeout=httpx.Timeout(None, connect=5.0),
                follow_redirects=False,  # the phone follows its own; a 302 is data here
            )
        return self._client

    # -- entry point --

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(scope, receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)

    async def _lifespan(self, scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- HTTP --

    def _forward_headers(self, scope) -> List[Tuple[str, str]]:
        headers = [
            (key.decode("latin-1"), value.decode("latin-1"))
            for key, value in scope["headers"]
            if key.lower().decode("latin-1") not in _DROP_REQUEST_HEADERS
        ]
        headers.append(("host", self.authority))
        # The dashboard reads X-Forwarded-Proto for the Secure flag on cookies it sets. It only
        # honours these from an allow-listed upstream, and loopback is the default allow list.
        headers.append(("x-forwarded-proto", "https"))
        client = scope.get("client")
        if client:
            headers.append(("x-forwarded-for", str(client[0])))
        # httpx adds its own Accept-Encoding when the request carries none, which would invite a
        # gzipped response for a client that never asked for one — and this proxy relays the body
        # raw, so the phone would get bytes it cannot read under a header it did not negotiate.
        if not any(key == "accept-encoding" for key, _ in headers):
            headers.append(("accept-encoding", "identity"))
        return headers

    async def _http(self, scope, receive, send) -> None:
        try:
            _authenticate(scope)
        except _Rejected as rejected:
            await _send_error(send, rejected)
            return

        import httpx

        async def body():
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                yield message.get("body", b"")
                more = message.get("more_body", False)

        url = scope["path"]
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode("latin-1")

        headers = self._forward_headers(scope)
        # Only stream a body when the client framed one. Passing an async iterable unconditionally
        # would put ``Transfer-Encoding: chunked`` on every GET, which is legal, pointless, and
        # rejected outright by some middleware.
        framed = any(
            key in {"content-length", "transfer-encoding"} for key, _ in headers
        ) or scope["method"] in {"POST", "PUT", "PATCH"}
        request = self._http_client().build_request(
            scope["method"], url, headers=headers, content=body() if framed else None
        )
        try:
            response = await self._http_client().send(request, stream=True)
        except httpx.HTTPError as exc:
            _log.warning("hermes-remote listener: upstream %s failed: %s", url, exc)
            await _send_error(send, _Rejected(502, "upstream unavailable"))
            return

        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (key.encode("latin-1"), value.encode("latin-1"))
                        for key, value in response.headers.multi_items()
                        if key.lower() not in _DROP_RESPONSE_HEADERS
                    ],
                }
            )
            # ``aiter_raw`` rather than ``aiter_bytes``: the response is relayed exactly as the
            # dashboard produced it, so a gzipped body keeps its Content-Encoding header and its
            # bytes rather than being silently decoded out from under the header.
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()

    # -- WebSocket --

    def _upgrade_url(self, scope) -> str:
        from urllib.parse import parse_qsl

        try:
            from . import hr_wsauth
        except ImportError:
            import hr_wsauth  # type: ignore[no-redef]

        params = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        injected = hr_wsauth.listener_upgrade_query()
        if injected:
            # The dashboard's session token, in loopback mode only. It replaces rather than joins
            # any ``token`` the client sent: the phone cannot know this value and must not be able
            # to influence it.
            params = [(k, v) for k, v in params if k not in injected]
            params.extend(injected.items())
        query = urlencode(params)
        return f"ws://{self.authority}{scope['path']}" + (f"?{query}" if query else "")

    async def _websocket(self, scope, receive, send) -> None:
        message = await receive()  # the ASGI "websocket.connect" that precedes any decision
        if message["type"] != "websocket.connect":
            return
        try:
            device = _authenticate(scope)
        except _Rejected as rejected:
            # Refusing before accepting produces an HTTP status on the handshake, which is what a
            # client can actually read; a close code after accept looks like a server fault.
            await send({"type": "websocket.close", "code": 1008})
            _log.info("hermes-remote listener: WS upgrade refused (%s)", rejected.detail)
            return

        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        headers = [
            (key.decode("latin-1"), value.decode("latin-1"))
            for key, value in scope["headers"]
            if key.lower().decode("latin-1") not in _DROP_WS_HEADERS
        ]
        subprotocols = list(scope.get("subprotocols") or [])
        try:
            upstream = await connect(
                self._upgrade_url(scope),
                additional_headers=headers,
                subprotocols=subprotocols or None,
                # An HTTP(S)_PROXY in the environment would otherwise be honoured for a loopback
                # connection, which turns a local hop into an external one.
                proxy=None,
                open_timeout=10,
                # Keepalive belongs on the leg that crosses Wi-Fi, which uvicorn owns below. On
                # loopback a dead peer sends a real FIN/RST, so a ping here only risks starving on
                # a busy loop and dropping a healthy socket.
                ping_interval=None,
                max_size=_WS_MAX_BYTES,
            )
        except (InvalidStatus, OSError, asyncio.TimeoutError) as exc:
            _log.warning("hermes-remote listener: upstream WS refused: %s", exc)
            await send({"type": "websocket.close", "code": 1011})
            return

        await send(
            {
                "type": "websocket.accept",
                "subprotocol": upstream.subprotocol,
                # The upstream's own accepted subprotocol, never the client's request: the
                # dashboard selects one deliberately, and echoing the client's would hand back a
                # ticket-bearing protocol value it took care not to reflect.
            }
        )

        async def phone_to_dashboard() -> None:
            while True:
                event = await receive()
                kind = event["type"]
                if kind == "websocket.disconnect":
                    return
                if kind != "websocket.receive":
                    continue
                if event.get("bytes") is not None:
                    await upstream.send(event["bytes"])
                elif event.get("text") is not None:
                    await upstream.send(event["text"])

        async def dashboard_to_phone() -> None:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await send({"type": "websocket.send", "bytes": frame})
                else:
                    await send({"type": "websocket.send", "text": frame})

        key, live = _register_live(device.id)
        pumps = [
            asyncio.create_task(phone_to_dashboard()),
            asyncio.create_task(dashboard_to_phone()),
            # Racing the pumps rather than polling inside them: a socket that is only receiving
            # heartbeats is exactly the one revocation has to reach, and it wakes no pump for
            # minutes at a time.
            asyncio.create_task(live.revoked.wait()),
        ]
        try:
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, ConnectionClosed):
                    _log.debug("hermes-remote listener: WS pump ended: %s", exc)
        finally:
            _live.pop(key, None)
            await upstream.close()
            # 1008 tells the phone this was policy and not a network fault, which is the difference
            # between showing "Pair again" and retrying a reconnect that can only be refused.
            code = 1008 if live.revoked.is_set() else 1000
            # The phone may already be gone when the last close goes out; that is not an error.
            with contextlib.suppress(RuntimeError, OSError):
                await send({"type": "websocket.close", "code": code})


async def _send_error(send, rejected: _Rejected) -> None:
    body = json.dumps({"detail": rejected.detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": rejected.status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                # No CORS headers, ever: a page on the LAN must not be able to reach this from a
                # browser, and the bearer requirement forces a preflight that is never answered.
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


# ---- running it ------------------------------------------------------------


async def start(upstream_port: int) -> None:
    """Bind the TLS socket and serve, on the caller's event loop.

    Runs on the dashboard's own loop rather than a thread of its own: the loop is already there,
    a second one would need its own shutdown story, and everything this does is I/O bound.
    ``uvicorn.Server.serve()`` is deliberately not used — it installs signal handlers, and stealing
    SIGINT from the dashboard would break Ctrl-C. ``startup()`` alone is already serving.
    """
    global _server, _sweeper
    if state.running:
        return
    if not enabled():
        state.error = f"disabled by {ENV_ENABLED}"
        _log.info("hermes-remote listener: %s", state.error)
        return

    import uvicorn

    identity = hr_identity.ensure_identity()
    host, port = configured_host(), configured_port()
    proxy = ReverseProxy(upstream_port)
    config = uvicorn.Config(
        proxy,
        host=host,
        port=port,
        log_level="warning",
        lifespan="on",
        ssl_certfile=str(identity.cert_path),
        ssl_keyfile=str(identity.key_path),
        ws_max_size=_WS_MAX_BYTES,
        # This leg crosses Wi-Fi, where a phone that walks out of range leaves a half-open socket
        # no FIN ever closes. The ping is what notices.
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        # The peer is the phone, and nothing here trusts a header it sends about itself.
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    # What ``Server.serve()`` does before ``startup()``, minus ``capture_signals()`` — installing
    # signal handlers here would steal Ctrl-C from the dashboard that owns this process.
    if not config.loaded:
        config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    if server.should_exit:  # bind failed; uvicorn has already logged why
        state.error = f"could not bind {host}:{port}"
        _log.error("hermes-remote listener: %s", state.error)
        return

    _server = server
    _sweeper = asyncio.create_task(_sweep_loop())
    state.running = True
    state.host = host
    state.port = port
    state.upstream_port = upstream_port
    state.error = ""
    state.started_at = int(time.time())
    _write_runtime()
    _log.info(
        "hermes-remote listener: https://%s:%d -> 127.0.0.1:%d (cert %s)",
        host,
        port,
        upstream_port,
        identity.fingerprint_hex[:16],
    )


async def stop() -> None:
    """Close the socket and drop the runtime record. Safe to call when nothing is running."""
    global _server, _task, _sweeper
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    if _sweeper is not None:
        _sweeper.cancel()
        _sweeper = None
    if _server is not None:
        _server.should_exit = True
        try:
            await _server.shutdown()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise into the dashboard's own
            _log.debug("hermes-remote listener: shutdown: %s", exc)
        _server = None
    state.running = False
    _clear_runtime()


async def _wait_for_bound_port(timeout: float) -> Optional[int]:
    """The dashboard's actual port, once uvicorn has bound it.

    ``app.state.bound_port`` is set in ``_on_server_started``, which runs *after* the lifespan
    startup that got us here — so it does not exist yet and polling is the only way to see it. It
    matters rather than assuming 9119 because ``--port 0`` is a supported bind and lands somewhere
    the operator never chose.
    """
    from hermes_cli.web_server import app

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = getattr(app.state, "bound_port", None)
        if port:
            return int(port)
        await asyncio.sleep(0.05)
    return None


async def _deferred_start() -> None:
    try:
        port = await _wait_for_bound_port(30.0)
        if port is None:
            state.error = "dashboard never reported a bound port"
            _log.warning("hermes-remote listener: %s", state.error)
            return
        await start(port)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a listener that cannot start must not stop the dashboard
        state.error = f"{type(exc).__name__}: {exc}"
        _log.exception("hermes-remote listener: failed to start")


def schedule_start() -> None:
    """Arrange for the listener to come up once the dashboard has bound its port.

    Called from the plugin router's ``startup`` event, which fires inside ``server.startup()`` —
    too early to read the port, so the real work is a task that waits for it.
    """
    global _task
    if _task is not None and not _task.done():
        return
    if not enabled():
        _log.info("hermes-remote listener: disabled by %s", ENV_ENABLED)
        return
    if "hermes_cli.web_server" not in sys.modules:
        # There is no dashboard in this process, so there is nothing to proxy. This is the
        # condition that keeps a bare ``include_router`` in a test — or in any other host that
        # mounts the plugin's router — from opening a LAN socket as a side effect.
        _log.debug("hermes-remote listener: no dashboard in this process; not starting")
        return
    _task = asyncio.create_task(_deferred_start())
