> Turn hermes-remote into a distributable Hermes plugin named hermes-talaria, and make a phone-sent
> message land in the session Paul is looking at. Start with V1: it is the only slice that turns the
> failure that already happened into something a person can see, and every later slice is verified
> through it. Diff sign-off: `git diff --stat` on both repos before each commit; the Android repo is
> `%USERPROFILE%\AndroidStudioProjects\HermesRemote`.

## Evidence

Gathered 2026-09-10, from the code and then from the logs.

- **The lost message was refused, not dropped.** `logs/agent.log` 13:57:03: `Refused active session
  20260910_130920_860de2: already held by pid=31536 surface=tui`. The phone's `session.resume` had
  succeeded at 13:49:42 (agent built in the dashboard process, Mnemosyne initialised for that session);
  the first `prompt.submit` then tried to claim the cross-process lease
  (`session_lifecycle._ensure_active_session_slot` → `active_sessions.try_acquire_active_session`) and
  lost to the TUI that owns the session. That is the 4090 path (`methods_prompt.py:563`), not the 4001
  stale-runtime path the first draft of this plan named. `Connection.kt:220` discards the outcome, so
  the app showed a sent-looking bubble and nothing else.
- **One live owner per session is a Hermes correctness rule**, enforced through the on-disk active
  session registry (`hermes_cli/active_sessions.py`). A second process can never run a turn in a session
  another surface has open, so no amount of client-side retry gets a phone turn into the TUI's session.
- **Three processes, one database.** `hermes --tui` is Ink over stdio to its own gateway; the desktop
  app spawns its own headless `hermes serve --port 0` (`apps/desktop/electron/backend-command.ts:21`);
  `hermes dashboard` on 9119 is what the phone reaches through 9443. They share only `state.db`. The
  desktop re-pulls an open transcript on the gateway's `sessions.changed` broadcast (state.db mtime,
  `wiring.tsx:443`), so a turn run elsewhere appears there once stored. The Ink TUI handles no
  `sessions.changed` and shows a foreign turn only after `/resume`.
- **Live sharing exists, but only inside one process.** `_resume_reuse_live` (`methods_session.py:644`)
  attaches a second client alongside the ones already streaming, and `session_transports.py` fans every
  event out to all of them. A prompt from any attached client streams to all. So "appear in the window I
  sent from" means "the phone must be in the window's process".
- **The desktop's process already mounts this plugin.** `_mount_plugin_api_routes()` is unconditional
  (`hermes_cli/web_server.py:971`), so the desktop's `hermes serve` imports `dashboard/api.py`, arms the
  9443 listener, and proxies to its own bound port (`hr_listener._deferred_start`). With no separate
  `hermes dashboard` running, the phone is already in the desktop's process. When both run, both race
  for 9443 and the loser only logs `state.error`; which one the phone reaches is silent.
- **The TUI has an attach mode that nothing serves yet.** `HERMES_TUI_GATEWAY_URL`
  (`ui-tui/src/gatewayClient.ts:51`) makes Ink connect to an existing gateway instead of spawning one.
  `hermes --tui --resume` calls `shared_session_attach.discover_attach_url`, which expects the owner to
  advertise `metadata.shared_runtime_url` and serve `/api/session-attach`; nothing in this tree does
  either. The chat PTY path (`web_server_chat.py:375`) does set the variable to attach into the
  dashboard's own gateway, so the client half is exercised.
- No core patches from this project: nothing in the hermes-agent diff mentions the plugin or 9443.
- Naming is clear: the curated catalog has no hermes-remote and nothing close to talaria.
- Caveat: this machine's tui_gateway carries 69 locally modified core files with LOCAL PATCH markers
  from unrelated work. Every protocol claim above was read against that tree, never against stock
  Hermes. Diff against the upstream blob before relying on one.

## Slices

### V1 — Surface RPC failures in the app · Fable 5.1 / medium

`prompt.submit` handles its outcome the way `approval.respond` does. On `Failed` or `Dropped`, the local
bubble is marked failed, a `Trouble` row carries the gateway's own message underneath it, and
`state.error` is set. The 4090 message is passed through verbatim because it names the surface and pid
holding the session, which is the answer to "where did my message go".

Visible: sending into a session the TUI has open shows the bubble outlined in the danger colour with
"Session … already has a live owner (tui, pid …)" under it. Sending with the socket gone shows "not
connected" instead of a bubble that looks sent.

Verify: `gradlew :shared:jvmTest --offline` with a scripted 4090 refusal and a scripted dropped socket;
then a real send from the phone into the session the TUI holds.

Kill: none. This slice cannot be wrong, only incomplete.

