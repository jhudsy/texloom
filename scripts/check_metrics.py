#!/usr/bin/env python3
"""The relay's /metrics, for Prometheus (server/metrics.py): its text format, the numbers it counts, and what it must
not say (no project ids, no codes; nothing a client sends that could add series without end).

Starts a throwaway `texloom-server` (SQLite) and drives two apps (texloom/replica.py) through it.

    uv run --extra server python scripts/check_metrics.py
"""

import asyncio
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_restore import App  # noqa: E402
from checklib import check, server, summary  # noqa: E402

from texloom.crypto import Keys  # noqa: E402

# a sample: name, optional {labels}, a number (Prometheus's text format)
SAMPLE = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*",?)*\})? -?[0-9.e+Inf]+')


def scrape(url: str) -> dict[str, float]:
    with urllib.request.urlopen(url + "/metrics", timeout=10) as r:
        body = r.read().decode()
        assert r.headers.get_content_type() == "text/plain", r.headers
    bad = [line for line in body.splitlines() if line and not line.startswith("#") and not SAMPLE.fullmatch(line)]
    assert not bad, f"not Prometheus's format: {bad[:3]}"
    out = {}
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            out[name] = float(value)
    out["_text"] = body  # type: ignore[assignment]
    return out


async def main() -> int:
    with server("metrics", "--default-allowance", "unlimited", kind="sqlite") as srv:
        before = scrape(srv.url)
        check("/metrics answers in Prometheus's text format", before.get("texloom_healthy") == 1
              and before.get("texloom_connections") == 0 and 'texloom_store_seconds_bucket{le="+Inf"}' in before, before["_text"][:800])
        keys = Keys.generate()
        a, b = App(srv.tmp / "a", keys, srv.url), App(srv.tmp / "b", keys, srv.url)
        try:
            await a.start()
            await b.start()
            for i in range(3):
                await a.edit(f"line {i}")
            m = scrape(srv.url)
            check("it counts connections, messages by type, stores and their time, catch-ups",
                  m.get("texloom_connections") == 2 and m.get("texloom_connections_total") == 2
                  and m.get('texloom_messages_total{type="hello"}', 0) >= 2 and m.get('texloom_messages_total{type="up"}') == 3
                  and m.get("texloom_stores_total") == 3 and m.get("texloom_store_seconds_count") == 3
                  and m.get("texloom_projects_open") == 1 and m.get("texloom_storage_used_bytes", 0) > 0, m["_text"])
            apps = {k: v for k, v in m.items() if k.startswith("texloom_apps{")}
            check("and the apps connected, by app and version", sum(apps.values()) == 2 and all('version="' in k for k in apps), apps)
            text = m["_text"]
            check("it names no project (no id, no label for one)", keys.project not in text and "project=" not in text
                  and "code=" not in text, text)
        finally:
            await a.stop()
            await b.stop()
        # an app saying it is some version that doesn't exist is counted as "other", not as a series of its own
        import json

        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(srv.url.replace("http", "ws") + f"/ws/{Keys.generate().project}") as ws:
                await ws.send_str(json.dumps({"t": "hello", "app": "made-up", "v": "999.1.1", "since": 0}))
                await ws.receive(timeout=10)
                m = scrape(srv.url)
        check("a made-up app or version is counted as \"other\"", m.get('texloom_apps{app="other",version="other"}') == 1
              and "999.1.1" not in m["_text"] and "made-up" not in m["_text"], m["_text"])
        await asyncio.sleep(0.3)
        check("a connection gone is counted out", scrape(srv.url).get("texloom_connections") == 0)
    return summary("metrics")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
