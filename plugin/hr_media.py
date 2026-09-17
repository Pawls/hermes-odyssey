"""Image bytes for the phone: the files an agent wrote during a session.

A reply that says "here is the chart" is worth nothing on a phone if the chart is a path on a
desktop. The gateway's own protocol has no way to hand one over - ``MEDIA:`` tags are delivered by
the platform adapters (Telegram, Discord) as uploads, and there is no such adapter here - so the
plugin serves the bytes itself, on the same TLS socket and behind the same device gate as
:mod:`hr_history`.

What may be read is decided twice over, and both have to agree.

*Allowed roots.* The session's own ``cwd`` (which is where an agent asked for a scratch PNG writes
it), the Hermes cache trees that every artifact tool already writes into (``cache/``, plus the
legacy ``image_cache``/``browser_screenshots`` names an older install still has, under the root and
under every profile), and the OS temp directory. A path outside all of them is refused even if it
exists and is an image: the route exists to show what the agent made, not to read the disk.

*The secret denylist.* Hermes' own ``_path_under_denied_prefix`` is consulted when it imports, so
this route can never be looser than ``MEDIA:`` delivery is. It is not the floor, though -
:func:`_local_denied` is, and it is checked first and always, because a route whose entire gate is
one optional import is a route that opens itself the day the import moves.

Everything is checked against the *resolved* path, so a symlink is judged by what it points at, and
``..`` has already been collapsed. Only image extensions are served, and only up to
:data:`MAX_BYTES`; SVG is text and is served as ``image/svg+xml`` rather than sniffed.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

_log = logging.getLogger(__name__)

try:  # package import (``hermes_plugins.hermes_odyssey``)
    from . import hr_paths
except ImportError:  # standalone path load (``dashboard/api.py``, tests)
    import hr_paths  # type: ignore[no-redef]

#: What the route serves, and what it calls each one on the wire. Nothing is sniffed: the extension
#: is the declaration, and a file whose bytes disagree is the caller's problem, not a content type
#: this route invents for it.
CONTENT_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

#: Enough for a phone screenshot at full resolution or a plotted figure, and small enough that a
#: refusal is what an accidental 4K video-frame dump gets rather than 200 MB across the Wi-Fi.
MAX_BYTES = 25 * 1024 * 1024

#: A query's ``path``. Longer than any real one; the point is to refuse before doing filesystem work.
MAX_PATH_CHARS = 4096

#: Seconds the phone may reuse a fetched image without asking again. Short on purpose: an agent
#: that overwrites ``plot.png`` on the next turn keeps the same path, and a long-lived disk cache
#: would show the old chart under the new reply.
CACHE_SECONDS = 60

#: Cache subdirectories of a Hermes home that hold agent artifacts, canonical and legacy. Mirrors
#: the allow side of ``gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS``; installs may have both.
_CACHE_DIRNAMES = ("cache", "images", "screenshots", "image_cache", "browser_screenshots")

#: The floor of the denylist, checked before and independently of Hermes'. Credential directories
#: by name, at any depth, because an allowed root can contain one: a session whose cwd is the
#: user's home puts ``.ssh`` inside the allowlist.
_DENIED_COMPONENTS = frozenset(
    {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".azure", ".gcloud", ".env",
     "keychains", "pairing", "mcp-tokens", "browser-profile"}
)


class MediaError(Exception):
    """An image that cannot be served. ``status`` is what the phone is told."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _read_session_cwd(session_id: str) -> Optional[str]:
    """The session's working directory, or None when the session does not exist.

    The same refcounted WAL reader :mod:`hr_history` pages through, released after the read. An
    empty string is a real answer: a session recorded before the column was filled has no cwd, and
    only the cache and temp roots are then reachable.
    """
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        row = db.get_session(session_id)
        if not row:
            return None
        return (row.get("cwd") or "").strip()
    finally:
        release_or_close(db)


#: Seam for tests, which drive the allowlist against a real ``SessionDB`` in a tmp home.
READ_SESSION_CWD: Callable[[str], Optional[str]] = _read_session_cwd

#: The OS scratch directory, as a seam: ``pytest``'s own ``tmp_path`` lives inside it, so a test
#: that did not redirect this would have every "outside the allowlist" fixture inside it.
TEMP_DIR: Callable[[], str] = tempfile.gettempdir


def _resolve(path: Path) -> Optional[Path]:
    """``path.resolve(strict=True)``, or None when it does not resolve to something that exists."""
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def _hermes_roots() -> list:
    """Every Hermes home whose caches are deliverable: the root and each ``<root>/profiles/*``.

    Enumerated per request rather than at import, for the same reason Hermes enumerates its own:
    a profile created after this process started is still a place the agent writes.
    """
    root = hr_paths.hermes_root()
    homes = [root]
    try:
        homes += [p for p in (root / "profiles").iterdir() if p.is_dir()]
    except OSError:
        pass
    return homes


