"""A person's own tags on projects, on this computer: the desktop side of web/src/tags.ts.

They are never written into a project, so collaborators don't see them. With a project list, they
also sync to the server, encrypted, next to it (listsync.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

from . import registry
from .crypto import b64e
from .listsync import _canon, empty_tags, fold_tag, merge_tags, tidy_tags

MAX_NAME = 60
_lock = threading.RLock()  # the window changes tags while a background thread syncs them


@dataclass(frozen=True)
class Tag:
    id: str
    name: str


def _file():
    return registry.config_dir() / "tags.json"


def doc() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_tags()
    return {**empty_tags(), **data} if isinstance(data, dict) else empty_tags()


def _write(data: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # says which projects you have
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
    tmp.replace(f)


def _stamp(prev: int = 0) -> int:
    """Later than `prev` even if this computer's clock is behind, so a change always takes effect."""
    return max(int(time.time() * 1000), prev + 1)


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip()[:MAX_NAME]


def _sort_key(tag: Tag) -> str:
    return fold_search(tag.name)


def fold_search(text: str) -> str:
    """For finding text whatever its case or accents."""
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").casefold()


def _live(data: dict) -> list[Tag]:
    return sorted((Tag(tid, t["n"]) for tid, t in data["tags"].items() if not t.get("gone")), key=_sort_key)


def all_tags() -> list[Tag]:
    return _live(doc())


def find(name: str) -> Tag | None:
    """The tag with this name, however it is capitalised."""
    want = fold_tag(name)
    return next((t for t in all_tags() if fold_tag(t.name) == want), None)


def reader() -> Callable[[str], list[Tag]]:
    """For looking up many projects: reads the stored tags once."""
    data = doc()
    every = _live(data)

    def of(project: str) -> list[Tag]:
        marks = data["on"].get(project, {})
        return [t for t in every if marks.get(t.id, 0) > 0]

    return of


def uses(tid: str) -> int:
    """How many projects have this tag."""
    return sum(1 for marks in doc()["on"].values() if marks.get(tid, 0) > 0)


def create(name: str) -> Tag | None:
    clean = clean_name(name)
    if not clean:
        return None
    with _lock:
        had = find(clean)
        if had:
            return had
        data, tid = doc(), b64e(os.urandom(6))
        data["tags"][tid] = {"n": clean, "at": _stamp()}
        _write(data)
        return Tag(tid, clean)


def rename(tid: str, name: str) -> None:
    """Rename a tag. To the name of another tag, it becomes that tag, keeping that tag's spelling."""
    clean = clean_name(name)
    with _lock:
        data = doc()
        tag = data["tags"].get(tid)
        other = find(clean) if clean else None
        if clean and tag and not tag.get("gone"):
            data["tags"][tid] = {**tag, "n": other.name if other and other.id != tid else clean, "at": _stamp(tag["at"])}
            _write(merge_tags(data, data))


def remove(tid: str) -> None:
    with _lock:
        data = doc()
        if tid in data["tags"]:
            data["tags"][tid] = {"n": "", "at": _stamp(data["tags"][tid]["at"]), "gone": True}
            for marks in data["on"].values():
                marks.pop(tid, None)
            _write(data)


def assign(projects: list[str], tid: str, on: bool) -> None:
    """Put a tag on these projects, or take it off them."""
    with _lock:
        data = doc()
        for p in projects:
            marks = data["on"].setdefault(p, {})
            if (marks.get(tid, 0) > 0) == on:
                continue
            when = _stamp(abs(marks.get(tid, 0)))
            marks[tid] = when if on else -when
        _write(data)


def keep_only(projects: set[str]) -> None:
    """Forget the tags of every other project (for when there is no list to keep them on)."""
    with _lock:
        _write(tidy_tags(doc(), projects))


def apply(synced: dict, projects: set[str]) -> bool:
    """Take on the merged tags, keeping any change made meanwhile. True if anything here changed."""
    with _lock:
        before = doc()
        after = tidy_tags(merge_tags(before, synced), projects)
        if _canon(before) == _canon(after):
            return False
        _write(after)
        return True
