"""Device tokens, stored as hashes only.

A device token is ``hr1.<device_id>.<secret>``: the id addresses one record so verification is a
single lookup rather than a scan over every device, and the secret is 32 CSPRNG bytes in
base64url. Only ``sha256(salt || secret)`` is written to disk, so the file is not a credential and
a copy of it grants nothing.

**Why SHA-256 and not scrypt.** Password hashes are slow to make a dictionary attack expensive.
There is no dictionary here: the secret is 256 bits of ``secrets.token_bytes`` and is never chosen
by a human, so an offline attacker's only route is brute force over 2^256. The per-device salt is
there to stop a stolen file being matched against another file's hashes, not to slow anything
down. Verification runs on every request from the phone, so a deliberately slow hash would be a
self-inflicted denial of service.

The store is stateless in memory: every call reads the file. ``dashboard/api.py`` loads this
module by path while ``__init__.py`` imports it as a package submodule, so two module objects can
exist in one process; keeping the file as the only state makes that harmless.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:  # package import (``hermes_plugins.hermes_talaria``)
    from . import hr_paths
except ImportError:  # standalone path load from dashboard/api.py
    import hr_paths  # type: ignore[no-redef]

#: Token prefix. Bump when the token or hash format changes so an old token fails fast
#: instead of failing obscurely.
TOKEN_PREFIX = "hr1"

_SECRET_BYTES = 32
_SALT_BYTES = 16
_ID_HEX_CHARS = 12
_STORE_VERSION = 1

#: ``last_seen_at`` is advisory, so it is written at most this often per device rather than on
#: every request from the phone.
LAST_SEEN_THROTTLE_SECONDS = 60

_write_lock = threading.Lock()
_last_seen_written: Dict[str, float] = {}


class DeviceStoreUnavailable(Exception):
    """The store exists but could not be read or written.

    Distinct from "no such device": the caller turns this into a 503, never a 401, so an
    unreadable file can never be mistaken for a rejected token.
    """


@dataclass(frozen=True)
class Device:
    """One paired phone. Never carries the secret — only its hash."""

    id: str
    label: str
    created_at: int
    last_seen_at: Optional[int] = None
    revoked_at: Optional[int] = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


# ---- token shape -----------------------------------------------------------


def parse_token(token: str) -> Optional[Tuple[str, str]]:
    """Split a token into ``(device_id, secret)``, or ``None`` when it is not ours.

    Returning ``None`` rather than raising is what lets the token-auth seam fall through to the
    next provider: another provider's opaque bearer is not an error here.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    prefix, device_id, secret = parts
    if prefix != TOKEN_PREFIX or not secret:
        return None
    if len(device_id) != _ID_HEX_CHARS or any(c not in "0123456789abcdef" for c in device_id):
        return None
    return device_id, secret


def _hash_secret(salt_hex: str, secret: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt_hex) + secret.encode("utf-8")).hexdigest()


# ---- file I/O --------------------------------------------------------------


def _empty_store() -> Dict[str, Any]:
    return {"version": _STORE_VERSION, "devices": {}}