def allowed_roots(cwd: str) -> list:
    """The resolved roots an image may live under, for a session whose working directory is [cwd]."""
    roots = [Path(cwd)] if cwd else []
    roots += [home / name for home in _hermes_roots() for name in _CACHE_DIRNAMES]
    roots.append(Path(TEMP_DIR()))
    resolved = [r for r in (_resolve(root) for root in roots) if r is not None]
    # Dedupe while keeping order: the cwd is the common case and should be matched first.
    return list(dict.fromkeys(resolved))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _local_denied(resolved: Path) -> bool:
    """The denylist this module owns, checked whether or not Hermes' imports.

    Names rather than absolute prefixes, because these have to hold inside an allowed root: a
    session run from ``$HOME`` makes ``$HOME`` an allowed root, and ``.ssh`` is under it.
    """
    if any(part.lower() in _DENIED_COMPONENTS for part in resolved.parts):
        return True
    # Odyssey's own state: the device store holds hashed tokens and the identity directory holds
    # the listener's private key. Neither is an image, but neither is ever worth one bug either.
    state = _resolve(hr_paths.state_dir())
    return state is not None and _is_within(resolved, state)


def _hermes_denied(resolved: Path) -> bool:
    """Hermes' own ``MEDIA:`` denylist, when it imports here. False when it does not.

    False, not fail-closed, because :func:`_local_denied` has already run and the allowlist still
    stands: losing this check narrows the gate's coverage, while failing closed would take the
    whole route out on a Hermes refactor that moved one private function.
    """
    try:
        from gateway.platforms.base import _path_under_denied_prefix

        return bool(_path_under_denied_prefix(resolved))
    except Exception as exc:  # noqa: BLE001 - an absent or moved helper is not a denial
        _log.debug("hermes-odyssey: Hermes media denylist unavailable: %s", exc)
        return False


#: Seam for tests, which assert the two halves independently.
DENIED: Callable[[Path], bool] = lambda resolved: _local_denied(resolved) or _hermes_denied(resolved)


def parse_query(params: Dict[str, str]) -> Tuple[str, str]:
    """``(session_id, path)`` from query parameters, or raise :class:`MediaError` 400."""
    session_id = (params.get("session_id") or "").strip()
    if not session_id:
        raise MediaError(400, "session_id is required")
    path = (params.get("path") or "").strip()
    if not path:
        raise MediaError(400, "path is required")
    if len(path) > MAX_PATH_CHARS:
        raise MediaError(400, "path is too long")
    if "\x00" in path:
        raise MediaError(400, "path is not a path")
    return session_id, path


def resolve_media(session_id: str, path: str) -> Tuple[Path, str]:
    """``(resolved file, content type)`` for one image, or raise :class:`MediaError`.

    404 for an unknown session or a file that is not there - the same answer for both, because
    "which paths exist on that desktop" is not a question this route answers. 403 for a real file
    outside the allowlist or inside the denylist, 415 for anything that is not an image by
    extension, and 413 for one over :data:`MAX_BYTES`.
    """
    cwd = READ_SESSION_CWD(session_id)
    if cwd is None:
        raise MediaError(404, "session not found")

    content_type = CONTENT_TYPES.get(Path(path).suffix.lower())
    if content_type is None:
        raise MediaError(415, "not an image")

    try:
        candidate = Path(os.path.expanduser(path))
    except (OSError, RuntimeError, ValueError):
        raise MediaError(400, "path is not a path") from None
    if not candidate.is_absolute():
        if not cwd:
            raise MediaError(404, "file not found")
        candidate = Path(cwd) / candidate

    resolved = _resolve(candidate)
    if resolved is None or not resolved.is_file():
        raise MediaError(404, "file not found")
    # The extension again, on the resolved name: a symlink called ``chart.png`` pointing at
    # ``id_rsa`` would otherwise be served as a PNG.
    if CONTENT_TYPES.get(resolved.suffix.lower()) != content_type:
        raise MediaError(415, "not an image")

    roots = allowed_roots(cwd)
    if not any(resolved == root or _is_within(resolved, root) for root in roots):
        raise MediaError(403, "not a readable location")
    if DENIED(resolved):
        raise MediaError(403, "not a readable location")

    try:
        size = resolved.stat().st_size
    except OSError:
        raise MediaError(404, "file not found") from None
    if size > MAX_BYTES:
        raise MediaError(413, "image is too large")
    return resolved, content_type


def read_media(session_id: str, path: str) -> Tuple[bytes, str]:
    """The bytes of one image and its content type. Blocking; both hosts call it off their loop."""
    resolved, content_type = resolve_media(session_id, path)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise MediaError(404, "file not found") from exc
    # Between the stat and the read: a file that grew past the cap is refused rather than sent.
    if len(data) > MAX_BYTES:
        raise MediaError(413, "image is too large")
    return data, content_type


def response_headers(content_type: str, length: int) -> Dict[str, str]:
    """What both hosts put on a served image, named here so they cannot differ."""
    return {
        "content-type": content_type,
        "content-length": str(length),
        # The extension decided the type; nothing downstream may reconsider by sniffing bytes.
        "x-content-type-options": "nosniff",
        "cache-control": f"private, max-age={CACHE_SECONDS}",
    }
