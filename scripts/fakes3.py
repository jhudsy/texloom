"""A stand-in for an S3-compatible store, for the failover checks (scripts/check_failover.py): the
requests server/files.py makes (PUT, GET, HEAD and DELETE of an object, HEAD of the bucket), with
the ETag and conditional writes (If-Match, If-None-Match: *) the failover lease relies on. It does
not check signatures. It runs in the check's own event loop, so a check can reach into it:

    s3 = FakeS3()
    url = await s3.start()               # the endpoint: http://127.0.0.1:PORT, bucket "bucket"
    s3.conditional = False               # a store that ignores conditions (the relay must refuse it)
    s3.racy = True                       # a store that checks a condition, then writes: not atomic (also refused)
    s3.fail_for.add("relay-a")           # requests signed with this access key id get a 503 (a relay
                                         # cut off from the store, the others unaffected)
    s3.hang_for.add("relay-a")           # ... or never answer (a time-out)
    await s3.stop()
"""

from __future__ import annotations

import asyncio
import hashlib
import re

from aiohttp import web

BUCKET = "bucket"


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}  # key -> (data, etag)
        self.conditional = True
        self.racy = False
        self.fail_for: set[str] = set()
        self.hang_for: set[str] = set()
        self.log: list[tuple[str, str, str]] = []  # (key id, method, key)
        self._runner: web.AppRunner | None = None
        self.url = ""

    @staticmethod
    def _who(request: web.Request) -> str:
        m = re.search(r"Credential=([^/]+)/", request.headers.get("Authorization", ""))
        return m.group(1) if m else ""

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        who = self._who(request)
        if who in self.hang_for:
            await asyncio.sleep(3600)
        if who in self.fail_for:
            return web.Response(status=503, text="unavailable")
        path = request.match_info["path"]
        bucket, _, key = path.partition("/")
        if bucket != BUCKET:
            return web.Response(status=404, text="no such bucket")
        self.log.append((who, request.method, key))
        if not key:
            return web.Response(status=200)  # HEAD of the bucket
        if request.method == "PUT":
            data = await request.read()
            old = self.objects.get(key)
            if self.conditional:
                if "If-None-Match" in request.headers and request.headers["If-None-Match"] == "*" and old is not None:
                    return web.Response(status=412, text="precondition failed")
                if "If-Match" in request.headers and (old is None or old[1] != request.headers["If-Match"]):
                    return web.Response(status=412, text="precondition failed")
            if self.racy:
                await asyncio.sleep(0.05)  # (the check above and the write below aren't one step)
            etag = '"' + hashlib.md5(data).hexdigest() + '"'
            self.objects[key] = (data, etag)
            return web.Response(status=200, headers={"ETag": etag})
        if request.method in ("GET", "HEAD"):
            if key not in self.objects:
                return web.Response(status=404, text="no such key")
            data, etag = self.objects[key]
            return web.Response(status=200, body=data if request.method == "GET" else None, headers={"ETag": etag})
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return web.Response(status=204)
        return web.Response(status=405)

    async def start(self) -> str:
        app = web.Application(client_max_size=1 << 28)
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
