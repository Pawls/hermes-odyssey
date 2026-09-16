"""Odyssey — the desktop half of a phone client for a live Hermes session.

Registered here:

1. :class:`~hr_provider.OdysseyDeviceProvider` on the dashboard's bearer-token seam, so a
   paired phone's token is recognised process-wide.
2. This plugin's own API paths as token routes, so the phone's bearer clears the dashboard's
   gate instead of being bounced to ``/login`` (gated mode) or 401'd for want of a session
   token (loopback mode).
3. ``hermes odyssey pair | status | revoke`` (:mod:`hr_cli`), which is where a person mints a
   device token and reads the certificate fingerprint off a QR code.

The fourth piece, the TLS listener, is armed in two places and never from ``register`` as such.
``register`` runs in every Hermes process, so opening a socket from it unconditionally would open
one in the CLI too. Where a dashboard is mounted it is armed from ``dashboard/api.py`` — that file
is imported only by the dashboard's plugin mounter, which makes it the one reliable marker for the
process that owns port 9119. In the always-on gateway, which mounts nothing, :func:`register`
calls ``hr_gateway_host.arm``, which refuses every process that is not a real ``gateway run`` and
holds the port only while no window is open to hold it.

The routes themselves live in ``dashboard/manifest.json`` + ``dashboard/api.py``, which the
dashboard imports separately — a plugin router is mounted at ``/api/plugins/hermes-odyssey`` only
when the plugin's name is in ``plugins.enabled`` (GHSA-mcfc-hp25-cjv7). ``register`` runs in every
Hermes process (``discover_plugins()`` precedes ``start_server``); the seam calls below are
harmless where no dashboard exists, and the module-level imports are deliberately cheap so plugin
load does not pull FastAPI into the CLI.

Nothing here touches core. Everything is the documented ``register(ctx)`` surface.
"""

from __future__ import annotations

import logging

from . import hr_paths, hr_routes
from .hr_provider import OdysseyDeviceProvider

logger = logging.getLogger(__name__)

_TAG = "hermes-odyssey"

#: Set when registration declines or fails, for ``hermes odyssey status`` to report.
LAST_SKIP_REASON: str = ""


def _register_token_routes() -> None:
    """Opt this plugin's paths into the bearer-token seam.

    Failure is logged, not raised: a plugin that cannot reach the seam should still load, and the
    consequence is a 401 the operator can see, not a dashboard that will not start.
    """
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    for path in hr_routes.TOKEN_ROUTES:
        register_token_route(path)


def _register_cli(ctx) -> None:
    """Wire ``hermes odyssey pair|status|revoke``.

    ``hr_cli`` is imported here rather than at module scope so plugin load does not pay for it in
    the gateway and dashboard processes, which never run a CLI command.
    """
    from . import hr_cli

    ctx.register_cli_command(
        name=hr_cli.COMMAND_NAME,
        help=hr_cli.COMMAND_HELP,
        description=hr_cli.COMMAND_DESCRIPTION,
        setup_fn=hr_cli.setup,
        handler_fn=hr_cli.handler,
    )


def _arm_gateway_host(ctx) -> None:
    """Host the listener here when this process is the always-on ``hermes gateway run``.

    ``arm`` is a no-op everywhere else, by the gateway's own process matcher plus its PID file,
    so this is safe to call from every ``register``. The gateway ranks below both windows, so it
    holds the port only while neither is open.
    """
    from . import hr_gateway_host

    if hr_gateway_host.arm():
        ctx.on_unload(hr_gateway_host.disarm)
        logger.info("%s: hosting the listener in this gateway process", _TAG)


def register(ctx) -> None:
    """Plugin entry point."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    try:
        if hr_paths.migrate_legacy_state():
            logger.info("%s: moved state from the hermes-talaria directory to %s", _TAG, hr_paths.state_dir())
    except Exception as exc:  # noqa: BLE001 — an unmoved store reads as no devices, not a failed load
        logger.warning("%s: legacy state not moved: %s", _TAG, exc)

    try:
        ctx.register_dashboard_auth_provider(OdysseyDeviceProvider())
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

    try:
        _register_cli(ctx)
    except Exception as exc:  # noqa: BLE001 — the dashboard half still works without the CLI
        LAST_SKIP_REASON = f"CLI registration failed: {exc}"
        logger.warning("%s: %s", _TAG, LAST_SKIP_REASON)

    try:
        _arm_gateway_host(ctx)
    except Exception as exc:  # noqa: BLE001 — a gateway that cannot host is a gateway without a phone, not a broken one
        logger.warning("%s: gateway host not armed: %s", _TAG, exc)

    logger.info(
        "%s: registered device auth provider and %d token route(s): %s",
        _TAG,
        len(hr_routes.TOKEN_ROUTES),
        ", ".join(hr_routes.TOKEN_ROUTES),
    )
