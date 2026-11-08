"""Keep a person's list of projects on the server, encrypted, so it can follow them between
devices (the same format as web/src/listsync.ts, which must match).

A list is unlocked by one random secret, carried in a "sync link" (https://server/#sync=…; like a
project link, the part after # never reaches the server). From the secret come an encryption key
and an Ed25519 signing key; the public key is the list's id on the server. The server therefore
stores something it can't read and can't change. See server/lists.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import registry
from .crypto import b64d, b64e

SIGN_LABEL, CRYPT_LABEL = b"texloom list sign", b"texloom list encrypt"
TIMEOUT = 15
YEAR_MS = 365 * 86400_000


class ListError(Exception):
    """code: needs-code, code-expired, list-full, not-found, or a short description."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ListKey:
    secret: bytes

    @classmethod
    def generate(cls) -> ListKey:
        return cls(os.urandom(32))

    @classmethod
    def from_link(cls, link: str) -> tuple[ListKey, str]:
        u = urlsplit(link.strip())
        values = parse_qs(u.fragment).get("sync")
        if not values or not u.scheme or not u.netloc:
            raise ValueError("not a sync link")
        secret = b64d(values[0])
        if len(secret) != 32:
            raise ValueError("not a sync link")
        return cls(secret), f"{u.scheme}://{u.netloc}"

    def link(self, server: str) -> str:
        return f"{server.rstrip('/')}/#sync={b64e(self.secret)}"

    @property
    def _signer(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(hmac.new(self.secret, SIGN_LABEL, hashlib.sha256).digest())

    @property
    def id(self) -> str:
        return b64e(self._signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def encrypt(self, plain: bytes) -> bytes:
        iv = os.urandom(12)
        return iv + AESGCM(hmac.new(self.secret, CRYPT_LABEL, hashlib.sha256).digest()).encrypt(iv, plain, None)

    def decrypt(self, data: bytes) -> bytes:
        return AESGCM(hmac.new(self.secret, CRYPT_LABEL, hashlib.sha256).digest()).decrypt(data[:12], data[12:], None)

    def sign_put(self, seq: int, data: bytes) -> str:
        return b64e(self._signer.sign(f"list:{self.id}:{seq}:".encode() + data))


def empty() -> dict:
    return {"v": 1, "projects": {}, "gone": {}}


def merge(a: dict, b: dict) -> dict:
    """Two lists as one, the same whichever way round. The most recently opened copy of an entry
    wins, except that an edit link is never replaced by a read-only one; removed projects stay
    removed unless they were opened again afterwards."""
    out = empty()
    for doc in (a, b):
        for p, when in doc.get("gone", {}).items():
            out["gone"][p] = max(out["gone"].get(p, 0), when)
    for p in sorted({*a.get("projects", {}), *b.get("projects", {})}):
        x, y = a.get("projects", {}).get(p), b.get("projects", {}).get(p)
        if x and y:
            new, old = (x, y) if (x.get("at", 0), json.dumps(x, sort_keys=True)) >= (y.get("at", 0), json.dumps(y, sort_keys=True)) else (y, x)
            e = {**old, **{k: v for k, v in new.items() if v not in (None, "")}}
            if old.get("w") and not new.get("w"):
                e["f"], e["w"] = old["f"], True
        else:
            e = dict(x or y)
        if e.get("at", 0) > out["gone"].get(p, 0):
            out["projects"][p] = e
    return out


def _forget_old_removals(doc: dict) -> dict:
    cutoff = time.time() * 1000 - YEAR_MS
    return {**doc, "gone": {p: when for p, when in doc["gone"].items() if when >= cutoff}}


def _canon(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except (urllib.error.URLError, OSError) as e:
        raise ListError(f"can't reach the server: {e}") from None


def pull(key: ListKey, server: str) -> tuple[int, dict | None]:
    """(sequence number, the list), or (0, None) if the server has none."""
    status, body, headers = _request("GET", f"{server}/api/list/{key.id}")
    if status == 404:
        return 0, None
    if status != 200:
        raise ListError(f"server said {status}")
    return int(headers.get("X-Seq", headers.get("x-seq", 0))), json.loads(key.decrypt(body))


def _push(key: ListKey, server: str, seq: int, doc: dict, code: str | None) -> bool:
    """False if someone else got there first."""
    data = key.encrypt(_canon(doc).encode())
    headers = {"X-Seq": str(seq), "X-Sig": key.sign_put(seq, data), "Content-Type": "application/octet-stream"}
    if code:
        headers["X-Create-Code"] = code
    status, body, _ = _request("PUT", f"{server}/api/list/{key.id}", data, headers)
    if status == 409:
        return False
    if status != 200:
        try:
            raise ListError(json.loads(body).get("code", f"server said {status}"))
        except (ValueError, AttributeError):
            raise ListError(f"server said {status}") from None
    return True


def sync(key: ListKey, server: str, local: dict, code: str | None = None) -> dict:
    """Merge `local` with the server's list, update the server if that changed it; returns the result."""
    for _ in range(5):
        seq, remote = pull(key, server)
        merged = _forget_old_removals(merge(remote or empty(), local))
        if remote is not None and _canon(merged) == _canon(remote):
            return merged
        if _push(key, server, seq + 1, merged, code):
            return merged
    raise ListError("the list keeps changing: try again")


# --- this computer -----------------------------------------------------------------------------
def _saved() -> Path:
    return registry.config_dir() / "project-list.json"


def saved_link() -> str | None:
    try:
        return json.loads(_saved().read_text()).get("link") or None
    except (OSError, ValueError):
        return None


def save_link(link: str | None) -> None:
    if link is None:
        _saved().unlink(missing_ok=True)
        return
    p = _saved()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it opens every project on the list
    with os.fdopen(fd, "w") as f:
        json.dump({"link": link}, f)


def local_folders(server: str) -> dict:
    """This computer's synced folders on `server`, as a list."""
    from .cli import config_path, load_config
    from .crypto import Keys
    doc = empty()
    for d in registry.folders():
        try:
            keys, there = Keys.from_link(load_config(d)["link"])
            at = int(config_path(d).stat().st_mtime * 1000)
        except (OSError, ValueError, KeyError):
            continue
        if there.rstrip("/") == server.rstrip("/"):
            doc["projects"][keys.project] = {"f": keys.link(there).split("#", 1)[1], "n": d.name, "at": at, "w": keys.writable}
    return doc


def not_here(doc: dict, server: str) -> list[tuple[str, dict]]:
    """Entries of the list with no folder on this computer, most recently opened first."""
    here = local_folders(server)["projects"]
    return sorted(((p, e) for p, e in doc["projects"].items() if p not in here), key=lambda x: -x[1].get("at", 0))


def sync_this_computer(code: str | None = None) -> tuple[ListKey, str, dict] | None:
    """Merge this computer's folders into its saved list; None if it has no list."""
    link = saved_link()
    if not link:
        return None
    key, server = ListKey.from_link(link)
    return key, server, sync(key, server, local_folders(server), code)
