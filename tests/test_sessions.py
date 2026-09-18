"""Renaming a stored session: the route that exists because ``session.title`` is live-scoped.

Every test writes a real ``SessionDB`` in the session's tmp ``HERMES_HOME`` and reads the title back
out of it, because the claim on trial is that a phone's rename and the desktop's rename are the same
write. The store's own rules - uniqueness, sanitisation, the length limit - are exercised through it
rather than restated here; what this module owns is the mapping from a refusal to a status code, and
the route being served identically on both hosts.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/plugins/hermes-odyssey"
ROUTE = f"{PREFIX}/session-title"


@pytest.fixture()
def hr_sessions(_plugin_on_path):
    return importlib.import_module("hr_sessions")


@pytest.fixture()
def db(_isolated_hermes_home):
    from hermes_state_registry import acquire, release_or_close

    handle = acquire()
    yield handle
    release_or_close(handle)


def _new_session(db, **kwargs) -> str:
    sid = f"test_{uuid.uuid4().hex[:10]}"
    db.create_session(sid, "cli", **kwargs)
    return sid


def _name() -> str:
    """A title no other test in this module has taken: the store enforces uniqueness across the
    whole store, and ``HERMES_HOME`` is one tmp directory for the session."""
    return f"the battery hour {uuid.uuid4().hex[:6]}"


# ---- the write ----------------------------------------------------------------


def test_a_rename_lands_in_the_store(hr_sessions, db):
    sid = _new_session(db)
    title = _name()

    body = hr_sessions.set_title(sid, title)

    assert body["ok"] is True
    assert db.get_session_title(sid) == title


def test_a_session_that_is_not_there_is_a_404(hr_sessions, db):
    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.set_title("test_nobody", "anything")

    assert raised.value.status == 404


def test_a_title_another_session_holds_is_a_409(hr_sessions, db):
    held = _new_session(db)
    title = _name()
    hr_sessions.set_title(held, title)
    other = _new_session(db)

    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.set_title(other, title)

    assert raised.value.status == 409
    assert "already in use" in raised.value.detail


def test_a_title_the_store_will_not_take_is_a_409(hr_sessions, db):
    """The length limit is the store's, and its refusal is reported rather than pre-empted."""
    sid = _new_session(db)

    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.set_title(sid, "x" * 600)

    assert raised.value.status == 409


def test_renaming_a_row_to_what_it_already_says_is_not_a_failure(hr_sessions, db):
    """The store's compare-and-swap writes a same-value title rather than reporting "nothing to
    do", so a repeat is a plain success and not a conflict."""
    sid = _new_session(db)
    title = _name()
    hr_sessions.set_title(sid, title)

    body = hr_sessions.set_title(sid, title)

    assert body["ok"] is True
    assert db.get_session_title(sid) == title


def test_a_write_that_lost_a_race_is_a_409(hr_sessions, monkeypatch):
    """False from the store is its compare-and-swap losing to a concurrent rename, not a no-op:
    the phone's title did not land, so it must not be reported as though it had."""
    monkeypatch.setattr(hr_sessions, "WRITE_TITLE", lambda _sid, _title: False)

    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.set_title("test_anything", "a name")

    assert raised.value.status == 409


def test_the_store_being_unreachable_is_a_503(hr_sessions, monkeypatch):
    def _broken(_session_id, _title):
        raise OSError("database is locked")

    monkeypatch.setattr(hr_sessions, "WRITE_TITLE", _broken)

    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.set_title("test_anything", "a name")

    assert raised.value.status == 503


# ---- the query ----------------------------------------------------------------


def test_the_query_needs_both_parameters(hr_sessions):
    for params in ({}, {"session_id": "s"}, {"title": "t"}, {"session_id": " ", "title": "t"}):
        with pytest.raises(hr_sessions.SessionOpError) as raised:
            hr_sessions.parse_query(params)
        assert raised.value.status == 400


def test_a_title_longer_than_any_real_one_is_refused_before_the_store(hr_sessions):
    with pytest.raises(hr_sessions.SessionOpError) as raised:
        hr_sessions.parse_query({"session_id": "s", "title": "x" * (hr_sessions.MAX_TITLE_CHARS + 1)})

    assert raised.value.status == 400


def test_the_query_trims_what_it_reads(hr_sessions):
    assert hr_sessions.parse_query({"session_id": " s ", "title": "  a name  "}) == ("s", "a name")


# ---- the routes ---------------------------------------------------------------


@pytest.fixture()
def paired(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    return token


@pytest.fixture()
def router_client(api, store_path, monkeypatch):
    monkeypatch.setattr(api.hr_wsauth, "auth_required", lambda: False)
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_the_route_is_a_token_route(_plugin_on_path):
    hr_routes = importlib.import_module("hr_routes")
    assert ROUTE in hr_routes.TOKEN_ROUTES


def test_the_dashboard_route_refuses_without_a_paired_device(router_client, db):
    sid = _new_session(db)
    assert router_client.post(ROUTE, params={"session_id": sid, "title": "n"}).status_code == 401
    unknown = _auth("hr1.aaaaaaaaaaaa.nope")
    assert router_client.post(ROUTE, params={"session_id": sid, "title": "n"}, headers=unknown).status_code == 401
    # And nothing was written by the refused calls.
    assert db.get_session_title(sid) in (None, "")


def test_both_hosts_rename_the_same_way(router_client, paired, db, tmp_path, monkeypatch):
    """Direct mode (the gateway, no window open) must rename exactly as the dashboard does."""
    through_dashboard = _new_session(db)
    through_gateway = _new_session(db)

    dashboard = router_client.post(
        ROUTE, params={"session_id": through_dashboard, "title": "named by the router"}, headers=_auth(paired)
    )
    missing = router_client.post(
        ROUTE, params={"session_id": "test_nobody", "title": "no one"}, headers=_auth(paired)
    )

    host = importlib.import_module("hr_gateway_host")
    listener = importlib.import_module("hr_listener")
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path)
    listener.state.failures.clear()

    async def main():
        import httpx
        import uvicorn

        config = uvicorn.Config(host.GatewayHost(), host="127.0.0.1", port=0, log_level="error", lifespan="on")
        server = uvicorn.Server(config)
        config.load()
        server.lifespan = config.lifespan_class(config)
        await server.startup()
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                path = host._SESSION_TITLE_PATH
                return (
                    await client.post(
                        path, params={"session_id": through_gateway, "title": "named by the host"},
                        headers=_auth(paired),
                    ),
                    await client.post(
                        path, params={"session_id": "test_nobody", "title": "no one"}, headers=_auth(paired)
                    ),
                    await client.post(path, params={"session_id": through_gateway, "title": "anonymous"}),
                    # A GET is not this route: only the POST writes.
                    await client.get(path, params={"session_id": through_gateway}, headers=_auth(paired)),
                )
        finally:
            await server.shutdown()

    direct, direct_missing, direct_anonymous, direct_get = asyncio.run(main())

    assert dashboard.status_code == 200 and dashboard.json()["ok"] is True
    assert direct.status_code == 200 and direct.json()["ok"] is True
    assert db.get_session_title(through_dashboard) == "named by the router"
    assert db.get_session_title(through_gateway) == "named by the host"
    assert missing.status_code == direct_missing.status_code == 404
    assert direct_anonymous.status_code == 401
    assert direct_get.status_code == 404
