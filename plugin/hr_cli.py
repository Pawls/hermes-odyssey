"""``hermes remote`` — pair a phone, see what is paired, take it away again.

Registered through ``ctx.register_cli_command``, which wires an argparse subtree at startup and
needs no change to ``hermes_cli/main.py``. The handler signature is ``fn(args) -> int``; the
integer becomes the process exit code, so a script can branch on it.

Three subcommands and no more, because those are the three states a device can be in. ``pair``
mints a token and draws it; ``status`` says what exists and whether the listener is up; ``revoke``
ends one. There is deliberately no ``rename`` and no ``list --json``: the phone is the client, and
anything richer belongs on the phone.

This module must stay importable without FastAPI, uvicorn or an event loop. ``hermes remote`` runs
in the plain CLI process, which has no dashboard in it, and a heavyweight import here would be
paid by every ``hermes`` invocation that touches plugin CLI discovery.
"""

from __future__ import annotations

import socket
import ssl
import sys
import time
from typing import List, Optional

try:  # package import (``hermes_plugins.hermes_remote``)
    from . import hr_devices, hr_identity, hr_listener, hr_pairing, hr_qr, hr_routes
except ImportError:  # standalone path load
    import hr_devices  # type: ignore[no-redef]
    import hr_identity  # type: ignore[no-redef]
    import hr_listener  # type: ignore[no-redef]
    import hr_pairing  # type: ignore[no-redef]
    import hr_qr  # type: ignore[no-redef]
    import hr_routes  # type: ignore[no-redef]

COMMAND_NAME = "remote"
COMMAND_HELP = "Pair a phone with this Hermes session (HermesRemote)"
COMMAND_DESCRIPTION = (
    "HermesRemote pairs an Android client with the dashboard on this machine over a TLS "
    "listener whose certificate the phone pins.\n\n"
    "  hermes remote pair --label 'Pixel 9'   show a pairing QR\n"
    "  hermes remote status                   what is paired, and is the listener up\n"
    "  hermes remote revoke <id>              end one device's access"
)


# ---- argparse tree ---------------------------------------------------------


def setup(parser) -> None:
    """Build the ``hermes remote`` subtree. Called by the dashboard's plugin CLI loader."""
    sub = parser.add_subparsers(dest="remote_command", metavar="<command>")

    pair = sub.add_parser("pair", help="Show a pairing QR for a new device")
    pair.add_argument(
        "--label", default="", help="What to call this phone in `hermes remote status`"
    )
    pair.add_argument(
        "--light",
        action="store_true",
        help="Draw the QR for a light-background terminal (default assumes a dark one)",
    )
    pair.add_argument(
        "--uri-only",
        action="store_true",
        help="Print only the pairing URI, for piping into another QR renderer",
    )

    status = sub.add_parser("status", help="Paired devices, the certificate, and the listener")
    status.add_argument(
        "--all", action="store_true", help="Include revoked devices"
    )

    revoke = sub.add_parser("revoke", help="End a device's access")
    revoke.add_argument("device", nargs="?", default="", help="Device id, or a unique prefix of it")
    revoke.add_argument("--all", action="store_true", help="Revoke every live device")

    parser.set_defaults(remote_command=None)


def handler(args) -> int:
    """Dispatch. Returns the process exit code."""
    command = getattr(args, "remote_command", None)
    if command == "pair":
        return _pair(args)
    if command == "status":
        return _status(args)
    if command == "revoke":
        return _revoke(args)
    print(COMMAND_DESCRIPTION)
    return 2


# ---- output helpers --------------------------------------------------------


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> int:
    print(text, file=sys.stderr)
    return 1


def _age(stamp: Optional[int]) -> str:
    if not stamp:
        return "never"
    seconds = max(0, int(time.time()) - int(stamp))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _grouped_fingerprint(hex_digest: str) -> str:
    """Hex in groups of four, which is how a person compares it to a phone screen without losing
    their place. The phone shows the same grouping."""
    return " ".join(hex_digest[i : i + 4] for i in range(0, len(hex_digest), 4))


# ---- pair ------------------------------------------------------------------


def _pair(args) -> int:
    try:
        identity = hr_identity.ensure_identity()
    except Exception as exc:  # noqa: BLE001 — a missing `cryptography` is the likely cause
        return _err(f"Could not create the listener certificate: {exc}")

    hosts = hr_pairing.candidate_hosts()
    port = hr_listener.configured_port()
    running = hr_listener.read_runtime()
    if running and running.get("port"):
        # A live listener is the authority on where the phone should connect; the configured port
        # is only what the next one will use.
        port = int(running["port"])

    try:
        device, token = hr_devices.create_device(args.label)
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not write the device store: {exc}")

    uri = hr_pairing.build(
        hosts=hosts,
        port=port,
        fingerprint=identity.fingerprint_b64,
        token=token,
        name=hr_pairing.machine_name(),
    )

    if args.uri_only:
        _out(uri)
        return 0

    try:
        matrix = hr_qr.encode(uri)
    except hr_qr.QrTooLong as exc:
        return _err(f"Pairing payload will not fit in a QR code: {exc}")

    _out()
    _out(hr_qr.to_text(matrix, dark_background=not args.light))
    _out()
    _out(f"  Device    {device.label}  ({device.id})")
    _out(f"  Listener  https://{hosts[0]}:{port}" + ("" if running else "   (not running yet)"))
    _out(f"  Addresses {', '.join(hosts)}")
    _out("  Certificate SHA-256")
    _out(f"            {_grouped_fingerprint(identity.fingerprint_hex[:32])}")
    _out(f"            {_grouped_fingerprint(identity.fingerprint_hex[32:])}")
    _out()
    _out("  Check that fingerprint against the one the phone shows. That comparison is the")
    _out("  only thing standing between you and a machine in the middle; everything else")
    _out("  about this exchange survives one.")
    _out()
    _out("  This code IS the credential. It stays valid until you revoke the device, so")
    _out("  clear the screen once the phone has it, and treat a screenshot of it as a")
    _out(f"  password. To end it:  hermes remote revoke {device.id}")
    _out()
    if not running:
        _out("  The listener starts with the dashboard. Run `hermes dashboard` if it is not up.")
        _out()
    return 0


