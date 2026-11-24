"""Which project paths may touch a disk, and how TeX is run on a project's files.

Everything here treats the shared document as written by others: file names and the
chosen main file come from collaborators.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path

STATE = ".texloom"
# In STATE, kept for the folder as a whole: the rest is each computer's own (state_dir)
SHARED_STATE = {"computers", "folder-id", "lock", ".gitignore"}


def write_json(path: Path, data, private: bool = False, **dump) -> None:
    """Write a JSON file so that a crash leaves the old one or the new one, never half of one: a
    temporary file beside it, then a rename. `private`: readable by you only (it holds keys or
    links). `dump`: json.dump's options (indent, ensure_ascii). The temporary file has a name of its
    own each time, made only if nothing is there (so never through a link planted beside the file,
    say by another computer sharing the folder through Dropbox)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")  # 0600, O_EXCL
    try:
        if not private:
            os.chmod(tmp, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class CachedFile:
    """What a file holds, read again only when it changes, for files read many times a second (the
    app reads the folder list and the tags every second): known by its date, size and inode (each
    write replaces the file: a new inode)."""

    def __init__(self, read) -> None:
        self.read = read  # path -> what it holds; may raise, and then nothing is kept
        self._last: tuple | None = None  # (path, date, size, inode), what it held: one value, for threads

    def get(self, path: Path, missing):
        """What the file holds, or `missing` if there is none."""
        try:
            st = path.stat()
        except OSError:
            return missing
        key = (path, st.st_mtime_ns, st.st_size, st.st_ino)
        if (last := self._last) is None or last[0] != key:
            last = self._last = (key, self.read(path))
        return last[1]

    def forget(self) -> None:
        """After writing the file: read it again next time."""
        self._last = None


def state_dir(folder: Path) -> Path:
    """This computer's records for a synced folder: its copy of the project, what it last synced,
    its settings and the project link. In the folder (so they move with it), but apart from other
    computers': a folder shared through Dropbox, iCloud, OneDrive or the like brings their
    `.texloom` here too, and each computer must only ever use its own.

    A folder from before 0.4.40 has them in `.texloom` itself: they are moved here the first time
    (they are this computer's; another computer's from the same file-sync service can't be told
    apart, as before)."""
    from .registry import computer_id
    top = folder / STATE
    mine = top / "computers" / computer_id()
    # compat(data 0.4.40): a folder's records in .texloom itself; converted on read: moved the first time
    if (top / "config.json").is_file() and not (mine / "config.json").exists():
        mine.mkdir(parents=True, exist_ok=True)
        # config.json last: until it has moved, this runs again and finishes the move
        for entry in sorted(top.iterdir(), key=lambda e: e.name == "config.json"):
            if entry.name in SHARED_STATE:
                continue
            try:
                os.replace(entry, mine / entry.name)
            except FileNotFoundError:
                pass  # another process moved it at the same moment
    return mine

# Files that TeX tools read as configuration or execute (latexmk runs latexmkrc as Perl).
# They are never written from, or sent to, the shared project.
UNSAFE_NAMES = {"latexmkrc", ".latexmkrc", "texmf.cnf", ".texmf.cnf", "mktexlsr", "ls-r"}
# Folders whose contents tools act on (version control hooks and config, which can name programs
# to run, and editor settings). Keep these rules in sync with cleanPath() in web/src/projectfiles.ts.
PROTECTED_DIRS = {STATE, ".git", ".hg", ".jj", ".sl", ".bzr", ".svn", ".vscode", ".idea"}
WIN_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
                *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


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
        # Windows drops trailing dots and spaces (".git." is ".git") and has 8.3 aliases ("GIT~1")
        if seg[-1] in ". " or re.search(r"~\d", seg):
            return None
        n = unicodedata.normalize("NFKC", seg).casefold()
        if n in PROTECTED_DIRS or n in UNSAFE_NAMES or n.endswith(".latexmkrc"):
            return None
        if os.name == "nt" and n.split(".")[0] in WIN_RESERVED:
            return None
    return rel


def main_file(files: object, meta: object) -> str | None:
    """The document to compile: the chosen main file if it's a safe path, else main.tex, else
    the first file with \\documentclass (top-level folders first)."""
    m = meta.get("main")  # type: ignore[attr-defined]
    if isinstance(m, str) and m in files and safe_rel(m):  # type: ignore[operator]
        return m
    cands = [p for p in files.keys() if p.endswith(".tex") and safe_rel(p)]  # type: ignore[attr-defined]
    for p in sorted(cands, key=lambda p: (p != "main.tex", p.count("/"), p)):
        if "\\documentclass" in str(files[p]):  # type: ignore[index]
            return p
    return None


def tex_safe_name(rel: str) -> bool:
    """Whether latexmk may be given this path (the document to compile, or one it takes labels from).
    latexmk runs some programs (kpsewhich, and any an rc file names) through the shell with the file
    name in double quotes, where ` and $( ) still run commands: only letters, digits, spaces and
    . _ - + / ( ) are let through."""
    return bool(rel) and all(c.isalnum() or c in " ._-+/()" or unicodedata.category(c).startswith("M") for c in rel)


def tex_names_refused(files: Mapping[str, str], main: str) -> str | None:
    """Why `main` mustn't be compiled because of a file name (its own, or that of a document it
    takes labels from, which is compiled first), or None."""
    from .xr import compile_order
    bad = [p for p in [main, *compile_order(files, main)] if not tex_safe_name(p)]
    if not bad:
        return None
    return (f"{bad[0]!r} has characters in its name or folder that LaTeX's tools could take as a command. "
            "Only letters, digits, spaces and . _ - + / ( ) are let through: rename it to compile.")


ENGINE_FLAGS = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}

