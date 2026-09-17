"""Images an agent wrote, and everything that must not be one.

The claim on trial is the gate, not the bytes: a path is served only when it resolves to a real
image inside an allowed root and outside the denylist, and every way of arriving somewhere else -
``..``, a symlink, a rename, an absolute path to a credential store - is refused. The allowlist is
driven against a real ``SessionDB`` in the session's tmp ``HERMES_HOME`` so the session's ``cwd``
is the one the route reads, and the route itself is driven on both hosts that answer it.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/plugins/hermes-odyssey"

#: The smallest valid PNG: an 8-bit greyscale 1x1. Real bytes, so nothing here passes on a stub.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a4944415478da6360000000020001e221bc330000000049454e44ae426082"
)


@pytest.fixture()
def hr_media(_plugin_on_path):
    return importlib.import_module("hr_media")


@pytest.fixture()
def db(_isolated_hermes_home):
    from hermes_state_registry import acquire, release_or_close

    handle = acquire()
    yield handle
    release_or_close(handle)


@pytest.fixture(autouse=True)
def scratch(hr_media, tmp_path, monkeypatch):
    """A temp directory of this test's own, standing in for the OS one.

    ``pytest``'s ``tmp_path`` is itself under the real temp directory, which the route allows. Left
    alone, every "outside the allowlist" fixture below would be inside it and prove nothing.
    """
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr(hr_media, "TEMP_DIR", lambda: str(directory))
    return directory


@pytest.fixture()
def session(db, tmp_path):
    """A session whose cwd is this test's tmp directory, with ``chart.png`` already in it."""
    sid = f"test_{uuid.uuid4().hex[:10]}"
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "chart.png").write_bytes(PNG)
    db.create_session(sid, "cli", cwd=str(cwd))
    return sid, cwd


@pytest.fixture()
def paired(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    return token


@pytest.fixture()
def client(api, store_path, monkeypatch):
    monkeypatch.setattr(api.hr_wsauth, "auth_required", lambda: False)
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _refusal(hr_media, sid: str, path: str) -> int:
    with pytest.raises(hr_media.MediaError) as refused:
        hr_media.read_media(sid, path)
    return refused.value.status


# ---- what is served ------------------------------------------------------------


def test_a_relative_path_resolves_against_the_session_cwd(hr_media, session):
    sid, _cwd = session
    data, content_type = hr_media.read_media(sid, "chart.png")
    assert data == PNG
    assert content_type == "image/png"


def test_an_absolute_path_under_the_cwd_is_served(hr_media, session):
    sid, cwd = session
    data, _type = hr_media.read_media(sid, str(cwd / "chart.png"))
    assert data == PNG


def test_a_subdirectory_of_the_cwd_is_served(hr_media, session):
    sid, cwd = session
    nested = cwd / "out" / "deep"
    nested.mkdir(parents=True)
    (nested / "shot.png").write_bytes(PNG)
    assert hr_media.read_media(sid, "out/deep/shot.png")[0] == PNG


def test_the_hermes_cache_is_served_whatever_the_session_cwd_is(hr_media, session, _isolated_hermes_home):
    sid, _cwd = session
    cache = Path(_isolated_hermes_home) / "cache" / "images"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "made.png").write_bytes(PNG)
    assert hr_media.read_media(sid, str(cache / "made.png"))[0] == PNG


def test_a_profiles_cache_is_served(hr_media, session, _isolated_hermes_home):
    """Profiles are created after this process started, so the roots are enumerated per request."""
    sid, _cwd = session
    cache = Path(_isolated_hermes_home) / "profiles" / "work" / "cache" / "screenshots"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "shot.png").write_bytes(PNG)
    assert hr_media.read_media(sid, str(cache / "shot.png"))[0] == PNG


def test_the_temp_directory_is_served(hr_media, session, scratch):
    """Where an agent told to "write a scratch PNG" with no cwd in mind actually writes it."""
    sid, _cwd = session
    (scratch / "quick.png").write_bytes(PNG)
    assert hr_media.read_media(sid, str(scratch / "quick.png"))[0] == PNG


@pytest.mark.parametrize(
    "name,content_type",
    [("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.jpeg", "image/jpeg"),
     ("a.gif", "image/gif"), ("a.webp", "image/webp"), ("a.svg", "image/svg+xml")],
)
def test_each_image_extension_names_its_content_type(hr_media, session, name, content_type):
    sid, cwd = session
    (cwd / name).write_bytes(PNG)
    assert hr_media.read_media(sid, name)[1] == content_type


