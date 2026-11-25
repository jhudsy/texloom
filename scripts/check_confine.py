#!/usr/bin/env python3
"""Checks that a compile can't carry files from outside the project into the PDF it shares
(texloom/confine.py; the security review of 2026-11-24). Compiles small documents with latexmk as
a synced folder does (paths.latexmk_command, folder.latexmk), with the sandbox and without it
(Windows and Linux without bubblewrap have none: the check alone must catch them), and asserts
that the check fails, so that the secret never reaches a shared PDF:

- XeLaTeX's \\special{pdf:fstream …} (and an octal-escaped name), which xdvipdfmx reads unrecorded;
- \\openout of the document's own .fls, blanking TeX's record, then \\pdfobj stream file{…};
- \\input of a file outside (what the check caught before);
and that ordinary documents (pdflatex, xelatex, BibTeX) still compile and pass.

Skips (successfully) what needs a TeX that isn't installed.

    uv run python scripts/check_confine.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from texloom.confine import outside_reads, forget_reads  # noqa: E402
from texloom.folder import latexmk  # noqa: E402
from texloom.paths import STATE, latexmk_command, use_tex_on_path  # noqa: E402

failures: list[str] = []
SECRET = "TEXLOOM-CONFINE-SECRET-7f3a"


def check(what: str, cond: object, detail: object = "") -> None:
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"), flush=True)
    if not cond:
        failures.append(what)


def octal(path: str) -> str:
    return "".join(f"\\{ord(c):03o}" for c in path)


async def compile_doc(tmp: Path, name: str, engine: str, tex: str, sandbox: bool,
                      extra: dict[str, str] | None = None, shell: tuple[str, ...] = ("-no-shell-escape",),
                      project: Path | None = None) -> tuple[bool, bytes | None, list[str]]:
    """(compiled, the PDF a synced folder would share, what the check found)."""
    project = project or tmp / f"{name}-{'sandbox' if sandbox else 'nosandbox'}"
    project.mkdir(exist_ok=True)
    (project / "main.tex").write_text(tex)
    for p, t in (extra or {}).items():
        (project / p).write_text(t)
    build = project / STATE / "build"
    build.mkdir(parents=True)
    forget_reads(build)
    cmd = latexmk_command(engine, None, "main.tex", f"-outdir={build}", *shell)
    code, out = await latexmk(cmd, project, build, project, confine=sandbox, timeout=300, capture=True)
    pdf = build / "main.pdf"
    outside = outside_reads(project, build)
    shared = pdf.read_bytes() if pdf.exists() and not outside else None  # as folder.py decides
    if code != 0 and not outside and os.environ.get("VERBOSE"):
        print(out.decode("utf-8", "replace")[-2000:])
    if outside and leaked(pdf.read_bytes() if pdf.exists() else None):
        print("  (the PDF built, not shared, held the secret: the check is what stopped it)")
    return code == 0 and pdf.exists(), shared, outside


def build_marker(tmp: Path) -> Path:
    """A file the shell-escape document writes in the temporary folder (allowed, so the command ran)."""
    return tmp / "write-sandbox" / "ran"


def leaked(pdf: bytes | None) -> bool:
    if pdf is None:
        return False
    import zlib
    if SECRET.encode() in pdf:
        return True
    for chunk in pdf.split(b"stream")[1:]:  # FlateDecode'd streams
        body = chunk.lstrip(b"\r\n").split(b"endstream")[0]
        try:
            if SECRET.encode() in zlib.decompressobj().decompress(body):
                return True
        except zlib.error:
            continue
    return False


async def shell_escape(tmp: Path) -> None:
    """An approved line runs its program; a command built from \\csname, which the scan for lines
    to approve doesn't see, can't run another (the security review of 2026-11-24)."""
    from types import SimpleNamespace
    os.environ["TEXLOOM_HOME"] = str(tmp / "home")
    from texloom import config
    from texloom.crypto import Keys
    from texloom.folder import FolderSync
    from texloom.scan import allow, shell_lines
    project = tmp / "shell"
    project.mkdir()
    config.save(project, Keys.generate().link("https://texloom.invalid"))
    tex = ("\\documentclass{article}\\begin{document}hi\n"
           "\\immediate\\write18{touch ran-approved}\n"
           "\\csname immediate\\endcsname\\csname write\\endcsname18{mkdir pwned-hidden}\n\\end{document}\n")
    files = {"main.tex": tex}
    allow(project, {f.key for f in shell_lines(files)})
    fs = SimpleNamespace(opts={"shell_escape": True}, r=SimpleNamespace(files=files, bin={}), root=project,
                         _warned_shell=frozenset())
    shell, _note = FolderSync.shell_mode(fs)  # type: ignore[arg-type]
    shell = [shell] if isinstance(shell, str) else shell  # (as before: one flag)
    await compile_doc(tmp, "shell", "pdflatex", tex, False, shell=tuple(shell), project=project)
    check("the approved line's program runs", (project / "ran-approved").exists() or (project / STATE / "build" / "ran-approved").exists(),
          shell)
    check("a command built from \\csname doesn't run another program",
          not (project / "pwned-hidden").exists() and not (project / STATE / "build" / "pwned-hidden").exists(), shell)


async def main() -> int:
    use_tex_on_path()
    if not shutil.which("latexmk"):
        print("latexmk isn't installed: skipped")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="texloom-confine-")).resolve()
    outside = tmp / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(SECRET + "\n")
    s = str(secret)
    have = {e: bool(shutil.which(e)) for e in ("pdflatex", "xelatex", "xdvipdfmx", "bibtex")}
    modes = [False, True] if sys.platform in ("darwin", "linux") else [False]

    for sandbox in modes:
        how = "with the sandbox" if sandbox else "without a sandbox (the check alone)"
        print(f"\n{how}:")
        if have["pdflatex"]:
            ok, pdf, found = await compile_doc(tmp, "plain", "pdflatex", "\\documentclass{article}\\begin{document}Hello\\end{document}\n", sandbox)
            check("pdflatex: an ordinary document compiles and is shared", ok and pdf is not None and not found, found)
            ok, pdf, found = await compile_doc(tmp, "input", "pdflatex",
                                               "\\documentclass{article}\\begin{document}\\input{" + s + "}\\end{document}\n", sandbox)
            check("pdflatex: \\input of a file outside isn't shared (refused by TeX, or the check fails)", not leaked(pdf), found)
            tex = ("\\documentclass{article}\\begin{document}hi\n"
                   "\\immediate\\pdfobj stream file {" + s + "}\\pdfrefobj\\pdflastobj\n"
                   "\\immediate\\openout5=main.fls \\immediate\\closeout5\n\\end{document}\n")
            ok, pdf, found = await compile_doc(tmp, "blank", "pdflatex", tex, sandbox)
            check("pdflatex: blanking its own .fls, then \\pdfobj stream file{outside}, fails the check",
                  found and not leaked(pdf), found)
        if have["bibtex"] and have["pdflatex"]:
            ok, pdf, found = await compile_doc(tmp, "bib", "pdflatex",
                                               "\\documentclass{article}\\begin{document}\\cite{k}\\bibliographystyle{plain}\\bibliography{refs}\\end{document}\n",
                                               sandbox, {"refs.bib": "@book{k, title={T}, author={A}, year=2000, publisher={P}}\n"})
            check("pdflatex + BibTeX: an ordinary document passes the check", not found, found)
        if have["xelatex"] and have["xdvipdfmx"]:
            ok, pdf, found = await compile_doc(tmp, "xplain", "xelatex", "\\documentclass{article}\\begin{document}Hello\\end{document}\n", sandbox)
            check("xelatex: an ordinary document compiles and is shared", ok and pdf is not None and not found, found)
            for label, name in (("", s), (" (its name octal-escaped)", "\\detokenize{" + octal(s) + "}")):
                tex = ("\\documentclass{article}\\begin{document}hello\n"
                       "\\special{pdf:fstream @foo (" + name + ")}\\special{pdf:put @catalog << /Foo @foo >>}\n\\end{document}\n")
                ok, pdf, found = await compile_doc(tmp, "fstream" + str(len(label)), "xelatex", tex, sandbox)
                check(f"xelatex: \\special{{pdf:fstream}} of a file outside{label} fails the check, and isn't shared",
                      found and not leaked(pdf), found)
        else:
            print("(xelatex isn't installed: skipped)")
        if sandbox and sys.platform == "darwin" and have["pdflatex"]:
            # a program the document runs (shell escape on) writes outside: in a folder that is
            # neither temporary nor TeX's (beside this script, removed afterwards)
            victim = ROOT / f".check-confine-{os.getpid()}"
            victim.mkdir()
            try:
                tex = ("\\documentclass{article}\\begin{document}hi\n"
                       f"\\immediate\\write18{{touch {victim}/written; touch {build_marker(tmp)}}}\n\\end{{document}}\n")
                ok, pdf, found = await compile_doc(tmp, "write", "pdflatex", tex, sandbox, shell=("-shell-escape",))
                check("macOS sandbox: a compile can't write outside the project, its build folder, TeX's caches "
                      "and the temporary folders", ok and not (victim / "written").exists() and build_marker(tmp).exists(),
                      (ok, sorted(p.name for p in victim.iterdir())))
            finally:
                shutil.rmtree(victim, ignore_errors=True)
    if have["pdflatex"]:
        print("\nshell_escape true (approved lines only):")
        await shell_escape(tmp)
    print(f"\ncompile confinement: {'FAILED' if failures else 'ok'}")
    if not failures:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
