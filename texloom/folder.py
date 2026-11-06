"""Bidirectional sync between a Replica and a plain directory on disk.

Invariant: between syncs, the CRDT text of each file equals the file on disk. Before
applying remote updates we ingest any local disk edits (so they're diffed against the
right base), then apply remote updates, then write merged results back to disk.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path

from pycrdt import Text
from watchfiles import awatch

from .replica import BLOB_RE, Replica

log = logging.getLogger("texloom")

STATE = ".texloom"
IGNORE_DIRS = {STATE, ".git", ".svn", "node_modules", "__pycache__", ".idea", ".vscode"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk", ".synctex.gz",
    ".synctex", ".bbl", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".vrb", ".idx", ".ilg",
    ".ind", ".xdv", ".dvi", ".swp", ".swx", ".tmp", "~",
)
TEXT_SUFFIXES = {
    ".tex", ".bib", ".sty", ".cls", ".bst", ".bbx", ".cbx", ".lbx", ".dtx", ".ins", ".cfg",
    ".def", ".clo", ".ltx", ".txt", ".md", ".csv", ".dat", ".tsv", ".tikz", ".pgf", ".py",
    ".json", ".yaml", ".yml", ".latexmkrc", ".gitignore",
}
MAX_TEXT = 4 * 1024 * 1024
MAX_FILE = 50 * 1024 * 1024

# Files that TeX tools read as configuration or execute (latexmk runs latexmkrc as Perl).
# They are never written from, or sent to, the shared project.
UNSAFE_NAMES = {"latexmkrc", ".latexmkrc", "texmf.cnf", ".texmf.cnf", "mktexlsr", "ls-r"}
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_rel(rel: object) -> str | None:
    """Validate a project path received from collaborators. None if it must not touch disk.

    Rejects absolute paths, drive letters, backslashes, '.'/'..'/empty segments, control
    characters, reserved names (compared case-insensitively, Unicode-normalised), and
    files that TeX tools would execute or treat as configuration.
    """
    if not isinstance(rel, str) or not rel or len(rel) > 300:
        return None
    if rel.startswith("/") or "\\" in rel or re.match(r"^[A-Za-z]:", rel):
        return None
    for seg in rel.split("/"):
        if seg in ("", ".", "..") or any(ord(c) < 32 or c in '<>:"|?*' for c in seg):
            return None
        n = unicodedata.normalize("NFKC", seg).casefold()
        if n in (STATE, ".git") or n in UNSAFE_NAMES or n.endswith(".latexmkrc"):
            return None
        if os.name == "nt" and n.split(".")[0] in WIN_RESERVED:
            return None
    return rel


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_text(path: str, data: bytes) -> bool:
    if len(data) > MAX_TEXT or b"\x00" in data[:8192]:
        return False
    if Path(path).suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def text_ops(old: str, new: str) -> list[tuple[int, int, str]]:
    """Small, local edits turning old into new: (char_start, char_end, replacement).

    Diffs by lines first, then refines each changed block by characters. Fine-grained
    ops matter: a coarse "replace the whole line" would discard a collaborator's
    concurrent edit to that line when the CRDT merges.
    """
    if old == new:
        return []
    o_lines = old.splitlines(keepends=True)
    n_lines = new.splitlines(keepends=True)
    o_off = [0]
    for line in o_lines:
        o_off.append(o_off[-1] + len(line))
    ops: list[tuple[int, int, str]] = []
    sm = difflib.SequenceMatcher(a=o_lines, b=n_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        start, end = o_off[i1], o_off[i2]
        o_blk, n_blk = old[start:end], "".join(n_lines[j1:j2])
        if tag != "replace" or len(o_blk) * len(n_blk) > 4_000_000:
            ops.append((start, end, n_blk))
            continue
        cm = difflib.SequenceMatcher(a=o_blk, b=n_blk, autojunk=False)
        for t2, a1, a2, b1, b2 in cm.get_opcodes():
            if t2 != "equal":
                ops.append((start + a1, start + a2, n_blk[b1:b2]))
    return ops


def apply_text(ytext: Text, old: str, new: str) -> None:
    """Apply a diff to a Y.Text. pycrdt indexes text by UTF-8 bytes."""
    for start, end, repl in reversed(text_ops(old, new)):  # back-to-front keeps offsets valid
        b_start = len(old[:start].encode())
        b_end = b_start + len(old[start:end].encode())
        if b_end > b_start:
            del ytext[b_start:b_end]
        if repl:
            ytext.insert(b_start, repl)


class FolderSync:
    def __init__(self, root: Path, replica: Replica, compile_: bool = False,
                 compile_opts: dict | None = None):
        self.root = root.resolve()
        self.r = replica
        self.compile = compile_
        # Local-only compile options (never shared: shell-escape runs code on THIS machine).
        self.opts = {"latexmk_args": [], "shell_escape": False, "label": "latexmk",
                     **(compile_opts or {})}
        self._fetching: set[str] = set()
        self._refused: set[str] = set()
        self.known: dict[str, dict] = {}  # path -> {"m": mtime_ns, "z": size, "h": hash}
        self._compile_task: asyncio.Task | None = None
        self._known_file = self.root / STATE / "known.json"
        if self._known_file.exists():
            self.known = json.loads(self._known_file.read_text())
        replica.before_remote = self.ingest_all
        replica.on_remote = self.on_remote
        replica.on_compile_request = self.on_compile_request

    # --- helpers -------------------------------------------------------------
    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    def target(self, rel: object) -> Path | None:
        """Absolute path for a collaborator-supplied project path, or None if unsafe.
        Guarantees the result is inside the project folder, even through symlinks."""
        good = safe_rel(rel)
        if good is None:
            if rel not in self._refused:
                self._refused.add(str(rel))
                log.warning("ignoring unsafe path from the project: %r", rel)
            return None
        full = self.root / good
        try:
            parent = full.parent.resolve()
        except OSError:
            return None
        if parent != self.root and self.root not in parent.parents:
            log.warning("ignoring %r: it would leave the project folder (symlink?)", rel)
            return None
        if full.is_symlink():
            log.warning("ignoring %r: it is a symlink on disk", rel)
            return None
        return full

    def ignored(self, rel: str) -> bool:
        parts = rel.split("/")
        if any(x in IGNORE_DIRS for x in parts[:-1]) or parts[0] in IGNORE_DIRS:
            return True
        name = parts[-1]
        if name in IGNORE_NAMES or name.startswith(".#") or name.endswith(IGNORE_SUFFIXES):
            return True
        main = str(self.r.meta.get("main", "main.tex"))
        return rel == main[:-4] + ".pdf"  # our own compiled output

    def disk_files(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for f in filenames:
                rel = self.rel(Path(dirpath) / f)
                if not self.ignored(rel):
                    out.append(rel)
        return out

    def save_known(self) -> None:
        self._known_file.write_text(json.dumps(self.known))

    def _stat_matches(self, rel: str) -> bool:
        k = self.known.get(rel)
        try:
            st = (self.root / rel).stat()
        except FileNotFoundError:
            return False
        return bool(k) and k["m"] == st.st_mtime_ns and k["z"] == st.st_size

    def _remember(self, rel: str, h: str) -> None:
        st = (self.root / rel).stat()
        self.known[rel] = {"m": st.st_mtime_ns, "z": st.st_size, "h": h}

    # --- disk -> CRDT ----------------------------------------------------------
    async def ingest_all(self) -> None:
        await self.ingest(set(self.disk_files()) | set(self.known))

    async def ingest(self, paths: set[str]) -> None:
        """Push local disk changes for `paths` into the CRDT."""
        if not self.r.keys.writable:
            return
        changed = False
        for rel in sorted(paths):
            if self.ignored(rel):
                continue
            if safe_rel(rel) is None:
                if rel not in self._refused:
                    self._refused.add(rel)
                    log.warning("not sharing %s: collaborators' helpers never accept this file name", rel)
                continue
            full = self.root / rel
            if not full.is_file():
                if rel in self.known:  # deleted locally
                    del self.known[rel]
                    def rm(rel=rel):
                        if rel in self.r.files:
                            del self.r.files[rel]
                        if rel in self.r.bin:
                            del self.r.bin[rel]
                    self.r.local_change(rm)
                    log.info("- %s", rel)
                    changed = True
                continue
            if self._stat_matches(rel):
                continue
            data = full.read_bytes()
            if len(data) > MAX_FILE:
                log.warning("skipping %s (>50MB)", rel)
                continue
            h = sha(data)
            if self.known.get(rel, {}).get("h") == h:
                self._remember(rel, h)  # touched, not changed
                continue
            if is_text(rel, data):
                new = data.decode("utf-8")
                def edit(rel=rel, new=new):
                    if rel in self.r.bin:
                        del self.r.bin[rel]
                    yt = self.r.files.get(rel)
                    if yt is None:
                        self.r.files[rel] = Text(new)
                    else:
                        apply_text(yt, str(yt), new)
                self.r.local_change(edit)
            else:
                bid = await self.r.put_blob(data)
                h = bid
                def setbin(rel=rel, bid=bid, size=len(data)):
                    if rel in self.r.files:
                        del self.r.files[rel]
                    self.r.bin[rel] = {"h": bid, "size": size}
                self.r.local_change(setbin)
            self._remember(rel, h)
            log.info("↑ %s", rel)
            changed = True
        if changed:
            self.save_known()
            self.schedule_compile()

    # --- CRDT -> disk ----------------------------------------------------------
    async def on_remote(self) -> None:
        await self.write_out()

    async def write_out(self) -> None:
        changed = False
        wanted: set[str] = set()

        def attempt(fn, rel):  # one bad entry (name clash, permissions) mustn't stop the rest
            nonlocal changed
            try:
                if fn():
                    changed = True
            except OSError as e:
                log.warning("could not write %s: %s", rel, e)

        for rel, yt in list(self.r.files.items()):
            if self.ignored(rel) or self.target(rel) is None:
                continue
            wanted.add(rel)
            data = str(yt).encode("utf-8")
            h = sha(data)
            if self.known.get(rel, {}).get("h") == h and (self.root / rel).exists():
                continue
            attempt(lambda rel=rel, data=data, h=h: self._write(rel, data, h) or True, rel)
        for rel, info in list(self.r.bin.items()):
            if self.ignored(rel) or self.target(rel) is None:
                continue
            bid = info.get("h") if isinstance(info, dict) else None
            if not isinstance(bid, str) or not BLOB_RE.match(bid):
                log.warning("ignoring %r: malformed file reference", rel)
                continue
            wanted.add(rel)
            if self.known.get(rel, {}).get("h") == bid and (self.root / rel).exists():
                continue
            cached = self.r.dir / "blobs" / bid
            if cached.exists():
                attempt(lambda rel=rel, cached=cached, bid=bid: self._write(rel, cached.read_bytes(), bid) or True, rel)
            elif bid not in self._fetching:
                # Fetch in the background: in relay mode the answer comes from a peer
                # over the same connection that is delivering this update.
                self._fetching.add(bid)
                asyncio.get_running_loop().create_task(self._fetch(rel, bid))
        for d in list(self.r.dirs.keys()):  # explicit (possibly empty) folders
            full = self.target(d)
            if full is not None and not full.exists():
                attempt(lambda full=full: full.mkdir(parents=True, exist_ok=True) or True, d)
        for rel in list(self.known):
            if rel not in wanted:  # deleted remotely
                full = self.target(rel)
                if full is not None:
                    attempt(lambda full=full: full.unlink(missing_ok=True) or self._prune(full.parent) or True, rel)
                del self.known[rel]
                log.info("✗ %s", rel)
                changed = True
        if changed:
            self.save_known()
            self.schedule_compile()

    def _prune(self, d: Path) -> None:
        """Remove now-empty folders left behind by deletions (never the project root)."""
        while d != self.root and self.root in d.parents:
            rel = d.relative_to(self.root).as_posix()
            if rel in self.r.dirs or any(d.iterdir()):
                return
            d.rmdir()
            d = d.parent

    async def _fetch(self, rel: str, bid: str) -> None:
        try:
            await self.r.get_blob(bid)
        except Exception as e:  # noqa: BLE001
            log.warning("could not fetch %s yet (%s); will retry on next sync", rel, e or "timeout")
            return
        finally:
            self._fetching.discard(bid)
        async with self.r.lock:
            await self.write_out()

    def _write(self, rel: str, data: bytes, h: str) -> None:
        full = self.target(rel)
        if full is None:
            return
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(f".{full.name}.texloom-tmp")
        tmp.write_bytes(data)
        tmp.replace(full)
        self._remember(rel, h)
        log.info("↓ %s", rel)

    # --- compile -------------------------------------------------------------
    def main_file(self) -> str | None:
        m = self.r.meta.get("main")
        if m and m in self.r.files:
            return str(m)
        cands = [p for p in self.r.files.keys() if p.endswith(".tex")]
        for p in sorted(cands, key=lambda p: (p != "main.tex", p.count("/"), p)):
            if "\\documentclass" in str(self.r.files[p]):
                return p
        return None

    def schedule_compile(self, delay: float = 1.5) -> None:
        if not self.compile:
            return
        if self._compile_task and not self._compile_task.done():
            self._compile_task.cancel()
        self._compile_task = asyncio.get_event_loop().create_task(self._compile(delay=delay))

    async def on_compile_request(self, frm: int | None) -> None:
        """A browser asked for a compile (its device setting: 'compile with my own TeX')."""
        if not self.compile or not self.r.keys.writable:
            return
        await self.r.p2p({"k": "cack", "by": self.opts["label"]}, frm)
        self.schedule_compile(delay=0)

    async def _compile(self, delay: float) -> None:
        await asyncio.sleep(delay)
        main = self.main_file()
        if not main:
            return
        engine = str(self.opts.get("engine") or self.r.meta.get("engine", "pdflatex"))
        flag = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}.get(engine, "-pdf")
        extra = list(self.opts["latexmk_args"])
        # Collaborators write the document, so by default TeX may not run programs
        # (-no-shell-escape), may not read or write outside the project (openin/openout
        # "paranoid"), and latexmk ignores rc files (-norc; latexmkrc is Perl). An rc you
        # trust can be named in .texloom/config.json as "latexmk_rc".
        extra.append("-shell-escape" if self.opts["shell_escape"] else "-no-shell-escape")
        if self.opts.get("latexmk_rc"):
            extra += ["-r", str(Path(self.opts["latexmk_rc"]).expanduser())]
        if self.r.meta.get("bib") == "none":
            extra.append("-bibtex-")
        build = self.root / STATE / "build"
        build.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        main_path = self.target(main)
        if main_path is None or not main_path.is_file():
            return
        env = {**os.environ, "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": str(build)}
        proc = await asyncio.create_subprocess_exec(
            "latexmk", "-norc", flag, "-interaction=nonstopmode", "-synctex=1", "-file-line-error",
            f"-outdir={build}", *extra, f"./{main_path.name}", cwd=main_path.parent, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            raise
        stem = Path(main).stem
        pdf = build / f"{stem}.pdf"
        logf = build / f"{stem}.log"
        ok = proc.returncode == 0 and pdf.exists()
        log_text = logf.read_bytes() if logf.exists() else out
        log.info("compiled %s in %.1fs: %s", main, time.time() - t0, "ok" if ok else "errors")
        out_pdf = self.target(main[:-4] + ".pdf")
        if pdf.exists() and out_pdf is not None:
            shutil.copyfile(pdf, out_pdf)
        if not self.r.keys.writable or not self.r.connected.is_set():
            return
        try:
            pdf_id = await self.r.put_blob(pdf.read_bytes()) if pdf.exists() else None
            log_id = await self.r.put_blob(log_text)
        except Exception as e:  # noqa: BLE001
            log.warning("could not upload output: %s", e)
            return
        def publish():
            self.r.out["latest"] = {
                "pdf": pdf_id, "log": log_id, "ok": ok, "by": self.opts["label"],
                "at": time.time(), "main": main,
            }
        self.r.local_change(publish)

    # --- main loop -------------------------------------------------------------
    async def watch(self) -> None:
        def keep(_change, path: str) -> bool:
            return f"/{STATE}/" not in path and not path.endswith(f"/{STATE}") and "/.git/" not in path

        if not self.r.keys.writable:
            log.warning("read-only link: local edits in this folder will NOT be shared")
        async for changes in awatch(self.root, debounce=300, step=100, recursive=True,
                                    watch_filter=keep):
            paths = set()
            for _, p in changes:
                try:
                    rel = self.rel(Path(p))
                except ValueError:
                    continue
                if not self.ignored(rel) and ".texloom-tmp" not in rel:
                    paths.add(rel)
            if paths:
                async with self.r.lock:
                    await self.ingest(paths)
