# HermesRemote — the plan

A phone client for a live Hermes session on the LAN, modelled on PawlRemote but much
smaller, because Hermes already has the wire.

Written 2026-09-05 against `hermes-agent` 0.21.0
(`code_sha f58fcc8118d9db092ad60d363d4a28520e08ac5a`, from `gateway_state.json`).
Where this file and the Python disagree, **the Python wins and this file is the bug** —
`hermes-agent` is upstream NousResearch code that `hermes update` pulls, so it moves
underneath us.

---

## 1. The finding that shapes everything

`hermes-agent/tui_gateway/` is a newline-delimited JSON-RPC server, and its own
`AGENTS.md` says it already has three consumers:

```
hermes --tui        Ink renderer      -- stdio JSON-RPC ---> tui_gateway (Python)
Desktop (Electron)  own renderer      -- WebSocket -------> the same server
dashboard /chat     embedded TUI      -- PTY + WebSocket -> the same server
```

The Android app is the **fourth renderer of a protocol that already exists**. There is no
phone-specific API to design, which is the same property that made PawlRemote cheap, only
here we did not have to build it.

`tui_gateway/ws.py` is the WebSocket transport. Its docstring is the whole contract:

> reuses `tui_gateway.server.dispatch` verbatim so every RPC, slash command, approval flow
> and agent event takes the same handlers as Ink over stdio. Wire protocol is identical to
> stdio (newline-delimited JSON-RPC both ways; `gateway.ready` right after accept). Mount
> as `@app.websocket("/api/ws") async def ws(ws): await handle_ws(ws)`.

That last sentence is the seam this entire project hangs on: the live-session transport is
one line of mounting, so a second listener can serve it without touching upstream code.

### 1.1 The catalog the phone needs

From `tui_gateway/AGENTS.md`, methods in `tui_gateway/methods_*.py`:

| Surface | Method / event |
| --- | --- |
| Chat streaming | `prompt.submit` -> `message.delta` / `message.complete` |
| Tool activity | `tool.start` / `tool.progress` / `tool.complete` |
| Approvals | `approval.request` -> `approval.respond` |
| Clarify / sudo / secret prompts | `clarify.respond`, `sudo.respond`, `secret.respond` |
| Sessions | `session.list` / `session.resume` |
| Slash commands | `slash.exec` -> `command.dispatch` |
| Handshake | `gateway.ready` (carries skin data) |

**Only the left half of each cell is a method.** Everything that flows the other way —
`message.delta`, every `tool.*`, `approval.request`, `gateway.ready` — is a notification whose
method is the literal string `event`, with the name above sitting in `params.type`
(`tui_gateway/server.py::_event_frame`). Read as a list of method names this table is a bug; see
§5.9.

**Multi-session is already in the protocol.** `session.list` and `session.resume` exist
today, so the "other users later" requirement costs nothing now and would cost a
navigation rewrite later. Build the session list as a real list in the first commit.

### 1.2 Four differences from PAWL, and what each one costs

| | PAWL | Hermes |
| --- | --- | --- |
| Protocol | designed for the phone (`SnapshotMsg` / `ViewMsg`) | already exists, ~40 methods plus events |
| State model | every snapshot **replaces** state; nothing to reconcile | incremental deltas; the client needs a reducer and a replay story |
| Transport | bespoke TLS listener inside the extension host | FastAPI on 9119, **no TLS**, loopback by default |
| Auth | pairing code -> bearer token, certificate pinned | `DashboardAuthProvider` gate, cookies, 30s WS tickets |
| Ownership | Paul's own repo | upstream NousResearch; local edits become `hermes-local-patches` entries |

The last row is the strongest constraint on the design. `hermes-agent/plugins/AGENTS.md`
states the rule directly: *"Plugins never touch core … A plugin MUST NOT modify
`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`."* Everything we add
lives out of tree and uses the documented `register(ctx)` surface.

---

## 2. Where the code lives

