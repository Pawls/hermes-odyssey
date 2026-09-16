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
sockets whose device has gone. Without it ``hermes talaria revoke`` ended a device's *requests* and
left its open session running, which a phone's heartbeat then kept alive indefinitely.

The bearer *is* forwarded upstream, because ``/api/plugins/hermes-talaria/…`` needs it to clear that
second gate. The dashboard session token travels the other way and only in loopback mode: it is
attached to the WebSocket upgrade query by :func:`hr_wsauth.listener_upgrade_query`, server side,
and never appears in a response, a log or a QR code.

One client cannot put a bearer on its upgrade: the Ink TUI in attach mode opens ``/api/ws`` with
Node's own ``WebSocket``, which takes a URL and nothing else. ``hermes talaria attach`` therefore
pairs the terminal as a device of its own and carries that token in the upgrade query
(:data:`WS_DEVICE_QUERY`), which :func:`_authenticate` accepts on WebSocket upgrades only and
:meth:`ReverseProxy._upgrade_url` strips before the upstream sees the query.
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
# imports ``hermes_cli.dashboard_auth`` and through it FastAPI. ``hermes talaria`` reads this
# module's configuration in the plain CLI process, and that cost would land on every invocation.
try:  # package import (``hermes_plugins.hermes_talaria``)
    from . import hr_devices, hr_identity, hr_paths
except ImportError:  # standalone path load from dashboard/api.py
    import hr_devices  # type: ignore[no-redef]
    import hr_identity  # type: ignore[no-redef]
    import hr_paths  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

#: Nothing standard claims it, and it is stable so a paired phone keeps working across restarts
#: without re-scanning.
DEFAULT_PORT = 9443

ENV_PORT = "HERMES_TALARIA_PORT"
ENV_HOST = "HERMES_TALARIA_HOST"
#: Set to ``0``/``off``/``false``/``no`` to load the plugin without opening a LAN socket.
ENV_ENABLED = "HERMES_TALARIA_LISTENER"

#: Where a running listener records itself, so ``hermes talaria status`` in another process can say
#: something true. Removed on clean shutdown; a stale one is detected by probing the port.
RUNTIME_FILENAME = "listener.json"

#: Query parameter a WebSocket upgrade may carry a device token in, for the one client that cannot
#: set a header (the attached TUI). Honoured on upgrades only; HTTP requests keep the bearer rule.
WS_DEVICE_QUERY = "device"

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

#: How often a live socket's device is re-checked against the store. ``hermes talaria revoke`` runs
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


def claim_path() -> Path:
    return hr_paths.state_dir() / CLAIM_FILENAME


# ---- which process hosts the port ------------------------------------------
#
# Three Hermes processes arm this listener. Two mount the dashboard router and proxy to it:
# ``hermes dashboard`` on 9119, and the desktop app's headless ``hermes serve --port 0``
# (``web_server.py`` mounts plugin routers unconditionally). The third is the always-on
# ``hermes gateway run``, which serves no HTTP of its own, so there the listener terminates
# ``/api/ws`` itself (:mod:`hr_gateway_host`). Only one can bind 9443, and it matters which,
# because a phone can stream into a live session only from inside the process that owns it
# (``session_transports.py`` fans events out across transports of one process and nothing crosses
# to another). So the host must be the process whose window Paul is looking at, and the gateway
# only when no window is open. The desktop app outranks the dashboard, which outranks the gateway.
# The rule is enforced with two files: the runtime record says who holds the port, and a claim
# says a better candidate wants it. The holder yields on the next sweep, the claimant binds on its
# next retry, and a phone's reconnect does the rest.

SURFACE_DESKTOP = "desktop"
SURFACE_DASHBOARD = "dashboard"
SURFACE_GATEWAY = "gateway"
_PRECEDENCE = {SURFACE_DESKTOP: 2, SURFACE_DASHBOARD: 1, SURFACE_GATEWAY: 0}

