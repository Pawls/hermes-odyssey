"""The plugin's HTTP surface, named in one place.

``register(ctx)`` opts these exact paths into the bearer-token seam and ``dashboard/api.py``
serves them; ``token_auth.is_token_route`` matches exactly, so a path spelled differently in the
two files would be served without a gate in loopback mode and 401 in gated mode. Keeping the
strings here makes that impossible.

The prefix is fixed by the dashboard, not chosen here: ``_mount_plugin_api_routes`` mounts every
plugin router at ``/api/plugins/<manifest name>``.
"""

from __future__ import annotations

#: Must match ``name`` in both ``plugin.yaml`` and ``dashboard/manifest.json``, and the entry in
#: ``plugins.enabled``. The dashboard gates the router on all three agreeing.
PLUGIN_NAME = "hermes-odyssey"

#: Reported on ``/health`` so the phone can refuse a desktop half it is too old to talk to.
#: Keep in step with ``version`` in ``plugin.yaml`` and ``dashboard/manifest.json``;
#: ``tests/test_manifests.py`` fails when they drift.
PLUGIN_VERSION = "0.1.0"

API_PREFIX = f"/api/plugins/{PLUGIN_NAME}"

#: Liveness plus "which device am I". Bearer-gated like everything else: an unauthenticated
#: prober learns nothing, not even that the plugin is loaded.
ROUTE_HEALTH = "/health"

#: Hands the phone whatever credential its ``/api/ws`` upgrade needs in the current mode.
ROUTE_WS_TICKET = "/ws-ticket"

#: A page of a session's transcript (``hr_history``). The session is a query parameter, not a path
#: segment, because the token seam matches paths exactly and cannot register a template.
ROUTE_MESSAGES = "/messages"

#: One image an agent wrote during a session (``hr_media``), by path. Query parameters for the same
#: reason ``/messages`` uses them, and because a path as a path segment would be re-normalised by
#: whichever server saw it first.
ROUTE_MEDIA = "/media"

#: Every path the token seam must recognise, absolute.
TOKEN_ROUTES: tuple[str, ...] = (
    f"{API_PREFIX}{ROUTE_HEALTH}",
    f"{API_PREFIX}{ROUTE_WS_TICKET}",
    f"{API_PREFIX}{ROUTE_MESSAGES}",
    f"{API_PREFIX}{ROUTE_MEDIA}",
)

#: Path of the JSON-RPC gateway socket, the same on every host: the dashboard mounts it there and
#: the gateway host answers it there. Lives here rather than in ``hr_wsauth`` because the plain
#: CLI (``hermes odyssey attach``) needs it without pulling FastAPI in.
GATEWAY_WS_PATH = "/api/ws"


# The response bodies, built here so the two hosts that answer these routes - the dashboard's
# router (``dashboard/api.py``) and the gateway's in-process app (``hr_gateway_host``) - cannot
# drift apart in a field the phone reads.


def health_payload(*, device_id: str, device_label: str, mode: str) -> dict:
    """``/health``: liveness plus the caller's own identity and the WS credential mode."""
    return {
        "ok": True,
        "plugin": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "mode": mode,
        "device": {"id": device_id, "label": device_label},
    }


def ws_ticket_payload(*, mode: str, ws_path: str, ticket, expires_in) -> dict:
    """``/ws-ticket``: what to put on the ``/api/ws`` upgrade. ``ticket`` is null whenever the
    listener itself vouches for the socket, which is every mode but gated."""
    return {"mode": mode, "ws_path": ws_path, "ticket": ticket, "expires_in": expires_in}
