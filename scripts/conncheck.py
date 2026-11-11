"""Connect to a TeXLoom server the way the app does, and say what happened at each step.

    uv run python scripts/conncheck.py [https://app.texloom.org]

Uses the app's own certificate check (texloom.tls) and the same WebSocket settings as
texloom.replica, against a made-up project id: the server answers its hello without
storing anything. Run by .github/workflows/conncheck.yml on Windows, macOS and Linux.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import ssl
import sys

import aiohttp

from texloom import tls


async def check(server: str) -> bool:
    ctx = tls.context()
    print(f"python {platform.python_version()} on {platform.platform()}; {ssl.OPENSSL_VERSION}")
    print(f"certificates: {type(ctx).__module__}.{type(ctx).__name__}")
    async with aiohttp.ClientSession(connector=tls.connector(), timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.get(f"{server}/api/info") as r:
            print(f"GET /api/info: {r.status} {(await r.text())[:200]}")
            if r.status != 200:
                return False
        project = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        ws_url = server.replace("http", "ws", 1) + f"/ws/{project}"
        async with http.ws_connect(ws_url, heartbeat=30, max_msg_size=64 * 1024 * 1024) as ws:
            print("WebSocket: connected")
            await ws.send_json({"t": "hello", "since": 0})
            msg = await ws.receive(timeout=20)
            print(f"first message: {msg.type.name} {str(msg.data)[:200]}")
            return msg.type == aiohttp.WSMsgType.TEXT and isinstance(json.loads(msg.data), dict)


def main() -> None:
    server = (sys.argv[1] if len(sys.argv) > 1 else "https://app.texloom.org").rstrip("/")
    try:
        ok = asyncio.run(check(server))
    except Exception as e:  # noqa: BLE001 - the point is to show it
        print(f"FAILED: {type(e).__name__}: {e}")
        ok = False
    print("OK" if ok else "NOT OK")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
