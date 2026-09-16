"""Working out what credential a ``/api/ws`` upgrade needs, in whichever mode the dashboard is in.

``_ws_auth_reason`` (``hermes_cli/web_server_chat.py``) accepts exactly one of three shapes, and
which one depends on ``app.state.auth_required``:

* **gated** (a non-loopback bind with an auth provider) — ``?ticket=`` (single use, 30 s) or
  ``?internal=``. The legacy session token is rejected outright.
* **loopback** — ``?token=<session token>`` and nothing else.

The consequence for the phone is the part worth remembering: in loopback mode there is no
short-lived credential to hand out. The session token is the dashboard's master key for the whole
process lifetime, so it must never leave the machine; the TLS listener attaches it on the phone's
behalf when it proxies the upgrade. In gated mode a ticket is safe to hand out, because it is what
the browser SPA already gets and it dies in 30 seconds.

Both branches assume the upgrade reaches the dashboard from loopback, which is what the listener
does. ``_ws_client_reason`` rejects a non-loopback peer outright in loopback mode.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

try:  # package import (``hermes_plugins.hermes_odyssey``)
    from . import hr_routes
    from .hr_provider import PROVIDER_NAME
except ImportError:  # standalone path load
    import hr_routes  # type: ignore[no-redef]
    from hr_provider import PROVIDER_NAME  # type: ignore[no-redef]

#: Kept here as well as in ``hr_routes`` because the two hosts read it from this module.
GATEWAY_WS_PATH = hr_routes.GATEWAY_WS_PATH

#: What the phone is told to do with the credential it was (or was not) given. Informational:
#: the phone appends ``?ticket=`` when it is handed one and otherwise opens the bare path, so a
#: mode it has never heard of costs it nothing.
MODE_GATED = "gated"
MODE_LOOPBACK = "loopback"
#: The gateway host (``hr_gateway_host``): no dashboard at all, the listener terminates the
#: socket itself, and the bearer on the upgrade is the whole credential.
MODE_DIRECT = "direct"


def auth_required() -> bool:
    """True when the dashboard is running its gated (cookie/ticket) auth mode."""
    try:
        from hermes_cli.web_server import app

        return bool(getattr(app.state, "auth_required", False))
    except Exception:
        # No dashboard app in this process (gateway, CLI, tests). Nothing here can open a WS
        # anyway, and treating an unknown mode as gated is the fail-closed answer.
        return True


def mode() -> str:
    """``"gated"`` or ``"loopback"``."""
    return MODE_GATED if auth_required() else MODE_LOOPBACK


def mint_client_ticket(*, user_id: str) -> Optional[Tuple[str, int]]:
    """A single-use WS ticket for the phone, or ``None`` in loopback mode.

    Returns ``(ticket, ttl_seconds)``. ``None`` is not a failure: it is the loopback branch, where
    the listener supplies the credential instead. Callers must have authenticated the device
    before calling — this mints, it does not check.
    """
    if not auth_required():
        return None
    from hermes_cli.dashboard_auth.ws_tickets import TTL_SECONDS, mint_ticket

    return mint_ticket(user_id=user_id, provider=PROVIDER_NAME), TTL_SECONDS


def listener_upgrade_query() -> Dict[str, str]:
    """Query parameters the TLS listener adds when it proxies an upgrade to loopback ``/api/ws``.

    Server-side only. In loopback mode this returns the dashboard session token, which is a
    process-lifetime master credential — it must never be written to a response, a log, or a QR
    code.
    """
    if auth_required():
        # Gated mode: the phone carries its own ticket through, so the listener adds nothing.
        return {}
    from hermes_cli.web_server import _SESSION_TOKEN

    return {"token": _SESSION_TOKEN}
