"""Transcript pages: the newest messages first, older ones by cursor, identical to what resume sends.

Every test writes a real ``SessionDB`` in the session's tmp ``HERMES_HOME`` and reads it back
through the real lineage read and the gateway's own projection, because the claim on trial is
parity with ``session.resume``: walking every page back to the start must reproduce, message for
message, the list ``session.resume`` would have put on the wire. The routes are then driven on
both hosts that answer them, the dashboard router and the gateway's in-process app.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/plugins/hermes-odyssey"


@pytest.fixture()
def hr_history(_plugin_on_path):
    return importlib.import_module("hr_history")


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


def _chat(db, sid: str, turns: int, start: int = 0) -> None:
    for n in range(start, start + turns):
        db.append_message(sid, "user", f"question {n}")
        db.append_message(sid, "assistant", f"answer {n}")


def _resume_wire(db, sid: str) -> list:
    """What ``session.resume`` sends for ``sid``: its display projection of the lineage."""
    from tui_gateway.server import _history_to_messages

    tip = db.resolve_resume_session_id(sid)
    return _history_to_messages(db.get_resume_conversations(tip)[1])


#: What a page adds to a tool row beyond the resume payload.
_RESULT_KEYS = ("tool_call_id", "result", "result_chars", "inline_diff")


def _walk(hr_history, sid: str, limit: int) -> list:
    """Every page from the newest back to the start, stitched into chronological order, with the
    tool results a page adds taken back out so it compares with the resume payload."""
    pages, before = [], None
    while True:
        page = hr_history.read_page(sid, before=before, limit=limit)
        pages.insert(0, page["messages"])
        if not page["has_more"]:
            assert page["before"] is None
            return [{k: v for k, v in m.items() if k not in _RESULT_KEYS} for chunk in pages for m in chunk]
        before = page["before"]
        assert before == page["messages"][0]["row_id"]


# ---- paging -------------------------------------------------------------------


def test_the_first_page_is_the_newest(hr_history, db):
    sid = _new_session(db)
    _chat(db, sid, 10)

    page = hr_history.read_page(sid, limit=4)

    assert [m["text"] for m in page["messages"]] == ["question 8", "answer 8", "question 9", "answer 9"]
    assert page["has_more"] is True
    assert page["session_id"] == sid
    assert page["before"] == page["messages"][0]["row_id"]


def test_walking_every_page_reproduces_the_resume_payload(hr_history, db):
    sid = _new_session(db)
    _chat(db, sid, 13)

    assert _walk(hr_history, sid, limit=5) == _resume_wire(db, sid)


def test_a_short_session_is_one_page_with_nothing_more(hr_history, db):
    sid = _new_session(db)
    _chat(db, sid, 2)

    page = hr_history.read_page(sid)

    assert len(page["messages"]) == 4
    assert page["has_more"] is False and page["before"] is None


def test_new_messages_do_not_move_an_older_cursor(hr_history, db):
    """The cursor is a row id, so a turn landing at the end while the phone reads history does not
    shift the next page by the length of that turn."""
    sid = _new_session(db)
    _chat(db, sid, 6)
    newest = hr_history.read_page(sid, limit=4)
    older_before = hr_history.read_page(sid, before=newest["before"], limit=4)

    _chat(db, sid, 3, start=6)

    assert hr_history.read_page(sid, before=newest["before"], limit=4) == older_before


def test_a_page_boundary_inside_a_tool_run_keeps_the_calls_arguments(hr_history, db):
    """Tool rows carry no row id and take their arguments from the assistant row before them. A
    page must not start on a tool row: it grows back to the message that owns the calls."""
    sid = _new_session(db)
    _chat(db, sid, 3)
    db.append_message(sid, "user", "list the files")
    calls = [
        {"id": f"call_{n}", "type": "function", "function": {"name": "terminal", "arguments": f'{{"command": "ls {n}"}}'}}
        for n in range(3)
    ]
    db.append_message(sid, "assistant", "", tool_calls=calls)
    for n in range(3):
        db.append_message(sid, "tool", f"file{n}.txt", tool_call_id=f"call_{n}", tool_name="terminal")
    db.append_message(sid, "assistant", "three files")

    newest = hr_history.read_page(sid, limit=3)

    tools = [m for m in newest["messages"] if m["role"] == "tool"]
    assert [t["args"] for t in tools] == [{"command": f"ls {n}"} for n in range(3)]
    assert newest["messages"][0].get("row_id") is not None
    assert _walk(hr_history, sid, limit=3) == _resume_wire(db, sid)


# ---- tool results -------------------------------------------------------------


def _tool_turn(db, sid: str, results: list, name: str = "terminal") -> None:
    db.append_message(sid, "user", "run them")
    calls = [
        {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": f'{{"n": {n}}}'}}
        for n in range(len(results))
    ]
    db.append_message(sid, "assistant", "", tool_calls=calls)
    for n, result in enumerate(results):
        db.append_message(sid, "tool", result, tool_call_id=f"call_{n}", tool_name=name)
    db.append_message(sid, "assistant", "done")


def test_tool_rows_carry_their_results_and_call_ids(hr_history, db):
    sid = _new_session(db)
    _tool_turn(db, sid, ["first output", '{"exit_code": 0}'])

    tools = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert [(t["tool_call_id"], t["result"]) for t in tools] == [("call_0", "first output"), ("call_1", '{"exit_code": 0}')]
    assert all("result_chars" not in t for t in tools)


def test_a_long_result_is_cut_and_says_how_long_it_was(hr_history, db):
    sid = _new_session(db)
    _tool_turn(db, sid, ["x" * (hr_history.RESULT_CHARS + 5)])

    (tool,) = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert len(tool["result"]) == hr_history.RESULT_CHARS
    assert tool["result_chars"] == hr_history.RESULT_CHARS + 5


def test_a_hidden_tool_row_does_not_shift_the_results(hr_history, db):
    """Pairing is by position among the rows the projection keeps; a dropped one must not hand its
    result to the next call."""
    sid = _new_session(db)
    db.append_message(sid, "user", "go")
    calls = [{"id": f"call_{n}", "type": "function", "function": {"name": "terminal", "arguments": "{}"}} for n in range(3)]
    db.append_message(sid, "assistant", "", tool_calls=calls)
    db.append_message(sid, "tool", "zero", tool_call_id="call_0", tool_name="terminal")
    db.append_message(sid, "tool", "scaffolding", tool_call_id="call_1", tool_name="terminal", display_kind="hidden")
    db.append_message(sid, "tool", "two", tool_call_id="call_2", tool_name="terminal")

    tools = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert [(t["tool_call_id"], t["result"]) for t in tools] == [("call_0", "zero"), ("call_2", "two")]


def test_an_image_in_a_result_is_a_placeholder_not_its_data(hr_history, db):
    sid = _new_session(db)
    parts = [
        {"type": "text", "text": "Image loaded into your context"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50_000}},
    ]
    _tool_turn(db, sid, [parts], name="vision_analyze")

    (tool,) = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert tool["result"] == "Image loaded into your context\n[image]"


def test_a_single_wrapped_tool_call_is_named_by_the_tool_it_ran(hr_history, db):
    sid = _new_session(db)
    db.append_message(sid, "user", "plan")
    wrapped = {"calls": [{"name": "todo_list", "arguments": {"todos": [{"id": "1", "content": "read", "status": "pending"}]}}]}
    two = {"calls": [{"name": "read_file", "arguments": {}}, {"name": "terminal", "arguments": {}}]}
    calls = [
        {"id": "call_0", "type": "function", "function": {"name": "tool_call", "arguments": json.dumps(wrapped)}},
        {"id": "call_1", "type": "function", "function": {"name": "tool_call", "arguments": json.dumps(two)}},
    ]
    db.append_message(sid, "assistant", "", tool_calls=calls)
    db.append_message(sid, "tool", '{"todos": []}', tool_call_id="call_0", tool_name="tool_call")
    db.append_message(sid, "tool", "both ran", tool_call_id="call_1", tool_name="tool_call")

    single, several = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert (single["name"], single["args"]) == ("todo_list", wrapped["calls"][0]["arguments"])
    assert isinstance(single["context"], str)
    assert (several["name"], several["args"]) == ("tool_call", two)


def test_a_patch_row_carries_its_diff(hr_history, db):
    sid = _new_session(db)
    diff = "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new\n"
    _tool_turn(db, sid, ['{"success": true, "diff": ' + json.dumps(diff) + "}"], name="patch")

    (tool,) = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert tool["inline_diff"] == diff


def test_a_non_patch_row_has_no_diff(hr_history, db):
    sid = _new_session(db)
    _tool_turn(db, sid, ['{"diff": "--- a\\n+++ b\\n"}'])

    (tool,) = [m for m in hr_history.read_page(sid)["messages"] if m["role"] == "tool"]

    assert "inline_diff" not in tool


def test_hidden_rows_are_not_on_the_page_and_do_not_shorten_it(hr_history, db):
    sid = _new_session(db)
    _chat(db, sid, 4)
    db.append_message(sid, "user", "runbook scaffolding", display_kind="hidden")
    db.append_message(sid, "user", "[System: model switched]")
    _chat(db, sid, 1, start=4)

    page = hr_history.read_page(sid, limit=4)

    texts = [m["text"] for m in page["messages"]]
    assert texts == ["question 3", "answer 3", "question 4", "answer 4"]
    assert _walk(hr_history, sid, limit=3) == _resume_wire(db, sid)


def test_compacted_turns_are_still_history(hr_history, db):
    """In-place compaction archives the summarized rows; the user's own turns must still page in,
    once each, exactly as resume shows them."""
    sid = _new_session(db)
    _chat(db, sid, 5)
    live = db.get_messages_as_conversation(sid)
    db.archive_and_compact(
        sid, [{"role": "user", "content": "[CONTEXT SUMMARY] earlier turns"}, *live[-2:]], tail_count=2
    )
    _chat(db, sid, 2, start=5)

    stitched = _walk(hr_history, sid, limit=3)

    texts = [m["text"] for m in stitched]
    assert "question 0" in texts
    assert texts.count("question 4") == 1
    assert stitched == _resume_wire(db, sid)


def test_a_compressed_parent_pages_from_its_tip_with_ancestors(hr_history, db):
    parent = _new_session(db)
    _chat(db, parent, 3)
    db.end_session(parent, "compression")
    child = _new_session(db, parent_session_id=parent)
    _chat(db, child, 2, start=3)

    page = hr_history.read_page(parent, limit=200)

    assert page["session_id"] == child
    assert [m["text"] for m in page["messages"] if m["role"] == "user"] == [f"question {n}" for n in range(5)]
    assert page["messages"] == _resume_wire(db, parent)


# ---- refusals -------------------------------------------------------------------


def test_an_unknown_session_is_404(hr_history, db):
    with pytest.raises(hr_history.PageError) as refused:
        hr_history.read_page("no_such_session")
    assert refused.value.status == 404


def test_a_cursor_no_longer_in_the_transcript_is_409(hr_history, db):
    sid = _new_session(db)
    _chat(db, sid, 2)
    with pytest.raises(hr_history.PageError) as refused:
        hr_history.read_page(sid, before=10**9)
    assert refused.value.status == 409


@pytest.mark.parametrize(
    "params",
    [{}, {"session_id": " "}, {"session_id": "s", "limit": "0"}, {"session_id": "s", "limit": "201"},
     {"session_id": "s", "limit": "ten"}, {"session_id": "s", "before": "latest"}],
)
def test_malformed_queries_are_400(hr_history, params):
    with pytest.raises(hr_history.PageError) as refused:
        hr_history.parse_query(params)
    assert refused.value.status == 400


# ---- both hosts -----------------------------------------------------------------


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
    assert f"{PREFIX}/messages" in hr_routes.TOKEN_ROUTES


def test_the_dashboard_route_refuses_without_a_paired_device(router_client, db):
    sid = _new_session(db)
    _chat(db, sid, 1)
    assert router_client.get(f"{PREFIX}/messages", params={"session_id": sid}).status_code == 401
    unknown = _auth("hr1.aaaaaaaaaaaa.nope")
    assert router_client.get(f"{PREFIX}/messages", params={"session_id": sid}, headers=unknown).status_code == 401


def test_both_hosts_answer_the_same_page(router_client, paired, db, tmp_path, monkeypatch):
    """Direct mode (the gateway, no window open) must page exactly like the dashboard does."""
    sid = _new_session(db)
    _chat(db, sid, 8)

    dashboard = router_client.get(f"{PREFIX}/messages", params={"session_id": sid, "limit": 5}, headers=_auth(paired))
    older = router_client.get(
        f"{PREFIX}/messages",
        params={"session_id": sid, "limit": 5, "before": dashboard.json()["before"]},
        headers=_auth(paired),
    )
    missing = router_client.get(f"{PREFIX}/messages", params={"session_id": "nope"}, headers=_auth(paired))

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
                path = host._MESSAGES_PATH
                return (
                    await client.get(path, params={"session_id": sid, "limit": 5}, headers=_auth(paired)),
                    await client.get(
                        path,
                        params={"session_id": sid, "limit": 5, "before": dashboard.json()["before"]},
                        headers=_auth(paired),
                    ),
                    await client.get(path, params={"session_id": "nope"}, headers=_auth(paired)),
                    await client.get(path, params={"session_id": sid}),
                )
        finally:
            await server.shutdown()

    direct, direct_older, direct_missing, direct_anonymous = asyncio.run(main())

    assert dashboard.status_code == 200 and dashboard.json()["has_more"] is True
    assert direct.json() == dashboard.json()
    assert direct_older.json() == older.json()
    assert missing.status_code == direct_missing.status_code == 404
    assert direct_anonymous.status_code == 401
