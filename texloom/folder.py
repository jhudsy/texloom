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
import posixpath
import shutil
import time
from pathlib import Path

from pycrdt import Map, Text
from watchfiles import awatch

from .anchors import assoc_of, fresh_anchor, index_in, u16
from .replica import BLOB_RE, Replica

log = logging.getLogger("texloom")

from .confine import OUTSIDE_NOTE, forget_reads, outside_reads, sandboxed
from .scan import SCANNED, added, allowed, scan
from .xr import compile_order, external_documents
from .paths import PROTECTED_DIRS, STATE, UNSAFE_NAMES, WIN_RESERVED, latexmk_command, main_file, safe_rel, tex_env  # noqa: F401
SOURCES = (".tex", ".bib", ".sty", ".cls", ".bst", ".ltx", ".dtx", ".ins", ".bbx", ".cbx")  # never counted as compile output
IGNORE_DIRS = {STATE, ".git", ".svn", "node_modules", "__pycache__", ".idea", ".vscode"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk", ".synctex.gz",
    ".synctex", ".bbl", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".vrb", ".idx", ".ilg",
    ".ind", ".xdv", ".dvi", ".swp", ".swx", ".tmp", "~",
)


def not_project_file(rel: str, main: str = "main.tex") -> bool:
    """Whether syncing leaves this path alone: tools' folders, LaTeX's working files, our compiled PDF."""
    parts = rel.split("/")
    if any(x in IGNORE_DIRS for x in parts[:-1]) or parts[0] in IGNORE_DIRS:
        return True
    name = parts[-1]
    if name in IGNORE_NAMES or name.startswith(".#") or name.endswith(IGNORE_SUFFIXES):
        return True
    return rel == main[:-4] + ".pdf"


def project_files(root: Path, main: str = "main.tex") -> list[str]:
    """What is on disk that syncing would treat as this project's files."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for f in filenames:
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if not not_project_file(rel, main) and safe_rel(rel) and not (Path(dirpath) / f).is_symlink():
                out.append(rel)
    return out


def copy_project_files(src: Path, dst: Path, main: str = "main.tex") -> None:
    """Copy a project's files and folders, as new files: not its state, or LaTeX's working files."""
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, _files in os.walk(src):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".") and not (here / d).is_symlink()]
        for d in dirnames:
            rel = (here / d).relative_to(src).as_posix()
            if safe_rel(rel):
                (dst / rel).mkdir(parents=True, exist_ok=True)
    for rel in project_files(src, main):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, dst / rel)


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


def merge3(base: str, theirs: str, ours: str) -> str:
    """Three-way merge by lines: `ours`' changes to `base`, applied on top of `theirs`.

    In a synced folder, `base` is the text last written to (or read from) disk, `ours` the
    file as it is now, and `theirs` the project's text, which may have moved on since (a
    collaborator typing). Applying only our changes keeps theirs; making the project equal
    to the file would undo them. Where both changed the same lines, ours wins; text both
    inserted at the same place is kept twice only if it differs.
    """
    if ours == base or theirs == ours:
        return theirs
    if theirs == base:
        return ours
    b, t, o = (x.splitlines(keepends=True) for x in (base, theirs, ours))
    ops_t = difflib.SequenceMatcher(a=b, b=t, autojunk=False).get_opcodes()
    ops_o = difflib.SequenceMatcher(a=b, b=o, autojunk=False).get_opcodes()

    def at(ops, i: int, end: bool) -> int:
        """Where a region starting (or ending) at base line i starts (ends) in the other text;
        i is never strictly inside one of that text's changes."""
        for tag, i1, i2, j1, j2 in (reversed(ops) if end else ops):
            if tag != "equal" and (i2 == i if end else i1 == i):
                return j2 if end else j1
        for tag, i1, i2, j1, _j2 in ops:
            if tag == "equal" and i1 <= i <= i2:
                return j1 + (i - i1)
        return ops[-1][4] if ops else 0  # the end of the other text

    hunks = sorted([(i1, i2, "t") for tag, i1, i2, _j1, _j2 in ops_t if tag != "equal"]
                   + [(i1, i2, "o") for tag, i1, i2, _j1, _j2 in ops_o if tag != "equal"])
    out: list[str] = []
    pos = 0
    k = 0
    while k < len(hunks):
        s, e, sides = hunks[k][0], hunks[k][1], {hunks[k][2]}
        k += 1
        # changes overlap if they share lines, or are insertions at the same place
        while k < len(hunks) and (hunks[k][0] < e or hunks[k][0] == hunks[k][1] == s == e):
            e = max(e, hunks[k][1])
            sides.add(hunks[k][2])
            k += 1
        out += b[pos:s]
        mine = o[at(ops_o, s, False):at(ops_o, e, True)]
        other = t[at(ops_t, s, False):at(ops_t, e, True)]
        if sides == {"o"}:
            out += mine
        elif sides == {"t"}:
            out += other
        elif s == e and mine != other:  # both inserted here: keep both
            out += other + mine
        else:  # both changed these lines: the file's version
            out += mine
        pos = e
    out += b[pos:]
    return "".join(out)


