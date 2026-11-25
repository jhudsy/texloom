#!/usr/bin/env python3
"""Checks that a compile can't carry files from outside the project into the PDF it shares
(texloom/confine.py; the security review of 2026-11-24). Compiles small documents with latexmk as
a synced folder does (paths.latexmk_command, folder.latexmk), with the sandbox and without it
(Windows and Linux without bubblewrap have none: the check alone must catch them), and asserts
that the check fails, so that the secret never reaches a shared PDF:

- XeLaTeX's \\special{pdf:fstream …} (and an octal-escaped name), which xdvipdfmx reads unrecorded;
- \\openout of the document's own .fls, blanking TeX's record, then \\pdfobj stream file{…};
- \\input of a file outside (what the check caught before);
- a .bib or .bst from outside, which BibTeX (reading unrestricted, for its .aux in .texloom/build) reads;
- Lua's io.open of a file outside (LuaLaTeX reads with openin_any=r, which LuaTeX records);
- Lua's io.lines of a file in a home folder, which nothing records: the sandbox hides it, and
  where there's no sandbox LuaLaTeX isn't run at all;
and that ordinary documents (pdflatex, xelatex, lualatex with fontspec where there's a sandbox,
BibTeX, its citations resolved) still compile and pass.

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
from texloom import confine  # noqa: E402
from texloom.folder import latexmk  # noqa: E402
from texloom.paths import STATE, latexmk_command, use_tex_on_path  # noqa: E402

failures: list[str] = []
SECRET = "TEXLOOM-CONFINE-SECRET-7f3a"
REAL_SANDBOX = confine._sandbox


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
    confine._sandbox = REAL_SANDBOX if sandbox else (lambda: None)  # as on Windows, or Linux without bubblewrap
    try:
        code, out = await latexmk(cmd, project, build, project, timeout=300, capture=True)
    finally:
        confine._sandbox = REAL_SANDBOX
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


def uncompressed(pdf: bytes) -> bytes:
    """A PDF's bytes with its FlateDecode'd streams inflated (to look for text in them)."""
    import zlib
    out = [pdf]
    for chunk in pdf.split(b"stream")[1:]:
        try:
            out.append(zlib.decompressobj().decompress(chunk.lstrip(b"\r\n").split(b"endstream")[0]))
        except zlib.error:
            continue
    return b"".join(out)


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


async def stop_recorded(tmp: Path, outside: Path) -> None:
    """A synced folder's compile stopped for reading outside the project, or for LuaLaTeX with no
    sandbox, records it (FolderSync.stopped), which the app asks about at once (scripts/check_app.py)."""
    from types import SimpleNamespace
    from texloom.folder import CompileRefused, FolderSync
    (outside / "stop.bib").write_text("@book{s, title={T}, author={A}, year=2000, publisher={P}}\n")
    bib = ("\\documentclass{article}\\begin{document}\\nocite{*}\\bibliographystyle{plain}\\bibliography{"
           + str(outside / "stop") + "}\\end{document}\n")
    plain = "\\documentclass{article}\\begin{document}Hello\\end{document}\n"
    cases = [("plain", "pdflatex", plain, True, None)]
    if shutil.which("bibtex"):
        cases.append(("outside", "pdflatex", bib, True, "stop.bib"))
    cases.append(("lua", "lualatex", plain, False, ""))
    for name, engine, tex, sandbox, want in cases:
        project = tmp / f"stop-{name}"
        project.mkdir()
        (project / "main.tex").write_text(tex)
        fs = SimpleNamespace(
            main_file=lambda: "main.tex", root=project, _clean_build=False, stopped=None, _stops=0, _warned_shell=frozenset(),
            opts={"engine": None, "latexmk_args": [], "shell_escape": False, "confine": True, "label": "check"},
            r=SimpleNamespace(meta={"engine": engine}, files={"main.tex": tex}, dir=project / STATE, can_write=False),
            target=lambda p, project=project: project / p)
        fs.shell_mode = lambda fs=fs: FolderSync.shell_mode(fs)  # type: ignore[arg-type]
        fs._stop = lambda files, fs=fs: FolderSync._stop(fs, files)  # type: ignore[arg-type]
        confine._sandbox = REAL_SANDBOX if sandbox else (lambda: None)
        try:
            await FolderSync._run_compile(fs)  # type: ignore[arg-type]
        except CompileRefused:
            pass
        finally:
            confine._sandbox = REAL_SANDBOX
        got = fs.stopped
        if want is None:
            check("an ordinary compile records no stop", got is None, got)
        elif want:
            check("a compile that read a file outside records the stop, naming it", bool(got) and any(want in f for f in got["files"]), got)
        else:
            check("LuaLaTeX with no sandbox records the stop (no files: it didn't run)", got == {"n": 1, "files": []}, got)


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
    have = {e: bool(shutil.which(e)) for e in ("pdflatex", "xelatex", "xdvipdfmx", "bibtex", "lualatex")}
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
            # BibTeX reads its .aux from .texloom/build, which paranoid reading refuses (a dot folder):
            # citations came out as "?" in every desktop compile until 0.4.57
            check("pdflatex + BibTeX: the citation is resolved (the PDF has the entry)", ok and pdf is not None and b"2000" in uncompressed(pdf), ok)
            secret_bib = outside / "secret.bib"
            secret_bib.write_text("@book{s, title={" + SECRET + "}, author={A}, year=2000, publisher={P}}\n")
            ok, pdf, found = await compile_doc(tmp, "bibout", "pdflatex",
                                               "\\documentclass{article}\\begin{document}\\nocite{*}\\bibliographystyle{plain}\\bibliography{"
                                               + str(outside / "secret") + "}\\end{document}\n", sandbox)
            check("pdflatex + BibTeX: a .bib from outside the project fails the check, and isn't shared", found and not leaked(pdf), found)
            (outside / "other.bst").write_text(Path(os.popen("kpsewhich plain.bst").read().strip()).read_text())
            ok, pdf, found = await compile_doc(tmp, "bstout", "pdflatex",
                                               "\\documentclass{article}\\begin{document}\\cite{k}\\bibliographystyle{" + str(outside / "other")
                                               + "}\\bibliography{refs}\\end{document}\n", sandbox,
                                               {"refs.bib": "@book{k, title={T}, author={A}, year=2000, publisher={P}}\n"})
            check("pdflatex + BibTeX: a .bst from outside the project fails the check", bool(found), found)
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
        if have["lualatex"]:
            # paranoid reading refused luaotfload TeX's own files: no LuaLaTeX document compiled until 0.4.58
            ok, pdf, found = await compile_doc(tmp, "lplain", "lualatex",
                                               "\\documentclass{article}\\usepackage{fontspec}\\begin{document}Hello\\end{document}\n", sandbox)
            if sandbox:
                check("lualatex: an ordinary document (fontspec) compiles and is shared", ok and pdf is not None and not found, found)
            else:  # its Lua code could read files unrecorded (loadfile, dofile, lfs.dir): the security audit of 2026-11-25
                check("lualatex: isn't run where there's no sandbox", not ok and pdf is None, ok)
            # Lua's io.lines isn't recorded: only the sandbox (which hides home folders) stops it, so the secret is in one
            home_secret = ROOT / f".check-confine-read-{os.getpid()}"
            home_secret.write_text(SECRET + "\n")
            try:
                tex = ("\\documentclass{article}\\begin{document}hi\\directlua{local r = \"\" "
                       "for l in io.lines(\"" + str(home_secret) + "\") do r = r .. l end pdf.immediateobj(\"(\" .. r .. \")\")}"
                       "\\end{document}\n")
                ok, pdf, found = await compile_doc(tmp, "lualines", "lualatex", tex, sandbox)
                check("lualatex: Lua's io.lines of a file in a home folder (unrecorded) can't reach the shared PDF", not leaked(pdf), found)
            finally:
                home_secret.unlink(missing_ok=True)
            tex = ("\\documentclass{article}\\begin{document}\\directlua{local f = io.open(\"" + s + "\", \"rb\") "
                   "if f then tex.print(f:read(\"*a\")) f:close() end}\\end{document}\n")
            ok, pdf, found = await compile_doc(tmp, "luaopen", "lualatex", tex, sandbox)
            check("lualatex: Lua's io.open of a file outside fails the check, and isn't shared", (found or not sandbox) and not leaked(pdf), found)
        else:
            print("(lualatex isn't installed: skipped)")
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
    if have["pdflatex"]:
        print("\nwhat the app is told:")
        await stop_recorded(tmp, outside)
    print(f"\ncompile confinement: {'FAILED' if failures else 'ok'}")
    if not failures:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
