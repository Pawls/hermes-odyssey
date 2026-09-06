"""The three files that must agree on what this plugin is called.

``plugins.enabled`` in ``config.yaml`` keys on ``plugin.yaml``'s ``name``; the dashboard mounts
the router at ``/api/plugins/<dashboard/manifest.json name>`` and gates it on the same enabled
set; and ``hr_routes`` builds the token-route paths from its own copy. A drift between any two
produces a router that is either mounted without a gate or gated at a path nothing serves — both
silent. This test is the tripwire.
"""

from __future__ import annotations

import importlib
import json

import yaml

from conftest import PLUGIN_DIR


def _plugin_yaml() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


def _dashboard_manifest() -> dict:
    return json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))


def test_the_name_is_the_same_in_all_three_places(_plugin_on_path):
    hr_routes = importlib.import_module("hr_routes")
    assert _plugin_yaml()["name"] == hr_routes.PLUGIN_NAME
    assert _dashboard_manifest()["name"] == hr_routes.PLUGIN_NAME


def test_the_version_is_the_same_in_all_three_places(_plugin_on_path):
    hr_routes = importlib.import_module("hr_routes")
    assert _plugin_yaml()["version"] == hr_routes.PLUGIN_VERSION
    assert _dashboard_manifest()["version"] == hr_routes.PLUGIN_VERSION


def test_token_routes_are_absolute_and_under_the_mount_prefix(_plugin_on_path):
    hr_routes = importlib.import_module("hr_routes")
    assert hr_routes.API_PREFIX == f"/api/plugins/{hr_routes.PLUGIN_NAME}"
    assert hr_routes.TOKEN_ROUTES
    for path in hr_routes.TOKEN_ROUTES:
        assert path.startswith(hr_routes.API_PREFIX + "/")


def test_every_declared_route_is_registered_as_a_token_route(_plugin_on_path, api):
    """A route served but not registered would be open in loopback mode."""
    hr_routes = importlib.import_module("hr_routes")
    served = {
        f"{hr_routes.API_PREFIX}{route.path}" for route in api.router.routes if hasattr(route, "path")
    }
    assert served == set(hr_routes.TOKEN_ROUTES)


def test_the_declared_api_file_exists(_plugin_on_path):
    """The dashboard warns and skips when ``api`` names a missing file."""
    assert (PLUGIN_DIR / "dashboard" / _dashboard_manifest()["api"]).is_file()


def test_the_declared_entry_bundle_exists(_plugin_on_path):
    """The SPA fetches ``entry`` whether or not the tab is hidden."""
    assert (PLUGIN_DIR / "dashboard" / _dashboard_manifest()["entry"]).is_file()