def lf(s: str) -> str:
    """Text with every line ending as "\n". A file saved on Windows ends lines with "\r\n": the
    project keeps "\n" only (editors and merges count a line break as one character), and the
    file is written back with the line endings it had (`crlf`, remembered per file)."""
    return s.replace("\r\n", "\n").replace("\r", "\n") if "\r" in s else s


def crlf(data: bytes) -> bool:
    """Whether a file's lines end with \r\n (judged by its first line)."""
    i = data.find(b"\n")
    return i > 0 and data[i - 1:i] == b"\r"


def on_disk(text: str, windows: bool) -> bytes:
    """A project text as its file holds it: \r\n line endings if it had them."""
    text = lf(text)
    return (text.replace("\n", "\r\n") if windows else text).encode("utf-8")


def strip_cr(yt: Text) -> bool:
    """Take every "\r" out of a project text, each by itself, so nothing else moves (a lone one
    becomes "\n"). Call inside a change. True if there were any."""
    s = str(yt)
    if "\r" not in s:
        return False
    for i in range(len(s) - 1, -1, -1):
        if s[i] == "\r":
            at = len(s[:i].encode())  # pycrdt counts UTF-8 bytes; "\r" is one
            del yt[at:at + 1]
            if s[i + 1:i + 2] != "\n":
                yt.insert(at, "\n")
    return True


def apply_text(ytext: Text, old: str, new: str) -> None:
    """Apply a diff to a Y.Text. pycrdt indexes text by UTF-8 bytes."""
    for start, end, repl in reversed(text_ops(old, new)):  # back-to-front keeps offsets valid
        b_start = len(old[:start].encode())
        b_end = b_start + len(old[start:end].encode())
        if b_end > b_start:
            del ytext[b_start:b_end]
        if repl:
            ytext.insert(b_start, repl)


