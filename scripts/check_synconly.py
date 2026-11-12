#!/usr/bin/env python3
"""End-to-end check of sync-only projects: two computers, nothing kept on the server.

Starts its own throwaway `texloom-server` that keeps projects only with a creation code, so a
project made without one is sync-only, as on app.texloom.org. Pairs of "computers" (each
`texloom run` with its own TEXLOOM_HOME and folder, as separate processes) then share a project
and are driven through their folders, the way people use the desktop app.

Each computer plays an operating system for line endings (this runs on one machine, so that is
all that differs): a "windows" one has files saved with \\r\\n line endings, and its editor saves
them that way, as Notepad or TeXstudio do; a "unix" one uses \\n. Every pairing is run, the first
named making the project and the second joining it: unix/unix, windows/windows, windows/unix and
unix/windows. Not simulated: Windows file systems themselves (locking, case, path rules).

  uv run python scripts/check_synconly.py                  # every pairing, about two minutes
  uv run python scripts/check_synconly.py windows/unix     # just these
  uv run python scripts/check_synconly.py --keep           # leave the folders and logs to look at

Checked for each: the project is made sync-only and the server stores nothing of it; the joining
computer gets every file (text, a picture, a file over 8 MB); edits go both ways; edits made at
once on both sides, and while one computer was off, all arrive; a burst of edits on both sides
ends the same on both; new pictures, renames and deletions arrive. Throughout: the text is the
same on both apart from line endings and never scrambled, files a "windows" computer saved keep
\\r\\n, and a "unix" computer never gets a \\r.

The web app's editor isn't part of this (it needs a browser); see DESIGN.md, Folder sync, on
line endings, for what went wrong there.
"""
import argparse
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = [sys.executable, "-c"]
CLI = "import sys; from texloom.cli import main; sys.argv[0] = 'texloom'; main()"
SERVER = "import sys; from server.relay import main; sys.argv[0] = 'texloom-server'; main()"
PAIRS = ["unix/unix", "windows/windows", "windows/unix", "unix/windows"]

MAIN = (
    "\\documentclass{article}\n\\usepackage{booktabs}\n\\begin{document}\n"
    "\\section{Intro}\nThe first paragraph.\n\n"
    "\\begin{tabular}{l|rrrr|rrrr|}\n\\toprule\nA & 1 & 2 & 3 & 4 & 5 & 6 & 7 & 8\\\\\n\\bottomrule\n\\end{tabular}\n\n"
    "The middle paragraph.\n\nThe last paragraph.\n\\input{chapter}\n\\end{document}\n"
)
KEEP_INTACT = ["\\begin{tabular}{l|rrrr|rrrr|}", "\\bottomrule", "\\toprule"]
TEXT_FILES = ["main.tex", "chapter.tex", "refs.bib"]


class Failed(Exception):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(what: str, ok, secs: float = 60) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        try:
            if ok():
                return
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        time.sleep(0.25)
    raise Failed(f"timed out after {secs:g}s: {what}")


def lf(s: str) -> str:
    return s.replace("\r\n", "\n")


class Computer:
    def __init__(self, name: str, system: str, tmp: Path):
        self.name, self.system = f"{name} ({system})", system
        self.home, self.dir = tmp / f"home-{name}", tmp / name
        self.home.mkdir()
        self.log = tmp / f"run-{name}.log"
        self.proc: subprocess.Popen | None = None
        self.saved: set[str] = set()  # text files this computer's editor saved (or made)

    @property
    def env(self) -> dict:
        return {**os.environ, "TEXLOOM_HOME": str(self.home), "PYTHONIOENCODING": "utf-8"}

    def texloom(self, *args: str, timeout: float = 180) -> str:
        p = subprocess.run(PY + [CLI, *args], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)
        if p.returncode:
            raise Failed(f"{self.name}: texloom {args[0]} failed:\n{p.stdout}{p.stderr}")
        return p.stdout + p.stderr

    def start(self) -> None:
        self.proc = subprocess.Popen(PY + [CLI, "run", str(self.dir)], cwd=ROOT, env=self.env,
                                     stdout=open(self.log, "a"), stderr=subprocess.STDOUT)  # noqa: SIM115 - open while it runs
        wait(f"{self.name} connects", lambda: "connected to" in self.log.read_text(errors="replace"), 30)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def file(self, rel: str) -> Path:
        return self.dir / rel

    def read(self, rel: str) -> str:
        return self.file(rel).read_bytes().decode("utf-8")

    def save(self, rel: str, content: str) -> None:
        """Save a text file as this computer's editor would: \\r\\n line endings on "windows"."""
        content = lf(content)
        self.file(rel).write_bytes((content.replace("\n", "\r\n") if self.system == "windows" else content).encode("utf-8"))
        self.saved.add(rel)

    def edit(self, rel: str, old: str, new: str) -> None:
        s = lf(self.read(rel))
        if old not in s:
            raise Failed(f"{self.name}: {rel}: {old!r} isn't there to change")
        self.save(rel, s.replace(old, new, 1))

    def has(self, rel: str, *bits: str) -> bool:
        return self.file(rel).exists() and all(b in self.read(rel) for b in bits)


