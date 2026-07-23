"""Backend endpoints for the Lymow Drive widget.

Each public function is a widget API endpoint (loaded by the runtime as
app.widgets.drive.api and called as func(homey=<Homey>, **request_args)).
The front-end streams drive() while an arrow is held, then sends dir='stop'
on release — that continuous stream is what makes the mower move constantly.
"""

from __future__ import annotations

from typing import Any


def _device(homey, device_id):
    """Resolve the LymowMowerDevice the widget was configured for."""
    if not device_id:
        return None
    try:
        return homey.drivers.get_driver("mower").get_device_by_id(device_id)
    except Exception:  # noqa: BLE001
        return None


def _body(kwargs) -> dict:
    b = kwargs.get("body")
    return b if isinstance(b, dict) else {}


async def drive(homey, **kwargs) -> dict:
    """Send one movement command (fwd/back/left/right) or stop."""
    body = _body(kwargs)
    dev = _device(homey, body.get("deviceId"))
    if dev is None:
        return {"ok": False, "error": "device not found"}
    await dev.async_drive(str(body.get("dir") or "stop"))
    return {"ok": True}


async def setDeck(homey, **kwargs) -> dict:
    """Set blade/deck height in mm."""
    body = _body(kwargs)
    dev = _device(homey, body.get("deviceId"))
    if dev is None:
        return {"ok": False, "error": "device not found"}
    try:
        mm = int(body.get("mm"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad height"}
    await dev.async_set_blade_height(mm)
    return {"ok": True, "mm": mm}


async def getState(homey, **kwargs) -> dict[str, Any]:
    """Lightweight status for the widget header (battery / status / deck)."""
    body = _body(kwargs)
    dev = _device(homey, body.get("deviceId"))
    if dev is None:
        return {"ok": False}
    def _cap(cap):
        try:
            return dev.get_capability_value(cap) if dev.has_capability(cap) else None
        except Exception:  # noqa: BLE001
            return None
    return {
        "ok": True,
        "battery": _cap("measure_battery"),
        "status": _cap("lymow_status"),
        "online": _cap("lymow_online"),
        "deck": dev._state.get("cutHeight"),
    }