def move_file(r: Replica, review: Map, a: str, b: str) -> None:
    """Move file `a` to `b` in the project, as renaming in the web app does (movePath in
    web/src/projectfiles.ts): the text is copied to the new path, and the comments and
    suggestions on it are anchored at the same spots in the copy. Call inside a change.

    Where a comment was is read from its anchors when the text is ASCII, else from its quote
    when that appears exactly once (see anchors.py on why); otherwise it keeps its old anchors
    and, as before, loses its place."""
    yt = r.files.get(a)
    if yt is not None:
        s = str(yt)
        spots = {}
        for iid, m in review.items():
            if not isinstance(m, Map) or m.get("file") != a:
                continue
            ends = [m.get("start"), m.get("end")]
            if all(isinstance(x, (bytes, bytearray)) and x for x in ends):
                at = [index_in(yt, s, bytes(x)) for x in ends]
                if None in at and isinstance(q := m.get("quote"), str) and q and s.count(q) == 1:
                    i = s.index(q)
                    at = [u16(s, i), u16(s, i + len(q))]
                if None not in at:
                    spots[iid] = (at, [assoc_of(bytes(x)) for x in ends])
        r.files[b] = Text(s)
        moved, n16 = r.files[b], u16(s, len(s))
        for iid, m in review.items():
            if not isinstance(m, Map) or m.get("file") != a:
                continue
            if iid in spots:
                (i, j), (ai, aj) = spots[iid]
                start, end = fresh_anchor(moved, n16, i, ai), fresh_anchor(moved, n16, j, aj)
                if start and end:
                    m["start"], m["end"] = start, end
            m["file"] = b
        del r.files[a]
    if a in r.bin:
        r.bin[b] = dict(r.bin[a])
        del r.bin[a]
    if r.meta.get("main") == a:
        r.meta["main"] = b


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
        # Text files: the text last synced with disk (the base of a three-way merge), and the
        # hashes of versions recently written or read here. An older one reappearing on disk
        # is a replay (an editor saving or reloading a stale copy), not an edit.
        self.base: dict[str, str] = {}
        self.recent: dict[str, list[str]] = {}
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
        self._asked: set = set()  # browsers whose compile requests the next compile answers
        self._compiled_at = 0.0  # when the last compile finished (monotonic)
        self._chained = 0  # compiles in a row started by this folder's changes just after the last
        self._generated: set[str] = set()  # files seen changing here just after a compile
        self._compiling = False  # a compile is running (not just waiting)
        self._again = False  # compile again once it finishes
        self._clean_build = False  # the next compile starts from scratch (requested from a browser)
        self._rewrite = False  # write the project's text to disk after this ingest
        self._lf_done = False  # "\r" taken out of the project's texts (once, at the start)

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
        return not_project_file(rel, str(self.r.meta.get("main", "main.tex")))

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
        if not self.r.can_write:
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

    def _remember(self, rel: str, h: str, windows: bool | None = None) -> None:
        """Note a file's state on disk; `windows`: it ends lines with \r\n (else as before)."""
        st = (self.root / rel).stat()
        self.known[rel] = {"m": st.st_mtime_ns, "z": st.st_size, "h": h,
                           "c": self._windows(rel) if windows is None else windows}

    def _windows(self, rel: str) -> bool:
        """Whether the file ends lines with \r\n: as remembered, or (noted before 0.4.21) as it is on disk."""
        k = self.known.get(rel, {})
        if "c" in k:
            return bool(k["c"])
        try:
            with open(self.root / rel, "rb") as f:
                return crlf(f.read(65536))
        except OSError:
            return False
        seen = self.recent.setdefault(rel, [])
        if h in seen:
            seen.remove(h)
        seen.append(h)
        del seen[:-30]

    def _stale(self, rel: str, h: str) -> bool:
        """The file holds a version this folder had earlier, not the latest: a replay."""
        return h in self.recent.get(rel, [])[:-1]

    def _moves(self, paths: set[str]) -> dict[str, tuple[str, str]]:
        """Files renamed or moved on disk: new path -> (old path, hash). A rename reaches us as a
        deletion and an addition, maybe in different batches: a file gone from disk whose last
        synced content is now in a file the project doesn't have yet has moved there."""
        def usable(rel: str) -> bool:
            return not self.ignored(rel) and safe_rel(rel) is not None
        gone = [rel for rel in sorted(paths) if rel in self.known and usable(rel) and not (self.root / rel).is_file()]
        if not gone:
            return {}
        sizes = {self.known[g].get("z") for g in gone}

        def fresh(rel: str) -> bool:
            if rel in self.known or rel in self.r.files or rel in self.r.bin or not usable(rel):
                return False
            try:
                return (self.root / rel).stat().st_size in sizes
            except OSError:
                return False
        new = [rel for rel in sorted(paths) if fresh(rel)]
        if len(new) < len(gone):  # the other half of a rename may come in a later batch
            new += [rel for rel in sorted(set(self.disk_files()) - set(paths)) if fresh(rel)]
        out: dict[str, tuple[str, str]] = {}
        for rel in new:
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                continue
            h = sha(data) if is_text(rel, data) else self.r.keys.blob_id(data)
            same = [g for g in gone if self.known[g].get("h") == h]
            if same:  # the same name in another folder first, as when a folder is renamed
                old = next((g for g in same if g.rsplit("/", 1)[-1] == rel.rsplit("/", 1)[-1]), same[0])
                gone.remove(old)
                out[rel] = (old, h)
        return out

    # --- disk -> CRDT ----------------------------------------------------------
    async def ingest_all(self) -> None:
        if not self._lf_done and self.r.can_write and self.r.deleted_at is None:
            self._lf_done = True  # "\r" a copy of the app from before 0.4.21 put into the project
            if any("\r" in str(t) for t in self.r.files.values()):
                self.r.local_change(lambda: [strip_cr(t) for t in self.r.files.values()])
        await self.ingest(set(self.disk_files()) | set(self.known))
        self.ingest_dirs()

    async def ingest(self, paths: set[str]) -> None:
        """Push local disk changes for `paths` into the CRDT."""
        if not self.r.can_write:
            return
        changed = False
        ups: set[str] = set()  # files changed here and sent
        moves = self._moves(paths)
        if moves:
            review = self.r.doc.get("review", type=Map)

            def move_all():
                for new, (old, _h) in moves.items():
                    move_file(self.r, review, old, new)
            self.r.local_change(move_all)
            for new, (old, h) in moves.items():
                self._remember(new, h, self.known.pop(old).get("c", False))
                if old in self.base:
                    self.base[new] = self.base.pop(old)
                yt = self.r.files.get(new)
                if yt is not None and sha(on_disk(str(yt), self._windows(new))) != h:
                    self._rewrite = True  # collaborators changed it since it was last synced
                log.info("%s → %s", old, new)
                ups.add(new)
            changed = True
            paths = paths - set(moves) - {old for old, _h in moves.values()}
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
                self._remember(rel, h, crlf(data))  # touched, not changed
                if is_text(rel, data):
                    self.base[rel] = lf(data.decode("utf-8"))
                continue
            if is_text(rel, data) and self._stale(rel, h):
                # An older version came back (an editor saved or reloaded a stale copy). It
                # isn't an edit: applying it would undo collaborators' changes. Write the
                # project's current text back instead.
                log.warning("%s: an older version reappeared on disk (another program wrote it); keeping the project's text", rel)
                self.known.pop(rel, None)
                self._rewrite = True
                continue
            windows = None
            if is_text(rel, data):
                new, windows = lf(data.decode("utf-8")), crlf(data)
                def edit(rel=rel, new=new):
                    if rel in self.r.bin:
                        del self.r.bin[rel]
                    yt = self.r.files.get(rel)
                    if yt is None:
                        self.r.files[rel] = Text(new)
                        self.base[rel] = new
                        return
                    strip_cr(yt)  # a "\r" from an older copy of the app
                    cur, base = str(yt), self.base.get(rel)
                    # only what changed on disk since it was last synced, on top of what
                    # collaborators changed meanwhile (without a base: the file as it is)
                    merged = merge3(base, cur, new) if base is not None else new
                    apply_text(yt, cur, merged)
                    self.base[rel] = merged
                    if merged != new:
                        self._rewrite = True  # the file lacks collaborators' changes
                self.r.local_change(edit)
            else:
                bid = await self.r.put_blob(data)
                h = bid
                def setbin(rel=rel, bid=bid, size=len(data)):
                    if rel in self.r.files:
                        del self.r.files[rel]
                    self.r.bin[rel] = {"h": bid, "size": size}
                self.r.local_change(setbin)
            self._remember(rel, h, windows)
            log.info("↑ %s", rel)
            changed = True
            ups.add(rel)
        if changed:
            self.save_known()
            self.schedule_compile(own_change=True, files=ups)
        if self._rewrite:
            self._rewrite = False
            await self.write_out()

    # --- CRDT -> disk ----------------------------------------------------------
    async def on_remote(self) -> None:
        await self.write_out()

    async def write_out(self) -> None:
        changed = False
        wanted: set[str] = set()

        downs: set[str] = set()  # files that changed elsewhere and were written here

        def attempt(fn, rel):  # one bad entry (name clash, permissions) mustn't stop the rest
            nonlocal changed
            try:
                if fn():
                    changed = True
                    downs.add(rel)
            except OSError as e:
                log.warning("could not write %s: %s", rel, e)

        for rel, yt in list(self.r.files.items()):
            if self.ignored(rel) or self.target(rel) is None:
                continue
            wanted.add(rel)
            text = lf(str(yt))
            data = on_disk(text, self._windows(rel))
            h = sha(data)
            self.base[rel] = text  # what disk will hold (or holds already), as the project has it
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
            self.schedule_compile(delay=4, files=downs)  # edits from elsewhere arrive keystroke by keystroke

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

    def schedule_compile(self, delay: float = 1.5, own_change: bool = False, files: set[str] | None = None) -> None:
        """Compile once changes have stopped for `delay` seconds (each change starts the wait
        again). A compile that's running is never stopped: one more follows it instead.

        Compiles mustn't set each other off. The PDF a compile writes here, and its working
        files, are never synced, and a published PDF isn't a change. But a compile can also
        write a file into the folder (a minted cache, a generated figure), differently each
        time. So a file that changes here just after a compile counts as generated:
        - `own_change` (files changed in this folder): after three compiles in a row each
          started by such a change straight after the last, changes here stop starting
          compiles for a while;
        - changes from elsewhere to generated files only (another computer's compile wrote
          them) don't start one, so two computers can't keep each other compiling.
        A compile asked for (`files` None, delay 0) always runs."""
        if not self.compile:
            return
        if own_change:
            if self._compiling or time.monotonic() - self._compiled_at < 3:
                self._generated |= {f for f in files or () if not f.lower().endswith(SOURCES)}
                self._chained += 1
                if self._chained > 3:
                    if self._chained == 4:
                        log.warning("files keep changing each time the project compiles, so changes here no longer "
                                    "start a compile for now (a compile asked for, or an edit from elsewhere, still does)")
                    return
            else:
                self._chained = 0
        else:
            self._chained = 0
            if files and files <= self._generated:
                return  # another computer's compile output, not an edit
        self._start(delay)

    def _start(self, delay: float) -> None:
        if self._compiling:
            self._again = True
            return
        if self._compile_task and not self._compile_task.done():
            self._compile_task.cancel()  # still waiting for changes to stop: wait again
        self._compile_task = asyncio.get_event_loop().create_task(self._compile(delay=delay))

    async def on_compile_request(self, frm: int | None, clean: bool = False) -> None:
        """A browser asked for a compile (its device setting: 'compile with my own TeX');
        `clean`: from scratch, without the previous run's .aux, .bbl and other files."""
        if not self.compile or not self.r.can_write:
            return
        # Anyone with the link can ask (requests aren't signed): at most one compile every few seconds.
        now = time.monotonic()
        if now - self._last_request < 5:
            return
        self._last_request = now
        self._asked.add(frm)
        await self.r.p2p({"k": "cack", "by": self.opts["label"]}, frm)
        if clean:
            self.compile_from_scratch()
        else:
            self.schedule_compile(delay=0)

    async def _compile(self, delay: float) -> None:
        await asyncio.sleep(delay)
        asked, self._asked = self._asked, set()
        self._compiling = True
        try:
            await self._run_compile()
            self._compiled_at = time.monotonic()
        except asyncio.CancelledError:
            self._asked |= asked  # stopped (the folder is closing): nothing to answer with
            raise
        except FileNotFoundError as e:
            await self._compile_failed(asked, "latexmk wasn't found on this computer, so it can't compile here. "
                                       "Install TeX Live (MacTeX on a Mac, TeX Live or MiKTeX on Windows), which includes it.", e)
        except Exception as e:  # noqa: BLE001 - say so, rather than leave whoever asked waiting
            await self._compile_failed(asked, f"the compile stopped: {e}", e)

        finally:
            self._compiling = False
            if self._again:  # changes or requests came in while it ran
                self._again = False
                self._start(1.5)

    async def _compile_failed(self, asked: set, why: str, e: BaseException) -> None:
        log.error("can't compile: %s (%s)", why, e)
        for frm in asked:
            await self.r.p2p({"k": "cerr", "by": self.opts["label"], "why": why}, frm)

    async def _run_compile(self) -> None:
        main = self.main_file()
        if not main:
            return
        engine = str(self.opts.get("engine") or self.r.meta.get("engine", "pdflatex"))
        extra = list(self.opts["latexmk_args"])
        # Collaborators write the document, so by default TeX may not run programs
        # (-no-shell-escape), may not write outside the project (openout "paranoid") or read
        # outside it (texloom/confine.py), and latexmk ignores rc files (-norc; latexmkrc is Perl). An rc you
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
        notes = [note] if note else []
        confine = self.opts.get("confine", True)
        forget_reads(build)
        notes += await self._compile_externals(main, engine, build, extra, confine)
        cmd = latexmk_command(engine, self.r.meta.get("bib"), main_path.name, "-synctex=1", f"-outdir={build}", *extra)
        proc = await asyncio.create_subprocess_exec(
            *(sandboxed(cmd, self.root, build) if confine else cmd),
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
        if notes:  # collaborators see why minted & co. didn't run, or labels are missing
            log_text = "".join(f"texloom: {n}\n" for n in notes).encode() + b"\n" + log_text
        outside = outside_reads(self.root, build) if confine else []
        if outside:  # the PDF, log, .aux and .bbl may hold what was read: share and keep none of it
            log.warning("compiling %s read files outside the project, so nothing from it is shared or kept:\n  %s",
                        main, "\n  ".join(outside[:20]))
            shutil.rmtree(build, ignore_errors=True)
            build.mkdir(parents=True, exist_ok=True)
            ok, log_text = False, OUTSIDE_NOTE.encode()
        log.info("compiled %s in %.1fs: %s", main, time.time() - t0, "ok" if ok else "errors")
        out_pdf = self.target(main[:-4] + ".pdf")
        if pdf.exists() and out_pdf is not None:
            shutil.copyfile(pdf, out_pdf)
        if not self.r.can_write or not self.r.connected.is_set():
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

    async def _compile_externals(self, main: str, engine: str, build: Path, extra: list[str], confine: bool) -> list[str]:
        """Compile the documents `main` takes labels from (\\externaldocument) first, each in
        its own folder under `build`, and copy each .aux to where the compile that reads it
        looks first: its output folder. In a cycle (a paper and its supplement citing each
        other), the document that closes it gets the other's .aux from its last compile, so
        repeated compiles settle. Returns notes for the log."""
        files = {p: str(t) for p, t in self.r.files.items() if self.target(p) is not None}
        order = compile_order(files, main)
        if not order:
            return []
        out = {doc: build / "xr" / Path(doc).with_suffix("") for doc in order} | {main: build}
        built = {doc: out[doc] / f"{Path(doc).stem}.aux" for doc in [*order, main]}
        notes = []
        for doc in [*order, main]:
            out[doc].mkdir(parents=True, exist_ok=True)
            for aux, tex in external_documents(files, doc):
                rel = posixpath.relpath(aux, posixpath.dirname(doc) or ".")
                if built[tex].is_file() and not rel.startswith("../"):  # TeX may not read "../" here anyway
                    (out[doc] / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(built[tex], out[doc] / rel)
            path = self.target(doc)
            if doc == main or path is None or not path.is_file():
                continue
            cmd = latexmk_command(engine, self.r.meta.get("bib"), path.name, f"-outdir={out[doc]}", *extra)
            proc = await asyncio.create_subprocess_exec(
                *(sandboxed(cmd, self.root, out[doc]) if confine else cmd),
                cwd=path.parent, env=tex_env(TEXMFOUTPUT=str(out[doc])),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await proc.wait()
            except asyncio.CancelledError:
                proc.kill()
                raise
            if proc.returncode != 0:
                notes.append(f"{doc}, which {main} takes labels from (\\externaldocument), has errors: compile it on its own to see them.")
        return notes

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

        if not self.r.can_write:
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
