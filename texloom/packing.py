"""Compressed document updates: what a snapshot (or a big change) holds once compressed.

A packed update is MARKER followed by the update compressed with zlib (deflate, with its checksum),
and it's encrypted like any other. Encrypted data doesn't compress, so this happens before.
Anything else is a plain update. Data counts as packed only if it has the marker and decompresses
cleanly (zlib checks it), so a plain update can't be taken for one.

Every app reads packed updates since 0.4.31, and writes them since 0.4.36 (issue #18): snapshots,
changes and what a peer catching up is sent, when PACK_FROM bytes or more and packing makes them
smaller. Keep in step with web/src/packing.ts; scripts/packing-vectors.json checks both.
"""

from __future__ import annotations

import zlib

MARKER = b"\x00TLz"
MAX_UNPACKED = 256 * 1024 * 1024  # an update this large would be refused anyway; stops a decompression bomb
PACK_FROM = 64 * 1024  # smaller updates (typing) go as they are: little to gain


def pack(update: bytes) -> bytes:
    return MARKER + zlib.compress(update, 6)


def packed(update: bytes) -> bytes:
    """What to send for an update: packed, if it's big enough to be worth it and packing makes
    it smaller; else as it is."""
    if len(update) < PACK_FROM:
        return update
    p = pack(update)
    return p if len(p) < len(update) else update


def unpack(data: bytes) -> bytes:
    """The update itself: decompressed if it's packed, else as it is."""
    if not data.startswith(MARKER):
        return data
    d = zlib.decompressobj()
    try:
        out = d.decompress(data[len(MARKER):], MAX_UNPACKED)
    except zlib.error:
        return data
    if not d.eof or d.unconsumed_tail or d.unused_data:
        return data  # cut short, too large, or something after it: not one of ours
    return out
