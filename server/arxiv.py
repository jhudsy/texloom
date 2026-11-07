"""Fetch an arXiv paper's LaTeX source for import, as a zip.

arXiv doesn't allow browsers to fetch sources directly (no CORS), so the relay does it:
one request at a time, at most one every few seconds (arXiv's guidance for automated
access), with per-address limits, size caps and an in-memory cache. Archives are read in
memory and repacked as a zip of regular files with safe relative names; nothing is written
to disk.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import re
import tarfile
import time
import zipfile
import zlib
from collections import OrderedDict, defaultdict, deque

from texloom.paths import safe_rel
from aiohttp import ClientSession, ClientTimeout, web

ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?/\d{7})(v\d{1,3})?$")
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
MAX_FILES = 3000
MIN_GAP = 3.0  # seconds between requests to arXiv
PER_IP = 10  # imports per address per hour
UA = "TeXLoom/0.1 (self-hosted LaTeX editor; imports a paper's source on a user's request)"


def safe_name(name: str) -> str | None:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or len(p) > 200 for p in parts) or len(parts) > 20:
        return None
    if any(ord(c) < 32 for c in name):
        return None
    joined = "/".join(parts)
    return joined if safe_rel(joined) else None  # e.g. no latexmkrc (latexmk runs it as Perl)


def to_zip(data: bytes, arxiv_id: str) -> bytes:
    """arXiv e-print (tar.gz, gzipped single file, or plain tar) -> zip. Raises ValueError."""
    if data.startswith(b"%PDF"):
        raise ValueError("arXiv has no LaTeX source for this paper (PDF only).")
    raw = data
    if data[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(data, MAX_UNPACKED + 1)
        if len(raw) > MAX_UNPACKED:
            raise ValueError("The source is too large to import.")
    out = io.BytesIO()
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        try:
            tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
        except tarfile.TarError:
            tar = None
        if tar is None:  # a single (gzipped) file: the paper's .tex
            if raw.startswith(b"%PDF"):
                raise ValueError("arXiv has no LaTeX source for this paper (PDF only).")
            z.writestr("main.tex", raw)
        else:
            n = 0
            for m in tar:
                if not m.isfile():
                    continue
                name = safe_name(m.name)
                if not name:
                    continue
                n += 1
                total += m.size
                if n > MAX_FILES or total > MAX_UNPACKED:
                    raise ValueError("The source is too large to import.")
                f = tar.extractfile(m)
                if f:
                    z.writestr(name, f.read(m.size))
    return out.getvalue()


class ArxivFetcher:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.recent: dict[str, deque[float]] = defaultdict(deque)
        self.cache: OrderedDict[str, bytes] = OrderedDict()

    def _allow(self, ip: str) -> bool:
        now = time.monotonic()
        if len(self.recent) > 10000:  # forget addresses with nothing recent (bounded memory)
            for k in [k for k, v in self.recent.items() if not v or now - v[-1] > 3600]:
                del self.recent[k]
        q = self.recent[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= PER_IP:
            return False
        q.append(now)
        return True

    async def handle(self, request: web.Request) -> web.Response:
        aid = request.match_info["id"].replace("_", "/")
        if not ARXIV_ID.match(aid):
            raise web.HTTPBadRequest(text="not an arXiv id")
        if aid in self.cache:
            self.cache.move_to_end(aid)
            return self._zip(self.cache[aid])
        ip = request.remote or "?"
        if ip in ("127.0.0.1", "::1"):  # behind the reverse proxy
            ip = request.headers.get("X-Real-IP") or ip
        if not self._allow(ip):
            raise web.HTTPTooManyRequests(text="too many imports; try again later")
        async with self.lock:
            wait = self.last + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                data = await self._download(aid)
            finally:
                self.last = time.monotonic()
        try:
            z = await asyncio.get_running_loop().run_in_executor(None, to_zip, data, aid)  # off the event loop
        except (ValueError, OSError, EOFError, zlib.error, gzip.BadGzipFile, tarfile.TarError) as e:
            msg = str(e) if isinstance(e, ValueError) else "Couldn’t read the source archive."
            raise web.HTTPUnprocessableEntity(text=msg)
        self.cache[aid] = z
        while sum(len(v) for v in self.cache.values()) > 100 * 1024 * 1024:
            self.cache.popitem(last=False)
        return self._zip(z)

    async def _download(self, aid: str) -> bytes:
        url = f"https://arxiv.org/e-print/{aid}"
        async with ClientSession(timeout=ClientTimeout(total=60), headers={"User-Agent": UA}) as s:
            async with s.get(url, allow_redirects=True, max_redirects=3) as r:
                if r.status == 404:
                    raise web.HTTPNotFound(text="No such arXiv paper.")
                if r.status != 200:
                    raise web.HTTPBadGateway(text=f"arXiv answered {r.status}.")
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_DOWNLOAD:
                        raise web.HTTPRequestEntityTooLarge(MAX_DOWNLOAD, len(buf), text="The source is too large to import.")
                return bytes(buf)

    @staticmethod
    def _zip(z: bytes) -> web.Response:
        return web.Response(body=z, content_type="application/zip", headers={"Cache-Control": "no-store"})
