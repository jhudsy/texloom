"""The rules a folder sync applies, apart from the syncing itself: which files in a folder are the
project's, which are text, merging a file saved while the project changed, Windows line endings,
renames, and a shared folder's versions of a file. The same rules as web/src/foldersync.ts (with
isText in projectfiles.ts and merge3 in diff.ts): scripts/folder-rules.json checks both.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from .paths import PROTECTED_DIRS, safe_rel


SOURCES = (".tex", ".bib", ".sty", ".cls", ".bst", ".ltx", ".dtx", ".ins", ".bbx", ".cbx")  # never counted as compile output
IGNORE_DIRS = {*PROTECTED_DIRS, "node_modules", "__pycache__"}  # never synced, so nothing there is ever written
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk", ".synctex.gz",
    ".synctex", ".bbl", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".vrb", ".idx", ".ilg",
    ".ind", ".xdv", ".dvi", ".swp", ".swx", ".tmp", "~",
)


# Copies that file-sync services make when a file changed on two computers at once: Dropbox's
# "x (Ann's conflicted copy 2026-11-20).tex" (Nextcloud's is alike), Syncthing's
# "x.sync-conflict-20261120-101500-ABCDEFG.tex". Sharing one would put a second, stale version of
# the file into the project; it's left for a person to merge (keep these in step with
# CONFLICT_COPY in web/src/foldersync.ts).
CONFLICT_COPY = re.compile(r" \([^()/]*conflicted copy[^()/]*\)|\.sync-conflict-\d{8}-\d{6}", re.IGNORECASE)


def conflict_copy(rel: str) -> bool:
    return bool(CONFLICT_COPY.search(rel.rsplit("/", 1)[-1]))


class Bases(dict):
    """Each text file's text last synced with disk (FolderSync.base), also keeping its few latest
    such texts: in a folder shared through a file-sync service, a save can come from another
    computer's older copy (fork_point)."""

    def __init__(self) -> None:
        super().__init__()
        self.history: dict[str, list[str]] = {}

    def __setitem__(self, rel: str, text: str) -> None:
        super().__setitem__(rel, text)
        h = self.history.setdefault(rel, [])
        if not h or h[-1] != text:
            h.append(text)
            del h[:-6]  # not a text: the list of versions


def fork_point(versions: list[str], new: str) -> str:
    """Which of a file's recent versions `new` was most likely made from: the closest, by lines
    (the latest when as close). A file-sync service shares a save made on another computer's copy
    of the file, which may not yet have had the latest version: merging from that older version
    keeps both edits, where merging from the latest would take the save as undoing what it lacks."""
    if len(versions) < 2 or len(new) > 1 << 20:
        return versions[-1]
    lines = new.splitlines(keepends=True)
    def close(v: str) -> float:
        return difflib.SequenceMatcher(None, v.splitlines(keepends=True), lines, autojunk=False).ratio()
    best = versions[-1]
    for v in reversed(versions[:-1]):
        if close(v) > close(best):
            best = v
    return best


def not_project_file(rel: str, main: str = "main.tex") -> bool:
    """Whether syncing leaves this path alone: tools' folders (and anything in a dot folder: .git,
    .github, .cache…, as for the project's folder list), LaTeX's working files, our compiled PDF,
    and file-sync services' conflicted copies. Same rule as syncedPath in web/src/foldersync.ts;
    scripts/folder-rules.json checks both."""
    parts = rel.split("/")
    if any(x in IGNORE_DIRS or x.startswith(".") for x in parts[:-1]) or parts[0] in IGNORE_DIRS:
        return True
    name = parts[-1]
    if name in IGNORE_NAMES or name.startswith(".#") or name.lower().endswith(IGNORE_SUFFIXES) or conflict_copy(name):
        return True
    return rel == main[:-4] + ".pdf"


def synced_file(rel: str, main: str = "main.tex") -> bool:
    """Whether a file at this path in the folder is one of the project's files."""
    return not not_project_file(rel, main) and safe_rel(rel) is not None


