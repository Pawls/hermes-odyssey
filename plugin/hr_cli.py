"""``hermes odyssey`` — pair a phone, see what is paired, take it away again, or sit beside it.

Registered through ``ctx.register_cli_command``, which wires an argparse subtree at startup and
needs no change to ``hermes_cli/main.py``. The handler signature is ``fn(args) -> int``; the
integer becomes the process exit code, so a script can branch on it.

Three device subcommands and no more, because those are the three states a device can be in.
``pair`` mints a token and draws it; ``status`` says what exists and whether the listener is up;
``revoke`` ends one. There is deliberately no ``rename`` and no ``list --json``: the phone is the
client, and anything richer belongs on the phone.

``attach`` is the fourth and is about the terminal, not the phone. A ``hermes --tui`` of its own
spawns its own gateway, which then *owns* any session it opens, and a phone turn into that session
is refused (the 4090 on record in the plan). ``attach`` instead launches the TUI as a second
transport on the process that is hosting the listener, so the terminal and the phone stream the
same live session. ``firewall`` prints, and never runs, the elevated command that admits the
phone; see :mod:`hr_firewall`. The terminal is paired as a device of its own for the duration, because the
listener's gate is the one thing that admits a socket on every host - dashboard, desktop app or
gateway - and Node's ``WebSocket`` can carry a credential only in the URL.

This module must stay importable without FastAPI, uvicorn or an event loop. ``hermes odyssey`` runs
in the plain CLI process, which has no dashboard in it, and a heavyweight import here would be
paid by every ``hermes`` invocation that touches plugin CLI discovery.
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import sys
import time
from typing import Callable, List, Optional
from urllib.parse import urlencode

try:  # package import (``hermes_plugins.hermes_odyssey``)
    from . import (
        hr_devices,
        hr_firewall,
        hr_identity,
        hr_listener,
        hr_mdns,
        hr_pairing,
        hr_qr,
        hr_routes,
    )
except ImportError:  # standalone path load
    import hr_devices  # type: ignore[no-redef]
    import hr_firewall  # type: ignore[no-redef]
    import hr_identity  # type: ignore[no-redef]
    import hr_listener  # type: ignore[no-redef]
    import hr_mdns  # type: ignore[no-redef]
    import hr_pairing  # type: ignore[no-redef]
    import hr_qr  # type: ignore[no-redef]
    import hr_routes  # type: ignore[no-redef]

COMMAND_NAME = "odyssey"
COMMAND_HELP = "Pair a phone with this Hermes session (Odyssey)"
COMMAND_DESCRIPTION = (
    "Odyssey pairs an Android client with the dashboard on this machine over a TLS "
    "listener whose certificate the phone pins.\n\n"
    "  hermes odyssey pair --label 'Pixel 9'   show a pairing QR\n"
    "  hermes odyssey status                   what is paired, and is the listener up\n"
    "  hermes odyssey revoke <id>              end one device's access\n"
    "  hermes odyssey firewall                 the command that lets the phone through\n"
    "  hermes odyssey attach [--resume <id>]   open the TUI on the session the phone sees"
)

#: Label of the device ``attach`` pairs for the terminal. The pid is in it so a record left by a
#: terminal that died without revoking itself can be recognised and reaped on the next attach.
TERMINAL_LABEL_PREFIX = "terminal pid "
_TERMINAL_LABEL = re.compile(re.escape(TERMINAL_LABEL_PREFIX) + r"(\d+)$")

#: What ``attach`` launches once the environment is prepared. Resolved lazily to Hermes' own TUI
#: launcher, which replaces this process's job with the Ink app and exits with its code; tests set
#: it to a stub that records the environment instead.
LAUNCH: Optional[Callable[[Optional[str]], None]] = None


# ---- argparse tree ---------------------------------------------------------


def setup(parser) -> None:
    """Build the ``hermes odyssey`` subtree. Called by the dashboard's plugin CLI loader."""
    sub = parser.add_subparsers(dest="remote_command", metavar="<command>")

    pair = sub.add_parser("pair", help="Show a pairing QR for a new device")
    pair.add_argument(
        "--label", default="", help="What to call this phone in `hermes odyssey status`"
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

    sub.add_parser("firewall", help="Print the command that admits the phone through this OS's firewall")

    attach = sub.add_parser(
        "attach", help="Open the TUI as a second view of the process the phone is attached to"
    )
    attach.add_argument(
        "--resume", default="", metavar="ID", help="Open this stored session rather than a new one"
    )

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
    if command == "firewall":
        return _firewall(args)
    if command == "attach":
        return _attach(args)
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
    running = hr_listener.live_runtime()
    if running and running.get("port"):
        # A live listener is the authority on where the phone should connect; the configured port
        # is only what the next one will use.
        port = int(running["port"])

    try:
        device, token = hr_devices.create_device(args.label)
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not write the device store: {exc}")

    mdns_name = hr_mdns.local_name()
    uri = hr_pairing.build(
        hosts=hosts,
        port=port,
        fingerprint=identity.fingerprint_b64,
        token=token,
        name=hr_pairing.machine_name(),
        mdns_name=mdns_name,
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
    _out(f"  Discovery {_discovery(mdns_name, hosts)}")
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
    _out(f"  password. To end it:  hermes odyssey revoke {device.id}")
    _out()
    if not running:
        _out("  The listener starts with the desktop app, `hermes dashboard`, or the gateway")
        _out("  (`hermes gateway start`). None of them is hosting it right now.")
        _out()
    if _loopback_only(running):
        _out("  The listener is bound to loopback only, so no phone can reach it.")
        _out()
    else:
        _out("  Phone cannot connect? The firewall is the usual cause:  hermes odyssey firewall")
        _out()
    return 0


def _listener_port() -> int:
    running = hr_listener.live_runtime()
    return int(running["port"]) if running and running.get("port") else hr_listener.configured_port()


def _loopback_only(running: Optional[dict]) -> bool:
    host = str((running or {}).get("host") or hr_listener.configured_host())
    return host in ("127.0.0.1", "::1", "localhost")


# ---- firewall --------------------------------------------------------------


def _firewall(args) -> int:
    steps = hr_firewall.instructions(_listener_port())
    if steps is None:
        return _err("No firewall command is known for this platform; open the listener's TCP port inbound.")
    _out()
    _out(f"  Run in {steps.shell}:")
    _out()
    for line in steps.command.splitlines():
        _out(f"    {line}")
    _out()
    _out(f"  {steps.note}")
    _out()
    return 0


def _discovery(mdns_name: str, hosts: List[str]) -> str:
    """One line on whether this machine answers for ``<hostname>.local``, which is what lets a
    paired phone find it again after the address moves.

    The check is the phone's own query, sent from the LAN adapter so the OS responder answers
    with the LAN address rather than whichever interface the loopback copy happened to take.
    Nothing here can fix a responder that is off; the line says so and names the consequence.
    """
    if not mdns_name:
        return "no host name to offer; a moved address means pairing again"
    lan = next((h for h in hosts if h != "127.0.0.1"), None)
    answered = hr_mdns.resolve(mdns_name, interface=lan)
    if not answered:
        return (
            f"{mdns_name} is in the code, but this machine is not answering mDNS for it;"
            " a moved address means pairing again"
        )
    if lan and lan not in answered:
        return f"{mdns_name} answers with {', '.join(answered)}, not {lan}; check which adapter is the LAN"
    return f"{mdns_name} answers ({', '.join(answered)}); the phone can follow a moved address"


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
    upstream = int(runtime.get("upstream_port") or 0)
    # The gateway host has no dashboard behind it; it records upstream_port 0 and answers itself.
    where = f"proxying to 127.0.0.1:{upstream}" if upstream else "serving in-process"
    lines = [f"               hosted by {surface}, pid {runtime.get('pid')}, {where}"]
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
    _out(f"Odyssey {hr_routes.PLUGIN_VERSION}")
    _out()

    if identity is None:
        _out("  Certificate  none yet — `hermes odyssey pair` creates it")
    else:
        _out(f"  Certificate  SHA-256 {_grouped_fingerprint(identity.fingerprint_hex[:32])}")
        _out(f"                       {_grouped_fingerprint(identity.fingerprint_hex[32:])}")
        _out(f"               expires {identity.not_after.date().isoformat()}")

    runtime = hr_listener.read_runtime()
    if runtime is None:
        _out(f"  Listener     not recorded as running (would use port {hr_listener.configured_port()})")
    elif not hr_listener.pid_alive(runtime.get("pid")):
        # Left behind by a host that did not shut down cleanly. It holds nothing: the next host
        # overwrites it, and until then the phone has nothing to reach.
        _out(
            f"  Listener     not running; a stale record from {runtime.get('surface') or 'an unknown surface'}"
            f" (pid {runtime.get('pid')}, no longer alive) remains"
        )
    elif identity is None:
        _out("  Listener     recorded as running, but there is no certificate on disk")
    else:
        _out(f"  Listener     {_probe(str(runtime.get('host') or ''), int(runtime.get('port') or 0), identity.der)}")
        for line in _host_lines(runtime):
            _out(line)
    if not hr_listener.enabled():
        _out(f"               disabled by {hr_listener.ENV_ENABLED}")
    _out(f"  Discovery    {_discovery(hr_mdns.local_name(), hr_pairing.candidate_hosts())}")

    _out()
    try:
        devices = hr_devices.list_devices()
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not read the device store: {exc}")

    shown = [d for d in devices if args.all or not d.revoked]
    if not shown:
        _out("  No paired devices. `hermes odyssey pair` shows a code.")
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
        return _err("Which device? `hermes odyssey status` lists them, or use --all.")

    device_id = _resolve(devices, args.device.strip())
    if device_id is None:
        return _err(f"No single live device matches {args.device!r}. See `hermes odyssey status`.")
    if hr_devices.revoke_device(device_id):
        _out(f"Revoked {device_id}. Its token stops working now, and a session it has open closes")
        _out("within a few seconds.")
        return 0
    return _err(f"{device_id} was already revoked.")


# ---- attach ----------------------------------------------------------------


def _launcher() -> Callable[[Optional[str]], None]:
    if LAUNCH is not None:
        return LAUNCH
    from hermes_cli.main_tui_launch import _launch_tui

    return _launch_tui


def _reap_dead_terminals(devices: List) -> int:
    """Revoke terminal devices whose process is gone. A terminal revokes itself on exit; one that
    was killed outright cannot, and its token is then held by nobody, so ending it costs nothing."""
    reaped = 0
    for device in devices:
        match = _TERMINAL_LABEL.match(device.label)
        if device.revoked or match is None or hr_listener.pid_alive(match.group(1)):
            continue
        if hr_devices.revoke_device(device.id):
            reaped += 1
    return reaped


def _attach(args) -> int:
    running = hr_listener.live_runtime()
    if running is None:
        return _err(
            "Nothing is hosting the listener right now, so there is no process to attach to.\n"
            "Open the desktop app, run `hermes dashboard`, or start the gateway (`hermes gateway "
            "start`), then try again."
        )
    identity = hr_identity.existing_identity()
    if identity is None:
        return _err("There is no listener certificate on disk; `hermes odyssey pair` creates it.")
    port = int(running.get("port") or 0)
    probe = _probe(str(running.get("host") or ""), port, identity.der)
    if not probe.startswith("listening"):
        return _err(f"The listener is {probe}.")

    try:
        _reap_dead_terminals(hr_devices.list_devices())
        device, token = hr_devices.create_device(f"{TERMINAL_LABEL_PREFIX}{os.getpid()}")
    except hr_devices.DeviceStoreUnavailable as exc:
        return _err(f"Could not write the device store: {exc}")

    # The TUI's attach mode reads the socket URL from the environment and connects with Node's own
    # WebSocket, which takes nothing but a URL: the device token rides in the query, which the
    # listener honours on upgrades and never relays. The certificate is self-signed, so Node is
    # handed it as an extra trust anchor; the SAN carries 127.0.0.1, which is what makes that
    # enough. ``_launch_tui`` keeps an explicit URL rather than discovering one.
    query = urlencode({hr_listener.WS_DEVICE_QUERY: token})
    os.environ["HERMES_TUI_GATEWAY_URL"] = f"wss://127.0.0.1:{port}{hr_routes.GATEWAY_WS_PATH}?{query}"
    os.environ["NODE_EXTRA_CA_CERTS"] = str(identity.cert_path)
    resume = (args.resume or "").strip() or None
    _out(f"Attaching to the {running.get('surface') or 'listener'} host (pid {running.get('pid')}) as device {device.id}.")
    code = 0
    try:
        _launcher()(resume)
    except SystemExit as exc:  # the launcher exits with the TUI's code; the device must still end
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        # Ends the socket within the listener's revocation sweep if the TUI left it open.
        hr_devices.revoke_device(device.id)
    return code
