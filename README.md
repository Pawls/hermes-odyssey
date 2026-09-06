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

## Status

Phase 1, first slice: the plugin loads, the device store works, and the router is mounted and
bearer-gated end to end. Still to come in Phase 1: the TLS listener and the `hermes remote`
pairing CLI. See `docs/PLAN.md` §5.6.