| Path | What |
| --- | --- |
| `%USERPROFILE%\source\repos\hermes-odyssey\` | this repo: the desktop-half plugin, the contract, the tests |
| `%LOCALAPPDATA%\hermes\plugins\hermes-odyssey` | a **directory junction** to `plugin/` in this repo |
| `%USERPROFILE%\AndroidStudioProjects\HermesOdyssey\` | the Android app and the shared Kotlin module |

Hermes discovers plugins in `$HERMES_HOME/plugins/`, so the code has to appear there; a
junction keeps the working copy in `source\repos` where the rest of the work lives and
keeps the plugin out of the upstream tree. `mklink /J` needs no administrator rights.

Splitting the app into its own repo follows the PawlRemote precedent: Android Studio owns
one Gradle root, and the plugin has no business inside it.

---

## 3. Phases

### Phase 0 — prove the path with no code

Bind the dashboard to the LAN behind the bundled `basic` auth provider and open `/chat`
from the phone browser. This exercises the exact backend the app will use, and it settles
the one open question (§4.1) by observation rather than by reading more Python.

Mechanics, all reversible and none of them editing `config.yaml`:

- `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` and `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`.
  Env wins over config when non-empty, and an env password is hashed in memory with
  stdlib scrypt (`plugins/dashboard_auth/basic/__init__.py::_settings`).
- `hermes dashboard --host 0.0.0.0 --no-open`.
- `--insecure` is a **no-op** since the June 2026 hardening; a non-loopback bind always
  requires a registered auth provider. There is no way to skip the password.

Accept for this phase only that there is no TLS: the password and the session cookie cross
the LAN in cleartext. That is precisely what Phase 1 fixes, and it is why the experiment
should not outlive the evening.

### Phase 1 — the desktop half, as an out-of-tree plugin

`plugin/` in this repo, registering three things through `register(ctx)`:

1. **A `DashboardAuthProvider` on the bearer-token seam.**
   `hermes_cli/dashboard_auth/token_auth.py` exists for exactly this: non-interactive
   clients that present a token instead of driving a login page. A route opts in via
   `register_token_route`, the middleware attaches a `TokenPrincipal`, and the cookie
   gates honour the flag rather than bouncing to `/login`. Registered through
   `ctx.register_dashboard_auth_provider` (`hermes_cli/plugins.py:740`).
2. **A CLI subcommand tree** via `ctx.register_cli_command` — `hermes remote pair`,
   `status`, `revoke` — printing the pairing QR into the terminal. No `main.py` change is
   needed for this; the argparse tree is wired at startup.
3. **A TLS listener** holding a self-signed P-256 certificate, reverse-proxying loopback
   9119 including the WebSocket upgrade, so the phone can pin a certificate.

The dashboard has no TLS of its own and expects a reverse proxy (`web_server.py` mentions
Caddy `basic_auth`). Owning that proxy inside the plugin is what buys certificate pinning
without a fork and without asking Paul to run Caddy.

Portable from PawlRemote: `vscode-pawl/src/remote/qr.ts` (byte-mode QR encoder, level M,
dependency-free) and `x509.ts` (a self-signed P-256 certificate written in DER by hand).

Device tokens are stored **as hashes only**, under `%LOCALAPPDATA%\hermes\remote\`.

### Phase 2 — the shared Kotlin module

Fork the PawlRemote skeleton, which is already the right shape: a Kotlin Multiplatform
`shared` module with `androidTarget` plus a `jvm` target that exists only so the protocol
tests run on Windows with no device and no emulator.

What is genuinely new, because the wire is different:

- a JSON-RPC frame codec (newline-delimited, both directions);
- a pending-request map keyed by request id, since responses are correlated, not streamed;
- a **transcript reducer** folding `message.delta` and the `tool.*` events into renderable
  state. PAWL needed none of this because every snapshot replaced everything;
- a reconnect that replays. `tui_gateway/event_replay.py` and its epochs suggest the
  server can replay a stream after a drop — confirm before designing around it.

Reusable as-is: the pinned trust manager (pin the whole leaf DER, do not validate it, and
do not reach for OkHttp `CertificatePinner`, which hashes the SubjectPublicKeyInfo
instead), the encrypted token store, and the candidate-address racing.

### Phase 3 — the app

Pair, Chat, Activity, Approvals, Sessions, Settings.

**Approvals are the feature that justifies a phone at all.** Everything else is nicer on a
laptop; being able to answer `approval.request` from another room is not.

### Phase 4 — the deferrals

- ~~**Close a revoked device's live socket**~~ — done, §5.12.
- ~~**Allow the listener through the firewall**~~ — done, §5.12. What is left of it is one thing a
  physical phone has to confirm, since every run so far originated on this host.
- **Push.** An approval you only see when the app is foregrounded is not an approval.
  FCM, or a foreground service holding the socket. PawlRemote deferred this too and it is
  the first thing worth adding once the app works.
- **iOS.** Unbuildable from Windows; Xcode is macOS-only. Keep `shared` written so an iOS
  shell stays an additive slice.
- ~~**mDNS.**~~ — done as V6 of `.agents/plans/hermes-odyssey-plan.md`. The desktop never
  advertises anything: the code carries `<hostname>.local` and the phone resolves it with one
  legacy unicast mDNS query, which the OS responder on every platform answers unaided.

---

## 4. Risks, in the order they can hurt

### 4.1 What "my live session" actually means

The dashboard's `/api/ws` builds its **own in-process agent** through
`tui_gateway.server._make_agent` (`hermes_cli/main.py:2474` comment). Paul's live session
runs in the separate gateway process, which holds a turn lease (`gateway/turn_lease.py`).
Sessions are shared through the state database, so listing and resuming will work — but
*joining a turn already in flight in another process* may not be something the code does
today.

Phase 0 settles this by looking. If a phone can only resume between turns rather than
watch one stream live, the plugin grows a second job: subscribing to the gateway's event
publisher and fanning it out read-only alongside the interactive channel. Note that
`/api/pub` and `/api/events` already exist as a broadcast pair
(`hermes_cli/web_routers/chat_ws.py:557`) and are the obvious place to start looking.

### 4.2 The plugin holds a private key

Phase 1 makes this repo network-facing code with a signing key in it. PawlRemote's three
security rules transfer verbatim and should be copied into this repo's README before the
listener is written:

1. Pin the certificate, do not validate it.
2. Store the token like a password.
3. Confirm the fingerprint by eye, once.

### 4.3 The upstream compatibility window

`COMPAT_MANIFEST.md` describes a decomposition compat window that closes **2026-09-14**,
after which `PluginManager` skips a plugin that imports through the old paths unless
`plugins.allow_deprecated_imports` is set. Anything written this week must import the
current paths, not the shims. `hermes plugins compat` reports on this.

---

## 5. Phase 0 log

### 5.1 Confirmed on loopback, 2026-09-05

A smoke run of `hermes dashboard --host 127.0.0.1 --no-open --skip-build`, with the
credential supplied through the two `HERMES_DASHBOARD_BASIC_AUTH_*` env vars and
`config.yaml` untouched:

- The server starts and prints `HERMES_DASHBOARD_READY port=9119`. `--skip-build` avoids
  npm entirely; the prebuilt SPA in `hermes_cli/web_dist/` serves fine.
- `GET /api/sessions` returns the **whole session store, every source**: 40 rows spanning
  `tui`, `cli`, `telegram` and `desktop`, each carrying title, model, cwd, message and
  tool counts, token totals and `ended_at`. This is a phone-ready surface as it stands and
  is what the Sessions screen renders.
- `GET /api/status` reports `gateway_running: true` with whatsapp, signal and telegram all
  `connected`, so the dashboard process sees the separate gateway's state.
- **`GET /api/ws` completes the JSON-RPC handshake and pushes `gateway.ready` with its
  skin payload**, exactly as `tui_gateway/ws.py` documents. This is the single most
  important thing to have proven: the phone's transport works, unchanged, today.
- On loopback the WS credential is `?token=<session token>`, the same value the SPA gets
  as `window.__HERMES_SESSION_TOKEN__`. In gated mode that path is **rejected** and only
  `?ticket=` (single use, 30s) or `?internal=` are accepted — see `_ws_auth_reason` in
  `hermes_cli/web_server_chat.py:220`. The Phase 1 token provider has to reckon with this:
  a phone holding a long-lived bearer still cannot open the WS with it, and will need
  either a ticket-minting round trip or a fourth credential shape.

That last bullet is a Phase 1 design constraint that was not visible from reading alone.

### 5.2 Still unproven, and why the LAN run is needed

- The `basic` provider actually engaging. It registers only on a non-loopback bind, so
  loopback proves nothing about it.
- **§4.1**, the whole reason Phase 0 exists. Open the live telegram session in `/chat` from
  the phone while the gateway is mid-turn and watch whether tokens stream.

### 5.3 Machine facts, valid until they are not

- LAN address `192.168.1.50`.
- The hermes venv `python.exe` has **no inbound firewall allow rule** (only the Electron
  `hermes.exe` does), so the first LAN bind raises a Windows Firewall prompt. Private
  networks only. **Corrected in §5.11:** the prompt fired and was answered *no*, so the rules
  now present are inbound **Block**, and they name the runtime interpreter
  (`.hermes-runtime\python\...`) rather than the venv one.

### 5.4 Read-only findings, 2026-09-06

Against the same `code_sha`. Everything here is read, not run — §5.5 says what a run would
settle.

**Plugins load in the dashboard process.** `hermes_cli/main.py:2467` calls
`discover_plugins()` before `start_server`, with the comment that the dashboard's runtime
depends on plugin-registered providers. So `register(ctx)` runs there, and anything the
plugin starts at registration runs in the process that owns port 9119.

**A plugin can mount its own FastAPI router, including a WebSocket.**
`_mount_plugin_api_routes` (`hermes_cli/web_server_dashboard.py:757`) imports
`<plugin>/dashboard/<api file>` and calls `app.include_router(router,
prefix="/api/plugins/<name>")`. The manifest is `dashboard/manifest.json` with an `api`
key; the bundled `kanban` plugin is the worked example. An `APIRouter` carries
`@router.websocket` as well as HTTP routes, so the plugin owns both surfaces without
touching core. Gate, from `_plugin_api_mount_skip_reason` (line 741): a **user** plugin's
Python is imported only when its name is in `plugins.enabled` and absent from
`plugins.disabled` (GHSA-mcfc-hp25-cjv7). Project plugins are never auto-imported.

**A bearer token cannot mint a WS ticket through the stock route.** `POST
/api/auth/ws-ticket` calls `_require_session`, which reads `request.state.session`
(`hermes_cli/dashboard_auth/routes.py:430`). The token middleware sets
`token_principal` and `token_authenticated`, never a `session`, so registering that path
with `register_token_route` would clear the outer gate and then 401. `mint_ticket` is
importable from `hermes_cli.dashboard_auth.ws_tickets`, so the plugin's own route can
verify the phone's bearer and mint a ticket itself. The round trip is unavoidable either
way: `token_auth_middleware` is HTTP middleware and never sees a WebSocket scope.

**§4.1 is answered, and it is the pessimistic branch.** The watch mechanism is
process-local. `_mirror_subagent_to_child` (`tui_gateway/agent_callbacks.py:31`) turns
relayed `subagent.*` events into native stream events on the child sid, but it reads
`_child_mirrors` and `_active_child_runs`, both plain module dicts. Nothing crosses to the
separate gateway process. A phone on the dashboard's `/api/ws` therefore sees the
gateway's sessions and their stored history through `session.list` / `session.resume`, and
does **not** see a turn streaming in the other process.

The fan-out to fix that already exists and is not PTY-specific: `/api/pub` rebroadcasts
verbatim newline-framed JSON to every `/api/events` subscriber on the same channel
(`hermes_cli/web_routers/chat_ws.py:576`). So the read-only live view is the same plugin,
loaded in the **gateway** process, registering `on_stream_start` / `on_stream_delta` /
`on_stream_end` and `pre_tool_call` / `post_tool_call` and publishing to `/api/pub`.
Approvals are the exception: `pre_approval_request` and `post_approval_response` are
observers whose return value is ignored, so answering one from the phone has to go through
`pre_tool_call`, which can decide.

**Consequence for Phase 1.** The `DashboardAuthProvider` on the token seam (§3 Phase 1,
item 1) stops being the load-bearing piece; the plugin's own router is. Keep the provider
only if a stock route ever has to accept the phone's bearer. Item 3, the TLS listener,
survives unchanged and is still the security boundary.

### 5.5 What only a run can settle

- The `basic` provider engaging on a non-loopback bind, and the Windows Firewall prompt
  for the hermes venv `python.exe` (§5.3).
- Whether a phone browser in `/chat` really shows nothing while the gateway streams a
  telegram turn. §5.4 predicts silence; watching it is cheap and the prediction is worth
  falsifying.
- Whether `include_router` on a plugin router accepts a `@router.websocket` route in this
  FastAPI version. Expected yes; unproven here.

### 5.6 Phase 1 log — the first slice, 2026-09-06

The plugin now loads. `plugins.enabled` gained `hermes-remote`, the junction is in place, and
`plugin/` holds the device store, the auth provider and the API router. The TLS listener and the
pairing CLI are the remaining Phase 1 items.

**Verified by running, against the real install** (`discover_plugins()` then the dashboard's own
`_mount_plugin_api_routes()`, then a `TestClient` over the whole middleware chain):

- The plugin imports as `hermes_plugins.hermes_remote` — the slug `_directory_module_name`
  derives from the manifest key, which matters because `dashboard/api.py` finds its siblings by
  that prefix.
- `register(ctx)` registers `hermes-remote-device` as the sole token provider and marks both
  routes token-authable.
- `include_router` mounts them at `/api/plugins/hermes-remote/`.
- A request with no bearer, an unknown bearer, or a revoked device's bearer gets 401 from the
  token seam. A paired device gets 200. An unreadable store gets 503, never 401.

38 tests pass under the Hermes venv interpreter.

**§5.4's conclusion about the auth provider was wrong, and the plan is the bug.** It said the
`DashboardAuthProvider` on the token seam "stops being the load-bearing piece" now that the
plugin owns a router. The opposite is true: a plugin router is mounted *inside* the dashboard's
FastAPI app, so it sits behind the same gates as every other `/api/` path — the cookie gate in
gated mode, the session-token gate on loopback. `register_token_route` plus a registered provider
is the only way a phone's bearer clears either one. Both pieces are required; neither is optional.

**A trap the seam sets.** `token_auth_middleware` authenticates a registered route against *every*
registered provider. If a drain secret is ever configured on this machine, that bearer would clear
the gate on HermesRemote's routes too. So each route re-checks `token_principal.provider` against
its own name before doing anything. That check is not belt-and-braces; without it the routes are
reachable by a credential that has nothing to do with a phone.

**The WS credential is mode-dependent and only one branch is safe to hand out.** In gated mode
`POST /ws-ticket` mints a real 30-second single-use ticket, the same shape the browser SPA gets.
On a loopback bind the only credential `_ws_auth_reason` accepts is `_SESSION_TOKEN`, which is a
process-lifetime master key for the whole dashboard; it cannot cross the network. So the route
answers `ticket: null` there and the TLS listener attaches the credential itself when it proxies
the upgrade. The phone's client code is the same either way: open `/api/ws`, append `?ticket=`
only when it was given one.

**Why this makes the listener simpler than §3 assumed.** Proxying from loopback means the
dashboard sees a loopback peer, which is exactly what `_ws_client_reason` demands in ungated mode,
and the listener controls the `Host` header so `_ws_host_origin_reason` is satisfied by
construction. The dashboard can stay bound to 127.0.0.1 and the listener becomes the whole
security boundary — which is what §4.2 wanted anyway.

**Device tokens.** `hr1.<device_id>.<secret>`, with only `sha256(salt || secret)` on disk under
`%LOCALAPPDATA%\hermes\remote\devices.json`. The id addresses one record, so verification is a
lookup rather than a scan. SHA-256 rather than scrypt is deliberate: the secret is 256 bits of
`secrets.token_bytes` and there is no dictionary to make expensive, while a slow hash on a
per-request path would be a self-inflicted denial of service. Revocation keeps the record.

**Still unproven from §5.5.** The `basic` provider on a non-loopback bind and the firewall prompt
are now moot — the listener replaces that bind, and it is the same `python.exe` either way, so the
prompt just moves. `include_router` accepting a `@router.websocket` route is still untested; the
router mounted here carries HTTP routes only. Falsifying the §5.4 prediction that a phone in
`/chat` sees nothing while the gateway streams remains worth one cheap run.

### 5.7 Phase 1 log — the listener and the CLI, 2026-09-06

Phase 1 is code-complete. `plugin/` gained the certificate (`hr_identity.py`), the QR encoder
(`hr_qr.py`), the pairing payload (`hr_pairing.py`), the TLS reverse proxy (`hr_listener.py`) and
the CLI (`hr_cli.py`). 116 tests pass under the Hermes venv interpreter.

**The listener authenticates; the dashboard does not.** This is the single most important thing
about the design and it was not in §3. On a loopback bind the dashboard trusts its peer, and every
proxied request arrives from loopback *because that is what a proxy is* — so a listener that
forwarded an unauthenticated request would hand the whole dashboard to anyone on the Wi-Fi. Every
request and every WebSocket upgrade is therefore checked against the device store before anything
is forwarded. The plugin's own routes are gated twice, by the listener and by the token seam;
every other path is gated only here. There is no unauthenticated route at all, not even a
`/hello`: the TLS handshake already proves which machine answered, so a phone racing candidate
addresses needs nothing else, and a drive-by learns nothing.

**Where the listener is armed, and why not in `register()`.** `register(ctx)` runs in every Hermes
process, so a socket opened there would open in the CLI and the gateway too. `dashboard/api.py` is
imported only by `_mount_plugin_api_routes`, which makes it the one reliable marker for the process
that owns 9119. It adds a `startup` handler to its own `APIRouter`.

That mechanism has a trapdoor worth writing down. The dashboard builds its app as
`FastAPI(..., lifespan=_lifespan)`, and a custom lifespan means the app router's `on_startup` list
is never run — so the handlers `include_router` copies onto the app are inert. What actually runs
is the *plugin router's own* `_DefaultLifespan`, which `include_router` merges into the app's
lifespan context. `tests/test_api.py` pins that, because losing it would mean the listener silently
never comes up with no error anywhere.

Arming is also two steps rather than one. `app.state.bound_port` is set in `_on_server_started`,
which runs *after* the `server.startup()` that fires the lifespan — so at handler time the port does
not exist yet. The handler schedules a task that waits for it. Reading the real port rather than
assuming 9119 matters because `--port 0` is a supported bind.

**Certificate.** Self-signed P-256, ten years, generated once into
`%LOCALAPPDATA%\hermes\remote\listener-cert.pem` and kept. The SAN names **only** loopback
(`localhost`, `127.0.0.1`, `::1`) — a LAN address there would rotate the fingerprint with the DHCP
lease and unpair every phone on renewal. The phone does no hostname verification, so it costs
nothing. Regeneration happens only when the files are absent, unparseable or expired.

**The QR encoder is a port, and it was verified against a decoder rather than by reading.** Every
subtle part of `qr.ts` fails silently — the symbol still draws and only a phone camera finds out —
so `hr_qr` was checked by rendering four payloads (versions 1, 4, 13, 20) to bitmaps and decoding
them with OpenCV's `QRCodeDetector`; all four round-tripped exactly. The data codeword stream was
separately checked against the `qrcode` package. Neither is in the Hermes venv, so `tests/test_qr.py`
carries digests of the verified symbols instead. `segno` disagrees on one thing — it emits an extra
`0x00` codeword after the terminator — and `qrcode` and the spec's worked example both side with
this encoder. It is padding, so all three decode identically.

**The pairing code carries the device token itself, unlike PawlRemote.** There the QR was a
short-lived offer redeemed over the network, because it was drawn in an editor panel anyone walking
past could photograph. Here pairing happens at a terminal a person is already sitting at, and a
redemption round trip would mean an unauthenticated route on a listener whose whole point is that
it has none. The accepted cost: the code on screen *is* the credential until the device is revoked,
and a screenshot of it stays valid. `hermes remote pair` says so on the same screen.

**Verified by running.** `hermes remote status` against the real install, through the real junction
and the real `plugins.enabled` entry, prints the expected report — so `register_cli_command` and the
argparse tree work end to end with no `main.py` change. Separately, a listener was started on a
throwaway `HERMES_HOME` in front of a stand-in upstream and driven over a real TLS socket
(TLS 1.3): the served leaf DER matched the on-disk certificate byte for byte (which is exactly what
the phone pins), an anonymous request got 401, a paired device's request reached the upstream with
`Host` rewritten to loopback and its query string intact, a WebSocket upgrade arrived carrying the
injected credential and echoed frames both ways, and revoking the device took effect on the next
request with no restart.

**That last clause is only true of requests, and §5.11 is where it bites.** A WebSocket already open
when the revocation lands keeps going: the listener authenticates the upgrade, not the frames after
it. A revoked phone was seen still live and still reading a session, and the heartbeat means the
socket never closes on its own. Fixing that is the first Phase 4 item.

**Not yet proven** (settled the same day, §5.8). The listener coming up inside a real
`hermes dashboard` process. Every part of
that path is tested in isolation — the lifespan merge, the deferred port wait, the proxy, the
certificate — but the assembled run has not happened, and it wants a decision first: the listener
binds `0.0.0.0` by default, which is the first LAN bind for the hermes venv `python.exe` and
raises the Windows Firewall prompt §5.3 predicted. `HERMES_REMOTE_HOST=127.0.0.1` avoids the prompt
and proves everything except reachability from the phone.

### 5.8 Phase 1 log — the assembled run, 2026-09-06

Phase 1 is proven end to end. `HERMES_REMOTE_HOST=127.0.0.1 hermes dashboard --skip-build
--no-open` came up (`HERMES_DASHBOARD_READY port=9119`) and `hermes remote status` in a second
shell reported the listener live in that process:

```
Listener     listening on 127.0.0.1:9443
             proxying to 127.0.0.1:9119, pid 21840
