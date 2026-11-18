"""Word counts with TeXCount, which reads one file: the project's main file with its
inputs written in, so it opens nothing itself."""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

from .daemon import FolderProblem
from .i18n import _
from .paths import main_file, tex_env
from .scan import code_part
from .xr import resolve


def find_texcount() -> str | None:
    """TeXCount from this computer's TeX (found through use_tex_on_path at start)."""
    return shutil.which("texcount")


def texcount_language(text: str) -> list[str]:
    """TeXCount counts one script at a time, so the text chooses which (web/src/texcount.ts too)."""
    code = {ord(c) for c in text if ord(c) > 0x2FFF}
    if any(0x3040 <= c <= 0x30FF for c in code):
        return ["-japanese"]
    if any(0x3400 <= c <= 0x9FFF or 0x20000 <= c <= 0x2FFFF for c in code):
        return ["-chinese"]
    if any(0xAC00 <= c <= 0xD7A3 for c in code):
        return ["-korean-words"]
    return []


INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{([^{}\n]*)\}")


def with_includes(files, main: str, depth: int = 0, seen: frozenset = frozenset(), base: str | None = None) -> str:
    """The main file with its \\input and \\include files written in, taken only from the project's
    own files (resolved from the main file's folder, as LaTeX does, and as xr.py finds them).
    TeXCount then counts one file and opens nothing itself: its own -inc option opens whatever
    name a document gives it, and Perl runs a name ending in "|" as a command."""
    text = str(files[main])
    if depth > 20:
        return text
    base = posixpath.dirname(main) if base is None else base

    def expand(m: re.Match) -> str:
        before = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if code_part(before) != before:
            return m.group(0)  # commented out
        target = resolve(base, m.group(1).strip())
        for cand in ((target, target + ".tex") if target else ()):
            if cand in files and cand not in seen and cand.endswith(".tex"):
                return "\n" + with_includes(files, cand, depth + 1, seen | {main}, base) + "\n"
        return m.group(0)

    return INCLUDE_RE.sub(expand, text)


async def texcount_report(st, d: Path, exe: str) -> str:
    """Runs on the sync thread, the only place a folder's project can be read. TeXCount reads one
    temporary file, the project's main file with its inputs written in (see with_includes)."""
    import tempfile
    files, meta = st.replica.files, st.replica.meta
    main = main_file(files, meta)
    if not main:
        raise FolderProblem(_("No .tex file with \\documentclass found."))
    whole = with_includes(files, main)

    def run_it() -> str:
        with tempfile.TemporaryDirectory(prefix="texloom-count-") as tmp:
            (Path(tmp) / "main.tex").write_text(whole, encoding="utf-8")
            args = [exe, "-utf8", *texcount_language(whole), "./main.tex"]
            proc = subprocess.Popen(args, cwd=tmp, env=tex_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", start_new_session=True)
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:  # stop it, and anything it started
                try:
                    os.killpg(proc.pid, 9) if hasattr(os, "killpg") else proc.kill()
                except OSError:
                    pass
                proc.communicate()
                raise FolderProblem(_("TeXCount took too long.")) from None
            if proc.returncode:
                raise FolderProblem(err.strip()[-500:] or str(proc.returncode))
            return out

    return await asyncio.to_thread(run_it)
