"""Paragraphs two people changed while apart: the desktop side of web/src/merges.ts (same rules).

Yjs merges such edits character by character, so when both rewrote the same sentence the
result can interleave their words. While this copy's changes can't reach anyone (not
connected, or still catching up), the places edited are remembered; when changes from others
then land in a paragraph holding one of them, a note goes into the project's Review list for
someone to check. Edits made apart have to be at least MIN_APART old by then, so brief
disconnections don't count; edits made while the app wasn't running count as old.

Positions are UTF-8 byte offsets (what pycrdt's text deltas count in), moved along with every
change, instead of sticky indices: pycrdt places those wrongly in non-ASCII text (anchors.py).
For the same reason the note has no anchors of its own, only the paragraph's text (`quote`):
the web app anchors it where that text is (merges.ts).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from pycrdt import Array, Map, Text

from .replica import Replica
from .textpos import Byte, Char, byte_of, char_of_byte

log = logging.getLogger("texloom")

MIN_APART = 30.0  # seconds: edits apart for less than this aren't flagged
SETTLE = 1.5  # seconds after the last change from others before notes are added
FORGET_AFTER = 10.0  # seconds caught up, with nothing arriving, before the places are forgotten
MAX_PLACES = 200  # per file before nearby places are merged into one
LONG = 3000  # characters: a "paragraph" longer than this is narrowed to the lines changed


def changed_ranges(delta: list[dict]) -> list[tuple[Byte, Byte]]:
    """Byte ranges a text delta changed, in the text as it is after it: empty where text went."""
    out: list[tuple[Byte, Byte]] = []
    i = 0
    for op in delta:
        if "retain" in op:
            i += op["retain"]
        elif "insert" in op:
            n = len(op["insert"].encode()) if isinstance(op["insert"], str) else 1
            out.append((Byte(i), Byte(i + n)))
            i += n
        elif "delete" in op:
            out.append((Byte(i), Byte(i)))
    return out


def shift(pos: Byte, delta: list[dict], start: bool) -> Byte:
    """Where byte offset `pos` is after `delta`. Text inserted exactly at a range's start goes
    before it, at its end after it: neither becomes part of the range."""
    return Byte(_shift(pos, delta, start))


def _shift(pos: int, delta: list[dict], start: bool) -> int:
    old = new = 0
    for op in delta:
        if "retain" in op:
            k = op["retain"]
            if pos < old + k or (not start and pos == old + k):
                return new + pos - old
            old += k
            new += k
        elif "insert" in op:
            if pos == old and not start:
                return new
            new += len(op["insert"].encode()) if isinstance(op["insert"], str) else 1
        elif "delete" in op:
            k = op["delete"]
            if pos < old + k:  # inside what went: where it was
                return new
            old += k  # just after it: an insertion reported after the deletion still goes before a start
    return new + pos - old


def paragraph_at(text: str, a: Char, b: Char) -> tuple[Char, Char]:
    """The paragraph around characters [a, b): between blank lines, or the lines if very long."""
    import re
    pa, pb = 0, len(text)
    for m in re.finditer(r"\n[ \t]*\n", text):
        if m.end() <= a:
            pa = m.end()
        elif m.start() >= b:
            pb = m.start()
            break
    if pb - pa > LONG:
        pa = text.rfind("\n", 0, a) + 1
        nl = text.find("\n", b)
        pb = len(text) if nl < 0 else nl
    return Char(pa), Char(pb)


def _state(doc) -> dict[int, int]:
    """A doc's state vector: client -> clock."""
    raw, i = doc.get_state(), 0

    def uint() -> int:
        nonlocal i
        n = shift_ = 0
        while True:
            x = raw[i]
            i += 1
            n |= (x & 0x7F) << shift_
            shift_ += 7
            if x < 0x80:
                return n
    out = {}
    for _ in range(uint()):
        c = uint()
        out[c] = uint()
    return out


def merge_text(me: str, others: list[str]) -> str:
    """The note, as the web app words it (it shows its own translation in the Review tab)."""
    if others:
        return (f"{me} and {', '.join(others)} both changed this paragraph while working apart, "
                "and the changes were merged. Check that it reads as intended.")
    return (f"{me} and someone else both changed this paragraph while working apart, "
            "and the changes were merged. Check that it reads as intended.")


