"""Keep compiles of collaborators' documents from reading this computer's other files.

TeX's paranoid mode (openin_any=p) only refuses dot files for reading: \\input{../../notes}, an
absolute path or a .bib file elsewhere still work, and LuaLaTeX's Lua code can read anything. The
PDF and log a compile publishes would carry what was read. So:

- where the system has a sandbox (macOS sandbox-exec; bubblewrap on Linux, when installed), the
  compile runs with home folders and mounted volumes hidden, except the project, TeX's own
  folders and fonts; the project's .texloom folder (its keys) is hidden too, but for the build
  folder;
- everywhere, afterwards, the files each TeX, BibTeX and biber run recorded reading (see RECORD
  in paths.py) are checked against the same places (and fontconfig's settings, which LuaLaTeX
  reads). If anything else was read, the output isn't
  shared and the build folder is emptied (its .aux and .bbl may hold what was read).

A folder's "confine": false (in .texloom/config.json) turns both off, for projects whose
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
from pathlib import Path

from .paths import STATE, tex_env

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


@functools.cache
def tex_dirs() -> tuple[Path, ...]:
    """TeX's own folders: its distribution, local trees, and per-user configuration and caches.
    Never a whole drive or a folder holding the home folder."""
    found: list[str] = []
    for var in TEX_VARS:
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


def outside_reads(project: Path, folder: Path) -> list[str]:
    """Files outside `project`, TeX's folders and fonts that compiles recorded reading, from the
    *.fls-reads and *.blg-reads files under `folder` (the build folder); the project's .texloom
    folder counts as outside, but for `folder`."""
    allowed = (project.resolve(), *tex_dirs(), *(d.resolve() for d in FONT_CONFIG), Path("/dev"))
    state, build = (project.resolve() / STATE,), (folder.resolve(),)
    _where.cache_clear()  # files may have moved since the last compile
    out: dict[str, None] = {}
    for rec in sorted(folder.rglob("*-reads")):
        pwd = rec.parent
        for line in rec.read_text("utf-8", "replace").splitlines():
            if line.startswith("PWD "):
                pwd = Path(line[4:].strip())
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
            if not FONT.search(p.name) and (not _inside(p, allowed) or _inside(p, state) and not _inside(p, build)):
                out[str(p)] = None
    return list(out)


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


def _sb(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandboxed(cmd: list[str], project: Path, build: Path) -> list[str]:
    """`cmd` run so it can't read the folders where people's own files are, except `project`,
    TeX's folders and fonts, nor the project's .texloom folder but for `build`; unchanged where
    there's no sandbox."""
    kind = _sandbox()
    if kind is None:
        return cmd
    hidden = _hidden()
    open_ = [d for d in (project.resolve(), *tex_dirs()) if _inside(d, tuple(hidden))]
    state, build = project.resolve() / STATE, build.resolve()
    if kind == "macos":
        font = r'#"(?i)\.(ttf|otf|ttc|otc|dfont|pfb|pfa|afm|woff2?)$"'
        profile = "\n".join([
            "(version 1)", "(allow default)",
            f"(deny file-read* {' '.join(f'(subpath {_sb(str(h))})' for h in hidden)})",
            f"(allow file-read* {' '.join(f'(subpath {_sb(str(d))})' for d in open_)} (regex {font})"
            f" {' '.join(f'(subpath {_sb(str(d))})' for d in FONT_CONFIG)})",
            f"(deny file-read* (subpath {_sb(str(state))}))",
            f"(allow file-read* (subpath {_sb(str(build))}))",
            "(allow file-read-metadata)",  # names and sizes only: paths to the project resolve
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