Status 2026-09-10: shipped as HermesRemote abe1835. 66 shared tests pass (three new). Debug build
installed on the phone; the real send into the TUI-held session is Paul's to fire and is the one
thing still unverified.

### V2 — Make the refusal actionable · Fable 5.1 / medium

On 4090 the app knows which surface owns the session. Offer the two things that are true: keep reading
it (the transcript is stored and `sessions.changed` is not needed for that), or open a new session on
the phone. On 4001 or 4007 re-resume the stored session id once and retry, using the
runtime-id-changed reseed rule in `docs/PLAN.md` §5.11. Then run the end-to-end send that has never
been fired at a live gateway, against a scratch session nothing else holds.

Kill: if the 4090 error `data` carries no owner fields, parse the message; if that is brittle, show it
verbatim and stop.

Status 2026-09-10: shipped as HermesRemote fb664cd, installed on the phone. The 4090 `data` carries
`reason: SESSION_NOT_OWNED` and nothing else, so the message stays verbatim and the reason gates the
owned branch. The real send was fired from a script over the phone's exact path (pair, health,
ws-ticket, /api/ws): `session.create`, then `prompt.submit` answered `{"status": "streaming"}`,
`message.start` and `message.complete` followed. The turn's text was the inference server refusing
the model, which is the desktop's state. Scratch device revoked and session deleted afterwards.
Observed while doing it: 9443 was proxying to pid 27388, which is the desktop app's
`hermes serve --host 127.0.0.1 --port 0`, so the scratch session was created inside the desktop
app's process - the V3 host case is already what happens today. Also `hermes remote status` prints
"listening on 127.0.0.1:9443" while netstat shows the bind on 0.0.0.0; the status line reports the
wrong host and V3 should fix it in passing.

### V3 — The listener follows the surface Paul is looking at · Fable 5.1 / high

Replaces the former "standalone serving mode". The host for 9443 must be the process whose window
Paul is watching, because that is the only process a phone turn can stream into.

- Desktop app open: its `hermes serve` is the host. Prove that a phone `session.resume` of the session
  the desktop has open takes `_resume_reuse_live`, and that a phone `prompt.submit` streams into the
  desktop window. This is expected from reading `session_transports.py` and is unproven by a run.
- Two candidate hosts: a deterministic rule instead of a bind race. Proposal: the desktop wins, and a
  `hermes dashboard` that finds 9443 taken by a live hermes-remote host says so at startup rather than
  logging `state.error` and going quiet. `hermes remote status` reports which process is hosting.
- No window open: the always-on gateway hosts, so the phone can start a session from nothing. This is
  the old V3 and keeps its kill criterion: if `handle_ws` cannot be driven from the bare-ASGI listener
  without pulling the dashboard's app state in, host a minimal Starlette app instead. Keep arming
  explicit so no socket opens in the plain CLI (`plugin/__init__.py:14`).

Verify: three runs, one per bullet, each ending in a phone message visible in the surface named.

### V4 — TUI as a viewer of the shared process · Fable 5.1 / high

`hermes remote attach [--resume <id>]` launches `hermes --tui` with `HERMES_TUI_GATEWAY_URL` pointing at
the hosting process's `/api/ws`, so the terminal becomes a second transport on the same live session
and a phone turn streams into it. Auth: the plugin already mints WS tickets; decide whether the URL
carries a ticket or the loopback session token, and never print either.

Kill: if Ink's attach mode cannot resume a named session without the unserved `/api/session-attach`
handshake, implement that route in the plugin instead of the launcher, and reconsider whether upstream
should own it.

### V5 — mDNS discovery · Opus 5 / medium

Replace the address baked into the pairing payload by `hr_pairing.build`, so the QR survives a moved
DHCP lease. The reservation on the gateway is the cheap version and stays until this lands.

### V6 — Rename to hermes-talaria · Opus 5 / medium

Across `plugin.yaml`, `dashboard/manifest.json`, `hr_routes.PLUGIN_NAME`, `plugins.enabled`, the
Android package, and the pairing URI scheme. Ship a migration note: an existing paired phone must pair
again. After V1–V5 so the rename lands once.

### V7 — Distribution · Opus 5 / medium

Firewall setup emitted as a command by `hermes remote pair` rather than living in the README, plus the
macOS and Linux equivalents. Then catalog packaging so `hermes plugins install` works, and an APK
story for the phone half.

## Rejected

- **Retry on 4001 as the fix for the lost message.** The refusal on record was 4090, which no retry
  clears. Kept as a small part of V2 because the 4001 path is real, just not what happened.
- **Fan-out through `/api/pub` for a read-only live view across processes.** Still possible, but it
  only ever shows a turn; it cannot let the phone send into a session another process owns. Same-process
  attachment (V3, V4) gives both, using mechanisms Hermes already has.
