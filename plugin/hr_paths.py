"""Where HermesRemote keeps its state, resolved the same way Hermes resolves its own.

Everything lives under ``$HERMES_HOME/remote/`` (``%LOCALAPPDATA%\\hermes\\remote\\`` on this
machine). ``hermes_constants.get_default_hermes_root`` is the authority — it unwraps a
``--profile`` home back to the root, so a profile-scoped dashboard and the gateway agree on one
device list. It is imported lazily and behind a fallback because this module is also loaded
standalone by ``dashboard/api.py``, outside any Hermes import context, in tests.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Subdirectory of the Hermes root that holds every HermesRemote artifact.
STATE_DIRNAME = "remote"

#: Device records: hashed tokens only, never a recoverable secret.
DEVICES_FILENAME = "devices.json"


def hermes_root() -> Path:
    """The Hermes root directory (not a profile subdirectory)."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        # Standalone/test import: mirror Hermes' own env precedence.
        env = os.environ.get("HERMES_HOME", "").strip()
        if env:
            return Path(env)
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "hermes"
        return Path.home() / ".hermes"


def state_dir() -> Path:
    """``<hermes root>/remote``. Not created here; writers create it on demand."""
    return hermes_root() / STATE_DIRNAME


def devices_path() -> Path:
    """Absolute path of the device store."""
    return state_dir() / DEVICES_FILENAME
