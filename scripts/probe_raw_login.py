#!/usr/bin/env python3
"""Raw POST /login probe that prints the full server response."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

SECRETS = json.loads((Path(__file__).resolve().parent / "secrets.json").read_text())

BASE = "https://schluterditraheat.com/api"
SWS = json.dumps({"web-app": {"interface": "schluter", "app-version": "1.0.0"}})

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://schluterditraheat.com",
    "Referer": "https://schluterditraheat.com/",
    "SWS-Requester": SWS,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
}

PAYLOAD = {
    "username": SECRETS["username"],
    "password": SECRETS["password"],
    "interface": "schluter",
    "stayConnected": True,
}


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE}/login", headers=HEADERS, json=PAYLOAD) as resp:
            print(f"Status: {resp.status}")
            print("--- Response headers ---")
            for k, v in resp.headers.items():
                print(f"  {k}: {v}")
            print("--- Cookies ---")
            for c in session.cookie_jar:
                print(f"  {c.key}={c.value}; domain={c['domain']}")
            body = await resp.text()
            print("--- Body ---")
            print(body)


if __name__ == "__main__":
    asyncio.run(main())