def test_an_svg_is_served_as_its_own_text(hr_media, session):
    """SVG is markup, not a bitmap: it goes out verbatim under ``image/svg+xml``, never sniffed."""
    sid, cwd = session
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
    (cwd / "plot.svg").write_bytes(svg)
    assert hr_media.read_media(sid, "plot.svg") == (svg, "image/svg+xml")


# ---- what is not ---------------------------------------------------------------


def test_path_traversal_out_of_the_cwd_is_refused(hr_media, session, tmp_path):
    sid, _cwd = session
    (tmp_path / "secret.png").write_bytes(PNG)
    assert _refusal(hr_media, sid, "../secret.png") == 403


def test_an_absolute_path_outside_every_root_is_refused(hr_media, session, tmp_path_factory):
    sid, _cwd = session
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "other.png"
    elsewhere.write_bytes(PNG)
    assert _refusal(hr_media, sid, str(elsewhere)) == 403


def test_a_symlink_out_of_the_cwd_is_judged_by_its_target(hr_media, session, tmp_path):
    """The link is inside the allowlist and the file it names is not. The target decides."""
    sid, cwd = session
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = cwd / "linked.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symlinks without elevation")
    assert _refusal(hr_media, sid, "linked.png") == 403


def test_a_symlink_renaming_a_secret_as_an_image_is_refused(hr_media, session, _isolated_hermes_home):
    """``chart.png -> .env``: the extension the caller asked for is not the extension it resolves to."""
    sid, cwd = session
    secret = Path(_isolated_hermes_home) / ".env"
    secret.write_text("OPENAI_API_KEY=sk-live", encoding="utf-8")
    link = cwd / "innocent.png"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symlinks without elevation")
    assert _refusal(hr_media, sid, "innocent.png") == 415


@pytest.mark.parametrize("name", [".ssh", ".aws", ".config", ".gnupg", ".docker", "mcp-tokens"])
def test_a_denylisted_directory_inside_an_allowed_root_is_refused(hr_media, session, name):
    """A session run from a home directory puts these inside the allowlist. They stay shut."""
    sid, cwd = session
    directory = cwd / name
    directory.mkdir()
    (directory / "key.png").write_bytes(PNG)
    assert _refusal(hr_media, sid, f"{name}/key.png") == 403


def test_the_odyssey_state_directory_is_refused(hr_media, session, monkeypatch, tmp_path):
    """The device store and the listener's private key live here; nothing under it is deliverable."""
    sid, cwd = session
    hr_paths = importlib.import_module("hr_paths")
    state = cwd / "odyssey"
    state.mkdir()
    (state / "cert.png").write_bytes(PNG)
    monkeypatch.setattr(hr_paths, "state_dir", lambda: state)
    assert _refusal(hr_media, sid, "odyssey/cert.png") == 403


def test_hermes_own_denylist_is_consulted(hr_media, session, monkeypatch):
    """The local floor is not the whole gate: whatever Hermes refuses to deliver, so does this."""
    sid, cwd = session
    seen = []

    def deny(resolved):
        seen.append(resolved)
        return True

    monkeypatch.setattr(hr_media, "DENIED", deny)
    assert _refusal(hr_media, sid, "chart.png") == 403
    assert seen and seen[0].name == "chart.png"


def test_a_missing_hermes_denylist_leaves_the_local_one_standing(hr_media, session, monkeypatch):
    """Losing the optional import narrows coverage; it must not open the route."""
    sid, cwd = session
    monkeypatch.setattr(hr_media, "_hermes_denied", lambda _resolved: False)
    (cwd / ".ssh").mkdir()
    (cwd / ".ssh" / "id.png").write_bytes(PNG)
    assert hr_media._local_denied((cwd / ".ssh" / "id.png").resolve()) is True


@pytest.mark.parametrize("name", ["notes.txt", "report.pdf", "archive.zip", "script.py", "Makefile"])
def test_a_non_image_extension_is_refused(hr_media, session, name):
    sid, cwd = session
    (cwd / name).write_bytes(b"whatever")
    assert _refusal(hr_media, sid, name) == 415


def test_an_oversize_image_is_refused(hr_media, session, monkeypatch):
    sid, cwd = session
    monkeypatch.setattr(hr_media, "MAX_BYTES", 16)
    assert _refusal(hr_media, sid, "chart.png") == 413


def test_a_directory_is_not_a_file(hr_media, session):
    sid, cwd = session
    (cwd / "frames.png").mkdir()
    assert _refusal(hr_media, sid, "frames.png") == 404


def test_a_missing_file_is_404(hr_media, session):
    sid, _cwd = session
    assert _refusal(hr_media, sid, "nothing.png") == 404