def _read_store(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the store. A missing file is an empty store; an unreadable or malformed one raises."""
    path = path or hr_paths.devices_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_store()
    except OSError as exc:
        raise DeviceStoreUnavailable(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        # Fail loudly rather than degrading to "no devices": a truncated write should page the
        # operator, not silently lock the phone out with a 401 that looks like a bad token.
        raise DeviceStoreUnavailable(f"malformed device store {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
        raise DeviceStoreUnavailable(f"malformed device store {path}: unexpected shape")
    return data


def _write_store(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomically replace the store, owner-only where the platform honours the mode.

    Windows ignores the POSIX mode; there the file inherits the ACL of the user's LOCALAPPDATA,
    which is the same protection ``auth.json`` relies on.
    """
    path = path or hr_paths.devices_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise DeviceStoreUnavailable(f"cannot write {path}: {exc}") from exc


def _device_from_record(device_id: str, record: Dict[str, Any]) -> Device:
    return Device(
        id=device_id,
        label=str(record.get("label", "")),
        created_at=int(record.get("created_at", 0)),
        last_seen_at=record.get("last_seen_at"),
        revoked_at=record.get("revoked_at"),
    )


# ---- public API ------------------------------------------------------------


def list_devices(path: Optional[Path] = None) -> list[Device]:
    """Every device, revoked ones included, oldest first."""
    devices = _read_store(path)["devices"]
    return sorted(
        (_device_from_record(k, v) for k, v in devices.items() if isinstance(v, dict)),
        key=lambda d: (d.created_at, d.id),
    )


def create_device(label: str, path: Optional[Path] = None) -> Tuple[Device, str]:
    """Pair a new device. Returns ``(device, token)``; the token is shown once and never stored."""
    label = (label or "").strip() or "unnamed device"
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    salt = secrets.token_bytes(_SALT_BYTES).hex()
    now = int(time.time())
    with _write_lock:
        data = _read_store(path)
        while True:
            device_id = secrets.token_hex(_ID_HEX_CHARS // 2)
            if device_id not in data["devices"]:
                break
        data["devices"][device_id] = {
            "label": label,
            "algo": "sha256",
            "salt": salt,
            "hash": _hash_secret(salt, secret),
            "created_at": now,
            "last_seen_at": None,
            "revoked_at": None,
        }
        _write_store(data, path)
    token = f"{TOKEN_PREFIX}.{device_id}.{secret}"
    return _device_from_record(device_id, data["devices"][device_id]), token


def revoke_device(device_id: str, path: Optional[Path] = None) -> bool:
    """Mark a device revoked. Returns False when it is unknown or already revoked.

    The record is kept rather than deleted so ``hermes talaria status`` can still show that a
    device existed and when its access ended.
    """
    with _write_lock:
        data = _read_store(path)
        record = data["devices"].get(device_id)
        if not isinstance(record, dict) or record.get("revoked_at") is not None:
            return False
        record["revoked_at"] = int(time.time())
        _write_store(data, path)
    _last_seen_written.pop(device_id, None)
    return True


def verify_token(token: str, path: Optional[Path] = None) -> Optional[Device]:
    """Return the device this token belongs to, or ``None``.

    ``None`` covers every rejection — not our prefix, unknown id, revoked, wrong secret — because
    the caller must not leak which one it was. Raises :class:`DeviceStoreUnavailable` only when
    the file itself could not be read.
    """
    parsed = parse_token(token)
    if parsed is None:
        return None
    device_id, secret = parsed
    record = _read_store(path)["devices"].get(device_id)
    if not isinstance(record, dict) or record.get("revoked_at") is not None:
        return None
    salt, expected = record.get("salt"), record.get("hash")
    if not isinstance(salt, str) or not isinstance(expected, str):
        return None
    try:
        actual = _hash_secret(salt, secret)
    except ValueError:
        return None  # salt is not hex; treat the record as unusable rather than crashing the gate
    if not hmac.compare_digest(actual, expected):
        return None
    _touch_last_seen(device_id, path)
    return _device_from_record(device_id, record)


def _touch_last_seen(device_id: str, path: Optional[Path] = None) -> None:
    """Best-effort, throttled ``last_seen_at`` update. Never raises: it is bookkeeping, and a
    failure to record it must not fail the request that was otherwise authenticated."""
    now = time.time()
    if now - _last_seen_written.get(device_id, 0.0) < LAST_SEEN_THROTTLE_SECONDS:
        return
    _last_seen_written[device_id] = now
    try:
        with _write_lock:
            data = _read_store(path)
            record = data["devices"].get(device_id)
            if isinstance(record, dict) and record.get("revoked_at") is None:
                record["last_seen_at"] = int(now)
                _write_store(data, path)
    except Exception:
        pass
