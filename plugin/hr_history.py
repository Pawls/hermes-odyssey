"""Transcript pages for the phone: the newest messages of a session first, then older ones on demand.

``session.resume`` returns a whole transcript in one frame, and the dashboard's paged REST
(``/api/sessions/{id}/messages``) needs the dashboard session token, which never leaves this
machine, and does not exist at all when the gateway hosts the listener. So the plugin answers
pages itself, from ``hermes_state`` directly, on every host.

A page is a slice of exactly what ``session.resume`` would have put on the wire: the same lineage
read (ancestors, compaction-archived rows deduped) through the gateway's own
``_history_to_messages``, so a paged transcript and a resumed one cannot disagree about which
messages exist or what they look like. Slicing after projection, rather than paging raw rows, is
what keeps a tool call's arguments attached to its row when the assistant message that carries
them falls on the other side of a page boundary, and what keeps hidden rows from producing short
or empty pages. The cost is one lineage read per page; the 789-message lineage on Paul's machine
reads and projects in about 140 ms.

The cursor is a ``row_id``, not an offset: messages arriving at the end of a live session do not
move it. Display order is not ``row_id`` order (a deduped compaction row keeps its first
position), so the cursor is located by position in the projection, never compared numerically.
Tool rows carry no ``row_id`` in the gateway's projection, so a page always starts on a message
that has one, growing backwards past any leading tool rows; the cursor it returns is that
message's id.

The one departure from the resume payload: tool rows also carry what the call returned. The
projection keeps a tool call's name and arguments but drops its result, which the desktop shows
when a call is expanded (it pages raw rows and pairs results by ``tool_call_id``). So each tool
row gains ``tool_call_id``, ``result`` capped at :data:`RESULT_CHARS` (``result_chars`` gives the
full length when it was cut), and for ``patch`` the unified ``inline_diff`` Hermes extracts from it.
A single call made through the ``tool_call`` wrapper is also named by the tool it ran, as it is live.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: A tool result's size on the page. Stored results run p50 0.7 KiB, p99 21 KiB, max 117 KiB
#: (Paul's store, 2026-09-17), so this keeps a page of tool calls small without cutting most.
RESULT_CHARS = 16_000


class PageError(Exception):
    """A page that cannot be answered. ``status`` is what the phone is told."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _project(history: List[dict]) -> List[dict]:
    # Bound into ``tui_gateway.server``'s namespace at its import; the copy in ``session_history``
    # is not callable on its own. Both hosts have already imported the server for ``/api/ws``.
    from tui_gateway.server import _history_to_messages

    return _history_to_messages(history)


def _read_display_history(session_id: str) -> Optional[tuple]:
    """``(tip id, display history)`` for a stored session, or None when it does not exist.

    The shared, refcounted handle this process already holds, released after the read. A WAL
    reader: it takes no write lock and does not wait on the agent's writes.
    """
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        if not db.get_session(session_id):
            return None
        tip = db.resolve_resume_session_id(session_id) or session_id
        history = db.get_messages_as_conversation(
            tip, include_ancestors=True, include_row_ids=True, include_compacted=True
        )
        return tip, history
    finally:
        release_or_close(db)


#: Seams for tests, which drive the slicing against a real ``SessionDB`` in a tmp home.
READ_HISTORY: Callable[[str], Optional[tuple]] = _read_display_history
PROJECT: Callable[[List[dict]], List[dict]] = _project


def _attach_tool_results(history: List[dict], messages: List[dict]) -> None:
    """Give each projected tool row its stored result, in place.

    The projection emits tool rows in stored order but can drop some (hidden, compaction), so the
    stored rows are filtered through the same projection before pairing: all at once when none
    drop, which is the usual case, one by one otherwise. If the counts still disagree the rows are
    left as resume sends them rather than paired with the wrong results.
    """
    shown = [m for m in messages if m.get("role") == "tool"]
    if not shown:
        return
    stored = [m for m in history if isinstance(m, dict) and m.get("role") == "tool"]
    if len(PROJECT(stored)) != len(stored):
        stored = [m for m in stored if PROJECT([m])]
    if len(stored) != len(shown):
        return
    for row, source in zip(shown, stored):
        _unwrap_tool_call(row)
        if source.get("tool_call_id"):
            row["tool_call_id"] = source["tool_call_id"]
        content = source.get("content")
        if content is None:
            continue
        text = _result_text(content)
        row["result"] = text[:RESULT_CHARS]
        if len(text) > RESULT_CHARS:
            row["result_chars"] = len(text)
        diff = _patch_diff(row.get("name"), text)
        if diff:
            row["inline_diff"] = diff[:RESULT_CHARS]


