"""Hosting the listener inside ``hermes gateway run``, for when no window is open.

With neither the desktop app nor ``hermes dashboard`` running, nothing mounts the dashboard router,
so nothing arms :mod:`hr_listener` and the phone cannot start a session. The gateway is always on
here, but it serves no HTTP at all, so there is nothing to reverse proxy to. Instead the listener's
TLS socket is answered in-process by :class:`GatewayHost`: the two plugin routes are answered
directly, and ``/api/ws`` is terminated by handing a Starlette ``WebSocket`` built from the bare
ASGI scope to ``tui_gateway.ws.handle_ws`` - the same handler the dashboard and the desktop app
mount, which needs only ``accept``, ``receive_text``, ``send_text`` and ``close`` on it.
``server.dispatch`` runs in any process that has imported ``tui_gateway.server``, and the gateway
already does that for its Group Chat worker, so the phone's RPCs take exactly the path they take
anywhere else.

Two things are deliberate about *where* this runs.

The socket lives on a thread and loop of its own rather than the gateway's. ``register(ctx)`` runs
during plugin discovery, which the gateway performs from synchronous startup code before its loop
is the one that will serve, so there is no loop to join at that moment; and nothing on the RPC path
needs the gateway's loop, because ``WSTransport`` binds to whichever loop accepted the socket and
marshals every write back onto it. A phone streaming tokens also should not compete with the
platform adapters for the same loop.

Arming is explicit, and refuses everything that is not a real gateway. ``register`` runs in every
Hermes process, so argv must be a ``gateway run`` by the gateway's own matcher, and the socket is
not opened until the gateway's PID file names this process. Two ``gateway run`` processes can be
alive at once (one left behind by ``hermes update``); only the one the PID file names ever hosts.

The gateway ranks below both windows in the host rule, so it yields the port the moment one opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional

try:  # package import (``hermes_plugins.hermes_remote``)
    from . import hr_listener, hr_routes, hr_wsauth
    from .hr_provider import PROVIDER_NAME
except ImportError:  # standalone path load
    import hr_listener  # type: ignore[no-redef]
    import hr_routes  # type: ignore[no-redef]
    import hr_wsauth  # type: ignore[no-redef]
    from hr_provider import PROVIDER_NAME  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

#: How long the host thread waits for the gateway to claim its PID file before concluding this
#: process is not going to be the gateway after all (a ``--replace`` loser, say).
_PID_CLAIM_TIMEOUT_SECONDS = 600.0
_PID_POLL_SECONDS = 3.0

_HEALTH_PATH = f"{hr_routes.API_PREFIX}{hr_routes.ROUTE_HEALTH}"
_WS_TICKET_PATH = f"{hr_routes.API_PREFIX}{hr_routes.ROUTE_WS_TICKET}"

#: The WebSocket handler. Resolved lazily because importing it imports ``tui_gateway.server``;
#: tests set it to a stub so a socket can be driven without a gateway in the process.
HANDLE_WS: Optional[Callable[..., Awaitable[None]]] = None


def _handler() -> Callable[..., Awaitable[None]]:
    if HANDLE_WS is not None:
        return HANDLE_WS
    from tui_gateway.ws import handle_ws

    return handle_ws


# ---- is this the gateway? ----------------------------------------------------


def is_gateway_process() -> bool:
    """Whether this process is a real ``hermes gateway run``, by the gateway's own argv matcher.

    Argv, not the ``_HERMES_GATEWAY`` environment marker: the gateway discovers plugins from
    ``hermes_cli.main`` before it imports ``gateway.run``, which is what sets the marker, so at
    ``register`` time it is absent in the one process that matters; and it is inherited by every
    child the gateway spawns, so it would be present in many that do not.
    """
    try:
        from gateway.status import looks_like_gateway_command_line
    except Exception:  # noqa: BLE001 - no gateway package, so not a gateway
        return False
    return bool(looks_like_gateway_command_line(" ".join(sys.argv)))


def _pid_file_names_this_process() -> bool:
    """Whether the gateway's PID file, verified against the live process, is this process.

    ``cleanup_stale=False``: reading must not unlink anything, that is the gateway's housekeeping.
    """
    try:
        from gateway.status import get_running_pid

        return get_running_pid(cleanup_stale=False) == os.getpid()
    except Exception as exc:  # noqa: BLE001 - treat an unreadable record as "not yet"
        _log.debug("hermes-remote gateway host: pid check failed: %s", exc)
        return False


async def _wait_for_pid_claim() -> bool:
    deadline = time.monotonic() + _PID_CLAIM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_pid_file_names_this_process):
            return True
        await asyncio.sleep(_PID_POLL_SECONDS)
    return False


# ---- the app behind the socket -------------------------------------------------


async def _drain(receive) -> None:
    """Consume a request body so the connection is left in a clean state for the next request."""
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            return


async def _send_json(send, status: int, body: Any) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload, "more_body": False})


class GatewayHost:
    """A bare ASGI app answering the plugin's routes and terminating ``/api/ws`` in-process.

    Bare ASGI for the same reason :class:`hr_listener.ReverseProxy` is: there are two routes and a
    socket, and every request is authenticated by the listener's own gate before anything else.
    """

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
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope, receive, send) -> None:
        try:
            device = hr_listener._authenticate(scope)
        except hr_listener._Rejected as rejected:
            await hr_listener._send_error(send, rejected)
            return
        await _drain(receive)
        path, method = scope["path"], scope["method"]
        if path == _HEALTH_PATH and method == "GET":
            body = hr_routes.health_payload(
                device_id=device.id, device_label=device.label, mode=hr_wsauth.MODE_DIRECT
            )
        elif path == _WS_TICKET_PATH and method == "POST":
            # No ticket: the bearer on the upgrade is the whole credential here, checked by the
            # same gate that just admitted this request.
            body = hr_routes.ws_ticket_payload(
                mode=hr_wsauth.MODE_DIRECT,
                ws_path=hr_wsauth.GATEWAY_WS_PATH,
                ticket=None,
                expires_in=None,
            )
        else:
            await hr_listener._send_error(send, hr_listener._Rejected(404, "not found"))
            return
        await _send_json(send, 200, body)

    async def _websocket(self, scope, receive, send) -> None:
        message = await receive()  # the ASGI "websocket.connect" that precedes any decision
        if message["type"] != "websocket.connect":
            return
        try:
            device = hr_listener._authenticate(scope)
        except hr_listener._Rejected as rejected:
            await send({"type": "websocket.close", "code": 1008})
            _log.info("hermes-remote gateway host: WS upgrade refused (%s)", rejected.detail)
            return
        if scope["path"] != hr_wsauth.GATEWAY_WS_PATH:
            await send({"type": "websocket.close", "code": 1008})
            return

        from starlette.websockets import WebSocket

        # Starlette's ``accept()`` waits for the ``websocket.connect`` that was consumed above
        # to decide whether to refuse, so it is handed back first.
        connect = [message]

        async def receive_replayed():
            return connect.pop() if connect else await receive()

        ws = WebSocket(scope, receive_replayed, send)
        # The identity the dashboard's ticket path would have stamped on the upgrade: the sole
        # identity authority downstream, and never something RPC params can supply.
        identity = {"user_id": f"device:{device.id}", "provider": PROVIDER_NAME}
        key, live = hr_listener._register_live(device.id)
        session = asyncio.create_task(_handler()(ws, auth_identity=identity))
        revoked = asyncio.create_task(live.revoked.wait())
        try:
            done, _pending = await asyncio.wait({session, revoked}, return_when=asyncio.FIRST_COMPLETED)
            if session in done:
                revoked.cancel()
                await asyncio.gather(revoked, return_exceptions=True)
                exc = session.exception()
                if exc is not None:
                    _log.debug("hermes-remote gateway host: WS session ended: %s", exc)
                return
            # Revoked mid-session. 1008 tells the phone this was policy, not a network fault. The
            # handler's pending receive then sees the disconnect and runs its own teardown, which
            # is what detaches the session cleanly rather than leaving it bound to a dead socket.
            with contextlib.suppress(Exception):
                await ws.close(code=1008)
            await asyncio.gather(session, return_exceptions=True)
        finally:
            hr_listener._live.pop(key, None)


# ---- arming it -----------------------------------------------------------------

_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_serving: Optional[asyncio.Task] = None


def armed() -> bool:
    return _thread is not None and _thread.is_alive()


def arm() -> bool:
    """Start hosting on a thread of its own. True when armed; False, with the reason logged, when
    this is not the gateway or the listener is switched off."""
    global _thread
    if armed():
        return True
    if not is_gateway_process():
        return False
    if not hr_listener.enabled():
        _log.info("hermes-remote gateway host: disabled by %s", hr_listener.ENV_ENABLED)
        return False
    _thread = threading.Thread(target=_run, name="hermes-remote-host", daemon=True)
    _thread.start()
    return True


def disarm(timeout: float = 5.0) -> None:
    """Release the port and end the thread. For the plugin's ``on_unload`` and tests."""
    global _thread
    loop, task, thread = _loop, _serving, _thread
    if loop is not None and task is not None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(task.cancel)
    if thread is not None:
        thread.join(timeout)
    _thread = None


def _run() -> None:
    global _loop
    loop = asyncio.new_event_loop()
    _loop = loop
    try:
        loop.run_until_complete(_serve())
    except Exception:  # noqa: BLE001 - the thread must end quietly, not take the gateway with it
        _log.exception("hermes-remote gateway host: stopped")
    finally:
        _loop = None
        loop.close()


async def _serve() -> None:
    global _serving
    _serving = asyncio.current_task()
    try:
        if not await _wait_for_pid_claim():
            hr_listener.state.error = "gateway never claimed its pid file; not hosting"
            _log.warning("hermes-remote gateway host: %s", hr_listener.state.error)
            return
        _log.info("hermes-remote gateway host: this is the gateway (pid %d); joining the host rule", os.getpid())
        await hr_listener._host_loop(0, GatewayHost())
    except asyncio.CancelledError:
        pass
    finally:
        _serving = None
        await hr_listener.stop()
