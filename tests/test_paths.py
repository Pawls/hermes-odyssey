"""The one-time move of the state directory from its hermes-remote name.

A copy or a fresh directory would unpair every phone: the device hashes and the pinned certificate
both live there. So the move has to happen exactly when there is something to move and nowhere to
collide.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def hr_paths(_plugin_on_path, tmp_path, monkeypatch):
    module = importlib.import_module("hr_paths")
    monkeypatch.setattr(module, "hermes_root", lambda: tmp_path)
    return module


def _legacy_store(root):
    legacy = root / "remote"
    legacy.mkdir()
    (legacy / "devices.json").write_text('{"devices": {}}', encoding="utf-8")
    (legacy / "listener-cert.pem").write_text("cert", encoding="utf-8")
    return legacy


def test_a_legacy_store_moves_whole(hr_paths, tmp_path):
    legacy = _legacy_store(tmp_path)
    assert hr_paths.migrate_legacy_state() is True
    assert not legacy.exists()
    assert (tmp_path / "talaria" / "devices.json").is_file()
    assert (tmp_path / "talaria" / "listener-cert.pem").read_text(encoding="utf-8") == "cert"
    assert hr_paths.migrate_legacy_state() is False


def test_an_existing_talaria_dir_is_never_overwritten(hr_paths, tmp_path):
    legacy = _legacy_store(tmp_path)
    (tmp_path / "talaria").mkdir()
    assert hr_paths.migrate_legacy_state() is False
    assert (legacy / "devices.json").is_file()
    assert not (tmp_path / "talaria" / "devices.json").exists()


def test_a_remote_dir_without_a_device_store_is_left_alone(hr_paths, tmp_path):
    # Another plugin could own a directory called "remote"; only ours carries devices.json.
    (tmp_path / "remote").mkdir()
    assert hr_paths.migrate_legacy_state() is False
    assert (tmp_path / "remote").is_dir()
    assert not (tmp_path / "talaria").exists()