def test_an_unknown_session_is_404(hr_media):
    assert _refusal(hr_media, "no-such-session", "chart.png") == 404


def test_a_session_with_no_cwd_can_still_reach_the_caches(hr_media, db, _isolated_hermes_home, monkeypatch):
    """A relative path has nothing to resolve against and is 404; the caches are still absolute."""
    sid = f"test_{uuid.uuid4().hex[:10]}"
    db.create_session(sid, "cli")
    # ``create_session`` stamps the process cwd when none is given; this is the legacy row that has
    # none at all, which is the case the fallback exists for.
    monkeypatch.setattr(hr_media, "READ_SESSION_CWD", lambda _sid: "")
    cache = Path(_isolated_hermes_home) / "cache" / "images"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "made.png").write_bytes(PNG)

    assert _refusal(hr_media, sid, "chart.png") == 404
    assert hr_media.read_media(sid, str(cache / "made.png"))[0] == PNG


@pytest.mark.parametrize(
    "params",
    [{}, {"session_id": " "}, {"session_id": "s"}, {"session_id": "s", "path": " "},
     {"session_id": "s", "path": "a\x00.png"}, {"session_id": "s", "path": "x" * 5000}],
)
def test_malformed_queries_are_400(hr_media, params):
    with pytest.raises(hr_media.MediaError) as refused:
        hr_media.parse_query(params)
    assert refused.value.status == 400


# ---- both hosts -----------------------------------------------------------------


def test_the_route_is_a_token_route(_plugin_on_path):
    hr_routes = importlib.import_module("hr_routes")
    assert f"{PREFIX}/media" in hr_routes.TOKEN_ROUTES


def test_the_dashboard_route_refuses_without_a_paired_device(client, session):
    sid, _cwd = session
    query = {"session_id": sid, "path": "chart.png"}
    assert client.get(f"{PREFIX}/media", params=query).status_code == 401
    unknown = _auth("hr1.aaaaaaaaaaaa.nope")
    assert client.get(f"{PREFIX}/media", params=query, headers=unknown).status_code == 401


def test_the_dashboard_route_serves_the_bytes_with_their_headers(client, paired, session):
    sid, _cwd = session
    response = client.get(f"{PREFIX}/media", params={"session_id": sid, "path": "chart.png"}, headers=_auth(paired))

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "max-age" in response.headers["cache-control"]


def test_both_hosts_answer_the_same_image(client, paired, session, tmp_path, monkeypatch):
    """Direct mode (the gateway, no window open) must serve and refuse exactly as the dashboard does."""
    sid, cwd = session
    # Outside the cwd, so the traversal below is refused for where it leads rather than for
    # naming a file that is not there.
    (cwd.parent / "escape.png").write_bytes(PNG)

    query = {"session_id": sid, "path": "chart.png"}
    dashboard = client.get(f"{PREFIX}/media", params=query, headers=_auth(paired))
    escaped = client.get(
        f"{PREFIX}/media", params={"session_id": sid, "path": "../escape.png"}, headers=_auth(paired)
    )
    wrong_type = client.get(
        f"{PREFIX}/media", params={"session_id": sid, "path": "notes.txt"}, headers=_auth(paired)
    )

    host = importlib.import_module("hr_gateway_host")
    listener = importlib.import_module("hr_listener")
    hr_paths = importlib.import_module("hr_paths")
    # Not ``tmp_path``: the session's cwd is under it, and the state directory is denied.
    monkeypatch.setattr(hr_paths, "state_dir", lambda: tmp_path / "odyssey-state")
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
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                path = host._MEDIA_PATH
                return (
                    await http.get(path, params=query, headers=_auth(paired)),
                    await http.get(path, params={"session_id": sid, "path": "../escape.png"}, headers=_auth(paired)),
                    await http.get(path, params={"session_id": sid, "path": "notes.txt"}, headers=_auth(paired)),
                    await http.get(path, params=query),
                )
        finally:
            await server.shutdown()

    direct, direct_escaped, direct_wrong_type, direct_anonymous = asyncio.run(main())

    assert dashboard.status_code == 200 and dashboard.content == PNG
    assert direct.status_code == 200 and direct.content == PNG
    assert direct.headers["content-type"] == dashboard.headers["content-type"]
    assert direct.headers["cache-control"] == dashboard.headers["cache-control"]
    assert direct.headers["x-content-type-options"] == dashboard.headers["x-content-type-options"]
    assert escaped.status_code == direct_escaped.status_code == 403
    assert wrong_type.status_code == direct_wrong_type.status_code == 415
    assert direct_anonymous.status_code == 401
