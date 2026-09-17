# Odyssey for Hermes Agent

Use a live [Hermes Agent](https://github.com/NousResearch/hermes-agent) session from your Android
phone: chat, stream turns, answer approvals and prompts, and resume sessions, over your own Wi-Fi.
This repository is the desktop half, a Hermes plugin. The phone connects straight to your machine
over TLS pinned to a certificate your desktop generates; there is no account and no cloud service.

Not affiliated with Nous Research. Everything here is free.

## Requirements

- Hermes Agent 0.21 or newer, on Windows, macOS, or Linux (so far exercised on Windows)
- An Android phone running Android 8.0 or newer, on the same network as the desktop

## Install

```
hermes plugins install Pawls/hermes-odyssey/plugin --enable
hermes gateway restart
```

Then get the app from the [latest release](https://github.com/Pawls/hermes-odyssey/releases/latest)
(`odyssey-<version>.apk`; allow installs from your browser or file manager when Android asks). A
Play Store listing, "Odyssey for Hermes Agent", is coming. Release APKs are signed with a
certificate whose SHA-256 is

```
61:76:6b:b4:d8:37:38:fe:55:c2:45:fa:8a:15:7b:a9:a9:93:36:c5:9e:7a:0a:9c:d3:76:da:01:77:fd:c6:7d
```

and `apksigner verify --print-certs odyssey-<version>.apk` shows it.

## Pair

```
hermes odyssey pair --label "My phone"
```

Scan the code with the app, and tap **Same, pair** only if the fingerprint on the phone matches
the one under the code in your terminal. If the phone cannot reach the desktop, run
`hermes odyssey firewall` and run the command it prints (see *Getting through the firewall*).
`hermes odyssey status` lists paired phones and `hermes odyssey revoke <id>` removes one.

## Development

`docs/PLAN.md` is the contract and the reasoning; the rest of this file is the map.

`hermes-agent` is upstream NousResearch code that `hermes update` pulls, and its
`plugins/AGENTS.md` says plugins never touch core. So this lives out of tree and reaches
Hermes through `register(ctx)` alone.

For development, link the working copy into `$HERMES_HOME/plugins/` instead of installing it.
On Windows (`%LOCALAPPDATA%\hermes\plugins\`) a directory junction needs no administrator rights:

```
mklink /J "%LOCALAPPDATA%\hermes\plugins\hermes-odyssey" "<this checkout>\plugin"
```

A user plugin's Python is imported only when its name is in `plugins.enabled` in
`config.yaml` (GHSA-mcfc-hp25-cjv7), so the link alone does nothing: add `hermes-odyssey` there,
or run `hermes plugins enable hermes-odyssey`.

## Layout

| path | what |
| --- | --- |
| `plugin/` | the Hermes plugin: device auth provider, API router, pairing CLI, TLS listener |
| `plugin/hr_identity.py` | the self-signed P-256 certificate the phone pins |
| `plugin/hr_listener.py` | the TLS reverse proxy in front of loopback 9119, and the rule for which process hosts it |
| `plugin/hr_gateway_host.py` | the same socket answered in-process inside `hermes gateway run`, for when no window is open |
| `plugin/hr_history.py` | transcript pages for the phone (`GET /messages?session_id=&before=&limit=`), read from `state.db`, tool rows with their results |
| `plugin/hr_cli.py`, `hr_qr.py`, `hr_pairing.py` | `hermes odyssey`, its QR encoder, and the payload |
| `plugin/hr_firewall.py` | the per-OS firewall command `hermes odyssey firewall` prints |
| `plugin/dashboard/` | what the dashboard imports: `manifest.json` + `api.py` |
| `tests/` | run with the Hermes venv; see below |
| `docs/PLAN.md` | the plan, the protocol findings, the risks |

## Running the tests

The plugin imports `hermes_cli.dashboard_auth` for the provider ABC, so the tests need the
Hermes interpreter rather than a venv of their own:

```
"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" -m pytest tests -q
```

They redirect `HERMES_HOME` and the device store to temp directories, so they never read or
write the real `%LOCALAPPDATA%\hermes\odyssey\`.

## Security rules

This repository is network-facing code holding a signing key. The three rules carry over
from PawlRemote unchanged, and the listener is written to them:

1. **Pin the certificate, do not validate it.** The desktop's certificate is self-signed and
   there is no CA to check it against. The phone stores the whole leaf DER at pairing and
   compares bytes on every connection. Do not use OkHttp's `CertificatePinner`, which hashes
   the SubjectPublicKeyInfo rather than the certificate.
2. **Store the token like a password.** The desktop keeps only `sha256(salt || secret)`
   (`plugin/hr_devices.py`); the phone keeps the token in encrypted storage. Neither writes
   it to a log.
3. **Confirm the fingerprint by eye, once.** Pairing shows the certificate fingerprint on
   both screens. A man in the middle survives everything else and dies here.

Two corollaries specific to Hermes, both load-bearing:

- **The dashboard session token never leaves the machine.** On a loopback bind it is the only
  credential `/api/ws` accepts, and it is a process-lifetime master key. The TLS listener
  attaches it when it proxies an upgrade; no response body ever carries it. In gated mode the
  phone gets a 30-second single-use ticket instead, which is what the browser SPA already
  gets.
- **A bearer accepted by some other token provider is not a paired phone.** The dashboard's
  token seam authenticates any registered provider's token on a registered route, so every
  route here checks *which* provider vouched for the caller before it does anything.

## Pairing a phone

```
hermes odyssey pair --label "Pixel 9"
hermes odyssey status
hermes odyssey revoke <id>
hermes odyssey firewall
hermes odyssey attach [--resume <id>]
```

`pair` draws a QR in the terminal and prints the certificate fingerprint underneath it. Compare
that fingerprint with the one the phone shows, because rule 3 above is the only rule a machine in
the middle does not survive.

The code in the QR **is** the credential, and unlike PawlRemote it is not a short-lived offer: it
stays valid until the device is revoked. Clear the screen once the phone has it, and treat a
screenshot of it as a password.

The code also carries this machine's `<hostname>.local`. When the stored address stops answering,
the phone asks the LAN for that name over mDNS and tries what comes back, so a DHCP lease that
moves no longer means pairing again. Nothing is advertised and nothing is installed for this: the
OS already answers for its own name (Windows' `Dnscache`, macOS' `mDNSResponder`, Avahi), and the
`Discovery` line under the code, and in `status`, reports whether it does here. A spoofed answer
buys an attacker one probe that the certificate pin then refuses; discovery decides what to try,
never what to believe. A phone paired before this landed has no name stored and walks its list as
before; pair it again to give it one.

## Sitting beside the phone in a terminal

A plain `hermes --tui` spawns a gateway of its own, and that gateway then owns any session it
opens: a phone turn into it is refused with 4090, because Hermes allows one live owner per
session. `hermes odyssey attach` launches the same Ink TUI in its attach mode instead, pointed at
`/api/ws` on whichever process is hosting the listener, so the terminal is a second transport on
the phone's process and both stream the same turn. `--resume <id>` opens a stored session; without
it the TUI starts a new one, which the phone can then resume.

The terminal is paired as a device of its own for the duration, labelled `terminal pid N`, and
revoked when the TUI exits. It has to be: the listener's gate is the one thing that admits a
socket on every host, and Node's `WebSocket` takes a URL and nothing else, so the token rides in
the upgrade query (`?device=`), which the listener honours on upgrades only and strips before the
dashboard sees the query. The TUI trusts the listener's self-signed certificate through
`NODE_EXTRA_CA_CERTS`, which `attach` sets for the child; a value already in the environment is
replaced for that process. A terminal killed outright leaves its device record behind, and the
next `attach` revokes any whose pid is gone.

## The listener

It starts and stops with the process that mounts the dashboard router, binds every interface on
port 9443, and reverse proxies that process's loopback port including the WebSocket upgrade. The
dashboard itself stays on 127.0.0.1.

### Which process hosts it

Two processes mount the dashboard router and therefore arm this listener: `hermes dashboard` on
9119, and the desktop app's own headless `hermes serve --port 0`. Only one can hold 9443, and it
has to be the one whose window you are looking at: a phone can stream into a live session only
from inside the process that owns it, because Hermes fans events out across the transports of one
process and allows one live owner per session across processes. So the desktop app outranks the
dashboard. The holder records itself in `odyssey/listener.json`; a higher-ranked candidate writes
`odyssey/listener-claim.json`, the holder yields on its next sweep, the claimant binds on its next
retry, and the phone's reconnect lands it in the new host. Opening or closing the desktop app
therefore moves the phone within a few seconds. `hermes odyssey status` says which process is
hosting and, while a handover is pending, which one is waiting.

With no window open at all, the always-on `hermes gateway run` hosts it (`hr_gateway_host.py`).
The gateway serves no HTTP, so there is nothing to proxy to; instead the listener answers the
plugin routes itself and terminates `/api/ws` in-process by handing a Starlette socket to the same
`tui_gateway.ws.handle_ws` the dashboard mounts. `/health` and `/ws-ticket` report `mode: direct`
there, and the ticket is null: the bearer on the upgrade is the whole credential. The gateway ranks
below both windows, so it yields the port the moment one opens and takes it back when it closes,
each within a few seconds. It arms only in a process whose argv is a real `gateway run` by the
gateway's own matcher, and only once the gateway's PID file names that process, so a `gateway
status`, a child of the gateway, or a second `gateway run` left behind by `hermes update` never
opens the socket. The socket runs on a thread and loop of its own inside the gateway; nothing on
the RPC path needs the gateway's loop, and a phone streaming tokens should not compete with the
platform adapters for it.

| variable | default | what |
| --- | --- | --- |
| `HERMES_ODYSSEY_PORT` | `9443` | TLS port |
| `HERMES_ODYSSEY_HOST` | `0.0.0.0` | bind address |
| `HERMES_ODYSSEY_LISTENER` | on | set to `0` / `off` to load the plugin without opening a socket |

The listener authenticates every request and every upgrade against the device store before
forwarding anything. That is not defence in depth, it is the only defence: on a loopback bind the
dashboard trusts its peer, and a proxy is a loopback peer. There is no unauthenticated route, not
even a liveness one — the TLS handshake already tells a phone which machine answered.

An open WebSocket is checked too, and separately, because its bearer was only ever on the upgrade.
The listener holds the device id beside each proxied socket and re-reads the device store every
five seconds, closing with 1008 any socket whose device has been revoked. `hermes odyssey revoke`
therefore ends a live session rather than only the next request. The store is the channel because
the CLI runs in its own process; an unreadable store closes nothing, since a disk error that
answered "no devices are paired" would drop every session on the machine.

### Getting through the firewall

`hermes odyssey firewall` prints the command for this OS, scoped to the listener's port: a Windows
allow rule on the runtime interpreter, the macOS Application Firewall's `--unblockapp`, or `ufw`,
`firewalld` or `nft` on Linux, whichever is installed. It prints and never runs it, because every
variant needs elevation. `pair` points at it. The rest of this section is why the Windows rule
looks the way it does.

The first LAN bind raises a Windows Firewall prompt. Answering it *no* writes two inbound `Block`
rules that suppress the prompt for good, and the program they name is the Hermes **runtime**
interpreter — `.hermes-runtime\python\generation-…\cpython-3.11.15-…\python.exe` — not the venv
one. A rule written against the venv `python.exe` is the wrong rule.

The runtime is reached through a junction (`cpython-3.11-…` → `cpython-3.11.15-…`) and Windows keys
the rule on the resolved path, so the command names the junction's target.

This is what is installed here instead of answering the prompt, and it is narrower than what the
prompt would have created, which allows the whole interpreter on every inbound port:

```powershell
New-NetFirewallRule -Name 'HermesOdysseyListener' `
    -DisplayName 'Hermes Odyssey listener (TLS 9443)' `
    -Direction Inbound -Action Allow -Enabled True -Profile Private `
    -Protocol TCP -LocalPort 9443 -Program '<the runtime python.exe>'
```

Elevation required, and Windows resolves the program by exact path, so `hermes update` moving to a
new `generation-…` directory will silently stop matching. If the phone stops connecting after an
update, check this rule's `Program` first.

## Development history

Phase 1 is done: the plugin loads, the device store works, the router is mounted and
bearer-gated, the TLS listener proxies HTTP and WebSocket traffic, and `hermes odyssey` pairs,
reports and revokes. 116 tests pass, and the listener has now been seen coming up inside a real
`hermes dashboard` run and refusing an anonymous request over TLS (`docs/PLAN.md` §5.8).

Phases 2 and 3 are done in the app's own repository: the shared Kotlin
protocol, and an Android app on top of it. An emulator has paired over the LAN address, listed the
real session store, resumed a session and rendered its history, survived the desktop restarting
underneath it, and refused itself after `hermes odyssey revoke`. See `docs/PLAN.md` §5.9–§5.11.

The `0.0.0.0` bind is proven: `https://192.168.1.50:9443` answers with TLS 1.3, the exact leaf the
phone pins, and `401` with no bearer.

Both Phase 4 blockers are now closed (`docs/PLAN.md` §5.12). Revocation ends a live socket:
against a real dashboard, an idle authenticated WebSocket was closed with 1008 five seconds after
`hermes odyssey revoke`. And the two inbound `Block` rules are gone, replaced by the scoped allow
above, so nothing on this machine blocks inbound 9443 any more.

A **physical** phone has now reached it. On the Wi-Fi, with no app installed, its browser opened
`https://192.168.1.50:9443/` and got `{"detail": "unauthorized"}` — the filter crossed, TLS
completed, and the bearer gate answering a request with no credential. Every earlier run originated
on this host, where traffic to a local interface never traverses the firewall at all, so this is
the first proof the allow rule matches anything.

The address is no longer the only thing the phone has. The code carries `<hostname>.local` too,
and a phone whose stored address has gone silent resolves it over mDNS (see *Pairing a phone*).
This machine keeps its DHCP reservation anyway: a name that resolves is the cure for a moved
address, a reservation is what stops it moving, and the two cost nothing together.