#: Written by a candidate that outranks the current holder. Cleared once it has bound.
CLAIM_FILENAME = "listener-claim.json"

#: How often a candidate that is not hosting retries the bind, and how often a host checks for a
#: claim. Also the upper bound on the handover a phone sees when the desktop app opens or closes.
_HOST_RETRY_SECONDS = 3.0


def this_surface() -> str:
    """Which Hermes process this is, for the host rule.

    The desktop's backend is the only one launched with ``HERMES_SERVE_HEADLESS=1``
    (``hermes_cli/main.py::_dashboard_sanitize_desktop_env``); ``hermes dashboard`` imports the
    web server without it; anything else is treated as the lowest rank. That "anything else" is
    the gateway when :mod:`hr_gateway_host` armed the listener, and nothing when it did not -
    ranking is not the gate on opening a socket, arming is.
    """
    if os.environ.get("HERMES_SERVE_HEADLESS", "").strip() == "1":
        return SURFACE_DESKTOP
    if "hermes_cli.web_server" in sys.modules:
        return SURFACE_DASHBOARD
    return SURFACE_GATEWAY


def outranks(candidate: str, holder: str) -> bool:
    """Whether ``candidate`` should host instead of ``holder``. Equal rank never displaces."""
    return _PRECEDENCE.get(candidate, -1) > _PRECEDENCE.get(holder, -1)


def pid_alive(pid: Any) -> bool:
    """Whether ``pid`` names a running process. A record from a dead process is noise, not a holder."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # ``os.kill(pid, 0)`` is *not* a probe on Windows: it calls TerminateProcess.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``record`` if it was written by a process that is still running and is not this one."""
    if not record or int(record.get("pid") or 0) == os.getpid() or not pid_alive(record.get("pid")):
        return None
    return record


def host_decision(surface: str, holder: Optional[Dict[str, Any]]) -> str:
    """What a candidate that is not hosting should do, given the live holder record (or None).

    ``"bind"``: nobody live holds the port, try it. ``"claim"``: a lower-ranked process holds it,
    write a claim and try anyway (it yields within a sweep). ``"wait"``: the holder ranks at least
    as high, so leave it alone and retry later.
    """
    if holder is None:
        return "bind"
    if int(holder.get("port") or 0) != configured_port():
        return "bind"  # it holds some other port; ours is not spoken for
    holder_surface = str(holder.get("surface") or "")
    if not holder_surface:
        return "wait"  # a pre-upgrade listener never says what it is; do not displace what you cannot rank
    return "claim" if outranks(surface, holder_surface) else "wait"


def should_yield(surface: str, claim: Optional[Dict[str, Any]]) -> bool:
    """Whether a host should release the port for the live claimant."""
    return claim is not None and outranks(str(claim.get("surface") or ""), surface)


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
        _log.warning("hermes-talaria listener: revocation sweep skipped: %s", exc)
        return
    for handle in list(_live.values()):
        if handle.device_id not in paired and not handle.revoked.is_set():
            _log.info(
                "hermes-talaria listener: closing live socket for revoked device %s",
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
            _log.debug("hermes-talaria listener: revocation sweep failed: %s", exc)


def _write_runtime() -> None:
    """Record the live endpoint for other processes. Best effort: it is a convenience for
    ``hermes talaria status``, and failing to write it must not stop the listener serving."""
    try:
        path = runtime_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "surface": this_surface(),
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
        _log.debug("hermes-talaria: could not write %s: %s", RUNTIME_FILENAME, exc)


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


def live_runtime() -> Optional[Dict[str, Any]]:
    """The runtime record, but only if the process that wrote it is still running.

    A host that dies by ``os._exit`` (the gateway's shutdown path) or a crash leaves its record
    behind; the host rule already ignores such a record, and this is the same answer for the CLI.
    """
    record = read_runtime()
    if not record or not pid_alive(record.get("pid")):
        return None
    return record


def _write_claim() -> None:
    try:
        path = claim_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "surface": this_surface(), "at": int(time.time())})
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        _log.debug("hermes-talaria: could not write %s: %s", CLAIM_FILENAME, exc)


