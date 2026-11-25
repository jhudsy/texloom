"""What clients send that every part of the relay checks: ids, base64, signatures and app versions."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Container

from aiohttp import web

from texloom.crypto import b64d as _b64d
from texloom.crypto import b64e, signer

PROJECT_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url(32-byte pubkey); use canonical_id()
BLOB_RE = re.compile(r"[0-9a-f]{32}")  # (fullmatch)


def canonical_id(s: object) -> bool:
    """A project's or list's id as clients spell it: base64url of 32 bytes, in its one spelling.
    Base64 decoders accept four spellings of each (the last character carries two unused bits),
    and every record kept by id (owners, revocations, deletions, storage modes, retired lists)
    must not be sidestepped with another one."""
    return isinstance(s, str) and PROJECT_RE.fullmatch(s) is not None and b64e(_b64d(s)) == s


def blob_id(s: object) -> bool:
    return isinstance(s, str) and BLOB_RE.fullmatch(s) is not None


def address_key(ip: str) -> str:
    """What per-address limits count by: an IPv4 address, or an IPv6 address's /64 (one home or
    server gets a whole /64, so counting single IPv6 addresses would limit nothing)."""
    try:
        a = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip.strip()[:64] or "?"
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def client_address(request: web.Request) -> str | None:
    """Who sent a request, for per-address limits (address_key). Behind the reverse proxy on this
    computer (deploy/install.sh's nginx, deploy/Caddyfile), the proxy's X-Real-IP, which it sets
    from the real client. None for a request from this computer that doesn't name one (a check,
    or a proxy that isn't set up to): those aren't limited per address, as they would all count as one."""
    ip = request.remote or ""
    if ip in ("127.0.0.1", "::1") or ip.startswith("127."):
        named = request.headers.get("X-Real-IP", "").strip()
        return address_key(named) if named else None
    return address_key(ip) if ip else None


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
