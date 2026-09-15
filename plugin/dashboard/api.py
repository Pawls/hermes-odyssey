"""HermesRemote's backend router, mounted by the dashboard at ``/api/plugins/hermes-remote``.

The dashboard imports this file *standalone* — ``_mount_plugin_api_routes`` builds it with
``spec_from_file_location`` under the synthetic name ``hermes_dashboard_plugin_hermes-remote``,
with no package — so ``from .. import hr_devices`` is not available here. :func:`_sibling` bridges
that: in the normal case the plugin package is already in ``sys.modules`` (``discover_plugins()``
runs before ``start_server``), so its submodules are reused; only outside a Hermes process does it
fall back to loading them by path.

Every route here is bearer-only. :func:`_require_device` re-verifies rather than trusting the
seam, and checks *which* provider vouched for the caller — a bearer accepted by some other token
provider in the same process (the bundled drain secret, say) is not a paired phone and must not
reach these routes.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

_log = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PACKAGE_PREFIX = "hermes_plugins.hermes_remote"


def _plugin_package() -> Optional[ModuleType]:
    """The already-imported plugin package, if this process loaded it.

    The loader may suffix the name (``…__home_<digest>``) when more than one Hermes home is live
    in one process, so match on the prefix rather than the exact name.
    """
    for name, module in list(sys.modules.items()):
        if name == _PACKAGE_PREFIX or name.startswith(_PACKAGE_PREFIX + "__home_"):
            if module is not None and hasattr(module, "register"):
                return module
    return None


def _sibling(module_name: str) -> ModuleType:
    """Import ``<plugin>/<module_name>.py``, reusing the package's copy when there is one."""
    package = _plugin_package()
    if package is not None:
        return importlib.import_module(f"{package.__name__}.{module_name}")
    # Standalone (tests, or any process that never ran ``discover_plugins()``): put the plugin
    # directory on the path once, so ordinary imports work and each module's own
    # ``except ImportError: import hr_x`` fallback resolves to this same object rather than
    # loading a second copy of it.
    plugin_dir = str(_PLUGIN_DIR)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    return importlib.import_module(module_name)


hr_devices = _sibling("hr_devices")
hr_listener = _sibling("hr_listener")
hr_provider = _sibling("hr_provider")
hr_routes = _sibling("hr_routes")
hr_wsauth = _sibling("hr_wsauth")

router = APIRouter()


# ---- the TLS listener's lifetime ------------------------------------------
#
# This file is imported by ``_mount_plugin_api_routes`` and nowhere else, which makes it the one
# reliable marker that we are inside the process that owns port 9119 — ``register(ctx)`` runs in
# every Hermes process, so it is the wrong place to open a socket. The handlers below ride the
# router's own lifespan: ``include_router`` merges it into the app's, so they run even though the
# dashboard passes a custom ``lifespan=`` that ignores the app router's own event handlers.


def _start_listener() -> None:
    """Arm the listener. Not ``async``: the real work waits for a port that is not bound yet, so
    it is a task, and blocking the dashboard's startup on it would deadlock."""
    hr_listener.schedule_start()


async def _stop_listener() -> None:
    await hr_listener.stop()


router.add_event_handler("startup", _start_listener)
router.add_event_handler("shutdown", _stop_listener)

_UNAUTHORIZED = HTTPException(status_code=401, detail="Unauthorized")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _require_device(request: Request):
    """The paired device behind this request, or 401 / 503.

    The token seam has usually already run and attached a principal; that is checked first and,
    crucially, checked for *provenance* — only our own provider's principal counts. The bearer is
    then re-verified against the store. Doing both means the route is still safe if the seam ever
    stops covering it (a renamed path, a failed ``register_token_route``) rather than silently
    becoming open in loopback mode.
    """
    principal = getattr(request.state, "token_principal", None)
    if principal is not None and getattr(principal, "provider", "") != hr_provider.PROVIDER_NAME:
        raise _UNAUTHORIZED
    try:
        device = hr_devices.verify_token(_bearer(request))
    except hr_devices.DeviceStoreUnavailable as exc:
        _log.warning("hermes-remote: device store unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Device store unavailable") from exc
    if device is None:
        raise _UNAUTHORIZED
    return device


@router.get(hr_routes.ROUTE_HEALTH)
def health(request: Request) -> Dict[str, Any]:
    """Liveness plus the caller's own identity and the current WS credential mode."""
    device = _require_device(request)
    return hr_routes.health_payload(
        device_id=device.id, device_label=device.label, mode=hr_wsauth.mode()
    )


@router.post(hr_routes.ROUTE_WS_TICKET)
def ws_ticket(request: Request) -> Dict[str, Any]:
    """The credential this device needs to open ``/api/ws``, for the mode the dashboard is in.

    ``ticket`` is null in loopback mode and that is the expected answer, not an error: the only
    credential the loopback gate accepts is the dashboard's process-lifetime session token, which
    must not cross the network, so the TLS listener attaches it when it proxies the upgrade. The
    phone opens the same URL either way and appends ``?ticket=`` only when it was given one.
    """
    device = _require_device(request)
    minted = hr_wsauth.mint_client_ticket(user_id=f"device:{device.id}")
    ticket, expires_in = minted if minted is not None else (None, None)
    return hr_routes.ws_ticket_payload(
        mode=hr_wsauth.mode(),
        ws_path=hr_wsauth.GATEWAY_WS_PATH,
        ticket=ticket,
        expires_in=expires_in,
    )