def _clear_claim(only_mine: bool = True) -> None:
    try:
        if only_mine and int((read_claim() or {}).get("pid") or 0) != os.getpid():
            return
        claim_path().unlink(missing_ok=True)
    except OSError:
        pass


def read_claim() -> Optional[Dict[str, Any]]:
    """A candidate's request for the port, or ``None``. Check its pid before believing it."""
    try:
        return json.loads(claim_path().read_text(encoding="utf-8"))
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


def _query_device(scope: Dict[str, Any]) -> str:
    from urllib.parse import parse_qs

    values = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(WS_DEVICE_QUERY) or []
    return values[0].strip() if len(values) == 1 else ""


def _credential(scope: Dict[str, Any]) -> str:
    """The device token this request presents: the bearer header, or on a WebSocket upgrade with
    no bearer, the :data:`WS_DEVICE_QUERY` parameter. Never both, and never the query on HTTP."""
    token = _bearer(scope.get("headers") or [])
    if not token and scope.get("type") == "websocket":
        token = _query_device(scope)
    return token


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
        device = hr_devices.verify_token(_credential(scope))
    except hr_devices.DeviceStoreUnavailable as exc:
        _log.warning("hermes-talaria listener: device store unavailable: %s", exc)
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
            _log.warning("hermes-talaria listener: upstream %s failed: %s", url, exc)
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
        # The attached TUI's own credential, already checked by ``_authenticate``. It is this
        # listener's secret, not the dashboard's, and a token seam upstream must never see it.
        params = [(k, v) for k, v in params if k != WS_DEVICE_QUERY]
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
            _log.info("hermes-talaria listener: WS upgrade refused (%s)", rejected.detail)
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
            _log.warning("hermes-talaria listener: upstream WS refused: %s", exc)
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
                    _log.debug("hermes-talaria listener: WS pump ended: %s", exc)
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


async def start(upstream_port: int, *, quiet: bool = False, app: Any = None) -> bool:
    """Bind the TLS socket and serve, on the caller's event loop. True once serving.

    Runs on the dashboard's own loop rather than a thread of its own: the loop is already there,
    a second one would need its own shutdown story, and everything this does is I/O bound.
    ``uvicorn.Server.serve()`` is deliberately not used — it installs signal handlers, and stealing
    SIGINT from the dashboard would break Ctrl-C. ``startup()`` alone is already serving.
    ``quiet`` makes a failed bind a debug line: the host loop retries every few seconds while
    another process holds the port, and that is expected, not an error.

    ``app`` is what answers behind the TLS socket: by default a :class:`ReverseProxy` to the
    dashboard on ``upstream_port``; the gateway host passes its own in-process app and
    ``upstream_port=0``, since there is nothing to proxy to.
    """
    global _server, _sweeper
    if state.running:
        return True
    if not enabled():
        state.error = f"disabled by {ENV_ENABLED}"
        _log.info("hermes-talaria listener: %s", state.error)
        return False

    import uvicorn

    identity = hr_identity.ensure_identity()
    host, port = configured_host(), configured_port()
    proxy = ReverseProxy(upstream_port) if app is None else app
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
    # A failed bind is a ``SystemExit`` from inside uvicorn's ``startup()`` (it logs, shuts the
    # lifespan down and calls ``sys.exit(1)``); older versions only set ``should_exit``. Neither
    # may leave here, because this runs as a task on the dashboard's own loop.
    bind_failed = False
    try:
        await server.startup()
    except SystemExit:
        bind_failed = True
    if bind_failed or server.should_exit:  # uvicorn has already logged why
        state.error = f"could not bind {host}:{port}"
        _log.log(logging.DEBUG if quiet else logging.ERROR, "hermes-talaria listener: %s", state.error)
        return False

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
        "hermes-talaria listener: https://%s:%d -> %s as %s (cert %s)",
        host,
        port,
        f"127.0.0.1:{upstream_port}" if upstream_port else "in-process",
        this_surface(),
        identity.fingerprint_hex[:16],
    )
    return True