# Each TeX, BibTeX and biber run appends what it read (its .fls or .blg, after a PWD line) to
# <name>.fls-reads / <name>.blg-reads: TeX rewrites its .fls on every pass, so a document could read
# a file on one pass and not the last. confine.outside_reads() checks them after the compile.
# With an output folder, latexmk runs BibTeX from inside it ("bibtex fudge"), where a bibliography
# named relative to the document (\bibliography{../refs}) isn't found: it runs from the document's
# folder instead (-bibfudge-), and then writes its .bbl and .blg through the path .texloom/build,
# which paranoid openout refuses (a dot folder). BibTeX writes no other files, so it alone may.
# latexmk runs its other programs through the shell, the file names in double quotes, where ` and
# $( ) still run commands: they too run as lists (texloom_exec), with no shell. kpsewhich, which
# latexmk runs through the shell for the names a document gives (\bibliography{$(…)}), isn't
# asked about a name the shell would act on: such a file is then only not watched for changes.
RECORD = (
    "use Cwd; sub texloom_run { my ($ext, $base, @cmd) = @_; "
    "local $ENV{openout_any} = q(a) if $cmd[0] eq q(bibtex); my $r = system(@cmd); "
    "if (open(my $in, q(<), qq($base.$ext)) and open(my $out, q(>>), qq($base.$ext-reads))) "
    "{ print $out q(PWD ), getcwd(), qq(\\n), <$in>; close $out } return $r } "
    + " ".join(f"${p} = q(internal texloom_run {ext} %Y%R {p} %O %S);"
               for p, ext in (("pdflatex", "fls"), ("xelatex", "fls"), ("lualatex", "fls"), ("bibtex", "blg"), ("biber", "blg")))
    + " sub texloom_exec { return system(@_) } "
    + " ".join(f"${p} = q(internal texloom_exec {cmd});" for p, cmd in (
        ("makeindex", "makeindex %O -o %D %S"), ("xdvipdfmx", "xdvipdfmx -E -o %D %O %S"),
        ("dvipdf", "dvipdf -dALLOWPSTRANSPARENCY %O %S %D"), ("dvips", "dvips %O -o %D %S"),
        ("ps2pdf", "ps2pdf -dALLOWPSTRANSPARENCY %O %S %D")))
    + r""" { no warnings q(redefine); my $k = \&kpsewhich; """
    r"""my $bad = $^O eq q(MSWin32) ? qr/["%\x00-\x1f]/ : qr/[`\$"\\\x00-\x1f]/; """
    r"""*kpsewhich = sub { my @a = grep { !/$bad/ } @_; return () unless grep { !/^-/ } @a; return $k->(@a) } }"""
)


def latexmk_command(engine: object, bib: object, main_name: str, *extra: str) -> list[str]:
    """latexmk for a document collaborators wrote: no rc files (latexmkrc is Perl), no shell
    escape unless `extra` asks for it."""
    flag = ENGINE_FLAGS.get(str(engine), "-pdf")
    shell = [] if any(a in ("-shell-escape", "-no-shell-escape", "-shell-restricted") for a in extra) else ["-no-shell-escape"]
    return ["latexmk", "-norc", flag, "-interaction=nonstopmode", "-file-line-error", *shell,
            "-recorder", "-bibfudge-", "-e", RECORD, *(["-bibtex-"] if bib == "none" else []), *extra, f"./{main_name}"]


def use_tex_on_path() -> None:
    """Let TeX's programs (latexmk, texcount…) be found when TeXLoom was started from the Finder,
    the Dock, the Start menu or at login: such programs don't get the PATH a terminal has. Adds
    macOS's system paths (/etc/paths and /etc/paths.d, where MacTeX registers itself) and TeX
    Live's and MiKTeX's usual folders, after what PATH already holds."""
    extra: list[str] = []
    if sys.platform == "darwin":
        for f in [Path("/etc/paths"), *sorted(Path("/etc/paths.d").glob("*"))]:
            try:
                extra += [line.strip() for line in f.read_text().splitlines() if line.strip()]
            except OSError:
                continue
        extra += ["/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin"]
    elif os.name == "nt":
        local, programs = os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files")
        extra += [str(d) for d in sorted(Path("C:/texlive").glob("*/bin/*"), reverse=True)]  # newest release first
        extra += [rf"{local}\Programs\MiKTeX\miktex\bin\x64", rf"{programs}\MiKTeX\miktex\bin\x64"]
    else:
        extra += [str(d) for d in sorted(Path("/usr/local/texlive").glob("*/bin/*"), reverse=True)]
        extra += ["/usr/local/bin", "/usr/bin"]
    have = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    add = [d for d in dict.fromkeys(extra) if d not in have and os.path.isdir(d)]
    if add:
        os.environ["PATH"] = os.pathsep.join([*have, *add])


def tex_env(**more: str) -> dict[str, str]:
    """TeX may only write inside the project (paranoid openout). Paranoid openin only refuses
    dot files: reading elsewhere is stopped by texloom/confine.py."""
    return {**os.environ, "openin_any": "p", "openout_any": "p", **more}