# ---- status ----------------------------------------------------------------


def _probe(bind_host: str, port: int, expected_der: bytes) -> str:
    """One line about the listener's socket: reachable, and is it presenting our certificate.

    Connects to loopback rather than the LAN address because that is the leg that proves the
    process is up; whether the phone can reach it is a question about the network, not about
    Hermes, and answering it from here would be a guess. The line names the *bind* host, since
    "listening on 127.0.0.1" would be a lie about a socket bound to every interface.
    """
    host = "127.0.0.1"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # the certificate is pinned, not validated — the phone too
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=2.0) as raw:
            with context.wrap_socket(raw) as tls:
                served = tls.getpeercert(binary_form=True)
    except OSError as exc:
        return f"not answering on {host}:{port} ({exc.__class__.__name__})"
    if served != expected_der:
        return f"answering on {host}:{port} with a DIFFERENT certificate — do not pair"
    return f"listening on {bind_host or host}:{port}"


def _host_lines(runtime: dict) -> list:
    """Who holds the port, and whether a better-ranked process is waiting for it."""
    surface = runtime.get("surface") or "unknown surface"
    lines = [
        f"               hosted by {surface}, pid {runtime.get('pid')},"
        f" proxying to 127.0.0.1:{runtime.get('upstream_port')}"
    ]
    claim = hr_listener.read_claim()
    if claim and hr_listener.pid_alive(claim.get("pid")):
        lines.append(
            f"               handover pending: {claim.get('surface')} (pid {claim.get('pid')})"
            " is waiting for the port"
        )
    return lines


def _status(args) -> int:
    identity = hr_identity.existing_identity()
    _out()
    _out(f"HermesRemote {hr_routes.PLUGIN_VERSION}")
    _out()

    if identity is None:
        _out("  Certificate  none yet — `hermes remote pair` creates it")
    else:
        _out(f"  Certificate  SHA-256 {_grouped_fingerprint(identity.fingerprint_hex[:32])}")
        _out(f"                       {_grouped_fingerprint(identity.fingerprint_hex[32:])}")
        _out(f"               expires {identity.not_after.date().isoformat()}")

    runtime = hr_listener.read_runtime()
    if runtime is None:
        _out(f"  Listener     not recorded as running (would use port {hr_listener.configured_port()})")
    elif identity is None:
        _out("  Listener     recorded as running, but there is no certificate on disk")
    else:
        _out(f"  Listener     {_probe(str(runtime.get('host') or ''), int(runtime.get('port') or 0), identity.der)}")
        for line in _host_lines(runtime):
            _out(line)
    if not hr_listener.enabled():
        _out(f"               disabled by {hr_listener.ENV_ENABLED}")

    _out()
    try:
        devices = hr_devices.list_devices()
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not read the device store: {exc}")

    shown = [d for d in devices if args.all or not d.revoked]
    if not shown:
        _out("  No paired devices. `hermes remote pair` shows a code.")
        _out()
        return 0

    _out(f"  {'ID':<14}{'LABEL':<24}{'LAST SEEN':<14}STATE")
    for device in shown:
        state = f"revoked {_age(device.revoked_at)}" if device.revoked else "live"
        _out(f"  {device.id:<14}{device.label[:22]:<24}{_age(device.last_seen_at):<14}{state}")
    _out()
    return 0


# ---- revoke ----------------------------------------------------------------


def _resolve(devices: List, prefix: str) -> Optional[str]:
    """A full device id from a unique prefix, or ``None`` when it matches none or several."""
    matches = [d.id for d in devices if d.id.startswith(prefix) and not d.revoked]
    return matches[0] if len(matches) == 1 else None


def _revoke(args) -> int:
    try:
        devices = hr_devices.list_devices()
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not read the device store: {exc}")

    if args.all:
        live = [d for d in devices if not d.revoked]
        if not live:
            _out("Nothing to revoke.")
            return 0
        for device in live:
            hr_devices.revoke_device(device.id)
        _out(f"Revoked {len(live)} device(s). Each has to scan a new code to come back.")
        return 0

    if not args.device:
        return _err("Which device? `hermes remote status` lists them, or use --all.")

    device_id = _resolve(devices, args.device.strip())
    if device_id is None:
        return _err(f"No single live device matches {args.device!r}. See `hermes remote status`.")
    if hr_devices.revoke_device(device_id):
        _out(f"Revoked {device_id}. Its token stops working now, and a session it has open closes")
        _out("within a few seconds.")
        return 0
    return _err(f"{device_id} was already revoked.")
