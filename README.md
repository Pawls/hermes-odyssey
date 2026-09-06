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

It starts and stops with `hermes dashboard`, binds every interface on port 9443, and reverse
proxies loopback 9119 including the WebSocket upgrade. The dashboard itself stays on 127.0.0.1.

| variable | default | what |
| --- | --- | --- |
| `HERMES_REMOTE_PORT` | `9443` | TLS port |
| `HERMES_REMOTE_HOST` | `0.0.0.0` | bind address |
| `HERMES_REMOTE_LISTENER` | on | set to `0` / `off` to load the plugin without opening a socket |

The listener authenticates every request and every upgrade against the device store before
forwarding anything. That is not defence in depth, it is the only defence: on a loopback bind the
dashboard trusts its peer, and a proxy is a loopback peer. There is no unauthenticated route, not
even a liveness one — the TLS handshake already tells a phone which machine answered.

The first LAN bind will raise a Windows Firewall prompt for the hermes venv `python.exe`. Private
networks only.

## Status

Phase 1 is done: the plugin loads, the device store works, the router is mounted and
bearer-gated, the TLS listener proxies HTTP and WebSocket traffic, and `hermes remote` pairs,
reports and revokes. 116 tests pass, and the listener has now been seen coming up inside a real
`hermes dashboard` run and refusing an anonymous request over TLS (`docs/PLAN.md` §5.8).

Not yet proven: the `0.0.0.0` bind, the firewall prompt it raises, and reachability from the
phone.

Phase 2 has started in `%USERPROFILE%\AndroidStudioProjects\HermesRemote`: the shared Kotlin
module and the frame codec. What reading the gateway settled about the wire — events are not
methods, the WebSocket has no newline delimiter, and reconnect replay is already specified — is
in `docs/PLAN.md` §5.9.
