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
import io
import json
import logging
import lzma
import re
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, web

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
                    if p.startswith("RELOC/"):
                        run.append(p[6:])
                    elif p.startswith("texmf-dist/"):
                        run.append(p[11:])
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
        self.used = 0
        self._open()

    # --- the index ------------------------------------------------------------------
    def _open(self) -> None:
        db = self.root / "index.db"
        if not db.exists():
            return
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.meta = json.loads((self.root / "repo.json").read_text())
        self.used = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM pkgs WHERE installed=1").fetchone()[0]

    @property
    def ready(self) -> bool:
        return self.db is not None

    def mirrors(self) -> list[str]:
        """The pinned mirror first, then others: the CTAN redirector three times (it sends each
        request to a different mirror, and mirrors update at different times), or the other
        historic archives; then any configured."""
        pinned = self.meta.get("mirror")
        if self.meta.get("historic"):
            rest = [h.format(year=YEAR) for h in HISTORIC]
        else:
            rest = [CTAN] * 3
        out = [pinned] if pinned else []
        for m in [*rest, *self.extra_mirrors]:
            if m != pinned and (m == CTAN or m not in out):
                out.append(m)
        return out

    def index_sources(self) -> list[str]:
        """Where to refresh the index from: not the pinned mirror first (it may be mid-update)."""
        others = [m for m in self.mirrors() if m != self.meta.get("mirror")]
        return list(dict.fromkeys(others)) + [m for m in [self.meta.get("mirror")] if m]

    @staticmethod
    async def _download(session: ClientSession, url: str, limit: int) -> tuple[bytes, str]:
        """(body, final URL after redirects)."""
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                raise ClientError(f"HTTP {r.status}")
            if (r.content_length or 0) > limit:
                raise ClientError("too large")
            buf = bytearray()
            async for chunk in r.content.iter_chunked(1 << 16):  # read() can return early
                buf += chunk
                if len(buf) > limit:
                    raise ClientError("too large")
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
                               installed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE files (base TEXT NOT NULL, path TEXT NOT NULL, pkg TEXT NOT NULL);
        """)
        old = {}
        if self.db is not None:  # keep what is installed already
            old = dict(self.db.execute("SELECT name, 1 FROM pkgs WHERE installed=1"))
        n = 0
        for name, sha, size, run in parse_tlpdb(text):
            db.execute("INSERT INTO pkgs VALUES (?,?,?,?)", (name, sha, size, int(name in old)))
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
            if self.used + size > self.max_bytes or shutil.disk_usage(self.root).free - 3 * size < self.min_free:
                raise RuntimeError("the TeX Live cache is full")
            async with self.slots:
                data = await self._fetch_container(pkg, sha, size)
            files = {p for (p,) in self.db.execute("SELECT path FROM files WHERE pkg=?", (pkg,))}
            await asyncio.get_running_loop().run_in_executor(None, self._unpack, data, files)
            with self.db:
                self.db.execute("UPDATE pkgs SET installed=1 WHERE name=?", (pkg,))
            self.used += size
            log.info("installed %s (%s KB)", pkg, size >> 10)

    async def _fetch_container(self, pkg: str, sha: str, size: int) -> bytes:
        mismatched = 0
        tried = []
        async with ClientSession(timeout=ClientTimeout(total=120)) as s:
            for base in self.mirrors():
                try:
                    data, _ = await self._download(s, f"{base}archive/{pkg}.tar.xz", max(size * 2, 1 << 20))
                except (ClientError, asyncio.TimeoutError) as e:
                    tried.append(f"{base}: {e}")
                    continue  # this mirror is down or slow: the next one
                if hashlib.sha512(data).hexdigest() == sha:
                    return data
                mismatched += 1
                tried.append(f"{base}: different version")
        # every mirror that answered has another version: the package changed since the index
        if mismatched and time.time() - self.refreshed > 600:
            log.info("%s changed upstream: refreshing the index", pkg)
            await self.build_index(self.index_sources())
            row = self.db.execute("SELECT sha, size FROM pkgs WHERE name=?", (pkg,)).fetchone()
            if row and row[0] != sha:
                return await self._fetch_container(pkg, row[0], row[1])
        raise RuntimeError(f"couldn't fetch {pkg}: " + "; ".join(tried))

    def _unpack(self, data: bytes, allowed: set[str]) -> None:
        """Write the package's run files (only those the index lists for it) under texmf/."""
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tar:
            for m in tar.getmembers():
                name = m.name[11:] if m.name.startswith("texmf-dist/") else m.name
                if not m.isfile() or name not in allowed or name.startswith("/") or ".." in name.split("/"):
                    continue
                dest = self.files / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                with tar.extractfile(m) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                tmp.replace(dest)

    # --- HTTP ------------------------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        year, fmt, name = request.match_info["year"], request.match_info["fmt"], request.match_info["name"]
        if year != str(YEAR) or not fmt.isdigit() or not NAME_RE.match(name) or name.startswith("."):
            return self._miss()
        hit = self.find(int(fmt), name)
        if hit is None:
            return self._miss()
        path, pkg = hit
        f = self.files / path
        if not f.is_file():
            job = self.jobs.get(pkg)
            if job is None or job.done():
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
        return web.FileResponse(f, headers={
            "Cache-Control": "public, max-age=31536000, immutable",  # the URL names the release
            "Content-Type": "application/octet-stream",
        })

    @staticmethod
    def _miss() -> web.Response:
        return web.Response(status=404, headers={"Cache-Control": "public, max-age=86400"})

    def info(self) -> dict | None:
        return {"endpoint": f"/texlive/{YEAR}", "year": YEAR} if self.ready else None
