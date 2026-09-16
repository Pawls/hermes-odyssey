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

The host for 9443 must be the process whose window Paul is watching, because that is the only
process a phone turn can stream into. Two processes arm the listener: `hermes dashboard` and the
desktop app's headless `hermes serve --port 0`. The desktop outranks the dashboard.

Mechanism (`hr_listener.py`): the holder records `surface` in `remote/listener.json`; a candidate
that outranks a live holder writes `remote/listener-claim.json` and retries the bind every 3 s; the
holder's tick sees the claim and releases the port; the claimant binds on its next retry; the
phone's reconnect lands it in the new host. A record from a dead pid holds nothing. A record with no
`surface` (a pre-upgrade listener) is never displaced. `hermes remote status` names the bind host,
the hosting surface and pid, and a pending handover.

Status 2026-09-10: shipped. Proven twice against real processes, in an isolated `HERMES_HOME` on
port 9444 so the live host was untouched:

- Same-process live sharing (bullet one): client A created a session, client B resumed the stored
  key and got A's runtime id back (`_resume_reuse_live`), B submitted, and A received every event of
  B's turn through `message.complete`. This is the desktop-window case with the window played by a
  second WebSocket client.
- Handover (bullet two): dashboard hosting at 17:32:00; a `HERMES_SERVE_HEADLESS=1 hermes serve`
  started at 17:32:32, claimed, the dashboard yielded at 17:32:33, the serve bound at 17:32:35, the
  dashboard settled into waiting at 17:32:36. Killing the serve: the dashboard re-hosted within 9 s.
- Found on the way: uvicorn's `startup()` raises `SystemExit` on a failed bind rather than setting
  `should_exit`, so the old "could not bind" branch never ran. Caught now.
- The live desktop backend (pid 27388) still runs the pre-upgrade listener, so status shows it as
  "unknown surface" until the desktop app restarts. The rule treats it as a holder to wait behind.

Ten new tests (`tests/test_host_rule.py`), suite at 129. What is not proven: a real phone crossing a
handover, which needs the desktop app restarted with the new plugin and Paul's thumb.

### V4 — Host when no window is open · Fable 5.1 / high

