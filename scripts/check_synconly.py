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

The web app is a computer too, in unix/browser and windows/browser: headless Chromium (Playwright)
runs it from the throwaway server (web/dist, so build it first) and types into its editor with real
keypresses, against a desktop playing that system. It compiles in the browser if tex-assets/ is set
up (CHECK_NO_TEX=1 to skip that); either way one ⌘S must start exactly one compile.

  uv run --with playwright python scripts/check_synconly.py           # every pairing, a few minutes
  uv run python scripts/check_synconly.py windows/unix                # just these (no Playwright needed)
  uv run --with playwright python scripts/check_synconly.py --keep    # leave the folders and logs
  uv run --with playwright python -m playwright install chromium      # once, for the browser pairings

Checked for the desktop pairings: the project is made sync-only and the server stores nothing of
it; the joining computer gets every file (text, a picture, a file over 8 MB); edits go both ways;
edits made at once on both sides, and while one computer was off, all arrive; a burst of edits on
both sides ends the same on both; slow saves and a big file copied in pieces never reach the other
computer cut off; new pictures, renames and deletions arrive. Throughout: the text is the same on
both apart from line endings and never scrambled, files a "windows" computer saved keep \\r\\n, and
a "unix" computer never gets a \\r.

For the browser pairings: the web app joins and gets every file; text typed in the browser lands
exactly where it was typed on the desktop (before 0.4.21, a Windows file's \\r\\n made it land a
character off per line above); the desktop's saves reach the editor, which shows exactly the shared
text; edits at once and taking turns; 300 random keypresses (emoji too) and a Windows paste end
identical everywhere, with no \\r or half characters; one ⌘S starts one compile, and quick presses
never two at once; the server keeps nothing.
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
BROWSER_PAIRS = ["unix/browser", "windows/browser"]  # the desktop makes the project, the web app joins

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

        # an editor that saves by emptying the file, then writing it (a moment later); and copying a
        # big file in, a piece at a time: the other computer must never get a cut-off file
        seen_cut: list[str] = []
        def watch_b(until: float, big: bytes | None = None) -> None:
            while time.monotonic() < until:
                try:
                    s = b.read("main.tex")
                    if any(x not in s for x in KEEP_INTACT):
                        seen_cut.append(f"main.tex with {len(s)} characters")
                    if big is not None and b.file("photo-big.png").exists():
                        z = b.file("photo-big.png").stat().st_size
                        if 0 < z != len(big):
                            seen_cut.append(f"photo-big.png at {z} of {len(big)} bytes")
                except (OSError, UnicodeDecodeError):
                    pass
                time.sleep(0.02)
        import threading
        w = threading.Thread(target=watch_b, args=(time.monotonic() + 6,))
        w.start()
        for i in range(5):
            content = lf(a.read("main.tex")).replace("The middle paragraph", f"% slow save {i}\nThe middle paragraph", 1)
            with open(a.file("main.tex"), "wb") as f:  # emptied now, written in a moment
                f.flush()
                time.sleep(0.15)
                f.write((content.replace("\n", "\r\n") if a.system == "windows" else content).encode())
            time.sleep(0.4)
        w.join()
        wait("the slow saves reach B", lambda: b.has("main.tex", "% slow save 4"), 30)
        big = b"\x89PNG\r\n\x1a\n" + rng.randbytes(20_000_000)
        w = threading.Thread(target=watch_b, args=(time.monotonic() + 15, big))
        w.start()
        with open(a.file("photo-big.png"), "wb") as f:
            for k in range(0, len(big), 2_000_000):
                f.write(big[k:k + 2_000_000])
                f.flush()
                time.sleep(0.1)
        wait("the big picture reaches B whole", lambda: b.file("photo-big.png").exists() and b.file("photo-big.png").read_bytes() == big, 60)
        w.join()
        if seen_cut:
            raise Failed(f"B got a cut-off file: {seen_cut[:3]}")
        sound(a, b)
        step("slow saves and a 20 MB file copied in pieces arrive whole, never cut off")

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


# ---- the web app as one of the computers -------------------------------------------------------
# Headless Chromium (Playwright) runs the real web app from the throwaway server and types into its
# editor with real keypresses. A test hook (main.ts, localhost only, when texloom.testHooks is set)
# lets this read the shared text and the editor's text, and open files.

