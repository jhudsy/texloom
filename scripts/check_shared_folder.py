#!/usr/bin/env python3
"""A synced folder that two computers also share through a file-sync service (Dropbox and the
like), each running TeXLoom on its copy (texloom/sharedfolder.py), end to end.

A stand-in for the service copies files between the two copies of the folder, a moment after they
stop changing, as Dropbox does: everything, `.texloom` too, and a "conflicted copy" when a file
changed on both sides at once. A third computer is an ordinary collaborator. Without taking turns,
each edit would reach the other computer twice (through TeXLoom and through the service) and be
shared again: the same text twice.

Checked: the second computer adds the service's copy of the folder (adopting the project link from
the first computer's records) and stands by; edits on either computer, and from the collaborator,
arrive everywhere exactly once; each computer keeps its own records; when the computer keeping the
folder in step stops, the other takes over, and edits keep arriving exactly once; a conflicted copy
the service makes is never shared.

    uv run --extra server python scripts/check_shared_folder.py
    TEXLOOM_NO_TURNS=1 uv run --extra server python scripts/check_shared_folder.py   # without taking turns: fails
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import Computer, Failed, invariants, lf, server, wait  # noqa: E402

os.environ.update({"TEXLOOM_TURN_ANNOUNCE": "2", "TEXLOOM_TURN_EXPIRE": "8"})  # (the computers inherit it)
MAIN = "\\documentclass{article}\n\\begin{document}\nThe first paragraph.\n\nThe middle paragraph.\n\nThe last paragraph.\n\\end{document}\n"


class FileSyncService:
    """Copies files between folders as Dropbox does: once a file has been unchanged for DELAY,
    whole and by renaming into place, with its date; a deletion likewise; a file changed on both
    sides since the last copy keeps the newer and leaves the other as a conflicted copy."""
    DELAY = 1.0

    def __init__(self, *dirs: Path) -> None:
        self.dirs = dirs
        self.last: dict[str, str | None] = {}  # path -> content hash both had at the last copy
        self.seen: dict[tuple[int, str], tuple] = {}  # (folder, path) -> (stat, since)
        self.stop = threading.Event()
        self.conflicts: list[str] = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def files(self, d: Path) -> dict[str, Path]:
        out = {}
        for p in d.rglob("*"):
            rel = p.relative_to(d).as_posix()
            if p.is_file() and ".texloom-tmp" not in rel and not rel.endswith(("/lock", ".tmp")) and rel != ".texloom/lock":
                out[rel] = p
        return out

    def stable(self, i: int, rel: str, p: Path | None) -> bool:
        st = None if p is None else (p.stat().st_size, p.stat().st_mtime_ns)
        was = self.seen.get((i, rel))
        if was is None or was[0] != st:
            self.seen[(i, rel)] = (st, time.monotonic())
            return False
        return time.monotonic() - was[1] >= self.DELAY

    @staticmethod
    def put(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.dropbox-tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)

    def once(self) -> None:
        a, b = self.dirs
        fa, fb = self.files(a), self.files(b)
        for rel in sorted(set(fa) | set(fb)):
            pa, pb = fa.get(rel), fb.get(rel)
            try:
                if not (self.stable(0, rel, pa) and self.stable(1, rel, pb)):
                    continue
                ha = hashlib.sha1(pa.read_bytes()).hexdigest() if pa else None
                hb = hashlib.sha1(pb.read_bytes()).hexdigest() if pb else None
            except OSError:
                continue
            last = self.last.get(rel)
            if ha == hb:
                self.last[rel] = ha
            elif hb == last:  # changed in a only
                if pa:
                    self.put(pa, b / rel)
                else:
                    (b / rel).unlink(missing_ok=True)
                self.last[rel] = ha
            elif ha == last:  # changed in b only
                if pb:
                    self.put(pb, a / rel)
                else:
                    (a / rel).unlink(missing_ok=True)
                self.last[rel] = hb
            elif pa and pb:  # changed on both: the newer stays, the other becomes a conflicted copy
                newer, older = (pa, pb) if pa.stat().st_mtime_ns >= pb.stat().st_mtime_ns else (pb, pa)
                stem, dot, ext = rel.rpartition(".")
                copy = f"{stem} (B's conflicted copy 2026-11-20){dot}{ext}" if dot else f"{rel} (B's conflicted copy 2026-11-20)"
                for d in (a, b):
                    self.put(older, d / copy)
                self.put(newer, (b if newer is pa else a) / rel)
                self.last[rel] = hashlib.sha1(newer.read_bytes()).hexdigest()
                self.conflicts.append(rel)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.once()
            except OSError:
                pass
            time.sleep(0.2)


def once_everywhere(cs, rel: str, *bits: str) -> bool:
    """Every computer's copy of the file has each bit exactly once."""
    return all(c.file(rel).exists() and all(lf(c.read(rel)).count(b) == 1 for b in bits) for c in cs)


def check(step) -> None:
    # (CI: CHECK_DIR, the runner's own temp folder, kept as an artifact if this fails)
    with server("shared", tmp=os.environ.get("CHECK_DIR")) as srv:
        shared(srv.url, srv.tmp, step)