def _unwrap_tool_call(row: dict) -> None:
    """Name a lazily loaded tool by the tool it ran, the way the live ``tool.start`` does.

    A model that loads tools on demand calls them through ``tool_call`` with
    ``{"calls": [{"name", "arguments"}]}``, and the projection names the row after that wrapper, so
    a todo update read ``tool_call`` in history and ``todo_list`` live. Only a single call is
    unwrapped: the stored result of several is one result for all of them.
    """
    calls = (row.get("args") or {}).get("calls") if row.get("name") == "tool_call" else None
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return
    name, args = calls[0].get("name"), calls[0].get("arguments")
    if not isinstance(name, str) or not name:
        return
    args = args if isinstance(args, dict) else {}
    row["name"] = name
    row["args"] = args
    try:
        from tui_gateway.server import _tool_ctx

        row["context"] = _tool_ctx(name, args)
    except Exception:
        row["context"] = ""


def _result_text(content: Any) -> str:
    """A stored result as text. A multimodal result (``vision_analyze`` loads its image into the
    model's context) keeps its text parts; an image part becomes ``[image]``, since its URL is
    usually a base64 data URL of up to 100 KiB that the row cannot show."""
    parts = content if isinstance(content, list) else [content] if isinstance(content, dict) else None
    if parts is None:
        return str(content)
    chunks = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif isinstance(part, dict) and "image" in str(part.get("type", "")):
            chunks.append("[image]")
        else:
            chunks.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(chunks)


def _patch_diff(name: Optional[str], result: str) -> Optional[str]:
    """The diff a live ``tool.complete`` would have rendered, for the edits a stored row can show.

    Only ``patch`` stores its diff in the result; ``write_file`` diffs come from a snapshot taken
    before the write, which history does not have.
    """
    if name != "patch":
        return None
    try:
        from agent.display import extract_edit_diff

        return extract_edit_diff(name, result)
    except Exception:
        return None


def parse_query(params: Dict[str, str]) -> tuple:
    """``(session_id, before, limit)`` from query parameters, or raise :class:`PageError` 400."""
    session_id = (params.get("session_id") or "").strip()
    if not session_id:
        raise PageError(400, "session_id is required")
    before: Optional[int] = None
    raw_before = (params.get("before") or "").strip()
    if raw_before:
        try:
            before = int(raw_before)
        except ValueError:
            raise PageError(400, "before must be an integer row id") from None
    limit = DEFAULT_LIMIT
    raw_limit = (params.get("limit") or "").strip()
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise PageError(400, "limit must be an integer") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise PageError(400, f"limit must be between 1 and {MAX_LIMIT}")
    return session_id, before, limit


def read_page(session_id: str, *, before: Optional[int] = None, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """The ``limit`` messages before the one whose ``row_id`` is ``before`` (the newest when None).

    404 for an unknown session. 409 when ``before`` is no longer in the transcript (a rewind or a
    compaction replaced it): the phone refetches the newest page rather than guess where it was.
    """
    read = READ_HISTORY(session_id)
    if read is None:
        raise PageError(404, "session not found")
    tip, history = read
    messages = PROJECT(history)
    _attach_tool_results(history, messages)

    end = len(messages)
    if before is not None:
        end = next((i for i, m in enumerate(messages) if m.get("row_id") == before), -1)
        if end < 0:
            raise PageError(409, "before is not in this transcript")
    start = max(0, end - limit)
    while start > 0 and messages[start].get("row_id") is None:
        start -= 1

    page = messages[start:end]
    return {
        "session_id": tip,
        "messages": page,
        "before": page[0].get("row_id") if page and start > 0 else None,
        "has_more": start > 0,
    }