def pick_moves(gone: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """Files renamed or moved: of the files gone from the folder (path -> the content's hash, as
    last synced) and those new in it (path -> hash), which new one each gone one became: new path
    -> old path. The same content moved, the same name first (a folder renamed), else the first
    by path; each gone file goes to one new one, taken in order of path. Same rule as pickMoves in
    web/src/foldersync.ts; scripts/folder-rules.json checks both."""
    left = sorted(gone)
    out: dict[str, str] = {}
    for rel in sorted(new):
        same = [g for g in left if gone[g] == new[rel]]
        if same:
            old = next((g for g in same if g.rsplit("/", 1)[-1] == rel.rsplit("/", 1)[-1]), same[0])
            left.remove(old)
            out[rel] = old
    return out


def synced_folder(rel: str) -> bool:
    """Whether a folder is on the project's folder list (empty ones too): not a dot folder, or in one."""
    return safe_rel(rel) is not None and not any(seg.startswith(".") for seg in rel.split("/"))


# Never text, whatever they hold (a PDF can be plain ASCII): as in isText in web/src/projectfiles.ts.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".eps", ".webp", ".zip", ".gz", ".ttf", ".otf",
                   ".woff", ".woff2", ".bmp", ".tif", ".tiff"}
MAX_TEXT = 4 * 1024 * 1024
MAX_FILE = 50 * 1024 * 1024


def is_text(path: str, data: bytes) -> bool:
    """Whether a file is kept as text (editable, merged by lines): UTF-8, not too big, no NUL near
    the start, and not a kind that's never text. Anything else (a .tex in Latin-1, say) is kept as
    it is, byte for byte. Same rule as isText in web/src/projectfiles.ts; scripts/folder-rules.json
    checks both."""
    if len(data) > MAX_TEXT or b"\x00" in data[:8192]:
        return False
    if Path(path).suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def lines_of(s: str) -> list[str]:
    """A text's lines, each with its "\n" (the last may have none). Only "\n" ends a line, as in the
    web app (linesOf in web/src/diff.ts) and editors: str.splitlines also breaks at a lone "\r",
    form feeds and Unicode line separators, and merged differently from the web app there."""
    return [x for x in re.split(r"(?<=\n)", s) if x]


def merge3(base: str, theirs: str, ours: str) -> str:
    """Three-way merge by lines: `ours`' changes to `base`, applied on top of `theirs`.

    In a synced folder, `base` is the text last written to (or read from) disk, `ours` the
    file as it is now, and `theirs` the project's text, which may have moved on since (a
    collaborator typing). Applying only our changes keeps theirs; making the project equal
    to the file would undo them. Where both changed the same lines, ours wins; text both
    inserted at the same place is kept twice only if it differs.

    The same rules, and the same line diff, as merge3 in web/src/diff.ts, so both merge alike
    (scripts/check_props.py compares them).
    """
    if ours == base or theirs == ours:
        return theirs
    if theirs == base:
        return ours
    b, t, o = (lines_of(x) for x in (base, theirs, ours))
    ht, ho = _hunks(b, t), _hunks(b, o)

    def at(hs: list[tuple[int, int, int, int]], i: int, end: bool) -> int:
        """Base line i (never inside one of these changes) in the other text."""
        shift = 0
        for i1, i2, j1, j2 in hs:
            if (i2 if end else i1) == i:
                return j2 if end else j1
            if i2 <= i:
                shift = j2 - i2
        return i + shift

    every = sorted([(h[0], h[1], "t") for h in ht] + [(h[0], h[1], "o") for h in ho])
    out: list[str] = []
    pos = k = 0
    while k < len(every):
        s, e, sides = every[k][0], every[k][1], {every[k][2]}
        k += 1
        # changes overlap if they share lines, or are insertions at the same place
        while k < len(every) and (every[k][0] < e or every[k][0] == every[k][1] == s == e):
            e = max(e, every[k][1])
            sides.add(every[k][2])
            k += 1
        out += b[pos:s]
        mine = "".join(o[at(ho, s, False):at(ho, e, True)])
        other = "".join(t[at(ht, s, False):at(ht, e, True)])
        out.append(mine if sides == {"o"} else other if sides == {"t"} else other + mine if s == e and mine != other else mine)
        pos = e
    out += b[pos:]
    return "".join(out)


