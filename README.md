# Lymow for Homey

An unofficial Homey app for **Lymow robot lawn mowers**, ported from the
[Lymow-HA](https://github.com/d3dfantasy99/Lymow-HA) Home Assistant integration.
Built with the **Homey Python Apps SDK (v3)**.

Not affiliated with or endorsed by Lymow. It talks to Lymow's AWS backend by
reverse-engineering the official app, and may break if that backend changes.

## What it does (v1 — core control)

- **Pairing** with your Lymow account: email/password **or** Google.
- **Live state** over AWS IoT MQTT: battery, status, online/offline, error/warning, RTK/GPS fix.
- **Commands**: Start/Resume, Pause, Return to Dock, Stop (cancel task) — from the device tile.
- **Flow cards**:
  - Actions: Start, Pause, Dock, Stop, and **Start mowing a zone** (zone autocomplete from the map).
  - Triggers: Status changed, Error occurred, Went online, Went offline.
  - Remote Control over WiFi.  Move your mower when you are not in bluetooth range.


## Architecture

> The shared package is called `lymow/`, **not** `lib/`, on purpose: the Homey runner has its own
> top-level `lib` package already in `sys.modules`, so `from lib.x import ...` inside an app
> resolves to the runner's `lib` and fails. Don't rename it back.

- `lymow/` — logic ported almost verbatim from the HA integration (Home-Assistant-free):
  - `api.py` Cognito SRP + Google OAuth + Identity-Pool creds + SigV4 S3; `mqtt.py` AWS IoT
    MQTT-over-WSS (SigV4 presigned); `protocol.py` + `proto/lymow_pb2.py` protobuf + wire parser;
    `const.py`, `state.py`, `state_matrix.py`.
- `drivers/mower/driver.py` — pairing handlers + Flow action/autocomplete registration.
- `drivers/mower/device.py` — MQTT runtime, reconnect on credential expiry, state → capabilities,
  command opcodes, Flow triggers (ported from the HA coordinator).
- `drivers/mower/pair/` — custom pairing views (`start.html`, `google.html`).

## Develop / run

Prerequisites: Node.js, `npm i -g homey`, Docker Desktop **running** (Python apps compile deps in Docker).

```bash
# from the app folder
homey app dependencies install     # cross-compile deps (Docker)   [Windows: then run the fix below]
bash scripts/fix_windows_venv.sh   # WINDOWS ONLY — strip dangling venv symlinks (see script header)
homey app validate                 # should pass at level `publish`
homey app run                      # upload + run on your Homey, streaming logs (Ctrl+C to stop)
```

> **Windows note:** after any `homey app dependencies add/install`, run
> `bash scripts/fix_windows_venv.sh` before `validate`/`run`, or the CLI fails with
> *"Error while collecting cross-compiled virtual environment"*. The script deletes the
> Linux-only interpreter symlinks the CLI can't copy on NTFS. (Not needed on macOS/Linux.)

## Pairing

Add device -> **Lymow Mower**, pick your **region**, then choose a sign-in method.

**Email & Password** (easy): enter credentials -> the app lists your mowers.

**Google** (fiddly, unavoidably): Lymow's Cognito client only permits the redirect URI
`myapp://callback/` -- every alternative (localhost, Homey's own `callback.athom.com`) returns
`redirect_mismatch`. That scheme belongs to the official Lymow phone app, so after signing in the
browser tries to open `myapp://callback/?code=...` and fails:

* Safari: *"Safari cannot open the page because the address is invalid"*
* Chrome: silently stays put

**The address bar never shows the code** -- it exists only inside a 302 `Location` header, so it has
to be captured. Use the helper:

1. In Homey: Google -> **Continue with Google** -> **Copy link**.
   (The link carries a one-time PKCE challenge, so use that exact link, not an old one.)
2. On your computer:

   ```bash
   python scripts/google_code_helper.py "<paste the link>"
   ```

   Chrome opens; sign in with Google as usual. The script watches the redirect chain and prints the
   code. It drives your installed Chrome/Edge, so no `playwright install` step is needed.
3. Paste the code (or the whole callback URL) into Homey -> **Verify**, within ~60s.

Manual alternative (no helper): open the link in Chrome with DevTools -> Network -> *Preserve log*,
sign in, then find the request whose `Location` starts with `myapp://callback/` and copy the code.

## Self-test (optional)

`lymow/_selftest.py` validates the ported code independently of Homey. Offline it checks imports +
protobuf round-trip; with credentials it does a full live login → device list → MQTT → decode:

```bash
LYMOW_EMAIL=you@example.com LYMOW_PASSWORD=... LYMOW_REGION=us-east-2 \
  python -m lymow._selftest
```