def same(a: Computer, b: Computer, rel: str) -> bool:
    return a.file(rel).exists() and b.file(rel).exists() and lf(a.read(rel)) == lf(b.read(rel))


def sound(*cs: Computer) -> None:
    """Nothing scrambled, and line endings as each computer's system would have them."""
    for c in cs:
        if c.file("main.tex").exists():
            s = c.read("main.tex")
            for line in KEEP_INTACT:
                if line not in s:
                    raise Failed(f"{c.name}: main.tex lost {line!r} (text scrambled?):\n{s[:600]}")
        for rel in TEXT_FILES:
            p = c.file(rel)
            if not p.exists():
                continue
            d = p.read_bytes()
            if c.system == "unix" and b"\r" in d:
                raise Failed(f"{c.name}: {rel} got \\r characters: {d[:200]!r}")
            if b"\r\r" in d or (b"\r" in d and d.count(b"\r\n") != d.count(b"\n")):
                raise Failed(f"{c.name}: {rel} has mixed or doubled line endings: {d[:200]!r}")
            if c.system == "windows" and rel in c.saved and d.count(b"\r\n") != d.count(b"\n"):
                raise Failed(f"{c.name}: {rel}, saved here with \\r\\n, lost them: {d[:200]!r}")


def pairing(name: str, url: str, tmp: Path, server_db: Path, step) -> None:
    first, second = name.split("/")
    a, b = Computer("A", first, tmp), Computer("B", second, tmp)
    rng = random.Random(7)
    try:
        a.dir.mkdir()
        a.save("main.tex", MAIN)
        a.save("chapter.tex", "Un chapitre écrit à Paris, naïvement.\n")
        a.save("refs.bib", "@book{key, title={A Book}, year={2026}}\n")
        a.file("fig.png").write_bytes(b"\x89PNG\r\n\x1a\n" + rng.randbytes(300_000))
        a.file("big.tex").write_text("".join(f"% line {i} {'x' * 70}\n" for i in range(120_000)))  # about 9 MB
        a.texloom("new", str(a.dir), "--server", url, "--name", f"check {name}")
        link = next(w for w in a.texloom("links", str(a.dir)).split() if "#p=" in w and "&w=" in w)
        project = link.split("#p=")[1].split("&")[0]
        a.start()
        b.texloom("clone", link, str(b.dir))
        b.start()
        for rel in [*TEXT_FILES, "big.tex"]:
            wait(f"B has {rel}", lambda rel=rel: same(a, b, rel), 120)
        wait("B has fig.png", lambda: b.file("fig.png").read_bytes() == a.file("fig.png").read_bytes(), 120)
        sound(a, b)
        step("the joining computer gets every file (a 9 MB one and a picture too)")

        a.edit("main.tex", "The middle paragraph.", "The middle paragraph, edited on A.")
        wait("A's edit reaches B", lambda: b.has("main.tex", "edited on A"))
        b.edit("main.tex", "The last paragraph.", "The last paragraph, edited on B.")
        wait("B's edit reaches A", lambda: a.has("main.tex", "edited on B"))
        sound(a, b)
        step("edits go both ways")

        a.edit("main.tex", "The first paragraph.", "The first paragraph, now on A too.")
        b.edit("main.tex", "edited on B.", "edited on B twice.")
        wait("edits made at once both arrive", lambda: all(c.has("main.tex", "now on A too", "edited on B twice") for c in (a, b)))
        sound(a, b)
        step("edits made at the same time on both computers both arrive")

        lines = [f"% burst {i}" for i in range(24)]
        for i, line in enumerate(lines):  # each side in turn, as soon as the other's has arrived
            c = (a, b)[i % 2]
            if i:  # (writing before then would be an editor saving a stale copy: not what's checked here)
                wait(f"{lines[i - 1]} reaches {c.name}", lambda c=c, i=i: c.has("main.tex", lines[i - 1]), 30)
            c.edit("main.tex", "The middle paragraph", f"{line}\nThe middle paragraph")
        wait("the burst ends the same on both", lambda: same(a, b, "main.tex") and a.has("main.tex", *lines), 90)
        sound(a, b)
        step("a burst of edits on both sides ends the same on both")

        b.stop()
        a.edit("chapter.tex", "naïvement.", "naïvement. A wrote this while B was off.")
        time.sleep(1)
        b.edit("chapter.tex", "Un chapitre", "Un chapitre (B, offline)")
        b.start()
        wait("both offline edits are on both", lambda: all(c.has("chapter.tex", "while B was off", "(B, offline)") for c in (a, b)), 90)
        sound(a, b)
        step("edits made while one computer was off arrive when it's back, both kept")

        pic = b"\x89PNG\r\n\x1a\n" + rng.randbytes(2_000_000)
        b.file("photo.png").write_bytes(pic)
        wait("B's new picture reaches A", lambda: a.file("photo.png").read_bytes() == pic, 90)
        a.file("chapter.tex").rename(a.file("chapter1.tex"))
        a.saved = {"chapter1.tex" if r == "chapter.tex" else r for r in a.saved}
        a.edit("main.tex", "\\input{chapter}", "\\input{chapter1}")
        a.file("refs.bib").unlink()
        wait("the rename and the deletion reach B", lambda: b.file("chapter1.tex").exists() and not b.file("chapter.tex").exists()
             and not b.file("refs.bib").exists() and b.has("chapter1.tex", "(B, offline)"), 60)
        b.edit("chapter1.tex", "Un chapitre", "Un chapitre renommé")
        wait("an edit to the renamed file reaches A", lambda: a.has("chapter1.tex", "renommé"))
        sound(a, b)
        step("a 2 MB picture, a rename (then an edit to it) and a deletion arrive")

        db = sqlite3.connect(server_db)
        stored = [db.execute(f"SELECT COUNT(*) FROM {t} WHERE project=?", (project,)).fetchone()[0] for t in ("log", "blob")]
        mode = db.execute("SELECT relay FROM mode WHERE project=?", (project,)).fetchone()
        db.close()
        if stored != [0, 0] or not (mode and mode[0]):
            raise Failed(f"the server kept something: {stored[0]} changes, {stored[1]} files, mode {mode}")
        step("the server kept nothing of the project (sync-only)")
    finally:
        for c in (a, b):
            c.stop()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # a Windows console's own code page has no ✓
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="*", choices=PAIRS, metavar="PAIR", help=f"which to run: {', '.join(PAIRS)} (default: all)")
    ap.add_argument("--keep", action="store_true", help="keep the folders, logs and database afterwards")
    args = ap.parse_args()
    tmp = Path(os.environ.get("CHECK_DIR") or tempfile.mkdtemp(prefix="texloom-synconly-"))
    tmp.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    (tmp / "codes.txt").write_text("CHECK-ONLY-NOT-A-REAL-CODE 1G check\n")  # stores projects only with a code
    server = subprocess.Popen(PY + [SERVER, "--port", str(port), "--db", str(tmp / "relay.db"), "--codes-file", str(tmp / "codes.txt")],
                              cwd=ROOT, stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115 - open while it runs
    failed: list[str] = []
    try:
        wait("the server starts", lambda: urllib.request.urlopen(url + "/api/info", timeout=2).status == 200, 30)
        for name in args.pairs or PAIRS:
            print(f"{name}:", flush=True)
            where = tmp / name.replace("/", "-")
            where.mkdir()
            try:
                pairing(name, url, where, tmp / "relay.db", lambda msg: print(f"  ✓ {msg}", flush=True))
            except Failed as e:
                print(f"  ✗ {e}", flush=True)
                failed.append(name)
    except Failed as e:
        print(f"✗ {e}", flush=True)
        failed.append("setup")
    finally:
        server.terminate()
        server.wait(10)
        if failed or args.keep:
            print(f"folders, logs and the server's database: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"sync-only: FAILED ({', '.join(failed)})" if failed else "sync-only: all checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