def _hunks(a: list[str], b: list[str]) -> list[tuple[int, int, int, int]]:
    """Changed line ranges from a to b: (i1, i2, j1, j2), lines [i1, i2) of a became [j1, j2) of b."""
    out: list[tuple[int, int, int, int]] = []
    cur: list[int] | None = None
    i = j = 0
    for op, ai, bj in _diff_lines(a, b):
        if op == "eq":
            if cur:
                out.append((cur[0], cur[1], cur[2], cur[3]))
                cur = None
            i, j = ai + 1, bj + 1
            continue
        if cur is None:
            cur = [i, i, j, j]
        if op == "del":
            cur[1] = i = ai + 1
        else:
            cur[3] = j = bj + 1
    if cur:
        out.append((cur[0], cur[1], cur[2], cur[3]))
    return out


def _diff_lines(a: list[str], b: list[str]) -> list[tuple[str, int, int]]:
    """Line diff, Myers' O(ND), as diffLineArrays in web/src/diff.ts: ("eq" | "del" | "add", line in a, line in b)."""
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    s = 0
    while s < len(a) - p and s < len(b) - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    ops = [("eq", i, i) for i in range(p)]
    ops += [(op, x + p, y + p) for op, x, y in _myers(a[p:len(a) - s], b[p:len(b) - s])]
    ops += [("eq", len(a) - s + i, len(b) - s + i) for i in range(s)]
    return ops


def _myers(a: list[str], b: list[str]) -> list[tuple[str, int, int]]:
    n, m = len(a), len(b)
    mx = n + m
    if not n:
        return [("add", 0, j) for j in range(m)]
    if not m:
        return [("del", i, 0) for i in range(n)]
    every = [("del", i, 0) for i in range(n)] + [("add", n, j) for j in range(m)]
    if n * m > 25_000_000:  # pathological sizes: replace the whole middle
        return every
    v = [0] * (2 * mx + 2)
    trace: list[list[int]] = []
    for d in range(mx + 1):
        if (d + 1) * len(v) > 16_000_000:  # the trace grows by a copy of v per step
            return every
        trace.append(v[:])
        for k in range(-d, d + 1, 2):
            x = v[mx + k + 1] if k == -d or (k != d and v[mx + k - 1] < v[mx + k + 1]) else v[mx + k - 1] + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[mx + k] = x
            if x >= n and y >= m:
                return _backtrack(trace, a, b, mx, d)
    return []


def _backtrack(trace: list[list[int]], a: list[str], b: list[str], mx: int, d_end: int) -> list[tuple[str, int, int]]:
    ops: list[tuple[str, int, int]] = []
    x, y = len(a), len(b)
    for d in range(d_end, 0, -1):
        v, k = trace[d], x - y
        prev_k = k + 1 if k == -d or (k != d and v[mx + k - 1] < v[mx + k + 1]) else k - 1
        prev_x = v[mx + prev_k]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            ops.append(("eq", x, y))
        if x == prev_x:
            y -= 1
            ops.append(("add", x, y))
        else:
            x -= 1
            ops.append(("del", x, y))
    while x > 0 and y > 0:
        x -= 1
        y -= 1
        ops.append(("eq", x, y))
    ops.reverse()
    return ops


def lf(s: str) -> str:
    """Text with every line ending as "\n". A file saved on Windows ends lines with "\r\n": the
    project keeps "\n" only (editors and merges count a line break as one character), and the
    file is written back with the line endings it had (`crlf`, remembered per file)."""
    return s.replace("\r\n", "\n").replace("\r", "") if "\r" in s else s  # a lone "\r": see strip_cr


def crlf(data: bytes) -> bool:
    """Whether a file's lines end with \r\n (judged by its first line)."""
    i = data.find(b"\n")
    return i > 0 and data[i - 1:i] == b"\r"


def on_disk(text: str, windows: bool) -> bytes:
    """A project text as its file holds it: \r\n line endings if it had them."""
    text = lf(text)
    return (text.replace("\n", "\r\n") if windows else text).encode("utf-8")
