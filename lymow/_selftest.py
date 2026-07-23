"""Standalone self-test for the ported Lymow lib — no Homey required.

Two modes:

  OFFLINE (default): verifies that every ported module imports and that the
  protobuf schema + protocol encoders/decoders round-trip. Proves the pip deps
  (protobuf, pycognito, paho-mqtt, aiohttp, certifi) are installed and the
  reverse-engineered wire logic works, without touching the network.

  LIVE (set env vars): actually logs in, lists devices, connects AWS IoT MQTT
  and prints the first decoded PbOutput (battery / workStatus). Credentials are
  read from the environment so no password is ever hard-coded:

      LYMOW_EMAIL=you@example.com \
      LYMOW_PASSWORD=... \
      LYMOW_REGION=us-east-2 \
      python -m lymow._selftest

Run from the app root:  python -m lymow._selftest
"""

from __future__ import annotations

import asyncio
import os
import sys


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def offline_checks() -> None:
    print("OFFLINE checks")

    # 1. Imports (proves deps resolve).
    from . import const, mqtt, protocol, state, state_matrix, api  # noqa: F401
    from .proto import lymow_pb2 as pb
    _ok("all modules import (const, api, mqtt, protocol, state, state_matrix, proto)")

    # 2. Protobuf schema loaded from the embedded descriptor.
    msg_in = pb.PbInput()
    msg_in.version = protocol.PB_VERSION_4_9
    msg_in.userCtrl = protocol.USER_CTRL_CLEAN
    raw = msg_in.SerializeToString()
    assert raw, "PbInput serialized empty"
    _ok(f"PbInput encodes ({len(raw)} bytes)")

    # 3. Protocol encoders produce bytes.
    for name, blob in [
        ("encode_userctrl(DOCK)", protocol.encode_userctrl(protocol.USER_CTRL_DOCK)),
        ("encode_query_map()", protocol.encode_query_map()),
        ("encode_start_zones(['abc'])", protocol.encode_start_zones(["abc123"])),
        ("encode_set_cut_height(40)", protocol.encode_set_cut_height(40)),
    ]:
        assert isinstance(blob, (bytes, bytearray)) and blob, f"{name} empty"
    _ok("protocol encoders (userctrl/query_map/start_zones/cut_height) produce bytes")

    # 4. Envelope round-trip + PbOutput decode.
    out = pb.PbOutput()
    out.version = 40
    out.robotInfo.battery = 87
    out.robotInfo.workStatus = const.WORK_STATUS_MOWING
    envelope = protocol.wrap_envelope(out.SerializeToString())
    decoded = protocol.decode_pboutput_envelope(envelope.encode("utf-8"))
    assert decoded is not None, "decode returned None"
    assert decoded.robotInfo.battery == 87
    assert decoded.robotInfo.workStatus == const.WORK_STATUS_MOWING
    _ok("JSON envelope wrap -> decode_pboutput round-trips (battery=87, workStatus=MOWING)")

    # 5. State merge maps protobuf -> flat dict the device consumes.
    flat: dict = {}
    state.merge_pboutput(flat, decoded)
    assert flat.get("battery") == 87, flat
    assert flat.get("workStatus") == const.WORK_STATUS_MOWING, flat
    _ok("state.merge_pboutput -> {battery: 87, workStatus: MOWING}")

    # 6. State matrix lookup picks the right opcodes.
    row = state_matrix.lookup(
        work_status=const.WORK_STATUS_MOWING, robot_status=0, is_recharging=False
    )
    assert row.pause == protocol.USER_CTRL_PAUSE
    assert "pause" in state_matrix.features_for(row)
    _ok("state_matrix.lookup(MOWING) -> pause enabled")

    print("OFFLINE: all checks passed\n")


async def live_check(email: str, password: str, region: str) -> int:
    print(f"LIVE check  region={region}  email={email}")
    import aiohttp

    from .api import CognitoAuth, LymowClient
    from .mqtt import MqttClient
    from .protocol import (
        build_initial_query_packets,
        decode_pboutput_envelope,
        wrap_envelope,
    )

    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(region, session)
        await auth.login(email, password)
        _ok("Cognito SRP login")
        await auth.get_aws_credentials()
        _ok("Identity Pool -> temporary AWS credentials")

        client = LymowClient(region, auth, session)
        devices = await client.get_device_list()
        if not devices:
            print("  [!!] no devices on account")
            return 1
        thing = (
            devices[0].get("deviceThingName")
            or devices[0].get("thingName")
            or devices[0].get("deviceId")
        )
        _ok(f"device list -> {len(devices)} device(s); first thing_name={thing}")

        loop = asyncio.get_running_loop()
        got = asyncio.Event()
        seen: dict = {}

        def on_pboutput(raw: bytes) -> None:
            try:
                m = decode_pboutput_envelope(raw)
                if m is None:
                    return
                seen["battery"] = m.robotInfo.battery
                seen["workStatus"] = m.robotInfo.workStatus
                loop.call_soon_threadsafe(got.set)
            except Exception as e:  # noqa: BLE001
                print("  decode error:", e)

        def on_notify(_data: dict) -> None:
            pass

        host = client._ep["iotDomain"].replace("https://", "").rstrip("/")
        mqtt = MqttClient(thing, host, region, on_pboutput, on_notify)
        await mqtt.connect(auth.access_key_id, auth.secret_access_key, auth.session_token)
        _ok("AWS IoT MQTT connected + subscribed")

        for pkt in build_initial_query_packets():
            mqtt.publish(pkt)

        try:
            await asyncio.wait_for(got.wait(), timeout=25)
            _ok(f"first PbOutput decoded: battery={seen.get('battery')}%  "
                f"workStatus={seen.get('workStatus')}")
        except asyncio.TimeoutError:
            print("  [!!] no PbOutput within 25s (mower may be offline)")
        finally:
            await mqtt.disconnect()

    print("LIVE: done")
    return 0


def main() -> int:
    offline_checks()
    email = os.environ.get("LYMOW_EMAIL")
    password = os.environ.get("LYMOW_PASSWORD")
    region = os.environ.get("LYMOW_REGION", "us-east-2")
    if email and password:
        return asyncio.run(live_check(email, password, region))
    print("LIVE check skipped (set LYMOW_EMAIL / LYMOW_PASSWORD / LYMOW_REGION to run it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
