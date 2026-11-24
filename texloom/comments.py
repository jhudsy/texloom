"""The Review tab's comments and suggestions, for people who work in a synced folder with their own
editor: an overview file kept in the folder's .texloom (read in any editor), counts and mention
notices in the desktop app, and `texloom comments` to list, reply and resolve from a terminal.

What a comment is lives in the project (the `review` map, see DESIGN.md); this reads it the way the
web app's Review tab does (web/src/review.ts): where it is now, who wrote it and whether a personal
link's signature vouches for that name, and whether it mentions you (web/src/mention.ts).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from pycrdt import Array, Assoc, Map, Text

from .anchors import index_in
from .crypto import b64e, signed_name
from .paths import replace_file
from .replica import Replica
from .textpos import Char, char_of_u16, u16_of

OVERVIEW = "comments.md"  # in the folder's .texloom


def mentions(text: str, name: str) -> bool:
    """Whether text mentions name: `@name`, any case, not followed by a letter or digit (as in web/src/mention.ts)."""
    n = name.strip().lower()
    if not n:
        return False
    hay, needle = text.lower(), "@" + n
    i = hay.find(needle)
    while i >= 0:
        after = hay[i + len(needle):i + len(needle) + 1]
        if not after or not (after.isalnum() or after == "_"):
            return True
        i = hay.find(needle, i + 1)
    return False


@dataclass
class Reply:
    by: str
    at: int  # ms
    text: str
    verified: bool


@dataclass
class Comment:
    id: str
    kind: str  # "comment" | "suggestion"
    file: str
    line: int | None  # None: the text it was on has gone
    quote: str
    text: str  # a comment's words, or a suggestion's replacement text
    by: str
    verified: bool  # a personal link's signature vouches for `by`
    at: int  # ms
    resolved: bool
    replies: list[Reply] = field(default_factory=list)
    span: tuple[int, int] | None = None  # the text it's on, as character offsets into the project's file

    def mentions(self, name: str) -> bool:
        return mentions(self.text, name) or any(mentions(r.text, name) for r in self.replies)


def _span(r: Replica, path: str, start, end, quote: str) -> tuple[int, int] | None:
    """The text a comment is on, as character offsets in the project's file; None if it can't be told."""
    if path not in r.files:
        return None
    t = r.files[path]
    s = str(t)
    if isinstance(start, (bytes, bytearray)):
        a = index_in(t, s, bytes(start))
        if a is not None:
            b = index_in(t, s, bytes(end)) if isinstance(end, (bytes, bytearray)) else None
            ca = char_of_u16(s, a)
            cb = char_of_u16(s, b) if b is not None and b >= a else ca + len(quote)
            return ca, min(cb, len(s))
    if quote and quote in s:  # (where the anchor can't be told: the quote's first place)
        i = s.index(quote)
        return i, i + len(quote)
    return None


def read(r: Replica, include_resolved: bool = False) -> list[Comment]:
    """The project's comments and suggestions, open ones unless include_resolved, in file and line order."""
    out: list[Comment] = []
    review = r.doc.get("review", type=Map)
    for iid, m in review.items():
        if not isinstance(m, Map) or (m.get("resolved") and not include_resolved):
            continue
        by, path, quote = str(m.get("by") or "")[:64], str(m.get("file") or ""), str(m.get("quote") or "")
        replies = []
        raw = m.get("replies")
        for x in (raw.to_py() or []) if isinstance(raw, Array) else []:
            if isinstance(x, dict):
                rb = str(x.get("by") or "")[:64]
                ok = signed_name(r.keys.project, b"reply:" + f"{iid}\n{x.get('at')}\n{x.get('by')}".encode(), x.get("s"), r.members) == rb
                replies.append(Reply(rb, int(x.get("at") or 0), str(x.get("text") or ""), ok))
        verified = signed_name(r.keys.project, b"review:" + f"{iid}\n{m.get('by')}".encode(), m.get("s"), r.members) == by
        span = _span(r, path, m.get("start"), m.get("end"), quote)
        line = str(r.files[path]).count("\n", 0, span[0]) + 1 if span else None
        out.append(Comment(iid, "suggestion" if m.get("kind") == "suggestion" else "comment", path, line,
                           quote, str(m.get("text") or ""), by, verified, int(m.get("at") or 0), bool(m.get("resolved")), replies, span))
    out.sort(key=lambda c: (c.file, c.line if c.line is not None else 1 << 30, c.at))
    return out


def _when(ms: int) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ms / 1000)) if ms else ""


def _who(name: str, verified: bool) -> str:
    return f"{name} ✓" if verified else name