ENGINE_LOG = """
localStorage.setItem('texloom.testHooks', '1');
window.__engine = [];
const post = Worker.prototype.postMessage;
Worker.prototype.postMessage = function (m, ...rest) { if (m && m.main_tex_path) __engine.push('compile'); return post.call(this, m, ...rest) };
const desc = Object.getOwnPropertyDescriptor(Worker.prototype, 'onmessage') || Object.getOwnPropertyDescriptor(EventTarget.prototype, 'onmessage');
Object.defineProperty(Worker.prototype, 'onmessage', { configurable: true, get() { return this.__om }, set(fn) { this.__om = fn;
  desc.set.call(this, (e) => { if (e.data && e.data.pdf !== undefined) __engine.push(e.data.exit_code === 0 ? 'ok' : 'error'); return fn(e) }) } });
"""


class Browser:
    """The web app in headless Chromium, as a computer: same reading helpers as Computer."""
    system = "browser"

    def __init__(self, tmp: Path):
        self.name, self.tmp, self.saved = "B (browser)", tmp, set()
        self.pw = self.browser = self.page = None
        self.alerts: list[str] = []

    def start(self, link: str, compile_here: bool) -> None:
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        self.page = self.browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        self.page.add_init_script(ENGINE_LOG)
        # recorded here: the page can't run anything while it shows an alert
        self.page.on("dialog", lambda d: (self.alerts.append(d.message), d.accept()))
        self.page.goto(link)
        self.page.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)
        self.page.fill("dialog[open] input[placeholder='Shown to collaborators']", "Browser")
        self.page.locator("dialog[open] label.radio", has_text="In this browser" if compile_here else "Don’t compile here").last.click()
        self.page.click("dialog[open] button:has-text('Start')")
        self.page.wait_for_function("() => !!window.__texloom", timeout=30000)

    def stop(self) -> None:
        for x in (self.browser, self.pw):
            try:
                if x is self.pw and x:
                    x.stop()
                elif x:
                    x.close()
            except Exception:  # noqa: BLE001, S110 - shutting down: nothing to do about it
                pass

    # the shared text, as this browser has it
    def text(self, rel: str) -> str | None:
        return self.page.evaluate("p => __texloom.r.files.get(p)?.toString() ?? null", rel)

    def has(self, rel: str, *bits: str) -> bool:
        t = self.text(rel)
        return t is not None and all(b in t for b in bits)

    def binary(self, rel: str) -> int | None:
        return self.page.evaluate("p => __texloom.r.bin.get(p)?.size ?? null", rel)

    def open(self, rel: str) -> None:
        self.page.evaluate("p => __texloom.open(p)", rel)
        wait(f"{rel} opens in the editor", lambda: lf(self.editor() or "") == lf(self.text(rel) or "\0"), 15)

    def editor(self) -> str | None:
        return self.page.evaluate("() => __texloom.view()?.state.doc.toString() ?? null")

    def cursor_after(self, needle: str, at_start: bool = False) -> None:
        """Put the cursor just after `needle` (or at its start) in the open file, as a click would."""
        s = self.editor() or ""
        i = s.find(needle)
        if i < 0:
            raise Failed(f"browser: {needle!r} isn't in the editor")
        pos = len(s[:i if at_start else i + len(needle)].encode("utf-16-le")) // 2
        self.page.evaluate("p => { const v = __texloom.view(); v.focus(); v.dispatch({ selection: { anchor: p } }) }", pos)

    def type(self, text: str) -> None:
        """Real keypresses: Enter for line breaks."""
        for i, part in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Enter")
            if part:
                self.page.keyboard.type(part)

    def paste(self, text: str) -> None:
        self.page.evaluate("""t => { const dt = new DataTransfer(); dt.setData('text/plain', t);
            document.querySelector('.cm-content').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })) }""", text)

    def in_step(self, rel: str) -> bool:
        """The editor shows exactly the shared text (they drifted apart with \\r\\n before 0.4.21)."""
        return self.editor() == self.text(rel)


def lone_halves(s: str) -> int:
    return sum(1 for c in s if 0xD800 <= ord(c) <= 0xDFFF)


