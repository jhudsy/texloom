"""Keep compiles of collaborators' documents from reading this computer's other files.

TeX's reading modes don't keep a document in its project: paranoid (openin_any=p) refuses dot
files and, to Lua's io.open, absolute paths, but \\input{../../notes}, an absolute path or a .bib
file elsewhere still work; LuaLaTeX runs with r (dot files only), since paranoid refuses its font
loader TeX's own files; and LuaLaTeX's Lua code has other ways to read. The PDF and log a compile
publishes would carry what was read. So:

- where the system has a sandbox (macOS sandbox-exec; bubblewrap on Linux, when installed), the
  compile runs with home folders and mounted volumes hidden, except the project, TeX's own
  folders and fonts; the project's .texloom folder (its keys) is hidden too, but for the build
  folder;
- everywhere, afterwards, the files each TeX, BibTeX and biber run recorded reading (see RECORD
  in paths.py) are checked against the same places (and fontconfig's settings, which LuaLaTeX
  reads); a record rewritten or blanked during the compile (TeX can \\openout its own .fls)
  fails the check. xdvipdfmx (XeLaTeX), dvipdfmx and dvips record nothing, so the .xdv or .dvi
  they turn into the PDF is read for specials and fonts that name files (pdf:fstream, pdf:image,
  PSfile=, map files…), and those are checked the same way. If anything else was read, the
  output isn't shared and the build folder is emptied (its .aux and .bbl may hold what was read).

Fonts are read where they are installed (LuaLaTeX scans the system's fonts): anywhere but home
folders and mounted volumes, and there only in the user's font folders (~/Library/Fonts, ~/.fonts…).

What the check can't see: Lua code in LuaLaTeX reading through loadfile, dofile, pdfe, img or
fontloader (TeX doesn't record those), or rewriting the records through Lua's io. Where there's a
sandbox it stops those reads; where there's none (Windows; Linux without bubblewrap), nothing
does (paranoid reading doesn't cover them either). What io.open reads is recorded.

A folder's "confine": false (a local setting, config.py) turns both off, for projects whose
collaborators you trust.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .paths import PROTECTED_DIRS, STATE, tex_env

log = logging.getLogger("texloom")

# Fonts are read wherever they are installed (LuaLaTeX scans the system's fonts); they print as glyphs.
FONT = re.compile(r"\.(?:ttf|otf|ttc|otc|dfont|pfb|pfa|afm|woff2?)$", re.I)
OUTSIDE_NOTE = ("texloom: this compile read files outside the project, so its PDF and log were not shared "
                "(they could contain those files). Collaborators' documents may only use the project's files, "
                "TeX's and fonts.\n")
# fontconfig's settings, read by LuaLaTeX's font loader on Linux
FONT_CONFIG = (Path("/etc/fonts"), Path("/usr/share/fontconfig"), Path("/usr/local/etc/fonts"),
               Path("/opt/homebrew/etc/fonts"), Path.home() / ".config/fontconfig")
TEX_VARS = ("TEXMFDIST", "TEXMFMAIN", "TEXMFLOCAL", "TEXMFSYSVAR", "TEXMFSYSCONFIG", "TEXMFHOME",
            "TEXMFVAR", "TEXMFCONFIG", "TEXMFCACHE", "SELFAUTOPARENT")


TEX_CACHES = ("TEXMFVAR", "TEXMFSYSVAR", "TEXMFCACHE")  # where TeX writes: formats, fonts, luaotfload's names


@functools.cache
def tex_cache_dirs() -> tuple[Path, ...]:
    """The folders TeX writes to while compiling (formats, fonts made on the fly, font caches)."""
    return _kpse_dirs(TEX_CACHES)


@functools.cache
def tex_dirs() -> tuple[Path, ...]:
    """TeX's own folders: its distribution, local trees, and per-user configuration and caches.
    Never a whole drive or a folder holding the home folder."""
    return _kpse_dirs(TEX_VARS)


def _kpse_dirs(names: tuple[str, ...]) -> tuple[Path, ...]:
    found: list[str] = []
    for var in names:
        try:
            out = subprocess.run(["kpsewhich", f"-expand-braces=${var}"], capture_output=True, text=True,
                                 env=tex_env(), timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found += [d.strip().lstrip("!") for d in out.strip().split(os.pathsep)]
    if os.name == "nt":  # MiKTeX keeps its files here, and its kpsewhich may not know the variables
        found += [os.path.join(os.environ.get(v, ""), *sub) for v, *sub in (
            ("LOCALAPPDATA", "MiKTeX"), ("APPDATA", "MiKTeX"), ("LOCALAPPDATA", "Programs", "MiKTeX"),
            ("ProgramFiles", "MiKTeX"), ("ProgramData", "MiKTeX")) if os.environ.get(v)]
    home = Path.home().resolve()
    keep: dict[Path, None] = {}
    for d in found:
        if not d:
            continue
        p = Path(os.path.expanduser(d)).resolve()
        if p != Path(p.anchor) and p != home and p not in home.parents:
            keep[p] = None
    return tuple(keep)


def _inside(p: Path, dirs: tuple[Path, ...]) -> bool:
    return any(p == d or d in p.parents for d in dirs)


@functools.lru_cache(maxsize=256)
def _where(name: str, pwd: Path, fmt: str) -> Path:
    """Where BibTeX or biber found `name` (its log shows the name as written): kpathsea's answer."""
    try:
        hit = subprocess.run(["kpsewhich", f"-format={fmt}", name], cwd=pwd, capture_output=True,
                             text=True, env=tex_env(), timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        hit = ""
    return (pwd / (hit or name)).resolve()


def user_font_dirs() -> tuple[Path, ...]:
    """The folders in the home folder where fonts are installed."""
    home = Path.home()
    dirs = [home / "Library" / "Fonts", home / ".fonts", home / ".local" / "share" / "fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    return tuple(d.resolve() for d in dirs)


def _font_ok(p: Path) -> bool:
    """A font file where fonts are installed: anywhere but home folders and mounted volumes, and
    there in the user's font folders only."""
    return bool(FONT.search(p.name)) and (not _inside(p, tuple(_hidden())) or _inside(p, user_font_dirs()))


TEX_LINE = re.compile(r"(?:INPUT|OUTPUT) .")
RECORD_FILE = re.compile(r"\.(?:fls|blg)$|-reads$", re.I)
OUTPUT_FILE = re.compile(r"\.(?:xdv|dvi|pdf)$", re.I)


def _resolved(p: str) -> Path:
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(p)


def record_problem(rec: Path, text: str) -> str | None:
    """Why a record of what was read can't be trusted, or None. TeX can \\openout its own .fls,
    blanking what it had recorded so far, so each TeX run's part must be whole: our PWD line
    (paths.RECORD), then TeX's own, then only INPUT and OUTPUT lines, none naming a record or
    the .xdv/.dvi that becomes the PDF."""
    if "\0" in text:
        return "it holds NUL bytes (it was blanked while TeX wrote it)"
    if rec.name.endswith(".blg-reads"):
        return None  # BibTeX's and biber's logs: written once each run, after TeX
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith("PWD "):
            return f"unexpected line {lines[i][:80]!r}"
        ours = _resolved(lines[i][4:].strip())
        i += 1
        if i < len(lines) and lines[i].startswith("MISSING"):
            return "a TeX run left no record"
        if i >= len(lines) or not lines[i].startswith("PWD ") or _resolved(lines[i][4:].strip()) != ours:
            return "a TeX run's record doesn't start as TeX starts it"
        i += 1
        written: set[str] = set()
        while i < len(lines) and not lines[i].startswith("PWD "):
            if not TEX_LINE.match(lines[i]):
                return f"unexpected line {lines[i][:80]!r}"
            if lines[i].startswith("OUTPUT "):
                name = lines[i][7:].strip()
                # TeX writes its .xdv, .dvi or .pdf once (and says so); a second time is the document
                if RECORD_FILE.search(name) or OUTPUT_FILE.search(name) and name in written:
                    return f"the document wrote {name!r}"
                written.add(name)
            i += 1
    return None


def outside_reads(project: Path, folder: Path) -> list[str]:
    """Files outside `project`, TeX's folders and fonts that compiles recorded reading, from the
    *.fls-reads and *.blg-reads files under `folder` (the build folder), or that the .xdv and .dvi
    files there ask xdvipdfmx, dvipdfmx or dvips to read; the project's .texloom folder counts as
    outside, but for `folder`. A record that can't be trusted counts too (in brackets, why)."""
    allowed = (project.resolve(), *tex_dirs(), *(d.resolve() for d in FONT_CONFIG), Path("/dev"))
    state, build = (project.resolve() / STATE,), (folder.resolve(),)
    _where.cache_clear()  # files may have moved since the last compile
    out: dict[str, None] = {}

    def bad(p: Path) -> bool:
        return not _font_ok(p) and (not _inside(p, allowed) or _inside(p, state) and not _inside(p, build))

    ran_in: dict[Path, Path] = {}  # output folder -> where TeX ran: what the converters' names are relative to
    for rec in sorted(folder.rglob("*-reads")):
        pwd = rec.parent
        text = rec.read_bytes().decode("utf-8", "replace")
        if why := record_problem(rec, text):
            out[f"(the record {rec.name} can't be trusted: {why})"] = None
        for line in text.splitlines():
            if line.startswith("PWD "):
                pwd = Path(line[4:].strip())
                ran_in.setdefault(rec.parent, pwd)
                continue
            if m := re.match(r"INPUT (.+)$", line):
                p = (pwd / m.group(1)).resolve()
            elif m := re.match(r"Database file #\d+: (.+)$", line):
                p = _where(m.group(1).strip(), pwd, "bib")
            elif m := re.match(r"The style file: (.+)$", line):
                p = _where(m.group(1).strip(), pwd, "bst")
            elif m := re.search(r"(?:data source|bibtex file) '(.+?)'", line):  # biber
                p = _where(m.group(1), pwd, "bib")
            else:
                continue
            if bad(p):
                out[str(p)] = None
    for dvi in sorted([*folder.rglob("*.xdv"), *folder.rglob("*.dvi")]):
        pwd = ran_in.get(dvi.parent, project.resolve())
        try:
            names = dvi_names(dvi.read_bytes())
        except ValueError as e:
            out[f"({dvi.name} couldn't be checked: {e})"] = None
            continue
        for name in names:
            for p in _candidates(name, pwd):
                if bad(p):
                    out[str(p)] = None
    return list(out)


# --- what xdvipdfmx, dvipdfmx and dvips would read ---------------------------------
# DVI commands with parameters of a fixed size (set_rule, put_rule, nop, bop, eop, push, pop,
# w0, x0, y0, z0, XDV's begin/end_reflect, pTeX's dir)
_FIXED = {132: 8, 137: 8, 138: 0, 139: 44, 140: 0, 141: 0, 142: 0, 147: 0, 152: 0, 161: 0, 166: 0,
          250: 0, 251: 0, 255: 1}
# commands with 1 to 4 bytes of parameter: set, put, right, w, x, down, y, z, fnt
_SIZED = ((128, 131), (133, 136), (143, 146), (148, 151), (153, 156), (157, 160), (162, 165), (167, 170), (235, 238))


def dvi_names(data: bytes) -> list[str]:
    """What a DVI or XDV file names that its converter may open as a file: every string and word
    in its specials (pdf:fstream @x (/path), pdf:image, PSfile="…", map lines…) and its fonts'
    names. ValueError if it isn't a DVI or XDV file this reads through to the end."""
    names: list[str] = []
    n, i = len(data), 0

    def take(k: int) -> bytes:
        nonlocal i
        if k < 0 or i + k > n:
            raise ValueError("it is cut short")
        b = data[i:i + k]
        i += k
        return b

    def num(k: int) -> int:
        return int.from_bytes(take(k), "big")

    if num(1) != 247:
        raise ValueError("it isn't a DVI file")
    ident = num(1)
    if ident not in (2, 3, 5, 6, 7):
        raise ValueError(f"unknown DVI version {ident}")
    take(12)
    take(num(1))  # comment
    while True:
        op = num(1)
        if op <= 127 or 171 <= op <= 234:  # set_char, fnt_num
            continue
        if op in _FIXED:
            take(_FIXED[op])
        elif 239 <= op <= 242:  # xxx: a special
            names += special_words(take(num(op - 238)).decode("latin-1"))
        elif 243 <= op <= 246:  # fnt_def: k c[4] s[4] d[4] a[1] l[1] n[a+l]
            take(op - 242 + 12)
            a, ln = num(1), num(1)
            names.append(take(a + ln).decode("latin-1"))
        elif op == 248:  # post
            take(28)
        elif op == 249:  # post_post: the end
            return names
        elif op == 252:  # XDV: a font, by file or by name: k[4] s[4] flags[2] l[1] name[l] index[4] …
            take(8)
            flags = num(2)
            names.append(take(num(1)).decode("utf-8", "replace").strip('[]"'))
            take(4 + 4 * sum(1 for f in (0x0200, 0x1000, 0x2000, 0x4000) if flags & f))
            if flags & 0x0800 and ident < 7:  # variations (older XDV)
                take(8 * num(2))
        elif op == 253:  # XDV glyphs: w[4] k[2] xy[8k] g[2k]
            take(4)
            take(10 * num(2))
        elif op == 254 and ident >= 7:  # XDV text and glyphs: l[2] t[2l] w[4] k[2] xy[8k] g[2k]
            take(2 * num(2))
            take(4)
            take(10 * num(2))
        elif op == 254:  # older XDV glyph string: w[4] k[2] x[4k] g[2k]
            take(4)
            take(6 * num(2))
        else:
            for lo, hi in _SIZED:
                if lo <= op <= hi:
                    take(op - lo + 1)
                    break
            else:
                raise ValueError(f"unknown command {op}")


_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _pdf_string(s: str, i: int) -> str:
    """The PDF literal string that starts at s[i] == '(', its escapes decoded."""
    out: list[str] = []
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            d = s[i + 1]
            if m := re.match(r"[0-7]{1,3}", s[i + 1:]):
                out.append(chr(int(m.group(), 8) & 0xFF))
                i += 1 + len(m.group())
            elif d in "\r\n":  # a line continued
                i += 2 + (d == "\r" and s[i + 2:i + 3] == "\n")
            else:
                out.append(_ESC.get(d, d))
                i += 2
            continue
        if c == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(c)
        i += 1
    return "".join(out)


def special_words(special: str) -> list[str]:
    """Everything in a special that could be a file name: its PDF strings (literal, from every
    '(', and hex), quoted parts, and words (split also at = < > [ ] { } and commas, as in map
    lines and PSfile=)."""
    found = [_pdf_string(special, m.start()) for m in re.finditer(r"\(", special)]
    for m in re.finditer(r"<([0-9A-Fa-f\s]+)>", special):
        h = re.sub(r"\s", "", m.group(1))
        found.append(bytes.fromhex(h + "0" * (len(h) % 2)).decode("latin-1"))
    found += re.findall(r'"([^"]*)"', special) + re.findall(r"'([^']*)'", special)
    found += [w for w in re.split(r"[\s=<>\[\]{}(),\"']+", special) if w]
    return found


# what kpathsea adds to a name when looking for an image, map, encoding or font
SUFFIXES = ("", ".map", ".enc", ".pdf", ".eps", ".ps", ".png", ".jpg", ".jpeg", ".bmp", ".pfb", ".pfa",
            ".tfm", ".vf", ".otf", ".ttf")


def _candidates(name: str, pwd: Path) -> list[Path]:
    """The files there are that a converter could open for `name`: relative to `pwd`, with ~ and
    $VARIABLES expanded (kpathsea expands them), and with the suffixes it tries."""
    name = name.strip()
    if not name or len(name) > 4096 or "\0" in name:
        return []
    out: list[Path] = []
    for n in dict.fromkeys([name, os.path.expandvars(os.path.expanduser(name))]):
        for suffix in SUFFIXES:
            try:
                p = (pwd / (n + suffix)).resolve()
                if p.is_file():
                    out.append(p)
            except (OSError, RuntimeError, ValueError):
                continue
    if not out and re.search(r"\$\w|\$\{|^~", name) and ("/" in name or "\\" in name):
        out.append(Path(name))  # a kpathsea variable this can't expand: it could name any file
    return out


def forget_reads(folder: Path) -> None:
    """Remove the records of earlier compiles, before compiling again."""
    for rec in folder.rglob("*-reads"):
        rec.unlink(missing_ok=True)


@functools.cache
def _sandbox() -> str | None:
    """The sandbox this system can run a compile in: 'macos', 'bwrap' or None."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        kind, probe = "macos", ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
    elif sys.platform.startswith("linux") and shutil.which("bwrap"):
        kind, probe = "bwrap", ["bwrap", "--dev-bind", "/", "/", "true"]
    else:
        return None
    try:  # may be refused: already inside a sandbox or container, user namespaces turned off, …
        ok = subprocess.run(probe, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        log.warning("%s doesn't work here: compiles aren't sandboxed (their output is still checked)", probe[0])
    return kind if ok else None


def _hidden() -> list[Path]:
    """The folders where people's own files are: home folders and mounted volumes."""
    home = Path.home().resolve()
    roots = [Path("/Users"), Path("/Volumes"), Path("/Network")] if sys.platform == "darwin" else \
        [Path("/home"), Path("/root"), Path("/media"), Path("/mnt"), Path("/run/media"), Path("/srv")]
    out = [r.resolve() for r in roots if r.exists()]
    if not _inside(home, tuple(out)):
        out.append(home)
    return [r for r in out if not _inside(r, tuple(x for x in out if x != r))]  # outermost only


def _temp_dirs() -> list[Path]:
    """The temporary folders programs may write to: /tmp, $TMPDIR, and on macOS this user's
    temporary and cache folders beside it (/var/folders/…/T and …/C)."""
    out = [Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir())]
    if os.environ.get("TMPDIR"):
        out.append(Path(os.environ["TMPDIR"]))
    if sys.platform == "darwin":
        out += [t.parent / "C" for t in out if t.name == "T"]
    return list(dict.fromkeys(p.resolve() for p in out if p.is_dir()))


def _sb(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandboxed(cmd: list[str], project: Path, build: Path) -> list[str]:
    """`cmd` run so it can't read the folders where people's own files are, except `project`,
    TeX's folders and fonts, nor the project's .texloom folder but for `build`; on macOS, so it
    can't write anywhere but the project (not its .texloom, .git and the like, nor a latexmkrc),
    `build`, TeX's caches and the temporary folders. Unchanged where there's no sandbox."""
    kind = _sandbox()
    if kind is None:
        return cmd
    hidden = _hidden()
    open_ = [d for d in (project.resolve(), *tex_dirs()) if _inside(d, tuple(hidden))]
    state, build = project.resolve() / STATE, build.resolve()
    if kind == "macos":
        profile = "\n".join([
            "(version 1)", "(allow default)",
            f"(deny file-read* {' '.join(f'(subpath {_sb(str(h))})' for h in hidden)})",
            f"(allow file-read* {' '.join(f'(subpath {_sb(str(d))})' for d in (*open_, *FONT_CONFIG, *user_font_dirs()))})",
            f"(deny file-read* (subpath {_sb(str(state))}))",
            f"(allow file-read* (subpath {_sb(str(build))}))",
            "(allow file-read-metadata)",  # names and sizes only: paths to the project resolve
            # writes: only the project (but its .texloom), the build folder, TeX's caches and the temporary folders
            '(deny file-write* (subpath "/"))',
            f"(allow file-write* {' '.join(f'(subpath {_sb(str(d))})' for d in (project.resolve(), *tex_cache_dirs(), *_temp_dirs()))}"
            ' (subpath "/dev"))',
            f"(deny file-write* {' '.join(f'(subpath {_sb(str(project.resolve() / d))})' for d in sorted(PROTECTED_DIRS))}"
            ' (regex #"(?i)latexmkrc$"))',
            f"(allow file-write* (subpath {_sb(str(build))}))",
        ])
        return ["sandbox-exec", "-p", profile, *cmd]
    args = ["bwrap", "--dev-bind", "/", "/", "--die-with-parent"]
    for h in hidden:
        args += ["--tmpfs", str(h)]
    for d in open_:  # writable: the build folder, TeX's caches
        args += ["--bind-try", str(d), str(d)]
    home = Path.home().resolve()
    for fonts in (home / ".fonts", home / ".local/share/fonts", home / ".config/fontconfig"):
        args += ["--ro-bind-try", str(fonts), str(fonts)]
    if state.is_dir() and _inside(build, (state,)):
        args += ["--tmpfs", str(state), "--bind", str(build), str(build)]
    return [*args, "--", *cmd]