def overview(items: list[Comment], project: str, me: str = "") -> str:
    """The open comments as Markdown, for reading in any editor. Kept the same while nothing changes,
    so a folder shared through Dropbox or the like doesn't see it rewritten."""
    mine = sum(1 for c in items if me and c.mentions(me))
    head = [f"# Comments on “{project}”", "",
            f"{len(items)} open" + (f" · {mine} mention you (@{me})" if mine else "")
            + ". Kept up to date by TeXLoom; reply or resolve in the web app's Review tab, or with "
              "`texloom comments`. ✓: a personal link vouches for the name.", ""]
    if not items:
        return "\n".join(head + ["Nothing open.", ""])
    body: list[str] = []
    current = None
    for c in items:
        if c.file != current:
            body += [f"## {c.file}", ""]
            current = c.file
        where = f"{c.file}:{c.line}" if c.line else f"{c.file} (the text it was on has changed)"
        flag = " · **mentions you**" if me and c.mentions(me) else ""
        body.append(f"### {where} · {c.kind} by {_who(c.by, c.verified)} · {_when(c.at)}{flag}")
        body.append("")
        if c.kind == "suggestion":
            body += [f"Replace: {c.quote!r}" if c.quote else "Insert:", f"With: {c.text!r}" if c.quote else repr(c.text)]
        else:
            body += [f"> {line}" for line in c.quote[:300].splitlines()] + ["", c.text]
        body += [f"- {_who(rp.by, rp.verified)} ({_when(rp.at)}): {rp.text}" for rp in c.replies]
        body += ["", f"<sub>id {c.id}</sub>", ""]
    return "\n".join(head + body)


def write_overview(folder: Path, r: Replica, me: str) -> bool:
    """(Re)write the folder's .texloom/comments.md if it changed. True if it was written."""
    text = overview(read(r), str(r.meta.get("name") or folder.name), me)
    path = folder / ".texloom" / OVERVIEW
    try:
        if path.read_text("utf-8") == text:
            return False
    except OSError:
        pass  # (not there yet)
    replace_file(path, text.encode("utf-8"))
    return True


def sticky(yt: Text, s: str, i: int, before: bool):
    """A place in `yt` (whose text is `s`) that moves with edits around it: at Python index `i`,
    kept with the text before it or after it."""
    return yt.sticky_index(u16_of(s, Char(i)), Assoc.BEFORE if before and i > 0 else Assoc.AFTER)


def add(r: Replica, kind: str, path: str, start: int, end: int, text: str, name: str) -> str:
    """Add a comment (or, with kind "suggestion", a suggested change: `text` replaces start..end) on
    `path` from Python index `start` to `end`, signed as the web app signs it (web/src/review.ts).
    Returns its id. Used by `texloom mcp` and `texloom lsp`."""
    if r.deleted_at is not None:
        raise PermissionError("The owner deleted this project for everyone: it can be read but not changed.")
    if not r.can_write:
        raise PermissionError("Comments and suggestions need an edit link.")
    yt = r.files[path]
    s = str(yt)
    iid = f"{secrets.token_hex(4)}-{secrets.token_hex(2)}-4{secrets.token_hex(2)[1:]}-a{secrets.token_hex(2)[1:]}-{secrets.token_hex(6)}"
    fields = {
        "kind": kind, "file": path, "text": text[:20000], "by": name, "at": int(time.time() * 1000),
        "quote": s[start:end][:20000], "resolved": False, "replies": Array(),
    }
    # pycrdt places anchors wrongly in non-ASCII text (anchors.py): there, the item goes without,
    # and the web app anchors it where its quote is (merges.ts)
    if s.isascii():
        fields["start"] = sticky(yt, s, start, start == len(s)).encode()
        fields["end"] = (sticky(yt, s, end, True) if end > 0 else sticky(yt, s, end, False)).encode()
    # who wrote it is signed (as in web/src/review.ts): its id with the name
    fields["s"] = b64e(r.keys.sign(b"review:" + f"{iid}\n{name}".encode()))
    item = Map(fields)
    r.local_change(lambda: r.doc.get("review", type=Map).__setitem__(iid, item))
    return iid


def reply(r: Replica, iid: str, text: str, name: str) -> None:
    """Reply to a comment or suggestion, signed as the web app signs it (web/src/review.ts)."""
    m = r.doc.get("review", type=Map).get(iid)
    replies = m.get("replies") if isinstance(m, Map) else None
    if not isinstance(replies, Array):
        raise KeyError(iid)
    at = int(time.time() * 1000)
    sig = b64e(r.keys.sign(b"reply:" + f"{iid}\n{at}\n{name}".encode()))
    r.local_change(lambda: replies.append({"by": name, "at": at, "text": text.strip()[:20000], "s": sig}))


def resolve(r: Replica, iid: str) -> None:
    """Resolve a comment (a suggestion is accepted or rejected in the web app, where its change can be seen)."""
    m = r.doc.get("review", type=Map).get(iid)
    if not isinstance(m, Map):
        raise KeyError(iid)
    if m.get("kind") == "suggestion":
        raise ValueError("that's a suggested change: accept or reject it in the web app's Review tab, where you can see it")
    r.local_change(lambda: m.__setitem__("resolved", True))


async def counts(r: Replica, me: str) -> tuple[int, list[str]]:
    """How many comments are open, and keys for the ones that mention `me` (an id, with how many of
    its parts mention them: a new reply that does counts as new). On the sync thread, for the app."""
    items = read(r)
    keys = []
    for c in items:
        n = int(mentions(c.text, me)) + sum(mentions(rp.text, me) for rp in c.replies) if me else 0
        if n:
            keys.append(f"{c.id}:{n}")
    return len(items), keys
