"""Renaming a stored session, which is the one session operation the gateway cannot do by stored id.

The phone lists sessions with ``session.list`` and every row it draws is a row in ``state.db``. Three
of the four things it can then do about a row already have a gateway method that takes a stored id:
``session.create`` mints one, ``session.set_hidden`` archives one, ``session.resume`` opens one.
Renaming does not. ``session.title`` is session-scoped - ``_with_session`` resolves the *runtime* id
out of the gateway's in-memory ``_sessions`` map (``tui_gateway/methods_session.py``) - so over the
socket it can only rename the session this phone happens to have resumed, and every other row in the
list has no runtime id at all.

So the plugin writes the title itself, through the same ``SessionDB`` API the desktop's own rename
uses (``hermes_cli/web_routers/sessions.py``), which is where all the rules live: provenance, the
uniqueness conflict, the canonical Bot Chat guard, and the length limit. Nothing is reimplemented
here - this module turns a query into one call and a refusal into a status code.

The id is used exactly as the list gave it, with no compression-tip resolution. ``hr_history``
resolves a tip because it is reading a *lineage*; a title belongs to one row, and the row whose
title the phone is showing is the row the phone named.

The desktop hears about the write the same way it hears about its own: ``state.db``'s mtime moves,
the gateway's change watcher broadcasts ``sessions.changed`` (floored at two seconds in
``tui_gateway/change_watcher.py``), and every client refetches its list.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

_log = logging.getLogger(__name__)

#: A query's ``title``. The store's own limit is lower and is what actually decides; this only keeps
#: a megabyte of query string from reaching it.
MAX_TITLE_CHARS = 4096


class SessionOpError(Exception):
    """A session operation that cannot be done. ``status`` is what the phone is told."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _write_title(session_id: str, title: str) -> Optional[bool]:
    """``db.set_session_title`` on the shared handle, released after the write.

    None when the row is not there; otherwise the store's own answer, which is False when its
    compare-and-swap lost to a concurrent write. The handle is refcounted and shared with whatever
    else this process is doing with ``state.db``; the write itself is one short transaction inside
    the store.
    """
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        if not db.get_session(session_id):
            return None
        return bool(db.set_session_title(session_id, title))
    finally:
        release_or_close(db)


#: Seam for the tests, which drive this against a real ``SessionDB`` in a tmp home.
WRITE_TITLE: Callable[[str, str], Optional[bool]] = _write_title


def parse_query(params: Dict[str, str]) -> Tuple[str, str]:
    """``(session_id, title)`` from query parameters, or raise :class:`SessionOpError` 400.

    Both ride the query string rather than a body: the token seam matches paths exactly, so every
    route here is already parameterised that way, and a bodyless POST is what lets the gateway's own
    host answer this with the same drain-and-dispatch it uses for the GETs.
    """
    session_id = (params.get("session_id") or "").strip()
    if not session_id:
        raise SessionOpError(400, "session_id is required")
    title = (params.get("title") or "").strip()
    if not title:
        # Clearing a title is a real operation in the store and deliberately not offered here: on a
        # phone it is one mistaken tap away from a list of rows that all read "Untitled".
        raise SessionOpError(400, "title is required")
    if len(title) > MAX_TITLE_CHARS:
        raise SessionOpError(400, "title is too long")
    return session_id, title


def set_title(session_id: str, title: str) -> Dict[str, object]:
    """Rename one stored session, or raise :class:`SessionOpError`.

    404 for a session that is not in the store, 409 for a title the store refuses (another session
    holds it, it is too long, or the row is a bot's canonical chat whose name is its identity), and
    503 when ``state.db`` cannot be reached at all.

    The store's write is a compare-and-swap against the title it just read, so ``False`` means the
    row was renamed by something else in between rather than "nothing to do" - renaming a row to the
    title it already holds still reports as written. A lost race is a 409 like any other conflict:
    the phone's rename did not happen, and saying it did would leave a name on screen that the
    store does not have.
    """
    try:
        written = WRITE_TITLE(session_id, title)
    except ValueError as exc:
        raise SessionOpError(409, str(exc)) from exc
    except SessionOpError:
        raise
    except Exception as exc:
        _log.warning("hermes-odyssey: rename failed for %r: %s", session_id, exc)
        raise SessionOpError(503, "session store unavailable") from exc
    if written is None:
        raise SessionOpError(404, "session not found")
    if not written:
        raise SessionOpError(409, "that session was renamed somewhere else a moment ago")
    return {"ok": True, "session_id": session_id, "title": title}
