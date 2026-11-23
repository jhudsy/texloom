"""What clients send that every part of the relay checks: ids, base64, signatures and app versions."""

from __future__ import annotations

import re
from collections.abc import Container

from texloom.crypto import b64d as _b64d
from texloom.crypto import signer

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url(32-byte pubkey)
BLOB_RE = re.compile(r"^[0-9a-f]{32}$")


def b64d(s: object) -> bytes:
    """Base64url from a client: anything but text is refused."""
    if not isinstance(s, str):
        raise ValueError("expected base64 text")
    return _b64d(s)


def verify(project: str, msg: bytes, sig: bytes, revoked: Container[str] = frozenset()) -> bool:
    """Signed by the project's owner key, or by a personal key the owner granted and hasn't revoked.
    `revoked` holds canonical spellings, as signer() returns them."""
    who = signer(project, msg, sig)
    return who is not None and who not in revoked


VERSION_RE = re.compile(r"\d{1,4}(\.\d{1,6}){0,3}")
APPS = ("desktop", "web")


def version_tuple(v: object) -> tuple[int, ...] | None:
    """"0.4.30" as (0, 4, 30), or None if it isn't a version."""
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and VERSION_RE.fullmatch(v) else None
