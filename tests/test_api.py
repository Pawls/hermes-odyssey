"""The plugin's router, mounted the way the dashboard mounts it.

A bare FastAPI app with ``include_router(..., prefix="/api/plugins/hermes-remote")`` reproduces
:func:`hermes_cli.web_server_dashboard._mount_plugin_api_routes` without starting a dashboard. The
token-auth seam is *not* installed here, which is the point of several of these tests: the routes
must refuse an unauthenticated caller on their own, because in loopback mode a route that trusted
the seam blindly and lost its registration would be wide open.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/plugins/hermes-remote"


@pytest.fixture()
def client(api, store_path, monkeypatch):
    """A client whose device store is this test's tmp file, in loopback mode by default."""
    monkeypatch.setattr(api.hr_wsauth, "auth_required", lambda: False)
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture()
def paired(hr_devices, store_path):
    """One paired device; returns its token."""
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- the gate ---------------------------------------------------------------


@pytest.mark.parametrize("path,method", [("/health", "get"), ("/ws-ticket", "post")])
def test_no_bearer_is_unauthorized(client, path, method):
    assert getattr(client, method)(PREFIX + path).status_code == 401


@pytest.mark.parametrize("path,method", [("/health", "get"), ("/ws-ticket", "post")])
def test_an_unknown_bearer_is_unauthorized(client, path, method):
    response = getattr(client, method)(PREFIX + path, headers=_auth("hr1.aaaaaaaaaaaa.nope"))
    assert response.status_code == 401


def test_a_revoked_device_is_unauthorized(client, hr_devices, store_path, paired):
    device_id = paired.split(".")[1]
    hr_devices.revoke_device(device_id, store_path)
    assert client.get(PREFIX + "/health", headers=_auth(paired)).status_code == 401


def test_another_providers_principal_cannot_use_these_routes(api, hr_devices, store_path, paired):
    """A drain secret, or any other token provider's bearer, is not a paired phone.

    The seam authenticates any registered provider's token on a registered route, so provenance —
    not merely "the seam let it through" — is what these routes require.
    """
    from hermes_cli.dashboard_auth.base import TokenPrincipal

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.token_principal = TokenPrincipal(principal="drain-control", provider="drain-secret")
        request.state.token_authenticated = True
        return await call_next(request)

    app.include_router(api.router, prefix=PREFIX)
    with TestClient(app) as foreign_client:
        assert foreign_client.get(PREFIX + "/health", headers=_auth(paired)).status_code == 401


def test_an_unreadable_store_is_503_not_401(client, store_path, paired):
    """503 says "nothing was checked"; 401 would tell the phone its token is wrong."""
    store_path.write_text("{ truncated", encoding="utf-8")
    assert client.get(PREFIX + "/health", headers=_auth(paired)).status_code == 503


# ---- /health ----------------------------------------------------------------


def test_health_names_the_calling_device(client, paired):
    body = client.get(PREFIX + "/health", headers=_auth(paired)).json()
    assert body["ok"] is True
    assert body["plugin"] == "hermes-remote"
    assert body["device"]["id"] == paired.split(".")[1]
    assert body["device"]["label"] == "Pixel 8"
    assert body["mode"] == "loopback"


# ---- /ws-ticket -------------------------------------------------------------


def test_loopback_mode_hands_out_no_credential(client, paired):
    """The only credential the loopback gate accepts is the session token, which must not leave
    the machine — the TLS listener attaches it instead."""
    body = client.post(PREFIX + "/ws-ticket", headers=_auth(paired)).json()
    assert body["mode"] == "loopback"
    assert body["ticket"] is None
    assert body["expires_in"] is None
    assert body["ws_path"] == "/api/ws"


def test_gated_mode_mints_a_single_use_ticket(api, client, paired, monkeypatch):
    from hermes_cli.dashboard_auth import ws_tickets

    monkeypatch.setattr(api.hr_wsauth, "auth_required", lambda: True)
    body = client.post(PREFIX + "/ws-ticket", headers=_auth(paired)).json()

    assert body["mode"] == "gated"
    assert body["expires_in"] == ws_tickets.TTL_SECONDS
    ticket = body["ticket"]
    assert ticket

    info = ws_tickets.consume_ticket(ticket)
    assert info["user_id"] == f"device:{paired.split('.')[1]}"
    assert info["provider"] == "hermes-remote-device"
    with pytest.raises(ws_tickets.TicketInvalid):
        ws_tickets.consume_ticket(ticket)


# ---- the listener's lifetime -------------------------------------------------


def test_the_routers_startup_hook_survives_a_host_app_with_its_own_lifespan(api):
    """This is how the TLS listener starts, and the mechanism is not obvious.

    The dashboard builds its app as ``FastAPI(..., lifespan=_lifespan)``. A custom lifespan means
    the app router's own ``on_startup`` list is never run — so the handlers ``include_router``
    copies onto the app are inert. What does run is the plugin router's *own* lifespan context,
    which ``include_router`` merges into the app's. Take that merge away and the listener silently
    never comes up, with no error anywhere.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ran = []

    @asynccontextmanager
    async def host_lifespan(_app):
        ran.append("host-start")
        yield
        ran.append("host-stop")

    app = FastAPI(lifespan=host_lifespan)
    app.include_router(api.router, prefix=PREFIX)
    app.router.on_startup.clear()  # what a custom lifespan effectively does to them
    app.router.on_shutdown.clear()

    with TestClient(app):
        pass

    assert ran == ["host-start", "host-stop"]
    assert api.router.on_startup and api.router.on_shutdown


def test_mounting_the_router_does_not_open_a_socket(api, monkeypatch):
    """A plugin router can be mounted by anything. Only a process that has the dashboard in it
    has something to proxy, and only that process may bind a LAN port."""
    import sys

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv(api.hr_listener.ENV_ENABLED, raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)

    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    with TestClient(app):
        pass

    assert api.hr_listener._task is None
    assert api.hr_listener.state.running is False


def test_responses_carry_only_their_declared_fields(client, paired):
    """Pinning the schema is what stops the session token being added to a response later.

    ``listener_upgrade_query`` returns that token for the TLS listener's own use; nothing in the
    router may ever put it on the wire, so both response shapes are frozen here.
    """
    health = client.get(PREFIX + "/health", headers=_auth(paired)).json()
    assert set(health) == {"ok", "plugin", "version", "mode", "device"}
    assert set(health["device"]) == {"id", "label"}

    ticket = client.post(PREFIX + "/ws-ticket", headers=_auth(paired)).json()
    assert set(ticket) == {"mode", "ws_path", "ticket", "expires_in"}
