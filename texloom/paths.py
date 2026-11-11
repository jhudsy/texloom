"""Which project paths may touch a disk, and how TeX is run on a project's files.

Everything here treats the shared document as written by others: file names and the
chosen main file come from collaborators.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path

STATE = ".texloom"

# Files that TeX tools read as configuration or execute (latexmk runs latexmkrc as Perl).
# They are never written from, or sent to, the shared project.
UNSAFE_NAMES = {"latexmkrc", ".latexmkrc", "texmf.cnf", ".texmf.cnf", "mktexlsr", "ls-r"}
# Folders whose contents tools act on (git hooks and config, editor settings). Keep these
# rules in sync with cleanPath() in web/src/projectfiles.ts.
PROTECTED_DIRS = {STATE, ".git", ".vscode", ".idea"}
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


ENGINE_FLAGS = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}


def latexmk_command(engine: object, bib: object, main_name: str, *extra: str) -> list[str]:
    """latexmk for a document collaborators wrote: no rc files (latexmkrc is Perl), no shell
    escape unless `extra` asks for it."""
    flag = ENGINE_FLAGS.get(str(engine), "-pdf")
    shell = [] if any(a in ("-shell-escape", "-no-shell-escape", "-shell-restricted") for a in extra) else ["-no-shell-escape"]
    return ["latexmk", "-norc", flag, "-interaction=nonstopmode", "-file-line-error", *shell,
            *(["-bibtex-"] if bib == "none" else []), *extra, f"./{main_name}"]


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
    """TeX may only read and write inside the project (paranoid openin/openout)."""
    return {**os.environ, "openin_any": "p", "openout_any": "p", **more}