def shared(url: str, tmp: Path, step) -> None:
    a, b, c = Computer("A", "unix", tmp), Computer("B", "unix", tmp), Computer("C", "unix", tmp)
    service = FileSyncService(a.dir, b.dir)
    try:
        a.dir.mkdir()
        b.dir.mkdir()
        a.save("main.tex", MAIN)
        a.save("chapter.tex", "A chapter.\n")
        a.texloom("new", str(a.dir), "--server", url, "--name", "check shared folder")
        link = next(w for w in a.texloom("links", str(a.dir)).split() if "#p=" in w and "&w=" in w)
        a.start()
        c.texloom("clone", link, str(c.dir))
        c.start()
        service.thread.start()
        wait("the service copies the folder to B", lambda: b.file("main.tex").exists() and any((b.dir / ".texloom" / "computers").glob("*/config.json")), 30)

        # B adds the service's copy of the folder: it has A's records, not its own
        out = b.texloom("add", str(b.dir))
        if "Added" not in out:
            raise Failed(f"B couldn't add the shared folder: {out}")
        b.start()
        mine = {p.name for p in (b.dir / ".texloom" / "computers").iterdir()}
        wait("B hears that A syncs the folder too", lambda: "also syncs this folder" in b.log.read_text(errors="replace"), 30)
        wait("each computer keeps its own records", lambda: len({p.name for p in (a.dir / ".texloom" / "computers").iterdir()}) == 2, 30)
        time.sleep(4)
        if "keeps the shared folder in step now" in b.log.read_text(errors="replace") and not os.environ.get("TEXLOOM_NO_TURNS"):
            raise Failed("B took the turn while A holds it")
        invariants(a, b, c)
        step(f"the second computer adds the service's copy of the folder and stands by ({len(mine)} computers' records in it)")

        a.edit("main.tex", "The first paragraph.", "The first paragraph, edited on A.")
        wait("A's edit reaches C", lambda: c.has("main.tex", "edited on A"))
        b.edit("main.tex", "The last paragraph.", "The last paragraph, edited on B.")
        wait("B's edit reaches C", lambda: c.has("main.tex", "edited on B"), 60)
        c.edit("main.tex", "The middle paragraph.", "The middle paragraph, edited on C.")
        wait("C's edit reaches B (A writes it, the service brings it)", lambda: b.has("main.tex", "edited on C"), 60)
        time.sleep(6)  # time for any second copy to show up
        bits = ("edited on A", "edited on B", "edited on C")
        if not once_everywhere((a, b, c), "main.tex", *bits):
            raise Failed("an edit arrived more than once:\n" + "\n".join(f"{x.name}: {lf(x.read('main.tex'))}" for x in (a, b, c)))
        invariants(a, b, c)
        step("edits on either computer, and from a collaborator, arrive everywhere exactly once")

        # the service runs while TeXLoom doesn't (it starts at login first, or TeXLoom was quit): what
        # it brings in meanwhile is already in the project, and mustn't be read in again as B's
        b.stop()
        a.edit("main.tex", "\\end{document}", "Added on A while B's app was off.\n\\end{document}")
        wait("A's edit reaches C", lambda: c.has("main.tex", "while B's app was off"))
        wait("the service brings it to B's folder", lambda: b.has("main.tex", "while B's app was off"), 30)
        b.start()
        time.sleep(8)
        if not once_everywhere((a, b, c), "main.tex", "while B's app was off", *bits):
            raise Failed("B read in, as its own, an edit the service brought while its app was off:\n" + lf(c.read("main.tex")))
        invariants(a, b, c)
        step("what the service brings while TeXLoom isn't running isn't read in a second time")

        a.stop()
        wait("B takes over once A has stopped", lambda: "keeps the shared folder in step now" in b.log.read_text(errors="replace"), 40)
        c.edit("chapter.tex", "A chapter.", "A chapter, edited on C while A was off.")
        wait("C's edit reaches B", lambda: b.has("chapter.tex", "while A was off"), 60)
        b.edit("chapter.tex", "A chapter", "A chapter (B)")
        wait("B's edit reaches C", lambda: c.has("chapter.tex", "A chapter (B)"), 60)
        time.sleep(6)
        if not once_everywhere((b, c), "chapter.tex", "while A was off", "(B)") or not once_everywhere((b, c), "main.tex", *bits):
            raise Failed("after the takeover an edit arrived more than once:\n" + lf(b.read("chapter.tex")) + lf(b.read("main.tex")))
        invariants(b, c)
        step("when the computer keeping it in step stops, the other takes over; edits still arrive exactly once")

        a.start()
        time.sleep(8)
        copy = "notes (B's conflicted copy 2026-11-20).tex"
        b.save(copy, "an old version\n")
        wait("B notices the conflicted copy", lambda: "not sharing notes (B" in (a.log.read_text(errors="replace") + b.log.read_text(errors="replace")), 30)
        time.sleep(3)
        if c.file(copy).exists():
            raise Failed("a conflicted copy was shared with the project")
        a.edit("main.tex", "edited on A.", "edited on A, again.")
        wait("A's edit reaches C", lambda: c.has("main.tex", "edited on A, again."), 60)
        time.sleep(6)
        if not once_everywhere((a, b, c), "main.tex", "edited on A, again.", "edited on B", "edited on C"):
            raise Failed("with A back, an edit arrived more than once:\n" + lf(c.read("main.tex")))
        invariants(a, b, c)
        step("a conflicted copy made by the service isn't shared; A comes back without bringing anything in twice")
    finally:
        service.stop.set()
        for x in (a, b, c):
            x.stop()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        check(lambda msg: print(f"✓ {msg}", flush=True))
    except Failed as e:
        print(f"✗ {e}")
        print("shared folder: FAILED")
        sys.exit(1)
    print("shared folder: all checks passed")
