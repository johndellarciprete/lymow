"""Lymow driver — pairing (email/password + Google OAuth) and device listing."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from typing import Any

# Make the app-root `lymow` package importable from this nested driver module.
# NOTE: it is named `lymow`, not `lib`, because the Homey runner already has its
# own top-level `lib` package in sys.modules — `from lib.x import` would hit that.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import aiohttp  # noqa: E402

from homey.driver import Driver, ListDeviceProperties  # noqa: E402

from lymow.api import CognitoAuth, LymowAuthError, LymowClient, new_session  # noqa: E402
from lymow.const import REGIONS  # noqa: E402


def _thing_name(d: dict) -> str:
    return (
        d.get("deviceThingName")
        or d.get("thingName")
        or d.get("thing_name")
        or d.get("deviceId")
        or d.get("id")
        or str(d)
    )


def _label(d: dict) -> str:
    n = d.get("deviceName") or d.get("name") or d.get("alias") or _thing_name(d)
    return f"Lymow {n}"


class LymowDriver(Driver):
    async def on_init(self) -> None:
        self._register_flow()
        self.log("Lymow driver initialized")

    # ── Flow cards ─────────────────────────────────────────────────────────
    def _register_flow(self) -> None:
        flow = self.homey.flow

        def _bind(method_name: str):
            async def listener(args, **kwargs):
                await getattr(args["device"], method_name)()
            return listener

        flow.get_action_card("start").register_run_listener(_bind("async_start_mow"))
        flow.get_action_card("pause").register_run_listener(_bind("async_pause"))
        flow.get_action_card("dock").register_run_listener(_bind("async_dock"))
        flow.get_action_card("stop").register_run_listener(_bind("async_stop"))

        zone_card = flow.get_action_card("start_zone")

        async def start_zone_run(args, **kwargs):
            device = args["device"]
            zone = args.get("zone") or {}
            hash_id = (zone.get("data") or {}).get("hashId") or zone.get("id")
            if not hash_id:
                raise Exception("No zone selected.")
            await device.async_start_zone(hash_id)

        zone_card.register_run_listener(start_zone_run)

        async def zone_autocomplete(query, **kwargs):
            device = kwargs.get("device")
            zones = device.get_zones() if device is not None else []
            q = (query or "").lower()
            return [
                {"name": z["name"], "data": {"hashId": z["hashId"]}}
                for z in zones
                if q in (z["name"] or "").lower()
            ]

        zone_card.get_argument("zone").register_autocomplete_listener(zone_autocomplete)

        # Alert trigger: fire only when the fired alert matches the flow's chosen
        # dropdown value (or "any"). The dropdown value may arrive as an id string
        # or a {id,...} object depending on the SDK, so handle both.
        async def alert_run(args, **kwargs):
            sel = args.get("type")
            sel_id = sel.get("id") if isinstance(sel, dict) else sel
            return sel_id == "any" or sel_id == kwargs.get("event_type")

        flow.get_device_trigger_card("mower_alert").register_run_listener(alert_run)

    # ── Pairing ────────────────────────────────────────────────────────────
    async def on_pair(self, session) -> None:
        # Scratch state for the pairing flow: populated by the login / google
        # handlers, consumed by list_devices.
        #
        # NOTE: kept on the DRIVER, not in this function's closure. If Homey
        # starts a fresh PairSession when the view changes, on_pair runs again
        # and a closure-local dict would silently reset to zero devices -- which
        # surfaces as an empty device list. Carrying it on self survives that.
        self._pair_seq = getattr(self, "_pair_seq", 0) + 1
        sid = self._pair_seq

        state: dict[str, Any] = getattr(self, "_pair_state", None) or {
            "region": "us-east-2",
            "auth_method": "password",
            "email": "",
            "password": "",
            "tokens": {},        # CognitoAuth.to_dict()
            "devices": [],       # raw device dicts from the API
            "pkce_verifier": "",
            "oauth_state": "",
        }
        self._pair_state = state
        self.log(f"[pair {sid}] session started (carrying {len(state['devices'])} device(s))")

        async def handle_regions(_data: Any = None) -> dict:
            """Return the region list for the picker in the start view."""
            return {"regions": REGIONS}

        async def handle_login(data: dict) -> dict:
            """Email/password login → fetch device list."""
            region = (data or {}).get("region") or "us-east-2"
            email = ((data or {}).get("email") or "").strip()
            password = (data or {}).get("password") or ""
            if region not in REGIONS:
                raise Exception("Please choose a valid region.")
            if not email or not password:
                raise Exception("Email and password are required.")
            try:
                async with new_session() as http:
                    auth = CognitoAuth(region, http)
                    await auth.login(email, password)
                    await auth.get_aws_credentials()
                    client = LymowClient(region, auth, http)
                    devices = await client.get_device_list()
                    tokens = auth.to_dict()
            except LymowAuthError as e:
                self.error("Lymow login failed:", e)
                raise Exception("Invalid email or password.") from e
            if not devices:
                raise Exception("No Lymow devices found on this account.")
            state.update(
                region=region,
                auth_method="password",
                email=email,
                password=password,
                tokens=tokens,
                devices=devices,
            )
            self.log(f"[pair {sid}] login OK -> stored {len(devices)} device(s)")
            return {"count": len(devices)}

        async def handle_set_region(data: dict) -> dict:
            region = (data or {}).get("region") or "us-east-2"
            if region not in REGIONS:
                raise Exception("Please choose a valid region.")
            state["region"] = region
            return {"ok": True}

        async def handle_google_start(data: dict) -> dict:
            """Build the Cognito Hosted-UI authorize URL (Google) with PKCE."""
            region = (data or {}).get("region") or state.get("region") or "us-east-2"
            if region not in REGIONS:
                raise Exception("Please choose a valid region.")
            verifier = secrets.token_urlsafe(64)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            oauth_state = secrets.token_urlsafe(24)
            state.update(region=region, pkce_verifier=verifier, oauth_state=oauth_state)
            async with new_session() as http:
                auth = CognitoAuth(region, http)
                url = auth.get_oauth_authorize_url(
                    redirect_uri="myapp://callback/",
                    provider="Google",
                    state=oauth_state,
                    code_challenge=challenge,
                )
            self.log(f"[pair {sid}] google_start -> built authorize URL for {region}")
            return {"url": url, "state": oauth_state}

        async def handle_google_code(data: dict) -> dict:
            """Exchange the pasted OAuth authorization code for tokens + devices."""
            region = state.get("region") or (data or {}).get("region") or "us-east-2"
            raw = ((data or {}).get("code") or "").strip()
            # Accept a full myapp://callback/?code=... paste or a bare code.
            import re

            m = re.search(r"[?&]code=([a-f0-9-]+)", raw, re.I)
            code = m.group(1) if m else raw
            if not code:
                raise Exception("Paste the authorization code from the redirect URL.")
            try:
                async with new_session() as http:
                    auth = CognitoAuth(region, http)
                    await auth.exchange_oauth_code(
                        code, "myapp://callback/",
                        code_verifier=state.get("pkce_verifier") or None,
                    )
                    await auth.get_aws_credentials()
                    client = LymowClient(region, auth, http)
                    devices = await client.get_device_list()
                    tokens = auth.to_dict()
            except LymowAuthError as e:
                self.error("Google OAuth failed:", e)
                raise Exception("Google sign-in failed — the code may have expired.") from e
            if not devices:
                raise Exception("No Lymow devices found on this account.")
            state.update(
                auth_method="google", email="", password="",
                tokens=tokens, devices=devices,
            )
            self.log(f"[pair {sid}] google_code OK -> stored {len(devices)} device(s)")
            return {"count": len(devices)}

        async def handle_list_devices(_data: Any = None) -> list[ListDeviceProperties]:
            """Return the collected devices for the list_devices template view."""
            self.log(f"[pair {sid}] list_devices called; state has {len(state['devices'])} device(s)")
            out: list[ListDeviceProperties] = []
            for d in state["devices"]:
                thing = _thing_name(d)
                out.append(
                    {
                        "name": _label(d),
                        "data": {"id": thing},
                        "store": {
                            "thing_name": thing,
                            "region": state["region"],
                            "auth_method": state["auth_method"],
                            "email": state["email"],
                            "password": state["password"],
                            "refresh_token": state["tokens"].get("refresh_token"),
                            "id_token": state["tokens"].get("id_token"),
                            "access_token": state["tokens"].get("access_token"),
                        },
                    }
                )
            self.log(f"[pair {sid}] list_devices -> returning {len(out)}: {[o['name'] for o in out]}")
            return out

        session.set_handler("get_regions", handle_regions)
        session.set_handler("login", handle_login)
        session.set_handler("set_region", handle_set_region)
        session.set_handler("google_start", handle_google_start)
        session.set_handler("google_code", handle_google_code)
        session.set_handler("list_devices", handle_list_devices)


homey_export = LymowDriver