async def _release() -> None:
    """Close the socket and drop the runtime record, leaving the host loop to run on."""
    global _server, _sweeper
    if _sweeper is not None:
        _sweeper.cancel()
        _sweeper = None
    if _server is not None:
        _server.should_exit = True
        try:
            await _server.shutdown()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise into the dashboard's own
            _log.debug("hermes-talaria listener: shutdown: %s", exc)
        _server = None
    state.running = False
    _clear_runtime()


async def stop() -> None:
    """Close the socket, drop the runtime record and the host loop. Safe when nothing is running."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    await _release()
    _clear_claim()


async def host_tick(upstream_port: int, app: Any = None) -> str:
    """One step of the host rule; the word returned says what it did, for logs and tests.

    Not hosting: ``wait`` behind a live holder that ranks at least as high, otherwise ``claim``
    (a lower-ranked holder, told to yield) or ``bind`` — and either of those becomes ``bound``
    when the bind succeeds. Hosting: ``yield`` when a live higher-ranked claimant wants the
    port, else ``hold``. ``app`` is passed through to :func:`start`.
    """
    surface = this_surface()
    if state.running:
        claim = _live_record(read_claim())
        if should_yield(surface, claim):
            await _release()
            state.error = f"yielded {state.port or configured_port()} to {claim['surface']} (pid {claim['pid']})"
            _log.info("hermes-talaria listener: %s", state.error)
            return "yield"
        return "hold"
    holder = _live_record(read_runtime())
    decision = host_decision(surface, holder)
    if decision == "wait":
        state.error = f"hosted by {holder['surface']} (pid {holder['pid']})"
        return decision
    if decision == "claim":
        _write_claim()
    if await start(upstream_port, quiet=holder is not None, app=app):
        _clear_claim()
        return "bound"
    return decision


async def _host_loop(upstream_port: int, app: Any = None) -> None:
    """Keep :func:`host_tick` running for the life of the process, logging only transitions."""
    last = ""
    while True:
        try:
            outcome = await host_tick(upstream_port, app)
            if outcome in {"wait", "claim", "bind"} and state.error and state.error != last:
                _log.info("hermes-talaria listener: %s; retrying every %.0fs", state.error, _HOST_RETRY_SECONDS)
            last = state.error if outcome in {"wait", "claim", "bind"} else ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad tick must not end the loop
            state.error = f"{type(exc).__name__}: {exc}"
            _log.exception("hermes-talaria listener: host tick failed")
        await asyncio.sleep(_HOST_RETRY_SECONDS)


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
            _log.warning("hermes-talaria listener: %s", state.error)
            return
        await _host_loop(port)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a listener that cannot start must not stop the dashboard
        state.error = f"{type(exc).__name__}: {exc}"
        _log.exception("hermes-talaria listener: failed to start")


def schedule_start() -> None:
    """Arrange for the listener to come up once the dashboard has bound its port.

    Called from the plugin router's ``startup`` event, which fires inside ``server.startup()`` —
    too early to read the port, so the real work is a task that waits for it.
    """
    global _task
    if _task is not None and not _task.done():
        return
    if not enabled():
        _log.info("hermes-talaria listener: disabled by %s", ENV_ENABLED)
        return
    if "hermes_cli.web_server" not in sys.modules:
        # There is no dashboard in this process, so there is nothing to proxy. This is the
        # condition that keeps a bare ``include_router`` in a test — or in any other host that
        # mounts the plugin's router — from opening a LAN socket as a side effect.
        _log.debug("hermes-talaria listener: no dashboard in this process; not starting")
        return
    _task = asyncio.create_task(_deferred_start())
