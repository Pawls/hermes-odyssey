"""Shared fixtures.

These tests import the plugin the way the dashboard does — by path, not as an installed package —
so they exercise the same import bridge ``dashboard/api.py`` relies on in production. They need
the Hermes venv on the interpreter: the plugin imports ``hermes_cli.dashboard_auth`` for the
provider ABC and there is no useful way to stub it. The README gives the invocation.

``HERMES_HOME`` is redirected to a tmp directory for the whole session so nothing here can read or
write the real device store at ``%LOCALAPPDATA%\\hermes\\remote\\``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"


@pytest.fixture(scope="session", autouse=True)
def _isolated_hermes_home(tmp_path_factory):
    """Point HERMES_HOME at a tmp root so no test can touch the real device store.

    The listener kill switch goes with it. ``hr_listener.schedule_start`` already refuses to run
    without a dashboard in the process, so this is belt and braces — but the thing it is guarding
    against is a test opening a socket on the LAN, which is worth two guards.
    """
    import os

    home = tmp_path_factory.mktemp("hermes-home")
    (home / "config.yaml").write_text("", encoding="utf-8")
    previous = {name: os.environ.get(name) for name in ("HERMES_HOME", "HERMES_REMOTE_LISTENER")}
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_REMOTE_LISTENER"] = "0"
    yield home
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def _plugin_on_path():
    """Make ``hr_*`` importable, the same way ``api.py`` does when no Hermes process loaded it."""
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))


@pytest.fixture()
def hr_devices(_plugin_on_path):
    """The device store module, with its throttle state reset between tests."""
    module = importlib.import_module("hr_devices")
    module._last_seen_written.clear()
    return module


@pytest.fixture(scope="session")
def api(_plugin_on_path):
    """``dashboard/api.py``, loaded by path exactly as the dashboard's mounter loads it."""
    path = PLUGIN_DIR / "dashboard" / "api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-remote", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def store_path(tmp_path, _plugin_on_path, monkeypatch) -> Path:
    """A device store file of this test's own, and the one every default-path caller resolves to.

    Redirecting ``hr_paths.devices_path`` rather than stubbing ``hr_devices.verify_token`` keeps
    the code under test intact: the router calls the real store, which reads this file.
    """
    path = tmp_path / "devices.json"
    hr_paths = importlib.import_module("hr_paths")
    monkeypatch.setattr(hr_paths, "devices_path", lambda: path)
    return path
