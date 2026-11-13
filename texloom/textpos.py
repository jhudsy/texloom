"""Positions in a text, by unit, and the only place a project text is edited by position.

Three units count the same place differently once a text holds non-ASCII characters:

- `Char`: an index into a Python str (code points);
- `Byte`: a UTF-8 byte offset, what pycrdt's `Text.insert`, `del text[a:b]` and its change events
  count;
- `U16`: a UTF-16 code unit, what Yjs, the web app's editor and sticky indices count.

Mixing them up put edits and comments in the wrong place before (anchors.py; the web app's editor
counting "\\r\\n" differently from the shared text was the same kind of bug). They are distinct types
here, so mypy (run on the modules that handle positions, in CI) rejects passing one for another,
and `scripts/check_textpos.py` checks that nothing else edits a project text by position.
"""

from __future__ import annotations

from typing import NewType

from pycrdt import Text

Char = NewType("Char", int)
Byte = NewType("Byte", int)
U16 = NewType("U16", int)


def byte_of(s: str, i: Char) -> Byte:
    return Byte(len(s[:i].encode()))


def u16_of(s: str, i: Char) -> U16:
    return U16(len(s[:i].encode("utf-16-le")) // 2)


def char_of_byte(s: str, b: Byte) -> Char:
    """The character at (or containing) byte offset `b`."""
    return Char(len(s.encode()[:b].decode("utf-8", errors="ignore")))


def char_of_u16(s: str, u: U16) -> Char:
    """The character at (or containing) UTF-16 offset `u`."""
    return Char(len(s.encode("utf-16-le")[:2 * u].decode("utf-16-le", errors="ignore")))


def length_u16(s: str) -> U16:
    return U16(len(s.encode("utf-16-le")) // 2)


def replace(t: Text, s: str, a: Char, b: Char, new: str = "") -> None:
    """Replace characters [a, b) of `t`, whose content is `s`, with `new`. Call inside a change."""
    if not 0 <= a <= b <= len(s):
        raise ValueError(f"no characters {a}..{b} in a text of {len(s)}")
    ba, bb = byte_of(s, a), byte_of(s, b)
    if bb > ba:
        del t[ba:bb]
    if new:
        t.insert(ba, new)


def insert(t: Text, s: str, at: Char, new: str) -> None:
    replace(t, s, at, at, new)


def delete(t: Text, s: str, a: Char, b: Char) -> None:
    replace(t, s, a, b)