```

So the whole arming path holds in a real dashboard: the plugin loads from `plugins.enabled`
through the junction, `dashboard/api.py` is imported by `_mount_plugin_api_routes`, the plugin
router's own `_DefaultLifespan` survives the app's custom lifespan, and the deferred wait for
`app.state.bound_port` resolves. That last one is the part no unit test can prove.

An anonymous `GET https://127.0.0.1:9443/` over TLS returned `401 {"detail": "unauthorized"}`,
which is the gate in force inside the real process rather than in a harness.

Still unproven, and only these: the `0.0.0.0` bind, the Windows Firewall prompt it raises, and
reachability from the phone.

### 5.9 Phase 2 log — the shared module and the frame codec, 2026-09-06

`AndroidStudioProjects\HermesRemote\` now exists: the PawlRemote Gradle skeleton forked to
`rootProject.name = "HermesRemote"`, one `:shared` module in package `dev.pawl.hermes`, an
`android` target and a `jvm` target that exists only so the protocol runs under
`gradlew :shared:jvmTest` on Windows with no device. No iOS target, same reason as before. No
`:androidApp` yet; that is Phase 3. 14 codec tests pass.

Reading the gateway to write the codec turned up four things, and two of them contradict §1.1.

**Agent events are not methods.** Every server-to-client event is a notification whose method is
the literal string `event`; the name lives in `params.type`, alongside `session_id`, an optional
`seq` and an optional `payload` (`server.py::_event_frame`). A client that dispatches on `method`
sees one event type and drops the protocol. §1.1 is corrected in place.

**The WebSocket has no newline delimiter.** The docstring says "newline-delimited JSON-RPC both
ways", and over stdio it literally is, but `WSTransport.write` sends one `json.dumps` per
`send_text` with no terminator, and the read loop is `json.loads(raw.strip())` on each frame
received (`ws.py`). Two frames joined by a newline in one message are therefore one parse error
and both are lost. `HermesCodec.encode` emits exactly one frame per message; the trailing newline
it writes is for the stdio wire and is free here because of that `strip()`. Decoding still splits
on newlines, because the specified wire allows it and JSON escapes every newline inside a string.

**Replay is real, and §3 can stop hedging about it.** `_stamp_event` gives every session-routed
event a per-session monotonic `seq` and files it in a ring of 512 events across at most 64
sessions, oldest session evicted. `session.events.since {session_id, last_seen}` returns
`{events, latest_seq, truncated, count, epoch}`, where `events` are bare event `params` objects
and **not** JSON-RPC envelopes. `truncated` means the gap fell out of the ring and the client must
refetch history rather than trust the replay. `gateway.ready` carries `replay_epoch`; the seq
counters are in-process, so an epoch that differs from the stored one means the backend restarted
and every watermark must reset to zero. That is the whole reconnect design, already specified.

**Session-less events carry no seq**, because `_stamp_event` only numbers what it can route —
`gateway.ready` and `skin.changed` among them. A watermark cannot be seeded from the handshake.

Also worth having: `gateway.ping` is answered by the WS read loop itself, ahead of `dispatch`,
and clients are expected to send it about every 15 seconds — it is what keeps the gateway's
scale-to-zero predicate from treating an idle phone as an absent one.

**One PawlRemote file does not port.** `Discovery.kt` races candidate addresses with
`PawlClient.hello()`, and this listener deliberately has no unauthenticated route (§5.7). Racing
has to be done with the TLS handshake plus an authenticated request instead, which is a design
change rather than a rename, so it waits for the connection slice.

### 5.10 Phase 2 log — the pinned client and the connection, 2026-09-06

The shared module now connects. `jvmShared` holds the ported pinning actual, and `commonMain` gained
`Socket.kt` (the platform seam), `Client.kt` (the plugin's two routes) and `Connection.kt` (the
loop). 50 tests pass under `gradlew :shared:jvmTest --offline`.

**The pin ported unchanged; the transport around it did not.** `PinnedTrustManager` and
`base64UrlToHex` are PawlRemote's, verbatim apart from the message text — the digest is still the
whole leaf DER, hostname verification is still off, and there is still no fallback to the system
store. What changed is that one `OkHttpClient` now carries both surfaces. Ktor's `WebSockets` plugin
is deliberately absent: its artifact is not in the Gradle cache, so depending on it would break the
`--offline` rule the README states, and it would only re-frame text that `HermesCodec` already
frames. OkHttp's own WebSocket is what Ktor's engine would have called anyway.

**Discovery is sequential, not raced, and §5.9 predicted the shape correctly.** With no
unauthenticated route, a probe costs a TLS handshake plus a bearer check against `/health`, so the
client walks the candidate list with a one-second timeout and `lastGoodHost` first. A refused bearer
throws instead of moving to the next address: every candidate would refuse the same token, so
walking the rest only spends the user's time before telling them to pair again.

**A 401 is the one failure that must not be retried**, and it now has two arrival paths that both
have to be caught — the `/health` probe, and the WebSocket upgrade, where OkHttp reports it as an
`onFailure` carrying a response rather than as a status code.

**The heartbeat is not decoration.** `ws.py` answers `gateway.ping` in the read loop ahead of
`dispatch`, and the separate gateway's scale-to-zero predicate reads a marker file that the WS
handler touches — so a client that stops pinging gets its backend shut down underneath it. Because
the reply skips `dispatch`, a slow agent turn cannot delay it, which is what makes a *missing* reply
worth acting on: ten seconds of silence closes the socket and the loop reconnects.

**The reconnect order is load-bearing and is written down in `replay()`.** Epoch first, because a
restarted backend renumbers from one and an old watermark would swallow the whole replay; then
`truncated`, because the transcript must be marked stale *before* a partial replay lands on it; then
the events, through the same reducer path a live event takes. The reducer's own `seq <= seq` guard
is what makes an overlapping window safe to ask for. The epoch is checked twice, at `gateway.ready`
and again on the `session.events.since` result — not redundant, because the backend can restart
between the two.

**A design constraint found by the tests, worth stating because it is not obvious.** A Ktor call
runs on the engine's own dispatcher, so a connection loop that made one directly cannot be driven by
a virtual clock — the first version advanced time past a request that had not moved, and every test
sat in `Searching`. `Transport` therefore owns the two HTTP routes as well as the socket: everything
the loop awaits goes through one interface, and so everything the loop awaits is scriptable. The
Ktor half is tested separately, on a real dispatcher, in `ClientTest`.

**Not built in this slice**, and each for a reason rather than an oversight: the pairing-URI parser
(`Desktop` is modelled, the scan that fills it is the app's), the Keystore-backed `DesktopStore`
(the interface is here, the Android actual belongs with the app), and any live run against a real
gateway. Everything above is unit-tested against a scripted socket and unproven against the real
one; `session.resume` in particular is modelled on reading `methods_session.py` and its result is
assumed to carry `session_id`.

**Phase 3's six screens are approved** — Pair, Chat, Activity, Approvals, Sessions, Settings, as
published to the design canvas. The app slice builds those.

### 5.11 Phase 3 log — the app, and the assembled run over the LAN, 2026-09-06

Phase 3 is done and proven against a real `hermes dashboard`. `:androidApp` exists, the six approved
screens are built on `HermesConnection`'s `StateFlow`, and an emulator paired over the LAN address,
listed the real session store, resumed a session, rendered its stored history, survived the desktop
restarting under it, and refused itself after `hermes remote revoke`. 63 shared tests pass under
`gradlew :shared:jvmTest --offline`.

**What reading `methods_session.py` corrected about §5.10.** That log said `session.resume`'s result
"is assumed to carry `session_id`". It does — and it also carries `messages`, the whole stored
conversation as `_history_to_messages` projects it. That is the difference between a Chat screen
that opens on the conversation and one that opens on an empty pane, and it changes the reconnect
design rather than decorating it: history and the replay ring describe the same past, so folding
both prints every recent turn twice. `resume` therefore seeds from history and adopts `latest_seq`
as the watermark without folding the ring at all, and only a *reconnect* replays.

**The case that would have been a silent bug.** Seeding cannot key on "is the transcript empty".
`session.resume` mints a **runtime** id; the replay ring and `_stamp_event` are keyed on that id,
not on the stored key. When the session is still live, `_resume_reuse_live` hands back the same
runtime id and the watermark is valid — that is the reconnect replay was designed for. When it is
not, resume builds a new session: empty ring, numbering back at one, and a watermark of 40 carried
over from the old id would make the reducer's `seq <= seq` guard drop **every live event** under it.
So the reseed condition is "blank transcript **or** a runtime id that changed", and the stored key
is kept beside the runtime one because that is what the Sessions screen highlights on and what the
next `session.resume` is asked for.

**A frame the app was sending wrong, found by reading rather than by running.** `approval.respond`
begins with `_sess(params, rid)` (`methods_prompt.py`), so it needs `session_id`. §5.10's version
sent only `request_id` and `choice`; that answers 4001 and leaves the approval pending with the
agent still blocked on it. Nothing in the unit tests could have caught it — the scripted socket
answers whatever it is asked.

**The 0.0.0.0 bind is proven, and the firewall is not.** Dialling `https://192.168.1.50:9443` from
this machine gets TLS 1.3, a leaf DER byte-identical to `listener-cert.pem`, and
`401 {"detail": "unauthorized"}` with no bearer. But `Get-NetFirewallApplicationFilter` shows two
inbound **Block** rules on the Private profile for the listener's interpreter, which are the rules
Windows writes when the §5.3 prompt is dismissed — so the prompt fired at some point and was
answered no. Same-machine traffic to a local interface does not traverse that filter, and neither
does the emulator's user-mode NAT, which originates on the host. **Reachability from a real phone
therefore remains unproven, and is currently blocked.** Also: the program in those rules is
`.hermes-runtime\python\...\cpython-3.11.15\python.exe`, not the venv interpreter §5.3 named, so a
rule written against the venv would be the wrong rule.

