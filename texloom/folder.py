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
import shutil
import time
from pathlib import Path

from pycrdt import Text
from watchfiles import awatch

from .replica import BLOB_RE, Replica

log = logging.getLogger("texloom")

from .scan import SCANNED, added, allowed, scan
from .paths import PROTECTED_DIRS, STATE, UNSAFE_NAMES, WIN_RESERVED, latexmk_command, main_file, safe_rel, tex_env  # noqa: F401
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
        # folders mirrored between disk and the project's `dirs` (so empty folders and
        # folder deletions sync too, not only files)
        self._known_dirs_file = self.root / STATE / "known_dirs.json"
        self.known_dirs: set[str] = set(json.loads(self._known_dirs_file.read_text())) if self._known_dirs_file.exists() else set()
        replica.before_remote = self.ingest_all
        replica.on_remote = self.on_remote
        replica.on_compile_request = self.on_compile_request
        self._warned_shell: frozenset[str] = frozenset()
        self._last_request = 0.0
        self._clean_build = False  # the next compile starts from scratch (requested from a browser)

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
        self._known_dirs_file.write_text(json.dumps(sorted(self.known_dirs)))

    def disk_dirs(self) -> set[str]:
        out = set()
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
            for d in dirnames:
                rel = (Path(dirpath) / d).relative_to(self.root).as_posix()
                if safe_rel(rel) and not self.ignored(rel + "/x"):
                    out.add(rel)
        return out

    def ingest_dirs(self) -> bool:
        """Folders made or deleted on disk -> the project's folder list."""
        if not self.r.keys.writable:
            return False
        disk = self.disk_dirs()
        added = {d for d in disk if d not in self.known_dirs and d not in self.r.dirs}
        removed = {d for d in self.known_dirs if d not in disk}
        if not added and not removed:
            self.known_dirs |= disk & set(self.r.dirs.keys())
            return False

        def apply():
            for d in added:
                self.r.dirs[d] = True
            for d in removed:
                for k in [k for k in self.r.dirs.keys() if k == d or k.startswith(d + "/")]:
                    del self.r.dirs[k]
        self.r.local_change(apply)
        self.known_dirs = (self.known_dirs | added) - removed
        for d in sorted(added):
            log.info("↑ %s/", d)
        for d in sorted(removed):
            log.info("- %s/", d)
        self.save_known()
        return True

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
        self.ingest_dirs()

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
        dirs = set(self.r.dirs.keys())
        for d in dirs:  # explicit (possibly empty) folders
            full = self.target(d)
            if full is not None and not full.exists():
                attempt(lambda full=full: full.mkdir(parents=True, exist_ok=True) or True, d)
            self.known_dirs.add(d)
        for d in sorted(self.known_dirs - dirs, key=len, reverse=True):  # folders deleted remotely
            full = self.target(d)
            if full is not None and full.is_dir() and not any(full.iterdir()):
                attempt(lambda full=full: full.rmdir() or True, d)
            self.known_dirs.discard(d)
            changed = True
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
        new_lines = self._program_lines(rel, full, data)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(f".{full.name}.texloom-tmp")
        tmp.write_bytes(data)
        tmp.replace(full)
        self._remember(rel, h)
        log.info("↓ %s", rel)
        for f in new_lines:
            log.warning("⚠ %s — if you compile this folder with your own LaTeX tools, check it first.", f.describe())

    @staticmethod
    def _program_lines(rel: str, full: Path, data: bytes) -> list:
        """Lines asking to run programs that this write brings into the folder."""
        if not rel.lower().endswith(SCANNED):
            return []
        try:
            old = full.read_text("utf-8", "replace") if full.is_file() else None
        except OSError:
            old = None
        return added(rel, old, data.decode("utf-8", "replace"))

    # --- compile -------------------------------------------------------------
    def main_file(self) -> str | None:
        return main_file(self.r.files, self.r.meta)

    def compile_from_scratch(self) -> None:
        """Compile now, without the previous run's .aux, .bbl and other working files."""
        self._clean_build = True
        self.schedule_compile(delay=0)

    def schedule_compile(self, delay: float = 1.5) -> None:
        if not self.compile:
            return
        if self._compile_task and not self._compile_task.done():
            self._compile_task.cancel()
        self._compile_task = asyncio.get_event_loop().create_task(self._compile(delay=delay))

    async def on_compile_request(self, frm: int | None, clean: bool = False) -> None:
        """A browser asked for a compile (its device setting: 'compile with my own TeX');
        `clean`: from scratch, without the previous run's .aux, .bbl and other files."""
        if not self.compile or not self.r.keys.writable:
            return
        # Anyone with the link can ask (requests aren't signed): at most one compile every few seconds.
        now = time.monotonic()
        if now - self._last_request < 5:
            return
        self._last_request = now
        await self.r.p2p({"k": "cack", "by": self.opts["label"]}, frm)
        if clean:
            self.compile_from_scratch()
        else:
            self.schedule_compile(delay=0)

    async def _compile(self, delay: float) -> None:
        await asyncio.sleep(delay)
        main = self.main_file()
        if not main:
            return
        engine = str(self.opts.get("engine") or self.r.meta.get("engine", "pdflatex"))
        extra = list(self.opts["latexmk_args"])
        # Collaborators write the document, so by default TeX may not run programs
        # (-no-shell-escape), may not read or write outside the project (openin/openout
        # "paranoid"), and latexmk ignores rc files (-norc; latexmkrc is Perl). An rc you
        # trust can be named in .texloom/config.json as "latexmk_rc".
        shell, note = self.shell_mode()
        extra.append(shell)
        if self.opts.get("latexmk_rc"):
            extra += ["-r", str(Path(self.opts["latexmk_rc"]).expanduser())]
        build = self.root / STATE / "build"
        if self._clean_build:  # compile from scratch: drop the last run's .aux, .bbl, …
            self._clean_build = False
            shutil.rmtree(build, ignore_errors=True)
        build.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        main_path = self.target(main)
        if main_path is None or not main_path.is_file():
            return
        proc = await asyncio.create_subprocess_exec(
            *latexmk_command(engine, self.r.meta.get("bib"), main_path.name, "-synctex=1", f"-outdir={build}", *extra),
            cwd=main_path.parent, env=tex_env(TEXMFOUTPUT=str(build)),
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
        if note:  # collaborators see why minted & co. didn't run
            log_text = f"texloom: {note}\n\n".encode() + log_text
        log.info("compiled %s in %.1fs: %s", main, time.time() - t0, "ok" if ok else "errors")
        out_pdf = self.target(main[:-4] + ".pdf")
        if pdf.exists() and out_pdf is not None:
            shutil.copyfile(pdf, out_pdf)
        if not self.r.keys.writable or not self.r.connected.is_set():
            return
        try:
            pdf_id = await self.r.put_blob(pdf.read_bytes(), "output") if pdf.exists() else None
            log_id = await self.r.put_blob(log_text, "output")
            stx = build / f"{stem}.synctex.gz"
            stx_id = await self.r.put_blob(stx.read_bytes(), "output") if stx.exists() and pdf.exists() else None
        except Exception as e:  # noqa: BLE001
            log.warning("could not upload output: %s", e)
            return
        prev = self.r.out.get("latest")
        def publish():
            self.r.out["latest"] = {
                "pdf": pdf_id, "log": log_id, "ok": ok, "by": self.opts["label"],
                "at": time.time(), "main": main, **({"synctex": stx_id} if stx_id else {}),
            }
        self.r.local_change(publish)
        # drop the output this replaces (browsers also tidy up older ones, using the history)
        if isinstance(prev, dict):
            new = {pdf_id, log_id, stx_id}
            files = {v.get("h") for v in self.r.bin.values() if isinstance(v, dict)}
            for k in ("pdf", "log", "synctex"):
                old = prev.get(k)
                if old and old not in new and old not in files:
                    await self.r.drop_output(old)

    def shell_mode(self) -> tuple[str, str]:
        """The latexmk shell-escape flag for this compile, and a note for the log.

        Full shell escape runs only lines you have approved (`texloom allow`): when a line
        that asks to run programs is new, this compile uses restricted mode instead."""
        mode = self.opts["shell_escape"]
        if mode == "restricted":
            return "-shell-restricted", ""
        if not mode:
            return "-no-shell-escape", ""
        files = {p: str(t) for p, t in self.r.files.items() if self.target(p) is not None}
        new = [f for f in scan(files) if f.kind == "shell" and f.key not in allowed(self.root)]
        if not new:
            return "-shell-escape", ""
        keys = frozenset(f.key for f in new)
        if keys != self._warned_shell:
            self._warned_shell = keys
            log.warning("⚠ not running programs this time: %d line(s) asking to run them are new:\n  %s\n"
                        "Check them, then run `texloom allow %s` to let them run.",
                        len(new), "\n  ".join(f.describe() for f in new[:10]), self.root)
        where = ", ".join(f"{f.path}:{f.line}" for f in new[:5]) + (" …" if len(new) > 5 else "")
        return "-shell-restricted", (f"compiled without shell escape: new lines ask to run programs ({where}). "
                                     f"The person running texloom there can allow them with `texloom allow`.")

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
            async with self.r.lock:
                if paths:
                    await self.ingest(paths)
                self.ingest_dirs()
