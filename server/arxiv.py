"""Fetch an arXiv paper's LaTeX source for import, as a zip.

arXiv doesn't allow browsers to fetch sources directly (no CORS), so the relay does it:
one request at a time, at most one every few seconds (arXiv's guidance for automated
access), with per-address limits, size caps and an in-memory cache of small results. Archives
are read a piece at a time, through temporary files (the download, and the zip of regular files
with safe relative names made from it), so an import holds little memory however big the paper.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import re
import shutil
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import IO

from texloom.paths import safe_rel
from aiohttp import ClientSession, ClientTimeout, web

from .wire import client_address

ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?/\d{7})(v\d{1,3})?$")
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
MAX_FILES = 3000
SPOOL = 1024 * 1024  # a temporary file stays in memory up to this
# What one import may use of the relay's shared memory at once: its spooled files, a cached
# result read back (CACHE_ITEM), and buffers.
IMPORT_MEMORY = 24 * 1024 * 1024
IMPORTS = 2  # imports being unpacked at once
CACHE_MAX = 32 * 1024 * 1024  # recent imports kept, so that a retry doesn't download again
CACHE_ITEM = 16 * 1024 * 1024  # bigger results are streamed from their file, not kept
MIN_GAP = 3.0  # seconds between requests to arXiv
PER_IP = 10  # imports per address (an IPv6 /64) per hour
UA = "TeXLoom/0.1 (self-hosted LaTeX editor; imports a paper's source on a user's request)"


class Capped(io.RawIOBase):
    """A stream that fails once more than `limit` bytes have been read from it (an archive that
    unpacks to far more than it says, a "zip bomb")."""

    def __init__(self, inner: IO[bytes], limit: int):
        self.inner, self.left = inner, limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = self.inner.readinto(b)
        self.left -= n
        if self.left < 0:
            raise ValueError("The source is too large to import.")
        return n


def safe_name(name: str) -> str | None:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or len(p) > 200 for p in parts) or len(parts) > 20:
        return None
    if any(ord(c) < 32 for c in name):
        return None
    joined = "/".join(parts)
    return joined if safe_rel(joined) else None  # e.g. no latexmkrc (latexmk runs it as Perl)


def to_zip(src: IO[bytes], out: IO[bytes]) -> None:
    """arXiv e-print (tar.gz, gzipped single file, or plain tar), read from `src` (seekable) ->
    a zip written to `out`, a piece at a time. Raises ValueError."""
    head = src.read(4)
    src.seek(0)
    if head.startswith(b"%PDF"):
        raise ValueError("arXiv has no LaTeX source for this paper (PDF only).")

    def raw() -> IO[bytes]:  # the archive's bytes, unpacked if gzipped, at most MAX_UNPACKED (and headers)
        src.seek(0)
        inner = gzip.GzipFile(fileobj=src, mode="rb") if head[:2] == b"\x1f\x8b" else src
        return io.BufferedReader(Capped(inner, MAX_UNPACKED + MAX_FILES * 1024 + 2 * 1024 * 1024), 1 << 16)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        try:
            tar = tarfile.open(fileobj=raw(), mode="r|")
            first = tar.next()
        except tarfile.TarError:
            tar = None
        if tar is None:  # a single (gzipped) file: the paper's .tex
            data = raw()
            if data.peek(4)[:4].startswith(b"%PDF"):
                raise ValueError("arXiv has no LaTeX source for this paper (PDF only).")
            with z.open("main.tex", "w") as dest:
                shutil.copyfileobj(data, dest, 1 << 16)
            return
        n = total = 0
        m = first
        while m is not None:
            name = safe_name(m.name) if m.isfile() else None
            if name:
                n += 1
                total += m.size
                if n > MAX_FILES or total > MAX_UNPACKED:
                    raise ValueError("The source is too large to import.")
                f = tar.extractfile(m)
                if f:
                    with z.open(name, "w", force_zip64=m.size > 1 << 30) as dest:
                        shutil.copyfileobj(f, dest, 1 << 16)
            m = tar.next()


class ArxivFetcher:
    def __init__(self, memory=None, tmp: Path | None = None) -> None:
        self.memory = memory  # the relay's Budget: an import takes IMPORT_MEMORY of it while it runs
        self.tmp = tmp  # where temporary files go (the server's data folder)
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(IMPORTS)
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

    def _spool(self):
        return tempfile.SpooledTemporaryFile(max_size=SPOOL, dir=self.tmp)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        aid = request.match_info["id"].replace("_", "/")
        if not ARXIV_ID.match(aid):
            raise web.HTTPBadRequest(text="not an arXiv id")
        if aid in self.cache:
            self.cache.move_to_end(aid)
            return self._zip(self.cache[aid])
        # (from this computer without an address named: one address for them all, as before)
        if not self._allow(client_address(request) or "local"):
            raise web.HTTPTooManyRequests(text="too many imports; try again later")
        if self.memory:
            await self.memory.take(IMPORT_MEMORY)
        try:
            async with self.slots:
                with self._spool() as download, self._spool() as out:
                    async with self.lock:
                        wait = self.last + MIN_GAP - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        try:
                            await self._download(aid, download)
                        finally:
                            self.last = time.monotonic()
                    try:
                        await asyncio.get_running_loop().run_in_executor(None, to_zip, download, out)  # off the event loop
                    except (ValueError, OSError, EOFError, zlib.error, gzip.BadGzipFile, tarfile.TarError) as e:
                        msg = str(e) if isinstance(e, ValueError) else "Couldn’t read the source archive."
                        return web.Response(status=422, text=msg)  # (returned, not raised: see the relay's middleware)
                    size = out.tell()
                    out.seek(0)
                    if size <= CACHE_ITEM:
                        z = out.read()
                    else:
                        return await self._stream(request, out, size)
        finally:
            if self.memory:
                self.memory.give(IMPORT_MEMORY)
        self.cache[aid] = z
        while sum(len(v) for v in self.cache.values()) > CACHE_MAX:
            self.cache.popitem(last=False)
        return self._zip(z)

    async def _download(self, aid: str, dest: IO[bytes]) -> None:
        url = f"https://arxiv.org/e-print/{aid}"
        async with ClientSession(timeout=ClientTimeout(total=60), headers={"User-Agent": UA}) as s:
            async with s.get(url, allow_redirects=True, max_redirects=3) as r:
                if r.status == 404:
                    raise web.HTTPNotFound(text="No such arXiv paper.")
                if r.status != 200:
                    raise web.HTTPBadGateway(text=f"arXiv answered {r.status}.")
                n = 0
                async for chunk in r.content.iter_chunked(65536):
                    n += len(chunk)
                    if n > MAX_DOWNLOAD:
                        raise web.HTTPRequestEntityTooLarge(MAX_DOWNLOAD, n, text="The source is too large to import.")
                    dest.write(chunk)
        dest.seek(0)

    @staticmethod
    async def _stream(request: web.Request, src: IO[bytes], size: int) -> web.StreamResponse:
        """A result too big to keep, sent from its file a piece at a time."""
        resp = web.StreamResponse(headers={"Content-Type": "application/zip", "Cache-Control": "no-store",
                                           "Content-Length": str(size)})
        await resp.prepare(request)
        while piece := src.read(1 << 16):
            await resp.write(piece)
        await resp.write_eof()
        return resp

    @staticmethod
    def _zip(z: bytes) -> web.Response:
        return web.Response(body=z, content_type="application/zip", headers={"Cache-Control": "no-store"})