**A revocation bypass, and it is the desktop half's to fix.** §5.7 said revoking "took effect on the
next request with no restart". True for HTTP. It is not true of a WebSocket that is already open:
the listener authenticates the *upgrade*, and frames after it are proxied without another store
lookup. A revoked phone was observed still Live and still reading a session, and the 15-second
heartbeat means that socket never closes on its own. Access ended only when the listener process
did. The fix belongs in `hr_listener.py` — hold the device id with each proxied socket and close the
ones whose record has been revoked — and it is the first Phase 4 item, ahead of push.

**What the assembled run showed, in order.** Pairing by deep link (the emulator has no camera worth
aiming at a terminal, and `hermes-remote://pair?…` reaches `PairScreen` by the route a scan does);
the fingerprint on screen matching the terminal group for group; `session.list` returning the real
store across `tui` and `cli` sources; `session.resume` on a two-day-old session rendering its stored
history with tool rows; the desktop killed and restarted underneath, the app going `Not found` with
the transcript intact and back to `Live` on its own with a new replay epoch; and revocation landing
as `Pair again` on the next attempt.

**Not proven, and each for a reason.** `prompt.submit` and `approval.respond` are wired and unit
tested but were not fired at the real gateway: both start or unblock work in Paul's own live
sessions, which is not a side effect to cause while testing. A physical phone on the Wi-Fi, for the
firewall reason above. And the Approvals screen was seen only in its empty state, because nothing
was pending.

