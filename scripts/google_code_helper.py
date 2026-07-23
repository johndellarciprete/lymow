"""Capture the Lymow Google OAuth `code` that no browser will show you.

Why this exists
---------------
Lymow's Cognito client only permits the redirect URI `myapp://callback/` (every
alternative returns redirect_mismatch -- verified). That scheme belongs to the
official Lymow phone app, so after you sign in with Google the browser tries to
open `myapp://callback/?code=...`, fails, and:

  * Safari shows "Safari cannot open the page because the address is invalid"
  * Chrome silently stays put

Either way the address bar never shows the code -- it only ever exists inside a
302 `Location` header.

How this works (and why it is not "automation")
----------------------------------------------
Google refuses to sign you in inside an automated browser ("This browser or app
may not be secure") -- it detects navigator.webdriver, which Playwright sets when
IT launches the browser. So this script does NOT launch Chrome through
Playwright. It starts a perfectly ordinary Chrome with only a debugging port
open, then ATTACHES read-only to watch the redirect chain. Chrome reports
navigator.webdriver = false, so Google treats it as a normal browser.

You sign in yourself, by hand, in a real Chrome window. This script never sees
or touches your Google password -- it only reads response headers.

Usage
-----
  1. In Homey: Add device -> Lymow Mower -> Google -> "Continue with Google",
     then press "Copy link" (the link embeds a one-time PKCE challenge, so it
     must be THIS link, not an old one).
  2. On this computer:

         python scripts/google_code_helper.py "<paste the link>"

  3. Sign in with Google in the Chrome window that opens.
  4. The script prints the code. Paste it into Homey and press Verify (~60s).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

CALLBACK_PREFIX = "myapp://callback"
TIMEOUT_S = 300
DEBUG_PORT = 9333

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]


def find_browser() -> str | None:
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def extract_code(url: str) -> str | None:
    m = re.search(r"[?&]code=([^&\s]+)", url)
    return m.group(1) if m else None


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required:  pip install playwright")
        return 2

    auth_url = (sys.argv[1] if len(sys.argv) > 1 else input("Paste the sign-in link from Homey:\n> ")).strip()
    if "oauth2/authorize" not in auth_url:
        print("That does not look like the Lymow sign-in link (expected .../oauth2/authorize?...).")
        return 2

    browser_exe = find_browser()
    if not browser_exe:
        print("Could not find Chrome or Edge. Install Chrome and retry.")
        return 2

    profile = os.path.join(tempfile.gettempdir(), "lymow_google_login_profile")
    os.makedirs(profile, exist_ok=True)

    # Launch a NORMAL browser: no --enable-automation, no Playwright launch.
    # Only a debugging port, so we can watch the redirects.
    # NOTE: open about:blank, NOT the auth URL -- if Chrome navigates during
    # startup the whole redirect chain completes before we attach, and the code
    # is gone. We navigate below, once the listeners are installed.
    proc = subprocess.Popen(
        [
            browser_exe,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("\nA Chrome window is opening. Sign in with Google there.")
    print("(This is a normal Chrome window -- Google will not call it insecure.)\n")

    for _ in range(40):
        if port_open(DEBUG_PORT):
            break
        time.sleep(0.5)
    else:
        print("Chrome did not expose its debugging port; cannot watch the redirect.")
        return 1

    found: dict[str, str] = {}

    def on_response(response) -> None:
        if found:
            return
        loc = response.headers.get("location", "") or ""
        if loc.startswith(CALLBACK_PREFIX):
            code = extract_code(loc)
            if code:
                found.update(code=code, url=loc)

    def on_request_failed(request) -> None:
        if found:
            return
        if request.url.startswith(CALLBACK_PREFIX):
            code = extract_code(request.url)
            if code:
                found.update(code=code, url=request.url)

    def watch(page) -> None:
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        except Exception as e:
            print("Could not attach to Chrome:", str(e)[:160])
            return 1

        for ctx in browser.contexts:
            ctx.on("page", watch)
            for pg in ctx.pages:
                watch(pg)

        # Listeners are live -- now send the browser to the sign-in page.
        try:
            ctx0 = browser.contexts[0]
            page = ctx0.pages[0] if ctx0.pages else ctx0.new_page()
            watch(page)
            page.goto(auth_url, wait_until="domcontentloaded")
        except Exception:
            pass  # a myapp:// hop can abort the navigation; handlers still fire

        deadline = time.time() + TIMEOUT_S
        while not found and time.time() < deadline:
            if proc.poll() is not None:
                break  # user closed the window
            time.sleep(0.4)

        try:
            browser.close()
        except Exception:
            pass

    try:
        proc.terminate()
    except Exception:
        pass

    if not found:
        print("No callback code seen. Did the sign-in finish? Try again with a FRESH link from Homey.")
        return 1

    print("=" * 62)
    print("AUTHORIZATION CODE (paste into Homey, then press Verify):")
    print()
    print("   " + found["code"])
    print()
    print("(Pasting this whole URL works too:)")
    print("   " + found["url"])
    print("=" * 62)
    print("Hurry -- the code expires in about 60 seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
