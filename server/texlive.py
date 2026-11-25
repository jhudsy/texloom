"""TeX Live files on demand for the in-browser TeX engine (/texlive/<year>/<format>/<name>).

The engine (texlyre-busytex) asks for any file it can't find locally as
GET <endpoint>/<kpathsea format number>/<file name>; a 200 response is the file, a 404 a
miss it remembers. This module answers those requests for the whole of TeX Live without
installing it: it keeps TeX Live's package index (texlive.tlpdb) in SQLite, and the first
time a file is asked for it downloads that one package from a mirror, checks it against
the SHA-512 in the index, and unpacks only that package's run files. Disk grows with what
people use, within a cap.

The year must be the engine's (its formats are built from that release's sources), so the
index comes from that year's repository: the live one while it is current, the historic
"tlnet-final" archive afterwards. Mirrors are tried in turn (a pinned one, then the CTAN
redirector, then any configured); a checksum mismatch means a mirror holds a different
version of the package, and if every mirror differs the index is refreshed.

    texloom-server --texlive DIR --texlive-init     # once (and to refresh): build the index
    texloom-server --texlive DIR …                  # serve /texlive/<year>/…
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import lzma
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from pathlib import Path

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout, web

from .wire import client_address

log = logging.getLogger("texloom.texlive")

YEAR = 2026  # the in-browser engine's TeX Live release (texlyre-busytex 1.4.x)
CTAN = "https://mirror.ctan.org/systems/texlive/tlnet/"
HISTORIC = [  # where a past year's final repository lives
    "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/{year}/tlnet-final/",
    "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/{year}/tlnet-final/",
]
NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,255}$")
MAX_PACKAGE = 300 * 1024 * 1024  # bigger containers (a few font collections) aren't fetched
WAIT = 25  # seconds a request waits for an install (the engine gives up after 30)
FAILED_WAIT = 600  # seconds a package that couldn't be fetched isn't tried again (each try asks every mirror)
TOUCH_EVERY = 3600  # seconds between recordings of a package's last use (for eviction)
# Anyone can ask for any file, and each first ask fetches its package, perhaps pushing others out
# of the cache: per address (an IPv6 /64), at most this many fetches and bytes of packages an
# hour; and for everyone, the cache's size an hour (it can't be turned over faster than that).
INSTALL_WINDOW = 3600
INSTALLS_PER_ADDRESS = 400
INSTALL_BYTES_PER_ADDRESS = 1024**3

# kpathsea format number -> (default suffixes, folders under texmf-dist in search order).
# Numbers follow kpse_file_format_type in kpathsea/types.h.
FORMATS: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {
    3: ((".tfm",), ("fonts/tfm/",)),
    4: ((".afm",), ("fonts/afm/",)),
    6: ((".bib",), ("bibtex/bib/",)),
    7: ((".bst",), ("bibtex/bst/",)),
    11: ((".map",), ("fonts/map/",)),
    20: ((".ofm", ".tfm"), ("fonts/ofm/", "fonts/tfm/")),
    23: ((".ovf", ".vf"), ("fonts/ovf/", "fonts/vf/")),
    25: ((".eps", ".epsi", ".pdf", ".png", ".jpg"), ("tex/",)),
    26: ((".tex",), ("tex/latex/", "tex/generic/", "tex/xelatex/", "tex/lualatex/", "tex/xetex/",
                    "tex/luatex/", "tex/plain/", "tex/")),
    30: ((".pro",), ("dvips/",)),
    32: ((".pfb", ".pfa"), ("fonts/type1/",)),
    33: ((".vf",), ("fonts/vf/",)),
    34: ((), ("dvips/", "fonts/map/dvips/")),
    35: ((".ist",), ("makeindex/",)),
    36: ((".ttf", ".ttc", ".TTF", ".TTC"), ("fonts/truetype/",)),
    37: ((".t42", ".T42"), ("fonts/type42/",)),
    39: ((), ("dvipdfmx/", "tex/", "fonts/")),
    40: ((), ("fonts/", "tex/")),
    41: ((), ("fonts/misc/",)),
    44: ((".enc",), ("fonts/enc/",)),
    45: ((), ("fonts/cmap/",)),
    46: ((".sfd",), ("fonts/sfd/",)),
    47: ((".otf", ".OTF"), ("fonts/opentype/",)),
    48: ((), ("tex/generic/config/", "tex/")),
    51: ((".lua", ".luc", ".texlua"), ("tex/luatex/", "tex/lualatex/", "tex/generic/", "tex/latex/",
                                     "scripts/", "tex/")),
    52: ((".fea",), ("fonts/fea/",)),
    53: ((".cid",), ("fonts/cid/",)),
    54: ((".mlbib", ".bib"), ("bibtex/bib/",)),
    55: ((".mlbst", ".bst"), ("bibtex/bst/",)),
    57: ((".ris",), ("bibtex/ris/",)),
    58: ((".bltxml",), ("bibtex/bltxml/",)),
}


def safe_path(p: str) -> bool:
    """Whether a path from the package index stays under texmf/: relative, with no "..", "." or
    empty parts and no backslashes (the index comes from a mirror, which mustn't name other files)."""
    parts = p.split("/")
    return bool(p) and "\\" not in p and "\0" not in p and all(x not in ("", ".", "..") for x in parts)


def parse_tlpdb(text: str):
    """Yield (package, sha512, size, [run file paths relative to texmf-dist]) per package."""
    for rec in text.split("\n\n"):
        lines = rec.split("\n")
        if not lines or not lines[0].startswith("name "):
            continue
        name = lines[0][5:].strip()
        if "." in name:  # platform binaries (name.x86_64-linux) and the like
            continue
        sha, size, run, in_run = None, 0, [], False
        for line in lines[1:]:
            if line.startswith(" "):
                if in_run:
                    p = line.strip().split(" ", 1)[0]
                    p = p[6:] if p.startswith("RELOC/") else p[11:] if p.startswith("texmf-dist/") else None
                    if p and safe_path(p):
                        run.append(p)
                continue
            in_run = line.startswith("runfiles")
            if line.startswith("containerchecksum "):
                sha = line.split()[1]
            elif line.startswith("containersize "):
                size = int(line.split()[1])
        if sha and run:
            yield name, sha, size, run


class TexLive:
    def __init__(self, root: Path, max_bytes: float = 4 * 1024**3, min_free: float = 2 * 1024**3,
                 extra_mirrors: list[str] | None = None) -> None:
        self.root = root
        self.files = root / "texmf"
        self.max_bytes, self.min_free = max_bytes, min_free
        self.extra_mirrors = [m.rstrip("/") + "/" for m in (extra_mirrors or [])]
        self.db: sqlite3.Connection | None = None
        self.meta: dict = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.slots = asyncio.Semaphore(3)  # packages downloading at once
        self.refreshed = 0.0
        self.used = 0  # bytes of installed packages' files on disk (what max_bytes caps)
        self.last_used: dict[str, float] = {}  # package -> when a file of it was last served
        self.failed: dict[str, float] = {}  # package -> when fetching it last failed
        self.fetched: dict[str, deque[tuple[float, int]]] = defaultdict(deque)  # address -> (when, bytes) of fetches asked for
        self.turnover: deque[tuple[float, int]] = deque()  # (when, bytes) of every fetch asked for
        self._forget_other_release()
        self._open()

    # --- the index ------------------------------------------------------------------
    def _forget_other_release(self) -> None:
        """After moving to a new engine: another release's files must not be served to it."""
        try:
            year = json.loads((self.root / "repo.json").read_text()).get("year")
        except (OSError, ValueError):
            return
        if year != YEAR:
            log.info("dropping the cached TeX Live %s files: the engine is now %d", year, YEAR)
            shutil.rmtree(self.files, ignore_errors=True)
            (self.root / "index.db").unlink(missing_ok=True)
            (self.root / "repo.json").unlink(missing_ok=True)

    def _open(self) -> None:
        db = self.root / "index.db"
        if not db.exists():
            return
        self.db = sqlite3.connect(db, check_same_thread=False)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pkgs)")}
        if not {"bytes", "used"} <= cols:  # an index of an older layout (before the cache was counted on disk): started again, as for a new engine
            log.info("dropping the TeX Live index and the files cached with it: it is of an older layout")
            self.db.close()
            self.db = None
            shutil.rmtree(self.files, ignore_errors=True)
            db.unlink(missing_ok=True)
            (self.root / "repo.json").unlink(missing_ok=True)
            return
        self.meta = json.loads((self.root / "repo.json").read_text())
        with self.db:
            for (pkg,) in self.db.execute("SELECT name FROM pkgs WHERE installed=1 AND bytes=0").fetchall():
                self.db.execute("UPDATE pkgs SET bytes=? WHERE name=?", (self._on_disk(pkg), pkg))
        self.used = self.db.execute("SELECT COALESCE(SUM(bytes), 0) FROM pkgs WHERE installed=1").fetchone()[0]
        self.last_used = dict(self.db.execute("SELECT name, used FROM pkgs WHERE installed=1"))

    def _on_disk(self, pkg: str) -> int:
        """Bytes a package's files take on disk now."""
        n = 0
        for (path,) in self.db.execute("SELECT path FROM files WHERE pkg=?", (pkg,)):
            try:
                n += (self.files / path).stat().st_size
            except OSError:
                pass
        return n

    @property
    def ready(self) -> bool:
        return self.db is not None

    def mirrors(self) -> list[str]:
        """Mirrors to fetch from, in order: the ones configured (--texlive-mirror), the pinned
        one (chosen when the index was built: the CTAN redirector picks one near the server),
        then the CTAN redirector three times (each request lands on a different mirror, and
        mirrors update at different times), or the other historic archives."""
        pinned = self.meta.get("mirror")
        rest = [h.format(year=YEAR) for h in HISTORIC] if self.meta.get("historic") else [CTAN] * 3
        out: list[str] = []
        for m in [*self.extra_mirrors, *([pinned] if pinned else []), *rest]:
            if m == CTAN or m not in out:
                out.append(m)
        return out

    def index_sources(self) -> list[str]:
        """Where to (re)build the index from: configured mirrors first, then the redirector
        (so the pin follows the server if it moves), the pinned mirror (it may be mid-update
        when a refresh is needed), and finally the archive: once the live repository moves to
        the next release, that is the only place this one still exists."""
        pinned = self.meta.get("mirror")
        archive = [h.format(year=YEAR) for h in HISTORIC]
        rest = archive if self.meta.get("historic") else [CTAN]
        return list(dict.fromkeys([*self.extra_mirrors, *rest, *([pinned] if pinned else []), *archive]))

    @staticmethod
    def _body(r: ClientResponse, limit: int) -> AsyncIterator[bytes]:
        """A response's body, a piece at a time; ClientError if it isn't a 200 of at most `limit` bytes
        (checked before the first piece, as far as the response says)."""
        if r.status != 200:
            raise ClientError(f"HTTP {r.status}")
        if (r.content_length or 0) > limit:
            raise ClientError("too large")

        async def pieces() -> AsyncIterator[bytes]:
            n = 0
            async for chunk in r.content.iter_chunked(1 << 16):  # read() can return early
                n += len(chunk)
                if n > limit:
                    raise ClientError("too large")
                yield chunk
        return pieces()

    @classmethod
    async def _download(cls, session: ClientSession, url: str, limit: int) -> tuple[bytes, str]:
        """(body, final URL after redirects)."""
        async with session.get(url, allow_redirects=True) as r:
            buf = bytearray()
            async for chunk in cls._body(r, limit):
                buf += chunk
            return bytes(buf), str(r.url)

    async def build_index(self, preferred: list[str] | None = None) -> None:
        """Download this year's texlive.tlpdb (checked against its .sha512) and index its run files."""
        self.root.mkdir(parents=True, exist_ok=True)
        candidates = preferred or [CTAN, *[h.format(year=YEAR) for h in HISTORIC], *self.extra_mirrors]
        errors = []
        async with ClientSession(timeout=ClientTimeout(total=300)) as s:
            for base in candidates:
                try:
                    raw, final = await self._download(s, base + "tlpkg/texlive.tlpdb.xz", 50 * 1024**2)
                    mirror = final[: -len("tlpkg/texlive.tlpdb.xz")]  # the redirector's choice, pinned
                    want = (await self._download(s, mirror + "tlpkg/texlive.tlpdb.sha512", 4096))[0].split()[0].decode()
                    text = lzma.decompress(raw).decode("utf-8", "replace")
                    if hashlib.sha512(text.encode()).hexdigest() != want and hashlib.sha512(raw).hexdigest() != want:
                        raise ValueError("index checksum mismatch")
                    m = re.search(r"^depend release/(\d+)$", text, re.M)
                    if not m or int(m.group(1)) != YEAR:
                        raise ValueError(f"repository is for TeX Live {m.group(1) if m else '?'}, not {YEAR}")
                    break
                except (ClientError, asyncio.TimeoutError, ValueError, lzma.LZMAError) as e:
                    errors.append(f"{base}: {e}")
            else:
                raise RuntimeError("no usable TeX Live repository: " + "; ".join(errors))
        await asyncio.get_running_loop().run_in_executor(None, self._write_index, text, mirror)

    def _write_index(self, text: str, mirror: str) -> None:
        new = self.root / "index.db.new"
        new.unlink(missing_ok=True)
        db = sqlite3.connect(new)
        db.executescript("""
            CREATE TABLE pkgs (name TEXT PRIMARY KEY, sha TEXT NOT NULL, size INTEGER NOT NULL,
                               installed INTEGER NOT NULL DEFAULT 0,
                               bytes INTEGER NOT NULL DEFAULT 0,  -- its files on disk, once installed
                               used REAL NOT NULL DEFAULT 0);     -- when last served (for eviction)
            CREATE TABLE files (base TEXT NOT NULL, path TEXT NOT NULL, pkg TEXT NOT NULL);
        """)
        old: dict[str, tuple[int, float]] = {}
        if self.db is not None:  # keep what is installed already
            old = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT name, bytes, used FROM pkgs WHERE installed=1")}
        n = 0
        for name, sha, size, run in parse_tlpdb(text):
            had = old.get(name)
            db.execute("INSERT INTO pkgs VALUES (?,?,?,?,?,?)", (name, sha, size, int(had is not None), *(had or (0, 0.0))))
            db.executemany("INSERT INTO files VALUES (?,?,?)", [(p.rsplit("/", 1)[-1], p, name) for p in run])
            n += len(run)
        db.execute("CREATE INDEX files_base ON files(base)")
        db.commit()
        db.close()
        new.replace(self.root / "index.db")
        historic = "/historic/" in mirror
        (self.root / "repo.json").write_text(json.dumps(
            {"year": YEAR, "mirror": mirror, "historic": historic, "built": time.time(), "files": n}))
        self.refreshed = time.time()
        self._open()
        log.info("TeX Live %d index: %d run files, from %s", YEAR, n, mirror)

    # --- finding a file ------------------------------------------------------------
    def find(self, fmt: int, name: str) -> tuple[str, str] | None:
        """(path under texmf-dist, package) for what kpathsea would find, or None."""
        spec = FORMATS.get(fmt)
        if spec is None or self.db is None:
            return None
        suffixes, folders = spec
        names = [name] if not suffixes or name.endswith(suffixes) else [name + s for s in suffixes] + [name]
        q = ",".join("?" * len(names))
        rows = self.db.execute(f"SELECT base, path, pkg FROM files WHERE base IN ({q})", names).fetchall()
        best = None
        for base, path, pkg in rows:
            rank = next((i for i, f in enumerate(folders) if path.startswith(f)), None)
            if rank is None:
                continue
            key = (names.index(base), rank, len(path), path)
            if best is None or key < best[0]:
                best = (key, path, pkg)
        return (best[1], best[2]) if best else None

    # --- installing a package ----------------------------------------------------------
    async def install(self, pkg: str) -> None:
        lock = self.locks.setdefault(pkg, asyncio.Lock())
        async with lock:
            row = self.db.execute("SELECT sha, size, installed FROM pkgs WHERE name=?", (pkg,)).fetchone()
            if row is None or row[2]:
                return
            sha, size, _ = row
            if size > MAX_PACKAGE:
                raise RuntimeError(f"{pkg} is too large to fetch on demand ({size >> 20} MB)")
            if time.time() - self.failed.get(pkg, 0) < FAILED_WAIT:
                raise RuntimeError(f"couldn't fetch {pkg} a few minutes ago: try again later")
            if shutil.disk_usage(self.root).free - 4 * size < self.min_free:
                raise RuntimeError("the server is short of disk")
            files = {p for (p,) in self.db.execute("SELECT path FROM files WHERE pkg=?", (pkg,))}
            loop = asyncio.get_running_loop()
            with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tar.xz") as tmp:
                try:
                    async with self.slots:
                        await self._fetch_container(pkg, sha, size, Path(tmp.name))
                except RuntimeError:
                    if len(self.failed) > 1000:
                        self.failed = {k: v for k, v in self.failed.items() if time.time() - v < FAILED_WAIT}
                    self.failed[pkg] = time.time()
                    raise
                need = await loop.run_in_executor(None, self._unpacked_size, Path(tmp.name), files)
                self._make_room(need, pkg)
                await loop.run_in_executor(None, self._unpack, Path(tmp.name), files)
            now = time.time()
            with self.db:
                self.db.execute("UPDATE pkgs SET installed=1, bytes=?, used=? WHERE name=?", (need, now, pkg))
            self.used += need
            self.last_used[pkg] = now
            log.info("installed %s (%s KB)", pkg, need >> 10)

    def _make_room(self, need: int, pkg: str) -> None:
        """Remove the least recently served packages until `need` more bytes fit under max_bytes."""
        if need > self.max_bytes:
            raise RuntimeError(f"{pkg} is larger than the whole TeX Live cache")
        while self.used + need > self.max_bytes:
            row = self.db.execute("SELECT name, bytes FROM pkgs WHERE installed=1 AND name<>? "
                                  "ORDER BY used LIMIT 1", (pkg,)).fetchone()
            if row is None:
                raise RuntimeError("the TeX Live cache is full")
            old, n = row
            for (path,) in self.db.execute("SELECT path FROM files WHERE pkg=?", (old,)).fetchall():
                if safe_path(path):
                    (self.files / path).unlink(missing_ok=True)  # (one being served stays readable until closed)
            with self.db:
                self.db.execute("UPDATE pkgs SET installed=0, bytes=0 WHERE name=?", (old,))
            self.used -= n
            self.last_used.pop(old, None)
            log.info("removed %s from the TeX Live cache (%s KB, least recently used)", old, n >> 10)

    def _touch(self, pkg: str) -> None:
        """Note that a package's file was served (written to the index at most hourly)."""
        now = time.time()
        if now - self.last_used.get(pkg, 0) < TOUCH_EVERY:
            return
        self.last_used[pkg] = now
        with self.db:
            self.db.execute("UPDATE pkgs SET used=? WHERE name=?", (now, pkg))

    @classmethod
    async def _download_to(cls, session: ClientSession, url: str, limit: int, dest: Path) -> str:
        """Write the body to `dest` (it never sits whole in memory); its SHA-512."""
        h = hashlib.sha512()
        async with session.get(url, allow_redirects=True) as r:
            body = cls._body(r, limit)
            with open(dest, "wb") as out:  # noqa: ASYNC230 - small writes to local disk, between network reads
                async for chunk in body:
                    h.update(chunk)
                    out.write(chunk)
        return h.hexdigest()

    async def _fetch_container(self, pkg: str, sha: str, size: int, dest: Path) -> None:
        """Download the package's container to `dest`, checked against the index."""
        mismatched = 0
        tried = []
        async with ClientSession(timeout=ClientTimeout(total=120)) as s:
            for base in self.mirrors():
                try:
                    got = await self._download_to(s, f"{base}archive/{pkg}.tar.xz", max(size * 2, 1 << 20), dest)
                except (ClientError, asyncio.TimeoutError, OSError) as e:
                    tried.append(f"{base}: {e}")
                    continue  # this mirror is down or slow: the next one
                if got == sha:
                    return
                mismatched += 1
                tried.append(f"{base}: different version")
        # every mirror that answered has another version: the package changed since the index
        if mismatched and time.time() - self.refreshed > 600:
            log.info("%s changed upstream: refreshing the index", pkg)
            was = self.meta.get("mirror")
            await self.build_index(self.index_sources())
            row = self.db.execute("SELECT sha, size FROM pkgs WHERE name=?", (pkg,)).fetchone()
            if row and (row[0] != sha or self.meta.get("mirror") != was):  # a new version, or a new place to look
                return await self._fetch_container(pkg, row[0], row[1], dest)
        raise RuntimeError(f"couldn't fetch {pkg}: " + "; ".join(tried))

    @staticmethod
    def _wanted(m: tarfile.TarInfo, allowed: set[str]) -> str | None:
        """The path under texmf/ of a container member to install, or None."""
        name = m.name[11:] if m.name.startswith("texmf-dist/") else m.name
        if not m.isfile() or name not in allowed or not safe_path(name):
            return None
        return name

    def _unpacked_size(self, archive: Path, allowed: set[str]) -> int:
        with tarfile.open(archive, mode="r:xz") as tar:
            return sum(m.size for m in tar.getmembers() if self._wanted(m, allowed))

    def _unpack(self, archive: Path, allowed: set[str]) -> None:
        """Write the package's run files (only those the index lists for it) under texmf/."""
        with tarfile.open(archive, mode="r:xz") as tar:
            for m in tar.getmembers():
                name = self._wanted(m, allowed)
                if name is None:
                    continue
                dest = self.files / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                with tar.extractfile(m) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                tmp.replace(dest)

    def _may_fetch(self, address: str | None, pkg: str) -> bool:
        """Whether a request from `address` may start fetching `pkg` (see INSTALL_WINDOW); counted if so."""
        row = self.db.execute("SELECT size FROM pkgs WHERE name=?", (pkg,)).fetchone()
        size = row[0] if row else 0
        now = time.monotonic()
        for q in (self.turnover, *([self.fetched[address]] if address else [])):
            while q and now - q[0][0] > INSTALL_WINDOW:
                q.popleft()
        if len(self.fetched) > 10000:  # forget addresses with nothing recent (bounded memory)
            for k in [k for k, q in self.fetched.items() if not q]:
                del self.fetched[k]
        if sum(n for _, n in self.turnover) + size > max(self.max_bytes, MAX_PACKAGE):
            return False
        if address:
            mine = self.fetched[address]
            if len(mine) >= INSTALLS_PER_ADDRESS or sum(n for _, n in mine) + size > INSTALL_BYTES_PER_ADDRESS:
                return False
            mine.append((now, size))
        self.turnover.append((now, size))
        return True

    # --- HTTP ------------------------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        year, fmt, name = request.match_info["year"], request.match_info["fmt"], request.match_info["name"]
        if year != str(YEAR) or not fmt.isdigit() or len(fmt) > 3 or not NAME_RE.match(name) or name.startswith("."):
            return self._miss()
        hit = self.find(int(fmt), name)
        if hit is None:
            return self._miss()
        path, pkg = hit
        if not safe_path(path):
            return self._miss()
        f = self.files / path
        if not f.is_file():
            job = self.jobs.get(pkg)
            if job is None or job.done():
                if not self._may_fetch(client_address(request), pkg):
                    return web.Response(status=503, text="too many TeX Live packages fetched: try again later",
                                        headers={"Retry-After": "600", "Cache-Control": "no-store"})
                job = self.jobs[pkg] = asyncio.create_task(self.install(pkg))
            try:
                await asyncio.wait_for(asyncio.shield(job), WAIT)
            except asyncio.TimeoutError:  # still downloading: the next compile will have it
                return web.Response(status=503, text="installing", headers={"Retry-After": "10", "Cache-Control": "no-store"})
            except RuntimeError as e:
                log.warning("%s", e)
                return web.Response(status=503, text=str(e)[:200], headers={"Cache-Control": "no-store"})
            if not f.is_file():
                return self._miss()
        self._touch(pkg)
        return web.FileResponse(f, headers={
            "Cache-Control": "public, max-age=31536000, immutable",  # the URL names the release
            "Content-Type": "application/octet-stream",
        })

    @staticmethod
    def _miss() -> web.Response:
        return web.Response(status=404, headers={"Cache-Control": "public, max-age=86400"})

    def info(self) -> dict | None:
        return {"endpoint": f"/texlive/{YEAR}", "year": YEAR} if self.ready else None