### 5.12 Both Phase 4 blockers, closed — 2026-09-06

**Revocation now closes a live socket, and the store is the only channel it could have used.**
`hermes remote revoke` runs in the plain CLI, a different process from the dashboard, so there is
no in-process signal for the listener to hook — the device store file is the whole interface
between them. `hr_listener.py` therefore holds the device id beside each proxied socket in `_live`
and sweeps every five seconds: one store read answers for every attached phone, and a socket is
raced against its own `revoked` event rather than polled inside a pump, because the socket
revocation has to reach is precisely the one that wakes no pump for minutes at a time.

Two decisions in that sweep are worth keeping:

- **An unreadable store closes nothing.** It is the thing that says which devices are live, and a
  disk error that answered "none" would drop every session on the machine. Same shape as
  `_authenticate` answering 503 rather than 401, and for the same reason.
- **The close code is 1008, not 1000.** It says policy rather than network fault. The app needs no
  code for it — it reconnects, `findHost` gets 401, and `rejected()` lands on pairing by the route
  that was already there — but a close code that lies about why is a log that lies later.

Proven against a real `hermes dashboard --skip-build --no-open` with `HERMES_REMOTE_HOST=0.0.0.0`,
not only in tests: a paired device opened `wss://192.168.1.50:9443/api/ws`, took `gateway.ready`,
sat idle for three seconds — the exact state the bug lived in — and was **closed with 1008 5.1
seconds after `hermes remote revoke` returned**. Three tests cover the sweep, and the suite is 119.

