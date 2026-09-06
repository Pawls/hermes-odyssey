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
| `%USERPROFILE%\source\repos\hermes-remote\` | this repo: the desktop-half plugin, the contract, the tests |
| `%LOCALAPPDATA%\hermes\plugins\hermes-remote` | a **directory junction** to `plugin/` in this repo |
| `%USERPROFILE%\AndroidStudioProjects\HermesRemote\` | the Android app and the shared Kotlin module |

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

- **Push.** An approval you only see when the app is foregrounded is not an approval.
  FCM, or a foreground service holding the socket. PawlRemote deferred this too and it is
  the first thing worth adding once the app works.
- **iOS.** Unbuildable from Windows; Xcode is macOS-only. Keep `shared` written so an iOS
  shell stays an additive slice.
- **mDNS.** Until the desktop advertises itself, a moved desktop means re-scanning the QR.

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
  networks only.

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