The old V3's third bullet, on its own because it is the one with real unknowns. With neither the
desktop app nor a dashboard running, nothing holds 9443 and the phone cannot start a session.
`hermes gateway run` is always on here (pid 35976) but serves no HTTP at all, so the listener would
have to terminate `/api/ws` itself inside the gateway process: build a `starlette.websockets.WebSocket`
from the bare ASGI scope and hand it to `tui_gateway.ws.handle_ws`, which needs only `accept`,
`receive_text`, `send_text` and `close` on it. `server.dispatch` runs in any process that imports
`tui_gateway.server` (the desktop's serve proves no `main()` is needed).

Unknowns to read first: whether `tui_gateway.server` can be imported inside the messaging gateway
process without its module-level threads and stdio transport colliding with the gateway's own loop;
how the plugin knows it is in the gateway process, since `register(ctx)` runs everywhere and arming
must stay explicit (`plugin/__init__.py:14`); and where the gateway's event loop can be joined from.
Rank: `gateway` is already the lowest surface in the host rule, so it yields to either window.

Kill: if `handle_ws` cannot be driven from the bare-ASGI listener without pulling the dashboard's
app state in, host a minimal Starlette app instead. If the gateway process cannot import
`tui_gateway.server` cleanly, stop and consider a separate always-on `hermes serve` instead.

Status 2026-09-15: shipped. The three unknowns, answered by reading and then by a run:

- `tui_gateway.server` is already imported in every gateway process: `run_startup.py:667` does
  it unconditionally to start the Group Chat worker. Its stdout redirect and threads are the
  gateway's normal state.
- The plugin does not know it is the gateway from `_HERMES_GATEWAY`: discovery runs from
  `hermes_cli.main` before `gateway.run` is imported, so the marker is absent at `register`
  time (found in the first live run, where the plugin registered and never armed). Arming is
  gated on argv by the gateway's own `looks_like_gateway_command_line` and on
  `gateway.status.get_running_pid()` naming this process, checked in the host thread before
  the first bind. A `gateway status`, a child, or a `--replace` loser never hosts.
- No loop to join: the socket runs on its own thread and loop (`hr_gateway_host.arm`).
  `WSTransport` binds to whichever loop accepts the socket and marshals writes onto it, so
  `handle_ws` needs nothing from the gateway's loop. Neither kill criterion fired: `handle_ws`
  ran on a `starlette.websockets.WebSocket` built from the bare scope, with the ASGI connect
  message replayed to it because Starlette's `accept()` consumes it.

Proven in an isolated `HERMES_HOME` on 9444: the gateway (pid 20024) bound with
`upstream_port: 0`; over the phone's exact path `/health` answered `mode: direct`, `/ws-ticket`
a null ticket, `/api/ws` sent `gateway.ready`, `session.create` answered, `prompt.submit` answered
`streaming`, and `message.start`, `thinking.delta` and `message.complete` followed (the turn's
text was LiteLLM refusing the keyless scratch home, which is the provider's state). A headless
serve then claimed the port and the gateway yielded in 3 s; killing the serve, the gateway
re-hosted in 3 s. Scratch home, device and session deleted afterwards. `hermes remote status`
now says "serving in-process" for this host and calls out a record from a dead pid as stale
instead of reporting it as hosting. Ten new tests (`tests/test_gateway_host.py`), suite at 139.

Live 2026-09-15: `hermes gateway restart` left one gateway (launcher pid 27056, runtime child
28616; the pre-update pair 39992/19240 was the same launcher-plus-child shape, not a stale
duplicate). The child bound 0.0.0.0:9443 six seconds after start, `hermes remote status` says
"hosted by gateway, pid 28616, serving in-process", and a scripted send over the phone's exact
path against the live host (pair, health, ws-ticket, `/api/ws`, `session.create`,
`prompt.submit`, `message.start` through `message.complete`) succeeded with no window open. The
turn's text was LiteLLM unable to reach `qwen3.8-27b-vision`, which is the inference server's
state. `session.delete` over the socket answered 4023 while the lease was live; `hermes sessions
delete` removed it afterwards, and the scratch device was revoked. The phone-in-hand send is
still Paul's to fire but nothing in the path is unexercised.

### V5 — TUI as a viewer of the shared process · Fable 5.1 / high

`hermes remote attach [--resume <id>]` launches `hermes --tui` with `HERMES_TUI_GATEWAY_URL` pointing at
the hosting process's `/api/ws`, so the terminal becomes a second transport on the same live session
and a phone turn streams into it. Auth: the plugin already mints WS tickets; decide whether the URL
carries a ticket or the loopback session token, and never print either.

Kill: if Ink's attach mode cannot resume a named session without the unserved `/api/session-attach`
handshake, implement that route in the plugin instead of the launcher, and reconsider whether upstream
should own it.

Status 2026-09-15: shipped. The auth question answered itself once the three hosts were lined up:
neither a ticket nor the loopback token exists on the gateway host, and the desktop's token is
known only inside its process, but the listener's device gate admits a socket on all three. So
the terminal is paired as a device of its own (`terminal pid N`) for the TUI's lifetime and
revoked on exit; a record left by a killed terminal is reaped on the next attach. Two things the
TUI's transport forced: Node's `WebSocket` takes a URL and nothing else, so the token rides in
the upgrade query (`hr_listener.WS_DEVICE_QUERY`, honoured on upgrades only, never on HTTP, and
stripped by the proxy before the dashboard sees the query); and the certificate is self-signed,
so the child gets it as `NODE_EXTRA_CA_CERTS`, which is enough because the SAN carries 127.0.0.1
(measured: without it Node fails with `DEPTH_ZERO_SELF_SIGNED_CERT`, with it the same request is
a 401). `_launch_tui` keeps an explicit `HERMES_TUI_GATEWAY_URL` rather than discovering one, so
the launcher is Hermes' own and `/api/session-attach` was never needed; the kill did not fire.

Proven against the live gateway host (pid 32248, after a restart to load the new listener): the
real `_attach` paired a terminal device, and a Node script standing in for Ink opened the URL
with the native `WebSocket`, received `gateway.ready`, created a session, submitted a prompt and
saw `message.start` through `message.complete`; the device was revoked on exit and the scratch
session deleted. Nine new tests, suite at 148. Not proven: Ink itself in a real terminal, which
needs a TTY and is Paul's to run (`hermes remote attach`, then send from the phone into the
session the TUI shows).

### V6 — mDNS discovery · Opus 5 / medium

Replace the address baked into the pairing payload by `hr_pairing.build`, so the QR survives a moved
DHCP lease. The reservation on the gateway is the cheap version and stays until this lands.

Status 2026-09-15: shipped, with no service advertised and no dependency added on either side.
The first design question was whether the plugin should run a DNS-SD responder (`zeroconf` is
not in the Hermes venv, and installing into it is not this project's to do). Measured before
deciding: this machine's own `Dnscache` already answers a legacy unicast mDNS query for
`pawl-desktop.local` with an A record, in under a millisecond, and macOS and Avahi do the same
for their hosts. The phone is not browsing for unknown desktops; it holds the port, the pin and
the token and is missing only the address, so a hostname lookup is the whole problem. Hence:

- The code gains an optional `m=<hostname>.local` (`hr_pairing.build`, no version bump; a
  v1 phone ignores it). `hr_mdns.py` builds the query and reads the answer, and `hermes remote
  pair` and `status` print a `Discovery` line from a self-check.
- The phone (`Mdns.kt`, `Mdns.jvm.kt`, HermesRemote) resolves the name with the same packet over
  a plain UDP socket: sent to the group, answered by unicast to the ephemeral port (RFC 6762
  §6.7), so no multicast lock and no new permission. `findHost` asks the LAN after `lastGoodHost`
  is silent and before the stale stored list, and the address that passes the pin becomes
  `lastGoodHost`. A `Desktop` stored before the field has `mdnsName = null` and walks its list
  as before; the phone must be paired again to gain the name.
- Found on the way: a machine querying *itself* gets the answer from whichever adapter the OS
  sent the loopback copy through, here Tailscale's link-local address. Both resolvers take a
  source address for that case; a phone leaves it unset and the query takes the Wi-Fi.

Proven: `hermes remote status` against the live gateway host prints `pawl-desktop.local answers
(192.168.1.50)`, and the opt-in `MdnsLiveTest` (`HERMES_LIVE_MDNS=pawl-desktop.local
HERMES_LIVE_MDNS_FROM=192.168.1.50`) ran the JVM resolver the phone uses and got the same
address. Nineteen new plugin tests (suite at 167) and eleven shared tests (79). Not proven: a
real phone crossing a moved lease, which needs a re-pair and a lease change, and a phone with a
VPN that captures multicast, where the query may leave by the wrong interface. No phone was
attached, so the debug build is not installed.

### V7 — Rename to hermes-talaria · Opus 5 / medium

Across `plugin.yaml`, `dashboard/manifest.json`, `hr_routes.PLUGIN_NAME`, `plugins.enabled`, the
Android package, and the pairing URI scheme. Ship a migration note: an existing paired phone must pair
again. After V1–V6 so the rename lands once.

Status 2026-09-16: shipped, deeper than listed and without the forced re-pair. Renamed: plugin
and manifest name, route prefix (`/api/plugins/hermes-talaria`, `Client.API_PREFIX`), pairing
scheme on both sides, provider `hermes-talaria-device` and its scope, the CLI verb (`hermes
talaria`), `HERMES_TALARIA_*` env vars, the state dir (`<hermes root>/talaria`), and display names
(`Talaria`, `TalariaDeviceProvider`, `Theme.Talaria`, new certificates' CN). Kept: the `hr_` module
prefix, the `hr1.` token prefix, the Android `applicationId` (`dev.pawl.hermes`, never carried the
old name), Gradle `rootProject.name`, both repo folders, and the one QR golden payload whose
digest was decoded outside the suite.

The re-pair the plan expected is avoided: device records never stored the provider name or
scope, so `hr_paths.migrate_legacy_state` (called first in `register`) renames `remote/` to
`talaria/` when only the old one holds a `devices.json`, and the stored token and pinned
certificate stay valid. The phone needs only the new build. README § "Renamed from hermes-remote"
is the migration note. On this machine the junction and `plugins.enabled` are swapped.

Proven: 170 plugin tests (three new in `test_paths.py`), 80 shared, `assembleDebug` clean; the
real `hermes_cli.main talaria status` against a scratch `HERMES_HOME` loaded the plugin through the
new junction name and moved a seeded `remote/` to `talaria/`. Not proven: the live move, because
the running gateway (pid 26484) still holds the old code and the old `remote/` store, and a CLI run
against the real home would have pulled the store out from under it. It happens on the next restart.

### V8 — Distribution · Opus 5 / medium

Firewall setup emitted as a command by `hermes talaria pair` rather than living in the README, plus the
macOS and Linux equivalents. Then catalog packaging so `hermes plugins install` works, and an APK
story for the phone half.

Status 2026-09-16: firewall part shipped; catalog packaging and the APK story remain. The command is
its own verb, `hermes talaria firewall` (`plugin/hr_firewall.py`), and `pair` prints one line pointing
at it, because a four-line elevated command on every pairing screen buries the credential warning.
A loopback bind gets a "loopback only" line instead. Finding: `sys._base_executable` names the
runtime through the `cpython-3.11-…` junction, while the working rule (and Windows' match) uses the
`cpython-3.11.15-…` target, so the path goes through `os.path.realpath`. Proven: 180 plugin tests
(including a real junction on Windows); live `hermes talaria firewall` printed exactly the
`Program` of the rule that admits the phone today (`HermesRemoteListener`).

V7's unproven live move is now proven: after `hermes update` and `hermes gateway restart` (gateway pid
17528), `remote/` is gone, `talaria/` holds the store, and `hermes talaria status` lists device
`02112da5c040` live with the gateway hosting 9443. Still open from V7: the phone reconnecting on the
new build with no re-scan.

Status 2026-09-16 (later): the V7 reconnect is proven. `installDebug` onto the Pixel 9, a cold launch,
and `hermes talaria status` showed `02112da5c040` live within a minute (gateway pid 43272), with no QR
scan.

Catalog packaging, first finding: `hermes plugins validate plugin` passes every admission check, but a
real install from git (`HERMES_HOME=<scratch> hermes plugins install file:///…/hermes-remote#plugin
--ref <sha> --no-enable`) is **blocked**. The install scanner's `\bsudo\b` rule
(`tools/skills_guard.py:316`, high, privilege_escalation) matches the six printed Unix commands in
`plugin/hr_firewall.py:75-100`. Every external plugin scans as `community` trust, and community plus a
caution verdict blocks without `--force`. Decided (Paul): drop the prefix and label the shell "as
root", since root is reachable several ways and the docstring must avoid the token too. Proven: 180
tests pass; the same scratch install at the fix commit installs `hermes-talaria` with no scan finding.
An upstream PR narrowing the rule to exempt printed text stays worth filing (the rule already carries a
`sudo.request` exemption for the same class of false positive). Catalog admission also needs a public https repo (this repo has no remote) and a PR adding
`plugin-catalog/hermes-talaria.yaml` to hermes-agent with `subdir: plugin` and an exact SHA.

## Rejected

- **Retry on 4001 as the fix for the lost message.** The refusal on record was 4090, which no retry
  clears. Kept as a small part of V2 because the 4001 path is real, just not what happened.
- **Fan-out through `/api/pub` for a read-only live view across processes.** Still possible, but it
  only ever shows a turn; it cannot let the phone send into a session another process owns. Same-process
  attachment (V3, V5) gives both, using mechanisms Hermes already has.
