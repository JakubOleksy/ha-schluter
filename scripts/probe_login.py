#!/usr/bin/env python3
"""Local probe for the Schluter API.

Usage:
    1. cp scripts/secrets.json.example scripts/secrets.json
    2. Edit scripts/secrets.json with your real username/password.
    3. python3 scripts/probe_login.py

scripts/secrets.json is gitignored. The script reads it, runs the
integration's real SchluterApi against the live backend, prints
results, and never writes credentials to disk or logs.
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
InvalidUserPasswordError = _api_module.InvalidUserPasswordError
SchluterApi = _api_module.SchluterApi

SECRETS_PATH = Path(__file__).resolve().parent / "secrets.json"


def _load_secrets() -> tuple[str, str]:
    if not SECRETS_PATH.exists():
        sys.exit(
            f"Missing {SECRETS_PATH}. Copy scripts/secrets.json.example and fill in your credentials."
        )
    data = json.loads(SECRETS_PATH.read_text())
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        sys.exit("secrets.json must contain non-empty 'username' and 'password'.")
    return username, password


async def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Silence aiohttp's very chatty wire logs but keep our api logger verbose.
    logging.getLogger("aiohttp").setLevel(logging.INFO)
    logging.getLogger("custom_components.schluter.api").setLevel(logging.DEBUG)
    logging.getLogger("schluter.api").setLevel(logging.DEBUG)

    username, password = _load_secrets()

    async with aiohttp.ClientSession() as session:
        api = SchluterApi(session)
        try:
            session_id = await api.async_get_sessionid(username, password)
        except InvalidUserPasswordError:
            print("ERROR: invalid username/password")
            return 2
        except ApiError as err:
            print(f"ERROR: login failed: {err}")
            return 3

        # Never print the full session id.
        print(f"Login OK. session_id={session_id[:4]}...{session_id[-4:]}")
        refresh_token = api.refresh_token
        if refresh_token:
            print(f"Captured refresh_token={refresh_token[:4]}...{refresh_token[-4:]}")
        else:
            print("WARNING: no refresh_token captured from login response")

        try:
            thermostats = await api.async_get_current_thermostats(session_id)
        except ApiError as err:
            print(f"ERROR: fetching thermostats failed: {err}")
            return 4

        print(f"Found {len(thermostats)} thermostats:")
        for serial, thermostat in thermostats.items():
            print(
                f"  {serial}: name={thermostat.name!r} "
                f"temp={thermostat.temperature} setpoint={thermostat.set_point_temp} "
                f"heating={thermostat.is_heating} online={thermostat.is_online}"
            )

        # Verify /logout then /connect lifecycle.
        if refresh_token:
            print("Logging out current session...")
            await api.async_logout()
            print("Reconnecting via refresh token...")
            try:
                new_session_id = await api.async_connect_with_refresh_token(refresh_token)
            except ApiError as err:
                print(f"ERROR: /connect failed: {err}")
                return 5
            print(f"Connect OK. session_id={new_session_id[:4]}...{new_session_id[-4:]}")
            thermostats = await api.async_get_current_thermostats(new_session_id)
            print(f"Re-fetched {len(thermostats)} thermostats after /connect.")
            await api.async_logout()
            print("Final logout OK.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