def browser_pairing(name: str, url: str, tmp: Path, server_db: Path, step) -> None:
    system = name.split("/")[0]
    a, b = Computer("A", system, tmp), Browser(tmp)
    rng = random.Random(11)
    compile_here = (ROOT / "tex-assets" / "busytex").is_dir() and not os.environ.get("CHECK_NO_TEX")
    try:
        a.dir.mkdir()
        a.save("main.tex", MAIN)
        a.save("storm.tex", "% not part of the document: random keypresses go here\nStorm.\n")
        a.save("chapter.tex", "Un chapitre écrit à Paris, naïvement.\n")  # (no emoji: pdfLaTeX can't typeset one)
        a.file("fig.png").write_bytes(b"\x89PNG\r\n\x1a\n" + rng.randbytes(300_000))
        a.texloom("new", str(a.dir), "--server", url, "--name", f"check {name}")
        link = next(w for w in a.texloom("links", str(a.dir)).split() if "#p=" in w and "&w=" in w)
        project = link.split("#p=")[1].split("&")[0]
        a.start()
        b.start(link, compile_here)
        for rel in ("main.tex", "chapter.tex"):
            wait(f"the browser has {rel}", lambda rel=rel: lf(b.text(rel) or "") == lf(a.read(rel)), 60)
        wait("the browser has fig.png", lambda: b.binary("fig.png") == 300_008, 60)
        step("the browser joins and gets every file")

        # typed in the browser: lands exactly where it was typed on the desktop
        b.open("main.tex")
        before = lf(a.read("main.tex"))
        b.cursor_after("The middle paragraph.")
        b.type(" Typed in the browser.")
        want = before.replace("The middle paragraph.", "The middle paragraph. Typed in the browser.", 1)
        wait("the browser's typing reaches the desktop, in place", lambda: lf(a.read("main.tex")) == want)
        if "\r" in (b.text("main.tex") or ""):
            raise Failed("the shared text has \\r (the editor counts line breaks differently from it: typing lands off)")
        sound(a)
        step("typing in the browser lands exactly where it was typed on the desktop")

        a.edit("main.tex", "The last paragraph.", "The last paragraph, saved on the desktop.")
        wait("the desktop's save reaches the browser", lambda: b.has("main.tex", "saved on the desktop"))
        wait("the editor shows exactly the shared text", lambda: b.in_step("main.tex"), 15)
        b.cursor_after("The first paragraph.")
        b.type(" Browser, at once.")
        a.edit("main.tex", "saved on the desktop.", "saved on the desktop, at once.")
        wait("edits made at once both arrive", lambda: b.has("main.tex", "Browser, at once.", "desktop, at once.")
             and "Browser, at once." in a.read("main.tex"))
        wait("the editor is in step", lambda: b.in_step("main.tex"), 15)
        sound(a)
        step("the desktop's saves reach the editor, and edits made at once both arrive")

        lines = [f"% turn {i}" for i in range(16)]
        for i, line in enumerate(lines):  # each side in turn, as soon as the other's has arrived
            if i:
                prev = lines[i - 1]
                wait(f"{prev} arrives", lambda prev=prev, i=i: (b.has("main.tex", prev) and b.in_step("main.tex")) if i % 2 == 0 else prev in a.read("main.tex"), 30)
            if i % 2 == 0:
                b.cursor_after("The middle paragraph", at_start=True)
                b.type(line + "\n")
            else:
                a.edit("main.tex", "The middle paragraph", f"{line}\nThe middle paragraph")
        wait("the turns end the same on both", lambda: lf(a.read("main.tex")) == b.text("main.tex") and all(x in a.read("main.tex") for x in lines), 60)
        sound(a)
        step("taking turns, browser and desktop, ends the same on both")

        # a storm of real keypresses in a file of its own: typing, Backspace, Enter, arrows, emoji
        wait("the browser has storm.tex", lambda: b.text("storm.tex") is not None, 30)
        b.open("storm.tex")
        b.cursor_after("Storm.")
        for i in range(300):
            k = rng.randrange(10)
            if k < 5:
                b.page.keyboard.type(rng.choice(["the ", "naïve ", "café ", "x", "😀", "\\cite{k} ", "𝒜 "]))
            elif k < 7:
                b.page.keyboard.press("Backspace")
            elif k < 8:
                b.page.keyboard.press("Enter")
            else:
                b.page.keyboard.press(rng.choice(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "End"]))
        b.paste("pasted from Windows\r\nsecond line\r\n")
        wait("the storm reaches the desktop", lambda: lf(a.read("storm.tex")) == b.text("storm.tex") and b.in_step("storm.tex"), 60)
        shared = b.text("storm.tex") or ""
        if "\r" in shared or lone_halves(shared):
            raise Failed(f"the shared text has \\r or half characters after the storm: {shared[-200:]!r}")
        sound(a)
        step("300 random keypresses and a Windows paste: desktop, editor and shared text identical")

        # ⌘S / Ctrl+S: one compile at a time, however often it's pressed
        b.page.evaluate("() => { __engine.length = 0 }")
        b.alerts.clear()
        b.open("main.tex")
        b.cursor_after("The first paragraph")
        idle = "() => __engine.length > 0 && __engine.filter(x => x === 'compile').length === __engine.filter(x => x !== 'compile').length"
        if compile_here:  # one press, one compile (before 0.4.23 the page's own ⌘S handler compiled a second time)
            b.page.keyboard.press("ControlOrMeta+s")
            wait("a compile finishes", lambda: b.page.evaluate(idle), 180)
            time.sleep(3)
            once = b.page.evaluate("() => __engine.filter(x => x === 'compile').length")
            if once != 1:
                raise Failed(f"one ⌘S press started {once} compiles")
            b.page.evaluate("() => { __engine.length = 0 }")
        if compile_here:
            for _ in range(3):
                b.page.keyboard.press("ControlOrMeta+s")
                time.sleep(0.3)
            wait("the compiles finish", lambda: b.page.evaluate(idle), 180)
            time.sleep(3)  # a queued compile would start now
            wait("the compiles finish", lambda: b.page.evaluate("() => __engine.filter(x => x === 'compile').length === __engine.filter(x => x !== 'compile').length"), 180)
            log = b.page.evaluate("() => [...__engine]")
            depth = worst = 0
            for x in log:
                depth += 1 if x == "compile" else -1
                worst = max(worst, depth)
            alerts = list(b.alerts)
            # quick presses: never two compiles at once (a press during a compile queues one more)
            if worst > 1 or alerts or "error" in log:
                tail = b.page.evaluate("() => [...document.querySelectorAll('pre, .logbox, [class*=log]')].map(e => e.innerText).sort((x, y) => y.length - x.length)[0]?.slice(-600) ?? ''")
                raise Failed(f"compiling: one at a time, no alerts or errors expected: {log} {alerts}\n{tail}")
            step(f"⌘S: one press, one compile; three quick presses, {log.count('compile')} compiles, one at a time; no alerts")
        else:
            b.page.keyboard.press("ControlOrMeta+s")
            time.sleep(2)
            alerts = list(b.alerts)
            if len(alerts) != 1:  # no TeX here: a compile says so once; twice meant one ⌘S compiled twice
                raise Failed(f"⌘S: one compile attempt expected, got {len(alerts)}: {alerts}")
            step("⌘S compiles once per press (not compiling here: tex-assets/ isn't set up, or CHECK_NO_TEX)")

        db = sqlite3.connect(server_db)
        stored = [db.execute(f"SELECT COUNT(*) FROM {t} WHERE project=?", (project,)).fetchone()[0] for t in ("log", "blob")]
        db.close()
        if stored != [0, 0]:
            raise Failed(f"the server kept something: {stored[0]} changes, {stored[1]} files")
        step("the server kept nothing of the project (sync-only)")
    finally:
        b.stop()
        a.stop()


def playwright_ready() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # a Windows console's own code page has no ✓
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="*", choices=PAIRS + BROWSER_PAIRS, metavar="PAIR",
                    help=f"which to run: {', '.join(PAIRS + BROWSER_PAIRS)} (default: all; the browser ones need Playwright)")
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
        for name in args.pairs or PAIRS + BROWSER_PAIRS:
            print(f"{name}:", flush=True)
            where = tmp / name.replace("/", "-")
            where.mkdir()
            try:
                run = browser_pairing if name in BROWSER_PAIRS else pairing
                if run is browser_pairing and not playwright_ready():
                    print("  – skipped: Playwright isn't installed (uv run --with playwright …; playwright install chromium)", flush=True)
                    continue
                run(name, url, where, tmp / "relay.db", lambda msg: print(f"  ✓ {msg}", flush=True))
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
