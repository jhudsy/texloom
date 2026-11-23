#!/usr/bin/env python3
"""Model-based check of the folder sync (texloom/folder.py): Hypothesis runs the real FolderSync on
two computers, plus a collaborator in the web app, through random sequences of what can happen,
and checks after every step that nothing was lost or undone.

The setting: one file, main.tex, split into parts, each written by one person only:
  edA    an editor on computer A, saving the file in its folder (Unix line endings)
  edB    an editor on computer B (Windows line endings)
  typA   typing into A's project directly (the web app on A, the assistant)
  C      a collaborator in the web app, whose changes arrive over the network
Because each part has a single writer, what it must hold is known exactly: its writer's last
version. That gives a strong check without restating the merge rules.

Steps (in any order, any number of times): an editor saves its part; typing; changes delivered
between any two of A, B and C (A and B take in their folder's changes first, as the app does);
a sync round (read the folder, write the project out); a computer's file writes refused or allowed
again (Windows, the file open elsewhere); an older copy of the file put back (an editor reloading a
stale copy, a backup tool, after the sync wrote the newest version); the app quitting, the file
saved while it isn't running, and the app starting again (its memory gone, its saved state read back).

After every step: each editor's part on disk is what that editor last saved (the sync never
overwrites an unread save, or reverts one); each project's typed parts are intact; the sync's own
checks (invariants.py, TEXLOOM_CHECKS) hold. At the end, with writes allowed and everything
delivered: all copies and both folders hold the same text, every part as its writer left it, and
B's file has Windows line endings again.

Timers are the test's: retries and "look again" are steps here, not background tasks, so a failing
sequence replays exactly. Hypothesis shrinks a failure to the shortest sequence that shows it.

  uv run --with hypothesis python scripts/check_folder_model.py      CHECK_RUNS=… for more
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["TEXLOOM_CHECKS"] = "1"  # the sync's own invariants, checked as it runs

import functools
import inspect
import json

from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from pycrdt import Doc, Map

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging

from texloom import folder, invariants
from texloom.crypto import Keys
from texloom.folder import FolderSync, apply_text, lf
from texloom.replica import Replica

logging.getLogger("texloom").setLevel(logging.CRITICAL)
folder.SETTLE, folder.SETTLE_DOUBT = 0.0005, 0.001
ZONES = ["edA", "edB", "typA", "C"]
F = "main.tex"

# --- the sync's own checks, and writes refused, made visible to the test ------------------------
BROKEN: list[str] = []
_broken = invariants.broken
def _record(what: str, detail: object = "") -> None:
    BROKEN.append(f"{what} {str(detail)[:300]}")
    _broken(what, detail)
folder.broken = _record  # type: ignore[attr-defined]

REFUSING: set[str] = set()  # folders whose file writes are refused now
_replace = pathlib.Path.replace
def _maybe_refuse(self: Path, target):
    if ".texloom-tmp" in self.name and any(str(self).startswith(r) for r in REFUSING):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process (model)")
    return _replace(self, target)
pathlib.Path.replace = _maybe_refuse  # type: ignore[method-assign]


# --- the file's parts ----------------------------------------------------------------------------
def sep(z: str) -> str:
    return f"%% ---- {z} ----"


def render(parts: dict[str, list[str]]) -> str:
    out = ["\\documentclass{article}"]
    for z in ZONES:
        out += [sep(z), *parts[z]]
    out.append("%% end")
    return "\n".join(out) + "\n"


def parse(text: str) -> dict[str, list[str]] | None:
    """The parts of a text, or None if its structure was damaged."""
    lines = lf(text).split("\n")
    if lines[-1] == "":
        lines.pop()
    try:
        at = [lines.index(sep(z)) for z in ZONES] + [lines.index("%% end")]
    except ValueError:
        return None
    if at != sorted(at) or lines[0] != "\\documentclass{article}":
        return None
    return {z: lines[at[i] + 1:at[i + 1]] for i, z in enumerate(ZONES)}


def parse_raw(text: str) -> tuple[list[str], dict[str, list[str]]] | None:
    """As parse, but lines exactly as they are (other people's "\r" included) and the fixed lines
    too: for editing one part without touching the others, as typing does."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    bare = [x.removesuffix("\r") for x in lines]
    try:
        at = [bare.index(sep(z)) for z in ZONES] + [bare.index("%% end")]
    except ValueError:
        return None
    head = [lines[0]] + [lines[i] for i in at]
    return head, {z: lines[at[i] + 1:at[i + 1]] for i, z in enumerate(ZONES)}


def render_raw(head: list[str], parts: dict[str, list[str]]) -> str:
    out = [head[0]]
    for i, z in enumerate(ZONES):
        out += [head[i + 1], *parts[z]]
    out.append(head[-1])
    return "\n".join(out) + "\n"


class Computer:
    def __init__(self, name: str, tmp: Path, keys: Keys, windows: bool):
        self.name, self.keys, self.windows = name, keys, windows
        self.root = tmp / name
        (self.root / ".texloom").mkdir(parents=True)
        self.r = Replica(self.root / ".texloom", keys, "http://127.0.0.1:1")
        self.fs = self._sync()
        self.clock = 1_700_000_000_000_000_000 + (0 if name == "A" else 10**15)  # editors' save times (ns)
        self.edited = False
        self.running = True

    def _sync(self) -> FolderSync:
        fs = FolderSync(self.root, self.r)
        fs._retry_writes = lambda: None  # type: ignore[method-assign]  # retries are steps here
        fs._later = lambda paths: None  # type: ignore[method-assign]   # so is looking again
        return fs

    def quit(self) -> None:
        """The app quits: its state is saved (doc.bin, known.json); what's in memory goes."""
        self.r.save()
        self.fs.save_known()
        self.running = False

    async def start(self) -> None:
        """The app starts: state read back, then the folder taken in and written out, as cli.py does."""
        self.r = Replica(self.root / ".texloom", self.keys, "http://127.0.0.1:1")
        self.fs = self._sync()
        self.running = True
        async with self.r.lock:
            await self.fs.ingest_all()
            await self.fs.write_out()

    @property
    def path(self) -> Path:
        return self.root / F

    def disk(self) -> str | None:
        try:
            return self.path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return None

    def save(self, text: str) -> None:
        """An editor saves the whole file (as editors do), with this computer's line endings."""
        data = (text.replace("\n", "\r\n") if self.windows else text).encode("utf-8")
        self.path.write_bytes(data)
        self.clock += 1_000_000_000
        os.utime(self.path, ns=(self.clock, self.clock))  # a save the sync can see by its date
        self.edited = True

    def project(self) -> str:
        t = self.r.files.get(F)
        return str(t) if t is not None else ""

    async def sync_round(self) -> None:
        async with self.r.lock:
            await self.fs.ingest_all()
            await self.fs.write_out()

    async def receive(self, update: bytes) -> None:
        """Changes from elsewhere, taken in as the app does: the folder's own changes first."""
        async with self.r.lock:
            await self.fs.ingest_all()
            with self.r.doc.transaction(origin="remote"):
                self.r.doc.apply_update(update)
            self.r.save()
            await self.fs.on_remote()


def update_for(src: Doc, dst: Doc) -> bytes:
    return src.get_update(dst.get_state())


def step(kind: str, *names: str):
    """A rule's step, also kept in folder-cases.json's form: a failing run prints its steps ready to
    add there, where both this check and the browser's (web/scripts/check-folder-model.mjs) run it."""
    def deco(f):
        sig = inspect.signature(f)

        @functools.wraps(f)
        def traced(self, *args, **kw):
            given = sig.bind(self, *args, **kw).arguments
            self.trace.append([kind, *(given[n] for n in names)])
            return f(self, *args, **kw)
        return traced
    return deco


class FolderModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        BROKEN.clear()
        REFUSING.clear()
        self.trace: list[list] = []  # the steps so far, as in folder-cases.json
        self.loop = asyncio.new_event_loop()
        self.tmp = Path(tempfile.mkdtemp(prefix="texloom-model-")).resolve()  # as the sync sees it (macOS: /private/var)
        keys = Keys.generate()
        self.n = 0
        self.want: dict[str, list[str]] = {z: [f"{z} start"] for z in ZONES}
        self.A = Computer("A", self.tmp, keys, windows=False)
        self.B = Computer("B", self.tmp, keys, windows=True)
        self.C = Doc()
        self.A.path.write_text(render(self.want))
        self.run(self.A.sync_round())
        self.run(self.B.receive(update_for(self.A.r.doc, self.B.r.doc)))
        self.C.apply_update(self.A.r.doc.get_update())
        self.seen: dict[str, list[str]] = {"A": [], "B": []}  # what each folder held after a sync round

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def comp(self, name: str) -> Computer:
        return self.A if name == "A" else self.B

    def token(self, z: str) -> str:
        self.n += 1
        return f"{z} {self.n} é😀" if self.n % 3 == 0 else f"{z} {self.n}"

    def changed(self, z: str, lines: list[str], op: int, at: int) -> list[str]:
        """One edit to a part: insert a line, delete one, or change one."""
        lines = list(lines)
        if op == 0 or not lines:
            lines.insert(at % (len(lines) + 1), self.token(z))
        elif op == 1:
            del lines[at % len(lines)]
        else:
            i = at % len(lines)
            lines[i] = lines[i] + " ✎"
        return lines

    # --- what can happen -----------------------------------------------------------------------
    @rule(who=st.sampled_from(["A", "B"]), op=st.integers(0, 2), at=st.integers(0, 50))
    @step("save", "who", "op", "at")
    def editor_saves(self, who: str, op: int, at: int) -> None:
        c = self.comp(who)
        z = "edA" if who == "A" else "edB"
        parts = parse(c.disk() or "")
        assert parts is not None, f"{who}'s file is damaged: {c.disk()!r}"
        parts[z] = self.want[z] = self.changed(z, parts[z], op, at)
        c.save(render(parts))

    @rule(op=st.integers(0, 2), at=st.integers(0, 50))
    @step("type", "op", "at")
    def typed_in_A(self, op: int, at: int) -> None:
        if not self.A.running:
            return  # (typing into A's project needs A's app)
        cur = self.A.project()
        got = parse_raw(cur)
        assert got is not None, f"A's project is damaged: {cur!r}"
        head, parts = got
        parts["typA"] = self.want["typA"] = self.changed("typA", [x.removesuffix("\r") for x in parts["typA"]], op, at)
        new = render_raw(head, parts)
        self.A.r.local_change(lambda: apply_text(self.A.r.files[F], cur, new))

    @rule(op=st.integers(0, 2), at=st.integers(0, 50))
    @step("collab", "op", "at")
    def collaborator_types(self, op: int, at: int) -> None:
        t = self.C.get("files", type=Map)[F]
        cur = str(t)
        got = parse_raw(cur)
        assert got is not None, f"C's copy is damaged: {cur!r}"
        head, parts = got
        parts["C"] = self.want["C"] = self.changed("C", [x.removesuffix("\r") for x in parts["C"]], op, at)
        apply_text(t, cur, render_raw(head, parts))

    @rule(op=st.integers(0, 2), at=st.integers(0, 50))
    @step("collab_crlf", "op", "at")
    def collaborator_pastes_windows_text(self, op: int, at: int) -> None:
        """C's older app (or a paste there) puts lines ending "\r\n" into the project."""
        t = self.C.get("files", type=Map)[F]
        cur = str(t)
        got = parse_raw(cur)
        assert got is not None, f"C's copy is damaged: {cur!r}"
        head, parts = got
        parts["C"] = self.want["C"] = self.changed("C", [x.removesuffix("\r") for x in parts["C"]], op, at)
        apply_text(t, cur, render_raw(head, {**parts, "C": [line + "\r" for line in parts["C"]]}))

    @rule(src=st.sampled_from(["A", "B", "C"]), dst=st.sampled_from(["A", "B", "C"]))
    @step("deliver", "src", "dst")
    def deliver(self, src: str, dst: str) -> None:
        if src == dst:
            return
        if any(x in "AB" and not self.comp(x).running for x in (src, dst)):
            return  # that app isn't running: nothing goes in or out
        doc = {"A": self.A.r.doc, "B": self.B.r.doc, "C": self.C}
        upd = update_for(doc[src], doc[dst])
        if dst == "C":
            self.C.apply_update(upd)
        else:
            self.run(self.comp(dst).receive(upd))

    @rule(who=st.sampled_from(["A", "B"]))
    @step("sync", "who")
    def sync_round(self, who: str) -> None:
        c = self.comp(who)
        if not c.running:
            return
        self.run(c.sync_round())
        if (d := c.disk()) is not None and (not self.seen[who] or self.seen[who][-1] != d):
            self.seen[who].append(d)

    @rule(who=st.sampled_from(["A", "B"]), refuse=st.booleans())
    @step("refuse", "who", "refuse")
    def writes_refused(self, who: str, refuse: bool) -> None:
        r = str(self.comp(who).root) + os.sep
        if refuse:
            REFUSING.add(r)
        else:
            REFUSING.discard(r)

    @precondition(lambda self: any(len(v) > 1 for v in self.seen.values()))
    @rule(who=st.sampled_from(["A", "B"]), pick=st.integers(0, 50))
    @step("older", "who", "pick")
    def older_copy_comes_back(self, who: str, pick: int) -> None:
        """A copy the folder held earlier is put back (a stale editor buffer, a backup tool), and
        the sync looks at once. It mustn't count as an edit: that would undo everyone's changes."""
        c = self.comp(who)
        if str(c.root) + os.sep in REFUSING or not c.running:
            return  # (the sync couldn't put the project's text back until writes are allowed)
        if folder.sha(c.path.read_bytes()) != c.fs.known.get(F, {}).get("h"):
            return  # a save the sync hasn't read yet would be gone from disk: nothing could get it back
        if not c.fs._wrote_last.get(F):
            return  # the newest version was a save here: going back to an older one is that editor's undo, an edit
        older = [d for d in self.seen[who][:-1] if d != c.disk()]
        if not older:
            return
        known_older = set(c.fs.recent.get(F, [])[:-1])
        text = older[pick % len(older)]
        data = text.encode("utf-8")
        if folder.sha(data) not in known_older:
            return  # one the sync never noted can't be told from an edit (nothing could)
        c.path.write_bytes(data)
        c.clock += 1_000_000_000
        os.utime(c.path, ns=(c.clock, c.clock))
        self.run(c.sync_round())  # the invariants then check nothing was undone

    @rule(who=st.sampled_from(["A", "B"]))
    @step("toggle", "who")
    def app_quits_or_starts(self, who: str) -> None:
        c = self.comp(who)
        if c.running:
            c.quit()
        else:
            self.run(c.start())

    # --- what must hold after every step ---------------------------------------------------------
    @invariant()
    def editors_parts_are_theirs(self) -> None:
        for who, z in (("A", "edA"), ("B", "edB")):
            parts = parse(self.comp(who).disk() or "")
            assert parts is not None, f"{who}'s file is damaged: {self.comp(who).disk()!r}"
            assert parts[z] == self.want[z], f"{who}'s editor saved {self.want[z]}, the file now has {parts[z]}"

    @invariant()
    def typed_parts_are_kept(self) -> None:
        a, c = parse(self.A.project()), parse(str(self.C.get("files", type=Map)[F]))
        if self.A.running:
            assert a is not None and a["typA"] == self.want["typA"], f"typed in A: {self.want['typA']}, A's project: {a and a['typA']}"
        assert c is not None and c["C"] == self.want["C"], f"C typed {self.want['C']}, C's copy: {c and c['C']}"

    @invariant()
    def sync_checks_hold(self) -> None:
        assert not BROKEN, "the sync's own checks failed: " + "; ".join(BROKEN[:3])

    def teardown(self) -> None:
        with contextlib.suppress(Exception):  # (only inside a check run: a known case has its steps already)
            note("the steps, as a case for scripts/folder-cases.json: " + json.dumps(self.trace))
        try:
            if not hasattr(self, "A"):
                return
            REFUSING.clear()
            for c in (self.A, self.B):
                if not c.running:
                    self.run(c.start())
            docs = lambda: {"A": self.A.r.doc, "B": self.B.r.doc, "C": self.C}
            for _ in range(4):  # everything delivered everywhere, both folders synced
                for src in "ABC":
                    for dst in "ABC":
                        if src != dst:
                            self.deliver(src, dst)
                self.run(self.A.sync_round())
                self.run(self.B.sync_round())
            texts = {k: str(d.get("files", type=Map)[F]) for k, d in docs().items()}
            assert len(set(texts.values())) == 1, f"the copies differ: {texts}"
            final = texts["A"]
            assert parse(final) == self.want, f"want {self.want}, have {parse(final)}"
            assert self.A.disk() == final, f"A's file: {self.A.disk()!r}"
            assert lf(self.B.disk() or "") == final, f"B's file: {self.B.disk()!r}"
            if self.B.edited:
                assert (self.B.disk() or "").count("\r\n") == final.count("\n"), "B's file lost its Windows line endings"
            assert not BROKEN, "the sync's own checks failed: " + "; ".join(BROKEN[:3])
        finally:
            REFUSING.clear()
            self.loop.close()
            shutil.rmtree(self.tmp, ignore_errors=True)


FolderModel.TestCase.settings = settings(
    max_examples=int(os.environ.get("CHECK_RUNS", "150")), stateful_step_count=40, deadline=None,
    suppress_health_check=list(HealthCheck), derandomize=bool(os.environ.get("CHECK_SEED")))


CASES = ROOT / "scripts" / "folder-cases.json"


def run_case(steps: list) -> None:
    """One case from folder-cases.json: its steps in order, the checks after each, then the end."""
    m = FolderModel()
    ok = False
    try:
        do = {
            "save": lambda who, op, at: m.editor_saves(who=who, op=op, at=at),
            "type": lambda op, at: m.typed_in_A(op=op, at=at),
            "collab": lambda op, at: m.collaborator_types(op=op, at=at),
            "collab_crlf": lambda op, at: m.collaborator_pastes_windows_text(op=op, at=at),
            "deliver": lambda src, dst: m.deliver(src=src, dst=dst),
            "sync": lambda who: m.sync_round(who=who),
            "time": lambda ms: None,  # (the desktop's timers are its sync rounds here)
            "refuse": lambda who, on: m.writes_refused(who=who, refuse=on),
            "older": lambda who, pick: m.older_copy_comes_back(who=who, pick=pick),
            "toggle": lambda who: m.app_quits_or_starts(who=who),
        }
        for kind, *args in steps:
            do[kind](*args)
            m.editors_parts_are_theirs()
            m.typed_parts_are_kept()
            m.sync_checks_hold()
        ok = True
    finally:
        if ok:
            m.teardown()  # checks that everything agrees at the end
        else:
            with contextlib.suppress(Exception):  # the step's failure is the one to report
                m.teardown()


def check_cases() -> list[str]:
    import json
    bad = []
    for case in json.loads(CASES.read_text(encoding="utf-8"))["cases"]:
        try:
            run_case(case["steps"])
        except Exception as e:  # noqa: BLE001 - report every failing case
            bad.append(f"{case['name']}: {type(e).__name__}: {str(e)[:400]}")
    return bad


def main() -> None:
    bad = check_cases()
    if bad:
        print("folder model: FAILED known cases (scripts/folder-cases.json)\n  " + "\n  ".join(bad))
        sys.exit(1)
    import unittest
    suite = unittest.TestLoader().loadTestsFromTestCase(FolderModel.TestCase)
    result = unittest.TextTestRunner(verbosity=0, stream=sys.stdout).run(suite)
    if not result.wasSuccessful():
        print("folder model: FAILED")
        sys.exit(1)
    print(f"folder model: every known case and every step held ({FolderModel.TestCase.settings.max_examples} random runs of up to 40 steps)")


if __name__ == "__main__":
    main()
