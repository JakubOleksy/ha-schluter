#!/usr/bin/env python3
"""Probe the Schluter backend to discover accepted `setpointMode` values.

Usage:
    python3 scripts/probe_setpoint_mode.py [device_id]

If device_id is omitted, the first thermostat returned by /devices is used.

The probe:
  1. Logs in and reads the device's current setpointMode (raw JSON).
  2. Tries a curated list of candidate values via PUT /device/{id}/attribute,
     printing the server response code for each.
  3. Restores the original setpointMode at the end.

scripts/secrets.json must exist (gitignored). The script never prints credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

import aiohttp

REPO_ROOT = Path(__file__).resolve().parent.parent
API_PATH = REPO_ROOT / "custom_components" / "schluter" / "api.py"

_spec = importlib.util.spec_from_file_location("schluter_api_probe", API_PATH)
assert _spec and _spec.loader
_api_module = importlib.util.module_from_spec(_spec)
sys.modules["schluter_api_probe"] = _api_module
_spec.loader.exec_module(_api_module)

ApiError = _api_module.ApiError
SchluterApi = _api_module.SchluterApi

SECRETS_PATH = REPO_ROOT / "scripts" / "secrets.json"

CANDIDATE_VALUES = [
    # strings
    "schedule", "Schedule", "SCHEDULE",
    "manual", "Manual", "MANUAL", "permanent", "PermanentHold", "permanent_hold",
    "away", "Away", "AWAY", "vacation", "Vacation",
    "off", "Off", "OFF", "standby", "Standby",
    "auto", "Auto", "AUTO",
    "hold", "Hold",
    # integers — some backends use enums
    0, 1, 2, 3, 4, 5,
]


def _load_secrets() -> tuple[str, str]:
    data = json.loads(SECRETS_PATH.read_text())
    return data["username"], data["password"]


async def _put_setpoint_mode(api: SchluterApi, device_id: str, value):
    """Send PUT and return (ok, code, raw_payload)."""
    try:
        data, _ = await api._request(
            "PUT",
            f"/device/{device_id}/attribute",
            json_data={"setpointMode": value},
        )
        return True, None, data
    except ApiError as err:
        return False, str(err), None


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("schluter_api_probe").setLevel(logging.INFO)

    target_device = sys.argv[1] if len(sys.argv) > 1 else None

    username, password = _load_secrets()

    async with aiohttp.ClientSession() as session:
        api = SchluterApi(session)
        await api.async_get_sessionid(username, password)

        # discover a device id
        if target_device is None:
            thermostats = await api.async_get_current_thermostats(api.sessionid)
            target_device = next(iter(thermostats))
            print(f"Using first thermostat: {target_device} ({thermostats[target_device].name})")
        else:
            print(f"Using device id: {target_device}")

        # 1) Read raw setpointMode
        raw, _ = await api._request(
            "GET",
            f"/device/{target_device}/attribute?attributes=setpointMode",
        )
        print("\n=== Raw GET setpointMode response ===")
        print(json.dumps(raw, indent=2, default=str))

        # extract the current value to restore later
        original_value = None
        def _find(obj):
            nonlocal original_value
            if isinstance(obj, dict):
                if "setpointMode" in obj and not isinstance(obj["setpointMode"], (dict, list)):
                    original_value = obj["setpointMode"]
                for v in obj.values():
                    _find(v)
            elif isinstance(obj, list):
                for v in obj:
                    _find(v)
        _find(raw)
        print(f"\nDetected current setpointMode value: {original_value!r}")

        # 2) Probe candidates
        print("\n=== Candidate probe ===")
        accepted: list = []
        for value in CANDIDATE_VALUES:
            if original_value is not None and value == original_value:
                continue
            ok, code, _ = await _put_setpoint_mode(api, target_device, value)
            marker = "OK " if ok else "ERR"
            print(f"  {marker} setpointMode={value!r:>20}   code={code}")
            if ok:
                accepted.append(value)
                # immediately restore so we don't leave it in a weird state
                if original_value is not None:
                    await _put_setpoint_mode(api, target_device, original_value)

        print("\n=== Summary ===")
        print(f"Accepted values: {accepted!r}")
        if original_value is not None:
            print(f"Restoring original setpointMode={original_value!r}")
            await _put_setpoint_mode(api, target_device, original_value)

        await api.async_logout()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