class MergeWatch:
    def __init__(self, r: Replica, name: Callable[[], str], state: Path):
        self.r, self.name = r, name
        self.file = state / "apart.json"
        self.places: dict[str, list[list[float]]] = {}  # file -> [[start, end, made at]] (bytes)
        self.found: dict[str, list[list[Byte]]] = {}  # file -> [[start, end]]: paragraphs to note
        self.writers: set[int] = set()  # clients whose changes arrived while notes were pending
        self.last_remote = 0.0
        self.exchanged_since = 0.0
        self.dirty = False
        self._load()
        self._sv = _state(r.doc)
        self.sub = r.files.observe_deep(self._on)

    @property
    def apart(self) -> bool:
        return not (self.r.ws is not None and getattr(self.r, "_caught_up", False))

    # --- keeping places across restarts --------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return
        for path, entry in data.items() if isinstance(data, dict) else ():
            t = self.r.files.get(path)
            if isinstance(t, Text) and isinstance(entry, dict) and entry.get("h") == hashlib.sha256(str(t).encode()).hexdigest():
                self.places[path] = [p for p in entry.get("p", []) if isinstance(p, list) and len(p) == 3]

    def _save(self) -> None:
        self.dirty = False
        try:
            if not self.places:
                self.file.unlink(missing_ok=True)
                return
            out = {}
            for path, ps in self.places.items():
                t = self.r.files.get(path)
                if isinstance(t, Text):
                    out[path] = {"h": hashlib.sha256(str(t).encode()).hexdigest(), "p": ps}
            self.file.write_text(json.dumps(out))
        except OSError as e:
            log.debug("couldn't keep the places edited apart: %s", e)

    # --- watching changes ------------------------------------------------------
    def _on(self, events) -> None:
        if not self.r.keys.writable or self.r.deleted_at is not None:
            return
        origin = events[0].transaction.origin if events else None
        local, remote = origin == "local", origin == "remote"
        if remote:
            self.last_remote = time.monotonic()
        for ev in events:
            t = ev.target
            if not isinstance(t, Text) or len(ev.path) != 1:
                continue
            path = ev.path[0]
            delta = ev.delta
            for p in self.places.get(path, []):  # keep positions current
                p[0], p[1] = shift(Byte(int(p[0])), delta, True), shift(Byte(int(p[1])), delta, False)
            for q in self.found.get(path, []):
                q[0], q[1] = shift(q[0], delta, True), shift(q[1], delta, False)
            if self.places.get(path):
                self.dirty = True
            ranges = changed_ranges(delta)
            if not ranges:
                continue
            if local and self.apart:  # edited apart: remember where
                at = time.time() if self.r.synced.is_set() else 0.0  # before the first catch-up: made while not running
                ps = self.places.setdefault(path, [])
                ps.extend([a, b, at] for a, b in ranges)
                if len(ps) > MAX_PLACES:
                    self._tidy(path)
                self.dirty = True
            elif remote and (mine := self.places.get(path)):
                old = time.time() - MIN_APART
                spans = [(p[0], p[1]) for p in mine if p[2] <= old]
                if not spans:
                    continue
                s = str(t)
                for a, b in ranges:
                    ca, cb = paragraph_at(s, char_of_byte(s, a), char_of_byte(s, b))
                    pa, pb = byte_of(s, ca), byte_of(s, cb)
                    if any(x <= pb and y >= pa for x, y in spans):
                        self.found.setdefault(path, []).append([pa, pb])

    def _tidy(self, path: str) -> None:
        merged: list[list[float]] = []
        for a, b, at in sorted(self.places[path]):
            if merged and a - merged[-1][1] < 200:
                merged[-1][1] = max(merged[-1][1], b)
                merged[-1][2] = min(merged[-1][2], at)
            else:
                merged.append([a, b, at])
        self.places[path] = merged

    # --- notes, and forgetting -------------------------------------------------
    def _note(self) -> None:
        review = self.r.doc.get("review", type=Map)
        open_quotes = {(m.get("file"), m.get("quote")) for m in review.values()
                       if isinstance(m, Map) and m.get("merge") is not None and not m.get("resolved")}
        me = self.name()
        names = sorted({n for c in self.writers if c != self.r.doc.client_id and (n := self.r.clients.get(str(c)))})
        notes = []
        for path, found in self.found.items():
            t = self.r.files.get(path)
            if not isinstance(t, Text):
                continue
            s = str(t)
            spans: list[list[Char]] = []
            for a, b in sorted(found):
                ca, cb = paragraph_at(s, char_of_byte(s, a), char_of_byte(s, b))  # the paragraph as it reads now
                if spans and ca < spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], cb)
                else:
                    spans.append([ca, cb])
            for ca, cb in spans:
                quote = s[ca:cb]
                if quote.strip() and (path, quote[:20000]) not in open_quotes:
                    notes.append((path, quote[:20000]))
        self.found.clear()
        self.writers.clear()
        if not notes:
            return

        def add():
            for path, quote in notes:
                review[str(uuid.uuid4())] = Map({
                    "kind": "comment", "file": path, "text": merge_text(me, names), "by": me,
                    "at": int(time.time() * 1000), "quote": quote, "resolved": False,
                    "merge": names, "replies": Array(),
                })
        self.r.local_change(add)
        log.warning("changed apart from others and merged: %s (noted in Review, to check)", ", ".join(sorted({p for p, _ in notes})))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            sv = _state(self.r.doc)
            if self.found:  # whose changes came in since the last look
                self.writers |= {c for c, k in sv.items() if k > self._sv.get(c, 0)}
            self._sv = sv
            now = time.monotonic()
            if self.found and now - self.last_remote >= SETTLE:
                try:
                    self._note()
                except PermissionError:  # read-only now, or deleted
                    self.found.clear()
            exchanged = not self.apart and not self.r.outbox
            if not (exchanged and now - self.last_remote >= FORGET_AFTER):
                self.exchanged_since = 0.0
            elif self.places:
                self.exchanged_since = self.exchanged_since or now
                if now - self.exchanged_since >= FORGET_AFTER and not self.found:
                    self.places.clear()
                    self.dirty = True
            if self.dirty:
                self._save()
