# hermes-remote

The desktop half of a phone client for a live [Hermes](https://github.com/NousResearch/hermes-agent)
session on the LAN. The phone half is `%USERPROFILE%\AndroidStudioProjects\HermesRemote`.

`docs/PLAN.md` is the contract and the reasoning. Read it first; this file is only the map.

## Why a plugin and not a fork

`hermes-agent` is upstream NousResearch code that `hermes update` pulls, and its
`plugins/AGENTS.md` says plugins never touch core. So this lives out of tree and reaches
Hermes through `register(ctx)` alone.

Hermes discovers plugins in `$HERMES_HOME/plugins/`, which on this machine is
`%LOCALAPPDATA%\hermes\plugins\`. The working copy stays here and appears there as a
directory junction:

```
mklink /J "%LOCALAPPDATA%\hermes\plugins\hermes-remote" "%USERPROFILE%\source\repos\hermes-remote\plugin"
```

No administrator rights needed for a junction.

A user plugin's Python is imported only when its name is in `plugins.enabled` in
`config.yaml` (GHSA-mcfc-hp25-cjv7), so the junction alone does nothing. `hermes-remote` is
in that list.

## Layout

| path | what |
| --- | --- |
| `plugin/` | the Hermes plugin: device auth provider, API router, pairing CLI, TLS listener |
| `plugin/hr_identity.py` | the self-signed P-256 certificate the phone pins |
| `plugin/hr_listener.py` | the TLS reverse proxy in front of loopback 9119 |
| `plugin/hr_cli.py`, `hr_qr.py`, `hr_pairing.py` | `hermes remote`, its QR encoder, and the payload |
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
write the real `%LOCALAPPDATA%\hermes\remote\`.

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
hermes remote pair --label "Pixel 9"
hermes remote status
hermes remote revoke <id>
```

`pair` draws a QR in the terminal and prints the certificate fingerprint underneath it. Compare
that fingerprint with the one the phone shows, because rule 3 above is the only rule a machine in
the middle does not survive.

The code in the QR **is** the credential, and unlike PawlRemote it is not a short-lived offer: it
stays valid until the device is revoked. Clear the screen once the phone has it, and treat a
screenshot of it as a password.

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
dashboard. The holder records itself in `remote/listener.json`; a higher-ranked candidate writes
`remote/listener-claim.json`, the holder yields on its next sweep, the claimant binds on its next
retry, and the phone's reconnect lands it in the new host. Opening or closing the desktop app
therefore moves the phone within a few seconds. `hermes remote status` says which process is
hosting and, while a handover is pending, which one is waiting.

| variable | default | what |
| --- | --- | --- |
| `HERMES_REMOTE_PORT` | `9443` | TLS port |
| `HERMES_REMOTE_HOST` | `0.0.0.0` | bind address |
| `HERMES_REMOTE_LISTENER` | on | set to `0` / `off` to load the plugin without opening a socket |

The listener authenticates every request and every upgrade against the device store before
forwarding anything. That is not defence in depth, it is the only defence: on a loopback bind the
dashboard trusts its peer, and a proxy is a loopback peer. There is no unauthenticated route, not
even a liveness one — the TLS handshake already tells a phone which machine answered.

An open WebSocket is checked too, and separately, because its bearer was only ever on the upgrade.
The listener holds the device id beside each proxied socket and re-reads the device store every
five seconds, closing with 1008 any socket whose device has been revoked. `hermes remote revoke`
therefore ends a live session rather than only the next request. The store is the channel because
the CLI runs in its own process; an unreadable store closes nothing, since a disk error that
answered "no devices are paired" would drop every session on the machine.

### Getting through the Windows firewall

The first LAN bind raises a Windows Firewall prompt. Answering it *no* writes two inbound `Block`
rules that suppress the prompt for good, and the program they name is the Hermes **runtime**
interpreter — `.hermes-runtime\python\generation-…\cpython-3.11.15-…\python.exe` — not the venv
one. A rule written against the venv `python.exe` is the wrong rule.

This is what is installed here instead of answering the prompt, and it is narrower than what the
prompt would have created, which allows the whole interpreter on every inbound port:

```powershell
New-NetFirewallRule -Name 'HermesRemoteListener' `
    -DisplayName 'Hermes Remote listener (TLS 9443)' `
    -Direction Inbound -Action Allow -Enabled True -Profile Private `
    -Protocol TCP -LocalPort 9443 -Program '<the runtime python.exe>'
```

Elevation required, and Windows resolves the program by exact path, so `hermes update` moving to a
new `generation-…` directory will silently stop matching. If the phone stops connecting after an
update, check this rule's `Program` first.

## Status

Phase 1 is done: the plugin loads, the device store works, the router is mounted and
bearer-gated, the TLS listener proxies HTTP and WebSocket traffic, and `hermes remote` pairs,
reports and revokes. 116 tests pass, and the listener has now been seen coming up inside a real
`hermes dashboard` run and refusing an anonymous request over TLS (`docs/PLAN.md` §5.8).

Phases 2 and 3 are done in `%USERPROFILE%\AndroidStudioProjects\HermesRemote`: the shared Kotlin
protocol, and an Android app on top of it. An emulator has paired over the LAN address, listed the
real session store, resumed a session and rendered its history, survived the desktop restarting
underneath it, and refused itself after `hermes remote revoke`. See `docs/PLAN.md` §5.9–§5.11.

The `0.0.0.0` bind is proven: `https://192.168.1.50:9443` answers with TLS 1.3, the exact leaf the
phone pins, and `401` with no bearer.

Both Phase 4 blockers are now closed (`docs/PLAN.md` §5.12). Revocation ends a live socket:
against a real dashboard, an idle authenticated WebSocket was closed with 1008 five seconds after
`hermes remote revoke`. And the two inbound `Block` rules are gone, replaced by the scoped allow
above, so nothing on this machine blocks inbound 9443 any more.

A **physical** phone has now reached it. On the Wi-Fi, with no app installed, its browser opened
`https://192.168.1.50:9443/` and got `{"detail": "unauthorized"}` — the filter crossed, TLS
completed, and the bearer gate answering a request with no credential. Every earlier run originated
on this host, where traffic to a local interface never traverses the firewall at all, so this is
the first proof the allow rule matches anything.

One thing to keep pinned. The pairing code carries the desktop's address, so give this machine a
DHCP **reservation** rather than a lease. A lease that moves strands every paired phone with no
visible cause, and re-pairing is the only cure until the mDNS item in Phase 4 lands.