`hermes remote revoke` also said the wrong thing ("stops working on the next request"), which was
the old behaviour described accurately. It now says the open session closes too.

**The firewall.** The two inbound `Block` rules are gone and a scoped allow replaces them:
`Program` the runtime interpreter, `Protocol TCP`, `LocalPort 9443`, `Profile Private`. That is
narrower than the rule the Windows prompt writes, which allows the whole interpreter on every
inbound port. The machine now has **zero** enabled inbound Block rules, so nothing can win over it
— Windows resolves Block before Allow, and one stray rule would have made this silently useless.

The cost of the narrow rule is that Windows matches `Program` by exact path, and the runtime
interpreter lives under `generation-1785721812-23864-f0965244`. A `hermes update` that mints a new
generation directory stops matching it, and the symptom is a phone that simply cannot connect. The
README says to check the rule's `Program` first when that happens.

**A physical phone has now reached it.** Paul's phone, on the Wi-Fi and with no app installed,
opened `https://192.168.1.50:9443/` and got `{"detail": "unauthorized"}`. That is three things at
once: the packet crossed the Windows filter from a genuinely remote host, TLS completed against the
self-signed leaf, and the bearer gate answered a request carrying no credential. Every run before
this one originated on this host — a local interface, or the emulator's user-mode NAT — and neither
traverses the filter, so this is the first evidence the allow rule matches anything. Phase 1's
"there is no unauthenticated route" also stops being a claim about the code at this point.

**Pin the address, because the QR bakes it in.** `192.168.1.50` was a 24-hour DHCP lease, not a
reservation, and `hr_pairing.build` writes the host list into the code the phone stores. A lease
that moved would strand every paired phone with no visible cause. It is now a fixed allocation on
the gateway at the same address, so nothing else had to change. This is the cheap version of the
mDNS item further down: mDNS makes a moved desktop findable, a reservation makes it not move.
