Turn hermes-remote into a distributable Hermes plugin named hermes-talaria, and fix the silent
prompt.submit failure that loses phone-sent messages.

Evidence gathered 2026-09-10:
- A phone message never became a turn. Device store shows last_seen 13:49:41, so the socket
  authenticated, but state.db has no matching message row and no new session.
- Cause is unhandled rejection, not transport. Connection.kt:217 appends the user bubble locally,
  Connection.kt:220 calls prompt.submit and discards the RpcOutcome. tui_gateway/server.py:1070
  returns a silent 4001 for a stale runtime id; methods_prompt.py also has a 4090 slot refusal and
  a reattach refusal. Every one is invisible to the user today.
- No core patches from this project: nothing in the hermes-agent diff mentions the plugin or 9443.
- Naming is clear: the curated catalog has no hermes-remote and nothing close to talaria.
- Caveat: this machine's tui_gateway carries 69 locally modified core files with LOCAL PATCH
  markers from unrelated work. Protocol claims verified by reading were read against that tree,
  never against stock Hermes.

Slice order, first two before any rename:

V1 - Surface RPC failures in the app. prompt.submit must handle its outcome the way approval
     respond does: mark the local bubble failed and set state.error with the gateway's message.
     Visible result: sending into a stale session shows an error instead of a sent-looking bubble.
V2 - Recover rather than only report. On 4001, re-resume the stored session id and retry once,
     using the runtime-id-changed reseed rule in docs/PLAN.md around line 599. Then run the real
     end-to-end send that has never been fired at a live gateway, against a scratch session.
V3 - Standalone serving mode, so no dashboard is needed. The client uses only three paths
     (Client.kt): the plugin /health, /ws-ticket, and /api/ws. tui_gateway/ws.py documents
     handle_ws as a one-line mount reusing server.dispatch, so /api/ws can be terminated in the
     always-on gateway process. Decide what /ws-ticket answers with no dashboard gate, and keep
     arming explicit so no socket opens in the plain CLI (see plugin/__init__.py:14).
V4 - mDNS discovery, replacing the address baked into the pairing payload by hr_pairing.build.
     This is what makes the QR portable off a reserved DHCP lease.
V5 - Rename to hermes-talaria across plugin.yaml, dashboard/manifest.json, hr_routes.PLUGIN_NAME,
     plugins.enabled, the Android package, and the pairing URI scheme. Ship a migration note: an
     existing paired phone will need to pair again. Do this after V1-V4 so the rename lands once.
V6 - Distribution. Firewall setup emitted as a command by `hermes remote pair` rather than living
     in the README, plus the macOS and Linux equivalents. Then catalog packaging so
     `hermes plugins install` works, and an APK story for the phone half.

Kill criterion for V3: if handle_ws cannot be driven from the bare-ASGI listener without pulling
the dashboard's app state in, stop and reconsider hosting a minimal Starlette app instead.
Do not verify protocol behavior by reading this machine's tui_gateway alone; diff against the
upstream blob first, since the working tree is patched.