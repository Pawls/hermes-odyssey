"""``DashboardAuthProvider`` for paired phones — the token half only.

The dashboard's token seam (``hermes_cli/dashboard_auth/token_auth.py``) asks every registered
provider to recognise an inbound bearer. This one recognises ``hr1.<id>.<secret>`` and vouches for
it as ``device:<id>``; anything else it returns ``None`` for, so other providers still get a look.

It is token-only, like the bundled ``drain`` provider: ``supports_session = False``, so it is never
offered as a login method and never mints a cookie. The interactive half of the protocol raises,
except ``verify_session``, which returns ``None`` so the provider stacks harmlessly in the
cookie-verify loop rather than aborting it.
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    Session,
    TokenPrincipal,
)

try:  # package import (``hermes_plugins.hermes_odyssey``)
    from . import hr_devices
except ImportError:  # standalone path load
    import hr_devices  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

#: Provider id. Stable: it appears in audit logs and in ``TokenPrincipal.provider``.
PROVIDER_NAME = "hermes-odyssey-device"

#: Granted to every paired device. Routes that must not be reachable from a phone can check for
#: its absence; today every Odyssey route requires exactly this one.
DEVICE_SCOPE = "hermes-odyssey"

_NOT_INTERACTIVE = (
    "OdysseyDeviceProvider is a paired-device credential; there is no interactive login."
)


class OdysseyDeviceProvider(DashboardAuthProvider):
    """Verifies a paired phone's bearer token against the hashed device store."""

    name = PROVIDER_NAME
    display_name = "Odyssey paired device"
    supports_token = True
    supports_session = False
    supports_password = False

    # ---- token capability (the only thing this provider implements) --------

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        """``TokenPrincipal`` for a live paired device, else ``None``.

        A store that cannot be read becomes ``ProviderError`` so the seam answers 503. Answering
        401 there would tell a phone its token was rejected when in fact nothing was checked.
        """
        try:
            device = hr_devices.verify_token(token)
        except hr_devices.DeviceStoreUnavailable as exc:
            raise ProviderError(str(exc)) from exc
        if device is None:
            return None
        return TokenPrincipal(
            principal=f"device:{device.id}", provider=self.name, scopes=(DEVICE_SCOPE,)
        )

    # ---- interactive methods: unsupported ----------------------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(_NOT_INTERACTIVE)

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(_NOT_INTERACTIVE)

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        # Never mints a Session, so never recognises a cookie. None (not a raise) keeps the
        # cookie-verify loop going for whichever provider actually owns the session.
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(_NOT_INTERACTIVE)

    def revoke_session(self, *, refresh_token: str) -> None:
        return None
