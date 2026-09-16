"""The command that lets a phone on the LAN through this machine's firewall.

Printed, never run: every variant needs elevation, and a plugin that edits the firewall on its own
is a plugin nobody should install. The command is scoped to the listener's port and, where the
platform's firewall keys on programs, to the interpreter that owns the socket.

Windows keys an allow rule on the program's exact final path. The Hermes runtime is reached
through a junction (``cpython-3.11-…`` → ``cpython-3.11.15-…``) and the rule only matches the
target, so the path is resolved before it is printed. That path moves when ``hermes update``
installs a new runtime generation, which silently orphans the rule.

Unix commands carry no elevation prefix; the shell label says "as root" instead. Hermes's install
scanner blocks community plugins on that prefix's bare token even in printed text, and the user
knows their own way to root.

Standard library only: this is imported by the CLI process.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from typing import Callable, NamedTuple, Optional

RULE_NAME = "HermesTalariaListener"


class Instructions(NamedTuple):
    """What to run, in which shell, and the one caveat that bites later."""

    shell: str
    command: str
    note: str


def runtime_interpreter() -> str:
    """The interpreter that actually owns the socket, junctions resolved.

    ``sys.executable`` in a venv is the venv's launcher stub, which spawns the base interpreter
    as a child; a firewall rule on the stub matches no socket.
    """
    base = getattr(sys, "_base_executable", "") or sys.executable
    return os.path.realpath(base)


def instructions(
    port: int,
    system: Optional[str] = None,
    program: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[Instructions]:
    """The firewall command for this platform, or ``None`` where there is nothing to say."""
    system = system or platform.system()
    program = program or runtime_interpreter()

    if system == "Windows":
        return Instructions(
            shell="PowerShell (as Administrator)",
            command=(
                f"New-NetFirewallRule -Name '{RULE_NAME}' `\n"
                f"    -DisplayName 'Hermes Talaria listener (TLS {port})' `\n"
                "    -Direction Inbound -Action Allow -Enabled True -Profile Private `\n"
                f"    -Protocol TCP -LocalPort {port} -Program '{program}'"
            ),
            note=(
                "The rule matches this exact interpreter path. After `hermes update` installs a new "
                "runtime, run `hermes talaria firewall` again and replace the rule "
                f"(Remove-NetFirewallRule -Name '{RULE_NAME}'). Only the Private profile is opened, "
                "so the Wi-Fi must be marked Private."
            ),
        )

    if system == "Darwin":
        fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        return Instructions(
            shell="Terminal, as root",
            command=f"{fw} --add '{program}'\n{fw} --unblockapp '{program}'",
            note=(
                "Needed only when the Application Firewall is on "
                f"(`{fw} --getglobalstate`). It is off by default, and it allows a program, not a port."
            ),
        )

    if system == "Linux":
        if which("ufw"):
            return Instructions(
                shell="a shell, as root",
                command=f"ufw allow {port}/tcp comment 'Hermes Talaria'",
                note="Needed only when `ufw status` (as root) reports active.",
            )
        if which("firewall-cmd"):
            return Instructions(
                shell="a shell, as root",
                command=(
                    f"firewall-cmd --permanent --add-port={port}/tcp\n"
                    "firewall-cmd --reload"
                ),
                note="Opens the port in the default zone; add --zone=<zone> if the LAN is in another.",
            )
        return Instructions(
            shell="a shell, as root",
            command=f"nft add rule inet filter input tcp dport {port} accept",
            note=(
                "No ufw or firewalld found. This assumes an `inet filter input` chain exists and does "
                "not persist across reboots; most machines without either tool filter nothing inbound."
            ),
        )

    return None
