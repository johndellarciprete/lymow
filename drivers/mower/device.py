"""Lymow mower device — MQTT push runtime + capabilities + commands.

Ported from the Home Assistant coordinator (coordinator.py). The protocol,
auth, MQTT and state logic are unchanged; only the Home Assistant glue is
replaced with the Homey Device SDK (capabilities, flow triggers, store).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

# Make the app-root `lymow` package importable from this nested driver module.
# NOTE: it is named `lymow`, not `lib`, because the Homey runner already has its
# own top-level `lib` package in sys.modules — `from lib.x import` would hit that.
_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import aiohttp  # noqa: E402

from homey.device import Device  # noqa: E402

from lymow.api import CognitoAuth, LymowClient, new_session  # noqa: E402
from lymow.const import (  # noqa: E402
    F_DEVICE_STATE,
    MOWING_STATUSES,
    RTK_STATUS_FIX,
    RTK_STATUS_FLOAT_FIX,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_ESCAPING,
    WORK_STATUS_OFFLINE,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    audio_event_type,
    audio_label,
    error_label,
    warning_label,
)
from lymow.mqtt import MqttClient  # noqa: E402
from lymow.protocol import (  # noqa: E402
    CLEAN_MODE_STR,
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    build_initial_query_packets,
    build_refresh_query_packets,
    decode_pboutput_envelope,
    encode_query_map,
    encode_query_path,
    encode_query_robot_config,
    encode_query_schedules,
    encode_set_audio_volume,
    encode_set_charging_mode,
    encode_set_clean_mode,
    encode_set_cut_height,
    encode_remote_control,
    encode_remote_stop,
    encode_set_dock_on_error,
    encode_set_headlights,
    encode_set_rr_config,
    encode_start_zones,
    encode_userctrl,
    parse_zone_catalog,
)
from lymow.state import derive_current_zone, merge_pboutput  # noqa: E402
from lymow.state_matrix import lookup as lookup_state_row  # noqa: E402

_REFRESH_INTERVAL = 30       # seconds — periodic net/config/RTK refresh
_REST_POLL_INTERVAL = 900    # seconds — online/device-info fallback
_RECONNECT_BASE_DELAY = 5
_WATCHDOG_TIMEOUT = 5.0

# RECHARGE_DOCK (33) only acts when there is a live, resumable task to come home
# to (mowing / resume / zone-partition / paused / escaping). From idle it is a
# silent no-op, so the Dock button must fall back to plain DOCK (2) there.
DOCKABLE_STATUSES = MOWING_STATUSES | {WORK_STATUS_PAUSE, WORK_STATUS_ESCAPING}

# ── Settings enum <-> wire-value maps ────────────────────────────────────────
# Mow-mode picker slug <-> CLEAN_MODE_* string used by encode_set_clean_mode.
_MODE_SLUG_TO_STR = {
    "zigzag": "ZIGZAG_MODE",
    "chess": "CHESS_BOARD_MODE",
    "perimeter": "PERIMETER_LAPS_ONLY_MODE",
    "adaptive": "ADAPTIVE_ZIGZAG_MODE",
}
_MODE_STR_TO_SLUG = {v: k for k, v in _MODE_SLUG_TO_STR.items()}
# Speaker-volume picker slug <-> audioVolume int (0/30/70/100).
_VOL_SLUG_TO_INT = {"mute": 0, "low": 30, "medium": 70, "high": 100}
_VOL_INT_TO_SLUG = {v: k for k, v in _VOL_SLUG_TO_INT.items()}
# Return-route picker slug <-> chargingMode int (1=direct, 0=perimeter).
_ROUTE_SLUG_TO_INT = {"direct": 1, "perimeter": 0}
_ROUTE_INT_TO_SLUG = {v: k for k, v in _ROUTE_SLUG_TO_INT.items()}

# Unit conversion (metric -> imperial) for the Units setting.
_M2_TO_FT2 = 10.7639
_M_TO_FT = 3.28084

# Remote-drive nudge parameters (Homey buttons are momentary, so each tap moves
# briefly then auto-stops). Signs may need swapping after a live test.
_REMOTE_LINEAR_SPEED = 0.5    # m/s forward/backward (was 0.25 — too slow)
_REMOTE_ANGULAR_SPEED = 0.6   # rad/s turn rate
_REMOTE_PULSE_SECONDS = 1.0   # how long each tap drives before auto-stopping


class LymowMowerDevice(Device):
    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def on_init(self) -> None:
        store = self.get_store()
        self._region = store.get("region", "us-east-2")
        self.thing_name = store.get("thing_name") or self.get_data().get("id")
        self._auth_method = store.get("auth_method", "password")
        self._email = store.get("email", "") or ""
        self._password = store.get("password", "") or ""

        self._loop = asyncio.get_running_loop()
        self._state: dict[str, Any] = {}
        self._mqtt: MqttClient | None = None
        self._shutting_down = False
        self._state_event = asyncio.Event()
        self._reconnect_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._rest_task: asyncio.Task | None = None
        self._last_status: str | None = None
        self._last_online: bool | None = None
        self._last_error_code: int = 0
        self._last_audio_id: int | None = None
        self._last_session_event_id: Any = None

        self._session = new_session()
        self._auth = CognitoAuth(self._region, self._session)
        self._auth.from_dict(store)
        self._client = LymowClient(self._region, self._auth, self._session)

        # Command buttons. The old button.* sub-capabilities stored their icon
        # per-device at pair time (never refreshed on update), so we migrated to
        # custom cmd_* capabilities whose icon lives in the capability definition
        # and updates globally. Drop the old caps from already-paired devices and
        # add the new ones so no re-pair is needed.
        # Remove superseded capabilities from already-paired devices: the old
        # button.* (replaced by cmd_*), and the Phase-2 setting capabilities that
        # moved to the device Settings page (blade height / thresholds / toggles).
        for old in (
            "button.start", "button.pause", "button.dock", "button.stop",
            "lymow_blade_height", "lymow_recharge_bat", "lymow_resume_bat",
            "lymow_auto_recharge", "lymow_headlights", "lymow_dock_on_error",
        ):
            if self.has_capability(old):
                try:
                    await self.remove_capability(old)
                except Exception as e:  # noqa: BLE001
                    self.error(f"remove_capability {old} failed:", e)

        # Ensure the current control + sensor capabilities exist on already-paired
        # devices.
        for cap in (
            "cmd_start", "cmd_pause", "cmd_dock", "cmd_stop",
            "cmd_fwd", "cmd_back", "cmd_left", "cmd_right", "lymow_mow_zone",
            "lymow_mow_mode", "lymow_volume", "lymow_dock_route",
            "lymow_clean_area", "lymow_clean_percent", "lymow_clean_time",
            "lymow_satellites", "lymow_gps_precision", "lymow_zone",
            "lymow_charging", "lymow_lifted",
        ):
            if not self.has_capability(cap):
                try:
                    await self.add_capability(cap)
                except Exception as e:  # noqa: BLE001
                    self.error(f"add_capability {cap} failed:", e)

        self._zone_opts_sig = None  # last-published Mow Zone picker options

        for cap, handler in (
            ("cmd_start", self._on_btn_start),
            ("cmd_pause", self._on_btn_pause),
            ("cmd_dock", self._on_btn_dock),
            ("cmd_stop", self._on_btn_stop),
            ("cmd_fwd", self._on_btn_fwd),
            ("cmd_back", self._on_btn_back),
            ("cmd_left", self._on_btn_left),
            ("cmd_right", self._on_btn_right),
            ("lymow_mow_zone", self._on_set_mow_zone),
            ("lymow_mow_mode", self._on_set_mow_mode),
            ("lymow_volume", self._on_set_volume),
            ("lymow_dock_route", self._on_set_dock_route),
        ):
            if self.has_capability(cap):
                self.register_capability_listener(cap, handler)

        self._units = self.get_setting("units") or "metric"
        await self._apply_units()

        try:
            await self._auth.ensure_valid(self._email or None, self._password or None)
        except Exception as e:  # noqa: BLE001
            self.error("Initial auth failed:", e)
            await self.set_unavailable("Sign-in failed — please repair the device.")
            return

        await self._do_rest_poll()
        await self._connect_mqtt()

        self._refresh_task = self._loop.create_task(self._refresh_loop())
        self._rest_task = self._loop.create_task(self._rest_loop())
        self.log("Lymow device initialized:", self.thing_name)

    async def on_uninit(self) -> None:
        self._shutting_down = True
        for t in (self._refresh_task, self._rest_task, self._reconnect_task):
            if t:
                t.cancel()
        if self._mqtt:
            try:
                await self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._mqtt = None
        if self._session and not self._session.closed:
            await self._session.close()

    async def on_deleted(self) -> None:
        await self.on_uninit()

    # ── MQTT connection ────────────────────────────────────────────────────
    async def _connect_mqtt(self) -> None:
        if self._mqtt:
            try:
                await self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._mqtt = None

        host = self._client._ep["iotDomain"].replace("https://", "").rstrip("/")
        cli = MqttClient(
            thing_name=self.thing_name,
            host=host,
            region=self._region,
            on_pboutput=self._on_pboutput,
            on_notify_app=self._on_notify_app,
            on_disconnect_cb=self._on_disconnect,
        )
        await cli.connect(
            access_key=self._auth.access_key_id,
            secret_key=self._auth.secret_access_key,
            session_token=self._auth.session_token,
        )
        self._mqtt = cli
        await self.set_available()
        self._fire_startup_queries()

    def _fire_startup_queries(self) -> None:
        for raw in build_initial_query_packets():
            self._publish(raw)
        self._loop.create_task(self._delayed_map_query())

    async def _delayed_map_query(self) -> None:
        for wait in (5, 8, 15, 30):
            await asyncio.sleep(wait)
            cat = self._state.get("zone_catalog")
            if cat is not None and (getattr(cat, "channels", None) or getattr(cat, "zones", None)):
                return
            self._publish(encode_query_map())

    # ── Inbound MQTT (runs on the event-loop thread via call_soon_threadsafe) ─
    def _on_pboutput(self, raw_envelope: bytes) -> None:
        try:
            msg = decode_pboutput_envelope(raw_envelope)
        except Exception:  # noqa: BLE001
            self.error("Failed to decode PbOutput")
            return
        if msg is None:
            return
        try:
            merge_pboutput(self._state, msg)
        except Exception:  # noqa: BLE001
            self.error("Failed to merge PbOutput")
            return

        # Rich QUERY_MAP zone catalog (needed for the start_zone autocomplete).
        try:
            if msg.btMap.ByteSize() > 200 and getattr(msg.btMap, "queryMap", False):
                catalog = parse_zone_catalog(msg.btMap)
                if catalog.zones:
                    self._state["zone_catalog"] = catalog
                    self._state["btMap"] = catalog.to_btmap_dict()
        except Exception:  # noqa: BLE001
            self.error("Failed to parse zone catalog")

        # Session-completed report -> lastSessionEvent (fires the flow trigger).
        try:
            if msg.cleanReport.ByteSize() > 0:
                report = msg.cleanReport
                report_ts = int(report.cleanStartTime or 0)
                if report_ts and self._state.get("lastCleanReportTs") != report_ts:
                    end_labels = {0: "unknown", 1: "completed", 2: "cancelled"}
                    ci = report.cleanInfo
                    self._state["lastCleanReportTs"] = report_ts
                    self._state["lastSessionEvent"] = {
                        "start_time": (
                            datetime.fromtimestamp(report.cleanStartTime, UTC).isoformat()
                            if report.cleanStartTime else None
                        ),
                        "duration_min": round(float(ci.cleanTime)) if ci.cleanTime is not None else 0,
                        "area_m2": round(float(ci.cleanArea), 1) if ci.cleanArea is not None else None,
                        "clean_percent": round(float(ci.cleanPercent) * 100) if ci.cleanPercent is not None else None,
                        "end_type": end_labels.get(report.mowEndType, "unknown"),
                        "used_battery": report.usedBattery,
                        "zones": list(ci.areaInfo.cleanZoneIds),
                    }
                    self._state["lastSessionEventId"] = report_ts
        except Exception:  # noqa: BLE001
            self.error("Failed to parse cleanReport")

        self._state_event.set()
        self._loop.create_task(self._flush_capabilities())

    def _on_notify_app(self, payload: dict) -> None:
        rs = payload.get("robotState")
        if rs == "online":
            self._state.update({"deviceState": "online", "isOnline": True})
        elif rs == "offline":
            self._state.update({"deviceState": "offline", "isOnline": False})
        self._loop.create_task(self._flush_capabilities())

    def _on_disconnect(self) -> None:
        if self._shutting_down:
            return
        self.log("MQTT disconnected — scheduling reconnect")
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = self._loop.create_task(self._reconnect_with_fresh_creds())

    async def _reconnect_with_fresh_creds(self) -> None:
        attempt = 0
        while not self._shutting_down:
            delay = min(_RECONNECT_BASE_DELAY * (2 ** attempt), 300)
            await asyncio.sleep(delay)
            if self._shutting_down:
                return
            try:
                await self._auth.ensure_valid(self._email or None, self._password or None)
                await self._connect_mqtt()
                self.log("MQTT reconnected")
                return
            except Exception as e:  # noqa: BLE001
                attempt += 1
                self.error(f"Reconnect attempt {attempt} failed:", e)

    # ── Background loops ────────────────────────────────────────────────────
    async def _refresh_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(_REFRESH_INTERVAL)
                if self._mqtt and self._mqtt.is_connected:
                    for raw in build_refresh_query_packets():
                        self._publish(raw)
                    if self.work_status in (2, 8, 9):
                        self._publish(encode_query_path())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error("Refresh loop error:", e)

    async def _rest_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(_REST_POLL_INTERVAL)
                await self._do_rest_poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error("REST loop error:", e)

    async def _do_rest_poll(self) -> None:
        try:
            await self._auth.ensure_valid(self._email or None, self._password or None)
            info = await self._client.get_device_info(self.thing_name)
            if info:
                ds = info.get("deviceState") or info.get("device_state") or "offline"
                self._state["deviceState"] = ds
                self._state["isOnline"] = ds == "online"
                for src in ("ipAddress", "sn", "macAddress", "mcuVersion", "softwareVersion"):
                    if info.get(src):
                        self._state[src] = info[src]
        except Exception as e:  # noqa: BLE001
            self.error("REST poll error:", e)
        await self._flush_capabilities()

    # ── Publish / wait helpers ──────────────────────────────────────────────
    def _publish(self, raw: bytes) -> bool:
        if not self._mqtt or not self._mqtt.is_connected:
            return False
        return self._mqtt.publish(raw)

    async def _wait_state_update(self, timeout: float = _WATCHDOG_TIMEOUT) -> bool:
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _preflight_query_map(self, timeout: float = 3.0) -> None:
        if self._mqtt and self._mqtt.is_connected:
            self._publish(encode_query_map())
            await self._wait_state_update(timeout=timeout)

    def _state_row(self):
        return lookup_state_row(
            work_status=self._state.get("workStatus", 0) or 0,
            robot_status=self._state.get("robotStatus", 0) or 0,
            is_recharging=bool(self._state.get("isRecharging")),
        )

    # ── Properties ──────────────────────────────────────────────────────────
    @property
    def work_status(self) -> int:
        return self._state.get("workStatus", WORK_STATUS_OFFLINE)

    @property
    def is_online(self) -> bool:
        if not self._state:
            return False
        return bool(
            self._state.get("isOnline", False)
            or self._state.get(F_DEVICE_STATE) == "online"
            or self.work_status not in (WORK_STATUS_OFFLINE, -1)
        )

    # ── Commands (ported from coordinator) ──────────────────────────────────
    async def async_start_mow(self, zone_ids: list[str] | None = None) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        await self._preflight_query_map()
        if zone_ids:
            raw = encode_start_zones(zone_ids)
        else:
            row = self._state_row()
            raw = encode_userctrl(row.start_mowing or USER_CTRL_CLEAN)
        ok = self._publish(raw)
        await self._wait_state_update()
        return ok

    async def async_pause(self) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        await self._preflight_query_map()
        row = self._state_row()
        ctrl = row.pause or (
            USER_CTRL_PAUSE_DOCK
            if self.work_status in (WORK_STATUS_DOCKING, WORK_STATUS_PAUSE_DOCKING)
            else USER_CTRL_PAUSE
        )
        ok = self._publish(encode_userctrl(ctrl))
        await self._wait_state_update()
        return ok

    async def async_dock(self) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        await self._preflight_query_map()
        row = self._state_row()
        # Keep-task dock (33) only works with a live task; from idle it does
        # nothing. Use plain DOCK (2) when not in a dockable/resumable state so
        # the button always sends the mower home.
        dockable = (
            self._state.get("workStatus") in DOCKABLE_STATUSES
            or self._state.get("robotStatus") in DOCKABLE_STATUSES
        )
        ctrl = (row.dock or USER_CTRL_RECHARGE_DOCK) if dockable else USER_CTRL_DOCK
        self.log(f"async_dock: dockable={dockable} -> userCtrl={ctrl}")
        ok = self._publish(encode_userctrl(ctrl))
        await self._wait_state_update()
        return ok

    async def async_stop(self) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        await self._preflight_query_map()
        ok = self._publish(encode_userctrl(USER_CTRL_FORCE_REINIT))
        await self._wait_state_update()
        return ok

    async def async_start_zone(self, zone_id: str) -> bool:
        return await self.async_start_mow([zone_id])

    def get_zones(self) -> list[dict]:
        """List of {hashId, name} for the start_zone autocomplete."""
        btmap = self._state.get("btMap") or {}
        zones = btmap.get("zones") or []
        return [
            {"hashId": z.get("hashId"), "name": z.get("name") or z.get("hashId")}
            for z in zones
            if z.get("hashId")
        ]

    # ── Button capability listeners ─────────────────────────────────────────
    async def _on_btn_start(self, value: bool, **kwargs) -> None:
        await self.async_start_mow()

    async def _on_btn_pause(self, value: bool, **kwargs) -> None:
        await self.async_pause()

    async def _on_btn_dock(self, value: bool, **kwargs) -> None:
        await self.async_dock()

    async def _on_btn_stop(self, value: bool, **kwargs) -> None:
        await self.async_stop()

    # ── Remote drive (ported from coordinator) ──────────────────────────────
    async def async_remote_control(self, *, linear_speed: float = 0.0,
                                   angular_speed: float = 0.0) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ls = max(-1.0, min(1.0, float(linear_speed)))
        as_ = max(-1.0, min(1.0, float(angular_speed)))
        return self._publish(encode_remote_control(linear_speed=ls, angular_speed=as_))

    async def async_remote_stop(self) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        return self._publish(encode_remote_stop())

    async def async_remote_pulse(self, *, linear_speed: float = 0.0,
                                 angular_speed: float = 0.0,
                                 duration: float = _REMOTE_PULSE_SECONDS) -> bool:
        """Nudge: STREAM the movement command for `duration`, then stop.

        A single one-shot command is often ignored — real remote control streams
        movement continuously while the control is held, so we repeat it every
        ~150 ms for the pulse window, then send a stop.
        """
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ls = max(-1.0, min(1.0, float(linear_speed)))
        as_ = max(-1.0, min(1.0, float(angular_speed)))
        interval = 0.15
        duration = max(0.15, min(2.0, float(duration)))
        sent = 0
        elapsed = 0.0
        while elapsed < duration:
            if self._publish(encode_remote_control(linear_speed=ls, angular_speed=as_)):
                sent += 1
            await asyncio.sleep(interval)
            elapsed += interval
        self._publish(encode_remote_stop())
        self.log(
            f"remote_pulse linear={ls} angular={as_} sent={sent} "
            f"workStatus={self._state.get('workStatus')} "
            f"mqtt={bool(self._mqtt and self._mqtt.is_connected)}"
        )
        return sent > 0

    async def _on_btn_fwd(self, value: bool, **kwargs) -> None:
        await self.async_remote_pulse(linear_speed=_REMOTE_LINEAR_SPEED)

    async def _on_btn_back(self, value: bool, **kwargs) -> None:
        await self.async_remote_pulse(linear_speed=-_REMOTE_LINEAR_SPEED)

    async def _on_btn_left(self, value: bool, **kwargs) -> None:
        await self.async_remote_pulse(angular_speed=_REMOTE_ANGULAR_SPEED)

    async def _on_btn_right(self, value: bool, **kwargs) -> None:
        await self.async_remote_pulse(angular_speed=-_REMOTE_ANGULAR_SPEED)

    async def async_drive(self, direction: str) -> bool:
        """Single movement command for the hold-to-drive widget (which streams
        these while a button is held, then sends 'stop' on release)."""
        if direction == "fwd":
            return await self.async_remote_control(linear_speed=_REMOTE_LINEAR_SPEED)
        if direction == "back":
            return await self.async_remote_control(linear_speed=-_REMOTE_LINEAR_SPEED)
        if direction == "left":
            return await self.async_remote_control(angular_speed=_REMOTE_ANGULAR_SPEED)
        if direction == "right":
            return await self.async_remote_control(angular_speed=-_REMOTE_ANGULAR_SPEED)
        return await self.async_remote_stop()

    # ── Settings commands (ported from coordinator) ─────────────────────────
    async def async_set_blade_height(self, height_mm: int) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_cut_height(int(height_mm)))
        self._state["cutHeight"] = int(height_mm)  # optimistic
        return ok

    async def async_set_clean_mode(self, mode: str) -> bool:
        mode_int = CLEAN_MODE_STR.get(mode)
        if mode_int is None:
            self.error("Unknown clean mode:", mode)
            return False
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_clean_mode(mode_int))
        self._state["cleanMode"] = mode
        return ok

    async def async_set_charging_mode(self, mode: int) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_charging_mode(int(mode)))
        self._state["chargingMode"] = int(mode)
        return ok

    async def async_set_audio_volume(self, volume: int) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_audio_volume(int(volume)))
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)
        return ok

    async def async_set_dock_on_error(self, enabled: bool) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_dock_on_error(bool(enabled)))
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)
        return ok

    async def async_set_headlights(self, enabled: bool) -> bool:
        await self._auth.ensure_valid(self._email or None, self._password or None)
        ok = self._publish(encode_set_headlights(bool(enabled)))
        self._publish(encode_query_robot_config())
        await self._wait_state_update(timeout=3.0)
        return ok

    async def _send_rr_update(
        self, *, enable_rr=None, recharge_bat=None, resume_bat=None
    ) -> bool:
        """Update rrConfig while preserving the fields we are not changing."""
        await self._auth.ensure_valid(self._email or None, self._password or None)
        d = self._state or {}
        cur_en = d.get("rrEnabled")
        cur_re = d.get("rrRechargeBat")
        cur_rs = d.get("rrResumeBat")
        if cur_en is None:
            cur_en = True
        if cur_re is None:
            cur_re = 10
        if cur_rs is None:
            cur_rs = 98
        rr_start = d.get("rrResumePeriodStart") or {}
        rr_end = d.get("rrResumePeriodEnd") or {}
        sh = int(rr_start.get("hour", 0)) if isinstance(rr_start, dict) else 0
        sm = int(rr_start.get("minute", 0)) if isinstance(rr_start, dict) else 0
        eh = int(rr_end.get("hour", 0)) if isinstance(rr_end, dict) else 0
        em = int(rr_end.get("minute", 0)) if isinstance(rr_end, dict) else 0
        raw = encode_set_rr_config(
            enable_rr=bool(cur_en if enable_rr is None else enable_rr),
            recharge_bat=int(cur_re if recharge_bat is None else recharge_bat),
            resume_bat=int(cur_rs if resume_bat is None else resume_bat),
            period_start_hour=sh, period_start_minute=sm,
            period_end_hour=eh, period_end_minute=em,
        )
        ok = self._publish(raw)
        await self._wait_state_update()
        return ok

    async def async_set_auto_recharge(self, enabled: bool) -> bool:
        self._state["rrEnabled"] = bool(enabled)  # optimistic
        return await self._send_rr_update(enable_rr=enabled)

    async def async_set_recharge_threshold(self, value: int) -> bool:
        value = max(1, min(100, int(value)))
        self._state["rrRechargeBat"] = value
        return await self._send_rr_update(recharge_bat=value)

    async def async_set_resume_threshold(self, value: int) -> bool:
        value = max(1, min(100, int(value)))
        self._state["rrResumeBat"] = value
        return await self._send_rr_update(resume_bat=value)

    # ── Settings capability listeners ───────────────────────────────────────
    async def _on_set_mow_mode(self, value: str, **kwargs) -> None:
        await self.async_set_clean_mode(_MODE_SLUG_TO_STR.get(value, ""))

    async def _on_set_volume(self, value: str, **kwargs) -> None:
        vol = _VOL_SLUG_TO_INT.get(value)
        if vol is not None:
            await self.async_set_audio_volume(vol)

    async def _on_set_dock_route(self, value: str, **kwargs) -> None:
        route = _ROUTE_SLUG_TO_INT.get(value)
        if route is not None:
            await self.async_set_charging_mode(route)

    async def _on_set_mow_zone(self, value: str, **kwargs) -> None:
        """Pick a zone from the on-device picker and start mowing it."""
        try:
            if value in (None, "__none__"):
                return
            if value == "__all__":
                await self.async_start_mow()
            else:
                await self.async_start_zone(value)
        finally:
            # Reset to the prompt so it's ready for next time (this does NOT
            # re-fire the listener — only trigger_capability_listener would).
            try:
                await self.set_capability_value("lymow_mow_zone", "__none__")
            except Exception:  # noqa: BLE001
                pass

    async def _sync_zone_options(self) -> None:
        """Populate the Mow Zone picker with the mower's live zones. Uses
        set_capability_options, and only when the zone set actually changed."""
        if not self.has_capability("lymow_mow_zone"):
            return
        zones = self.get_zones()
        sig = tuple((z["hashId"], z["name"]) for z in zones)
        if sig == getattr(self, "_zone_opts_sig", None):
            return
        self._zone_opts_sig = sig
        values = [
            {"id": "__none__", "title": {"en": "Select a zone…"}},
            {"id": "__all__", "title": {"en": "Whole lawn"}},
        ]
        for z in zones:
            values.append({"id": z["hashId"], "title": {"en": z["name"]}})
        try:
            await self.set_capability_options("lymow_mow_zone", {"values": values})
            self.log(f"Mow Zone options updated: {len(zones)} zone(s)")
        except Exception as e:  # noqa: BLE001
            self.error("set Mow Zone options failed:", e)

    # ── Device Settings page (gear) ─────────────────────────────────────────
    async def on_settings(self, old_settings, new_settings, changed_keys) -> str | None:
        """User changed a setting on the device's Settings page -> send to mower."""
        try:
            if "blade_height" in changed_keys and new_settings.get("blade_height") is not None:
                await self.async_set_blade_height(int(new_settings["blade_height"]))
            if "recharge_bat" in changed_keys and new_settings.get("recharge_bat") is not None:
                await self.async_set_recharge_threshold(int(new_settings["recharge_bat"]))
            if "resume_bat" in changed_keys and new_settings.get("resume_bat") is not None:
                await self.async_set_resume_threshold(int(new_settings["resume_bat"]))
            if "auto_recharge" in changed_keys:
                await self.async_set_auto_recharge(bool(new_settings.get("auto_recharge")))
            if "headlights" in changed_keys:
                await self.async_set_headlights(bool(new_settings.get("headlights")))
            if "dock_on_error" in changed_keys:
                await self.async_set_dock_on_error(bool(new_settings.get("dock_on_error")))
            if "units" in changed_keys:
                self._units = new_settings.get("units") or "metric"
                await self._apply_units()
                await self._flush_capabilities()  # re-convert values immediately
        except Exception as e:  # noqa: BLE001
            self.error("on_settings error:", e)
            raise Exception("Could not apply that setting to the mower.") from e
        return None

    async def _apply_units(self) -> None:
        """Swap the units label on Units-aware sensors (area, distance)."""
        imperial = self._units == "imperial"
        for cap, label in (
            ("lymow_clean_area", "ft²" if imperial else "m²"),
            ("lymow_gps_precision", "ft" if imperial else "m"),
        ):
            if self.has_capability(cap):
                try:
                    await self.set_capability_options(cap, {"units": {"en": label}})
                except Exception as e:  # noqa: BLE001
                    self.error(f"set units on {cap} failed:", e)

    async def _sync_settings_from_state(self) -> None:
        """Reflect the mower's live config on the Settings page. set_settings does
        NOT fire on_settings, so this cannot loop back into a command."""
        desired: dict[str, Any] = {}
        cut = self._state.get("cutHeight")
        if isinstance(cut, (int, float)):
            desired["blade_height"] = max(20, min(100, round(int(cut) / 5) * 5))
        rb = self._state.get("rrRechargeBat")
        if rb is not None:
            desired["recharge_bat"] = max(1, min(100, int(rb)))
        sb = self._state.get("rrResumeBat")
        if sb is not None:
            desired["resume_bat"] = max(1, min(100, int(sb)))
        if self._state.get("rrEnabled") is not None:
            desired["auto_recharge"] = bool(self._state["rrEnabled"])
        cam = self._state.get("camLedStatus")
        if cam is not None:
            desired["headlights"] = int(cam) == 3
        doe = self._state.get("dockOnError")
        if doe is not None:
            desired["dock_on_error"] = bool(doe)

        changed = {}
        for k, v in desired.items():
            try:
                if self.get_setting(k) != v:
                    changed[k] = v
            except Exception:  # noqa: BLE001
                changed[k] = v
        if changed:
            try:
                await self.set_settings(changed)
            except Exception as e:  # noqa: BLE001
                self.error("set_settings sync failed:", e)

    async def _sync_device_info(self) -> None:
        """Reflect read-only device info (firmware / IP / serial) in Settings."""
        fw = self._state.get("mcuVersion") or self._state.get("fwVersion")
        desired = {
            "info_firmware": str(fw) if fw else None,
            "info_ip": self._state.get("ipAddress"),
            "info_serial": self._state.get("sn"),
        }
        changed = {}
        for k, v in desired.items():
            if not v:
                continue
            try:
                if self.get_setting(k) != v:
                    changed[k] = v
            except Exception:  # noqa: BLE001
                changed[k] = v
        if changed:
            try:
                await self.set_settings(changed)
            except Exception as e:  # noqa: BLE001
                self.error("device-info sync failed:", e)

    # ── State → capabilities + flow triggers ────────────────────────────────
    async def _flush_capabilities(self) -> None:
        try:
            online = self.is_online
            battery = self._state.get("battery")
            if isinstance(battery, (int, float)):
                await self._safe_set("measure_battery", float(battery))

            # Status enum.
            if not online:
                status = "offline"
            else:
                row = self._state_row()
                status = row.activity or "unknown"
            await self._safe_set("lymow_status", status)
            await self._safe_set("lymow_online", online)

            # Error / warning text.
            err_codes = self._state.get("errorCodes") or []
            warn_codes = self._state.get("warningCodes") or []
            if err_codes:
                err_text = error_label(int(err_codes[0]))
            elif warn_codes:
                err_text = warning_label(int(warn_codes[0]))
            else:
                err_text = "None"
            await self._safe_set("lymow_error", err_text)

            # RTK fix.
            rtk = self._state.get("rtkStatus")
            if rtk is not None:
                rtk_map = {RTK_STATUS_FIX: "fixed", RTK_STATUS_FLOAT_FIX: "float_fix"}
                await self._safe_set("lymow_rtk", rtk_map.get(int(rtk), "not_ready"))

            # Picker mirrors (controls that stay on the main device view).
            # set_capability_value does NOT fire the listener, so no command loop.
            cm = self._state.get("cleanMode")
            if cm in _MODE_STR_TO_SLUG:
                await self._safe_set("lymow_mow_mode", _MODE_STR_TO_SLUG[cm])
            av = self._state.get("audioVolume")
            if av is not None and int(av) in _VOL_INT_TO_SLUG:
                await self._safe_set("lymow_volume", _VOL_INT_TO_SLUG[int(av)])
            cmode = self._state.get("chargingMode")
            if cmode is not None and int(cmode) in _ROUTE_INT_TO_SLUG:
                await self._safe_set("lymow_dock_route", _ROUTE_INT_TO_SLUG[int(cmode)])

            # ── Sensors ──────────────────────────────────────────────────────
            imperial = getattr(self, "_units", "metric") == "imperial"

            area = self._state.get("cleanArea")
            if isinstance(area, (int, float)):
                val = float(area) * (_M2_TO_FT2 if imperial else 1.0)
                await self._safe_set("lymow_clean_area", round(val))
            pct = self._state.get("cleanPercent")
            if isinstance(pct, (int, float)):
                await self._safe_set("lymow_clean_percent", round(float(pct) * 100))
            ct = self._state.get("cleanTime")
            if isinstance(ct, (int, float)):
                await self._safe_set("lymow_clean_time", round(float(ct)))
            sats = self._state.get("gnssNumSatellites")
            if isinstance(sats, (int, float)):
                await self._safe_set("lymow_satellites", round(float(sats)))
            prec = self._state.get("gnssHorizontalAccuracy")
            if isinstance(prec, (int, float)):
                val = float(prec) * (_M_TO_FT if imperial else 1.0)
                await self._safe_set("lymow_gps_precision", round(val, 2))

            # Current zone (derived from live GPS vs the map).
            if self._state.get("isCharging") or self._state.get("robotStatus") in (
                WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL,
            ):
                await self._safe_set("lymow_zone", "Docked")
            else:
                zone = derive_current_zone(self._state)
                if zone:
                    await self._safe_set("lymow_zone", str(zone))

            await self._safe_set("lymow_charging", bool(self._state.get("isCharging")))
            err_set = set(err_codes)
            await self._safe_set("lymow_lifted", bool(err_set & {7, 8}))

            # Device-info labels on the Settings page.
            await self._sync_device_info()

            # Mow Zone picker options (only re-published when zones change).
            await self._sync_zone_options()

            # Device Settings page mirrors (blade height, thresholds, toggles).
            await self._sync_settings_from_state()

            await self._fire_triggers(status, online, int(err_codes[0]) if err_codes else 0)
        except Exception as e:  # noqa: BLE001
            self.error("flush_capabilities error:", e)

    async def _safe_set(self, cap: str, value: Any) -> None:
        if not self.has_capability(cap):
            return
        try:
            if self.get_capability_value(cap) != value:
                await self.set_capability_value(cap, value)
        except Exception as e:  # noqa: BLE001
            self.error(f"set {cap} failed:", e)

    async def _fire_triggers(self, status: str, online: bool, error_code: int) -> None:
        flow = self.homey.flow
        if status != self._last_status and self._last_status is not None:
            try:
                await flow.get_device_trigger_card("status_changed").trigger(
                    self, {"status": status}
                )
            except Exception:  # noqa: BLE001
                pass
        self._last_status = status

        if online != self._last_online and self._last_online is not None:
            card = "went_online" if online else "went_offline"
            try:
                await flow.get_device_trigger_card(card).trigger(self, {})
            except Exception:  # noqa: BLE001
                pass
        self._last_online = online

        if error_code and error_code != self._last_error_code:
            try:
                await flow.get_device_trigger_card("error_occurred").trigger(
                    self, {"error": error_label(error_code)}
                )
            except Exception:  # noqa: BLE001
                pass
        self._last_error_code = error_code

        # Audio-prompt alerts (blade stuck, cliff, theft, …). audioId is sticky
        # between frames, so fire only on a new, non-None/Max prompt.
        aid = self._state.get("audioId")
        if isinstance(aid, int) and aid not in (0, 33) and aid != self._last_audio_id:
            self._last_audio_id = aid
            slug = audio_event_type(aid)
            try:
                await flow.get_device_trigger_card("mower_alert").trigger(
                    self,
                    {"event": audio_label(aid), "event_type": slug},
                    event_type=slug,
                )
            except Exception:  # noqa: BLE001
                pass

        # Session completed.
        sev = self._state.get("lastSessionEvent")
        sid = self._state.get("lastSessionEventId")
        if isinstance(sev, dict) and sid is not None and sid != self._last_session_event_id:
            self._last_session_event_id = sid
            imperial = getattr(self, "_units", "metric") == "imperial"
            area = sev.get("area_m2")
            if area is None:
                area_out = 0
            else:
                area_out = round(float(area) * (_M2_TO_FT2 if imperial else 1.0))
            try:
                await flow.get_device_trigger_card("session_complete").trigger(
                    self,
                    {
                        "area": area_out,
                        "duration": int(sev.get("duration_min") or 0),
                        "end_type": sev.get("end_type") or "unknown",
                        "battery_used": int(sev.get("used_battery") or 0),
                    },
                )
            except Exception:  # noqa: BLE001
                pass


homey_export = LymowMowerDevice
