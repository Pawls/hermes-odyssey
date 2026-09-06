"""HermesRemote — the desktop half of a phone client for a live Hermes session.

Registered here:

1. :class:`~hr_provider.HermesRemoteDeviceProvider` on the dashboard's bearer-token seam, so a
   paired phone's token is recognised process-wide.
2. This plugin's own API paths as token routes, so the phone's bearer clears the dashboard's
   gate instead of being bounced to ``/login`` (gated mode) or 401'd for want of a session
   token (loopback mode).

The routes themselves live in ``dashboard/manifest.json`` + ``dashboard/api.py``, which the
dashboard imports separately — a plugin router is mounted at ``/api/plugins/hermes-remote`` only
when the plugin's name is in ``plugins.enabled`` (GHSA-mcfc-hp25-cjv7). ``register`` runs in every
Hermes process (``discover_plugins()`` precedes ``start_server``); the seam calls below are
harmless where no dashboard exists, and the module-level imports are deliberately cheap so plugin
load does not pull FastAPI into the CLI.

Nothing here touches core. Everything is the documented ``register(ctx)`` surface.
"""

from __future__ import annotations

import logging

from . import hr_routes
from .hr_provider import HermesRemoteDeviceProvider

logger = logging.getLogger(__name__)

_TAG = "hermes-remote"

#: Set when registration declines or fails, for ``hermes remote status`` to report.
LAST_SKIP_REASON: str = ""


def _register_token_routes() -> None:
    """Opt this plugin's paths into the bearer-token seam.

    Failure is logged, not raised: a plugin that cannot reach the seam should still load, and the
    consequence is a 401 the operator can see, not a dashboard that will not start.
    """
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    for path in hr_routes.TOKEN_ROUTES:
        register_token_route(path)


def register(ctx) -> None:
    """Plugin entry point."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    try:
        ctx.register_dashboard_auth_provider(HermesRemoteDeviceProvider())
    except Exception as exc:  # noqa: BLE001 — a failed registration must not abort plugin load
        LAST_SKIP_REASON = f"dashboard-auth provider registration failed: {exc}"
        logger.warning("%s: %s", _TAG, LAST_SKIP_REASON)
        return

    try:
        _register_token_routes()
    except Exception as exc:  # noqa: BLE001 — same reasoning; the routes then 401 visibly
        LAST_SKIP_REASON = f"token route registration failed: {exc}"
        logger.warning("%s: %s", _TAG, LAST_SKIP_REASON)
        return

    logger.info(
        "%s: registered device auth provider and %d token route(s): %s",
        _TAG,
        len(hr_routes.TOKEN_ROUTES),
        ", ".join(hr_routes.TOKEN_ROUTES),
    )
