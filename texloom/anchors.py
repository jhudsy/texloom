"""Yjs relative positions ("anchors": where a comment or suggestion starts and ends), made and
read without pycrdt's StickyIndex.

pycrdt (0.14) places a StickyIndex wrongly in text holding non-ASCII characters once the text
is made of several pieces (it mixes UTF-8 and UTF-16 offsets): an anchor it makes can point at
another character, and one it reads can come back at another index. On ASCII text it is right,
including text that once held non-ASCII characters. So:

- reading an anchor: pycrdt only when the text is ASCII (`index_in`), else nothing;
- making anchors: only in a text that is one fresh piece (just created from a string), whose
  characters' ids follow from the first one's (`fresh_anchor`).

The encoding is Yjs's encodeRelativePosition: 0, the item id (client, clock) as varuints, then
assoc as a lib0 varint (0: sticks to the character at the index; -1: to the one before it).
"""

from __future__ import annotations

from pycrdt import Assoc, StickyIndex, Text


def _varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _varint(n: int) -> bytes:  # lib0 writeVarInt: sign in the first byte's second bit
    neg, n = n < 0, abs(n)
    out = bytearray([(0x80 if n > 0x3F else 0) | (0x40 if neg else 0) | (n & 0x3F)])
    n >>= 6
    while n:
        out.append((0x80 if n > 0x7F else 0) | (n & 0x7F))
        n >>= 7
    return bytes(out)


def _read_varuint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if x < 0x80:
            return n, i


def item_anchor(client: int, clock: int, assoc: int) -> bytes:
    return b"\x00" + _varuint(client) + _varuint(clock) + _varint(assoc)


def anchor_id(raw: bytes) -> tuple[int, int] | None:
    """The (client, clock) an item anchor points at, or None for any other kind."""
    if not raw or raw[0] != 0:
        return None
    client, i = _read_varuint(raw, 1)
    clock, _ = _read_varuint(raw, i)
    return client, clock


def u16(s: str, i: int) -> int:
    """Python string index -> UTF-16 offset (what Yjs counts in)."""
    return len(s[:i].encode("utf-16-le")) // 2


def index_in(text: Text, s: str, raw: bytes) -> int | None:
    """Where an anchor is in `text` (whose content is `s`), as a UTF-16 offset; None if that can't
    be told reliably (non-ASCII text, or a damaged anchor)."""
    if not s.isascii() or not raw:
        return None
    try:
        k = StickyIndex.decode(bytes(raw), text).get_index()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - pycrdt panics on some anchors (a PanicException isn't an Exception)
        return None
    return k if 0 <= k <= len(s) else None


def assoc_of(raw: bytes) -> int:
    """An anchor's assoc: 0 or -1 (the last byte, a one-byte varint for these values)."""
    return -1 if raw and raw[-1] == 0x41 else 0


def fresh_anchor(text: Text, n16: int, index: int, assoc: int) -> bytes | None:
    """An anchor at UTF-16 offset `index` in `text`, which must be one piece just made from a
    string of n16 UTF-16 units (character i has clock first + i). None if the text is empty."""
    if n16 == 0:
        return None
    first = anchor_id(text.sticky_index(0, Assoc.AFTER).encode())  # offset 0 is right whatever the text
    if first is None:
        return None
    client, clock = first
    if assoc >= 0 and index < n16:
        return item_anchor(client, clock + index, 0)
    if assoc < 0 and index > 0:
        return item_anchor(client, clock + min(index, n16) - 1, -1)
    return item_anchor(client, clock, 0) if index < n16 else item_anchor(client, clock + n16 - 1, -1)
