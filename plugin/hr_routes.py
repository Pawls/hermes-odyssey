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
PLUGIN_NAME = "hermes-remote"

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

#: Every path the token seam must recognise, absolute.
TOKEN_ROUTES: tuple[str, ...] = (
    f"{API_PREFIX}{ROUTE_HEALTH}",
    f"{API_PREFIX}{ROUTE_WS_TICKET}",
)
