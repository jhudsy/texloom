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
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import registry, tls
from .crypto import b64d, b64e
from .i18n import _

log = logging.getLogger(__name__)

SIGN_LABEL, CRYPT_LABEL, TAGS_LABEL = b"texloom list sign", b"texloom list encrypt", b"texloom list tags"
SETTINGS_LABEL = b"texloom list settings"
TIMEOUT = 15
YEAR_MS = 365 * 86400_000
# Must match web/src/listsync.ts. A password only ever opens a small entry holding the list's secret.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1


def explain(code: str) -> str:
    """A ListError's code, in words."""
    return {
        "needs-code": _("This server needs a creation code to store a list."),
        "code-expired": _("The creation code this list was created with has expired."),
        "list-full": _("The server has no room for another list."),
        "list-retired": _("This list was replaced or deleted. Use its new sync link."),
        "wrong-password": _("That username and password don't open a list on this server."),
    }.get(code, code)


class ListError(Exception):
    """code: needs-code, code-expired, list-full, list-retired, wrong-password, or a short description."""

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
        if not values or u.scheme not in ("http", "https") or not u.netloc:
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


def _canon(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def empty() -> dict:
    return {"v": 1, "projects": {}, "gone": {}}


def merge(a: dict, b: dict) -> dict:
    """Two lists as one, the same whichever way round. The most recently opened copy of an entry
    wins, except that an edit link is never replaced by a read-only one, and the dates keep the
    earliest creation and the latest change; removed projects stay removed unless they were opened
    again afterwards."""
    out = empty()
    for doc in (a, b):
        for p, when in doc.get("gone", {}).items():
            out["gone"][p] = max(out["gone"].get(p, 0), when)
    for p in sorted({*a.get("projects", {}), *b.get("projects", {})}):
        x, y = a.get("projects", {}).get(p), b.get("projects", {}).get(p)
        if x and y:
            new, old = (x, y) if (x.get("at", 0), _canon(x)) >= (y.get("at", 0), _canon(y)) else (y, x)
            e = {**old, **{k: v for k, v in new.items() if v not in (None, "")}}
            if old.get("w") and not new.get("w"):
                e["f"], e["w"] = old["f"], True
            if x.get("c") and y.get("c"):
                e["c"] = min(x["c"], y["c"])
            if x.get("m") and y.get("m"):
                e["m"] = max(x["m"], y["m"])
        else:
            e = dict(x or y)
        if e.get("at", 0) > out["gone"].get(p, 0):
            out["projects"][p] = e
    return out


def _forget_old_removals(doc: dict) -> dict:
    cutoff = time.time() * 1000 - YEAR_MS
    return {**doc, "gone": {p: when for p, when in doc["gone"].items() if when >= cutoff}}


# --- tags (web/src/listcore.ts must match) --------------------------------------------------------
def empty_tags() -> dict:
    return {"v": 1, "tags": {}, "on": {}}


# The order and length matter: a tag nobody has chosen a colour for gets one from its id, the same
# on every device, and changing this list would repaint those tags.
TAG_COLOURS = ("red", "orange", "yellow", "green", "teal", "blue", "indigo", "purple", "pink", "grey")


def tag_colour(tid: str, chosen: str | None = None) -> str:
    """The colour a tag shows: the one chosen for it if this version knows it, else one from its id."""
    if chosen in TAG_COLOURS:
        return chosen
    total = 0
    for ch in tid:
        total = (total * 31 + ord(ch)) & 0xFFFFFFFF
    return TAG_COLOURS[total % len(TAG_COLOURS)]


def fold_tag(name: str) -> str:
    """The form in which two tag names count as the same."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().lower()


def _later_mark(a: int, b: int) -> int:
    return (a if abs(a) > abs(b) else b) if abs(a) != abs(b) else max(a, b)


def merge_tags(a: dict, b: dict) -> dict:
    """Two sets of tags as one, the same whichever way round. Each tag, and each tag on each project
    (`on`: when last assigned, negative if taken off), takes its latest change; on a tie, deleting
    and assigning win. Tags given the same name apart become one, with every project either had."""
    out = empty_tags()
    atags, btags = a.get("tags", {}), b.get("tags", {})
    for tid in {*atags, *btags}:
        x, y = atags.get(tid), btags.get(tid)
        if not x or not y:
            out["tags"][tid] = dict(x or y)
            continue
        if x["at"] != y["at"]:
            first = x["at"] > y["at"]
        elif bool(x.get("gone")) != bool(y.get("gone")):
            first = bool(x.get("gone"))
        else:
            first = _canon(x) >= _canon(y)
        out["tags"][tid] = dict(x if first else y)
    for p in {*a.get("on", {}), *b.get("on", {})}:
        marks = out["on"][p] = dict(a.get("on", {}).get(p, {}))
        for tid, when in b.get("on", {}).get(p, {}).items():
            marks[tid] = _later_mark(marks[tid], when) if tid in marks else when
    named: dict[str, str] = {}
    for tid in sorted(out["tags"]):
        tag = out["tags"][tid]
        if tag.get("gone"):
            continue
        keep = named.setdefault(fold_tag(tag["n"]), tid)
        if keep == tid:
            continue
        out["tags"][tid] = {"n": "", "at": tag["at"], "gone": True}
        for marks in out["on"].values():
            if tid in marks:
                when = marks.pop(tid)
                marks[keep] = _later_mark(marks[keep], when) if keep in marks else when
    return out


def tidy_tags(doc: dict, listed: set[str], now: int | None = None) -> dict:
    """Drop what is no longer needed: old deletions, marks for deleted tags, and projects that left the list."""
    cutoff = (int(time.time() * 1000) if now is None else now) - YEAR_MS
    out = empty_tags()
    out["tags"] = {tid: tag for tid, tag in doc.get("tags", {}).items() if not tag.get("gone") or tag["at"] >= cutoff}
    for p, marks in doc.get("on", {}).items():
        if p not in listed:
            continue
        kept = {tid: when for tid, when in marks.items()
                if tid in out["tags"] and not out["tags"][tid].get("gone") and (when > 0 or -when >= cutoff)}
        if kept:
            out["on"][p] = kept
    return out


# --- settings that follow the list (web/src/listcore.ts must match) --------------------------------
def empty_settings() -> dict:
    return {"v": 1, "s": {}}


def merge_settings(a: dict, b: dict) -> dict:
    """Settings that travel with the list, such as the creation code, each with when it was set:
    the latest wins, and a tie goes the same way on every device."""
    out = empty_settings()
    for name in sorted({*a.get("s", {}), *b.get("s", {})}):
        x, y = a.get("s", {}).get(name), b.get("s", {}).get(name)
        out["s"][name] = y if not x else x if not y else x if (x["at"], _canon(x)) >= (y["at"], _canon(y)) else y
    return out


def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    """(status, body, headers)."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=tls.context()) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except (urllib.error.URLError, OSError) as e:
        raise ListError(f"can't reach the server: {e}") from None


def pull(key: ListKey, server: str) -> tuple[int, dict | None]:
    """(sequence number, the list), or (0, None) if the server has none."""
    status, body, headers = _request("GET", f"{server}/api/list/{key.id}")
    if status == 404:
        return 0, None
    if status == 410:
        raise ListError("list-retired")
    if status != 200:
        raise ListError(f"server said {status}")
    return int(headers.get("X-Seq", 0)), json.loads(key.decrypt(body))


def _push(key: ListKey, server: str, seq: int, doc: dict, code: str | None) -> bool:
    """False if someone else got there first."""
    data = key.encrypt(_canon(doc).encode())
    headers = {"X-Seq": str(seq), "X-Sig": key.sign_put(seq, data), "Content-Type": "application/octet-stream"}
    if code:
        headers["X-Create-Code"] = code
    status, body, _headers = _request("PUT", f"{server}/api/list/{key.id}", data, headers)
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
    for _attempt in range(5):
        seq, remote = pull(key, server)
        merged = _forget_old_removals(merge(remote or empty(), local))
        if remote is not None and _canon(merged) == _canon(remote):
            return merged
        if _push(key, server, seq + 1, merged, code):
            return merged
    raise ListError("the list keeps changing: try again")


def sync_tags(key: ListKey, server: str, local: dict, listed: set[str], code: str | None = None) -> dict:
    """Merge `local` with the tags kept beside the list (an entry of their own, so that clients which
    don't know about tags can't lose them). A new entry needs a creation code; without one, none is
    made until there is something to put in it."""
    entry = ListKey(hmac.new(key.secret, TAGS_LABEL, hashlib.sha256).digest())
    for _attempt in range(5):
        seq, remote = pull(entry, server)
        merged = tidy_tags(merge_tags(remote or empty_tags(), local), listed)
        before = remote if remote is not None else None if code else empty_tags()
        if before is not None and _canon(merged) == _canon(before):
            return merged
        if _push(entry, server, seq + 1, merged, code):
            return merged
    raise ListError("the list keeps changing: try again")


def sync_settings(key: ListKey, server: str, local: dict, code: str | None = None) -> dict:
    """Merge `local` with the settings kept beside the list, such as the creation code (an entry of
    their own, like tags). A missing entry isn't made until there is something to put in it."""
    entry = ListKey(hmac.new(key.secret, SETTINGS_LABEL, hashlib.sha256).digest())
    for _attempt in range(5):
        seq, remote = pull(entry, server)
        merged = merge_settings(remote or empty_settings(), local)
        if _canon(merged) == _canon(remote if remote is not None else empty_settings()):
            return merged
        if _push(entry, server, seq + 1, merged, code):
            return merged
    raise ListError("the list keeps changing: try again")


def password_secret(username: str, password: str) -> bytes:
    """The secret a username and password stand for. Slow on purpose, and done on this computer:
    neither is ever sent anywhere."""
    salt = "texloom list password v1:" + unicodedata.normalize("NFKC", username.strip().lower())
    return hashlib.scrypt(unicodedata.normalize("NFKC", password).encode(), salt=salt.encode(),
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=256 * 1024 * 1024)


def unlock_with_password(server: str, username: str, password: str) -> ListKey:
    """The list a username and password open."""
    unlocking = ListKey(password_secret(username, password))
    try:
        _seq, entry = pull(unlocking, server)
    except ListError as e:
        if e.code != "list-retired":  # a password that was changed or removed
            raise
        entry = None
    if entry is None:
        raise ListError("wrong-password")
    return ListKey(b64d(entry["wrap"]))


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


def _name_file() -> Path:
    return registry.config_dir() / "name.json"


def my_name() -> tuple[str, int]:
    """The name shown to collaborators, and when it was set (milliseconds; 0 if never). With a
    project list, the latest one set anywhere, in a browser or here, follows the list."""
    try:
        d = json.loads(_name_file().read_text())
        if isinstance(d, dict) and isinstance(d.get("v"), str) and isinstance(d.get("at"), int):
            return d["v"], d["at"]
    except (OSError, ValueError):
        pass
    return "", 0


def remember_name(name: str, at: int | None = None) -> None:
    p = _name_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"v": name.strip()[:64], "at": int(time.time() * 1000) if at is None else at}, f)


def _codes_file() -> Path:
    return registry.config_dir() / "creation-codes.json"


def _saved_codes() -> dict:
    try:
        codes = json.loads(_codes_file().read_text())
        return codes if isinstance(codes, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_code(server: str, code: str, at: int | None = None) -> None:
    """Keep the creation code used on `server`, with when it was entered; with a project list, the
    latest one entered anywhere follows the list."""
    codes = {**_saved_codes(), server.rstrip("/"): {"v": code, "at": int(time.time() * 1000) if at is None else at}}
    p = _codes_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(codes, f, indent=2)


def code_for(server: str) -> str:
    """The creation code to offer for a new project on `server`: the latest one entered here or on
    the project list, else one a folder here already uses."""
    saved = _saved_codes().get(server.rstrip("/"))
    if isinstance(saved, dict) and isinstance(saved.get("v"), str) and saved["v"]:
        return saved["v"]
    return _known_code(server) or ""


def folder_dates(d: Path) -> tuple[int, int]:
    """(when this folder became a project here, when one of its files last changed), in ms."""
    from .cli import config_path
    from .folder import project_files
    st = config_path(d).parent.stat()
    born = getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else config_path(d).stat().st_mtime)
    newest = 0.0
    for rel in project_files(d):
        try:
            newest = max(newest, (d / rel).stat().st_mtime)
        except OSError:
            continue
    return int(born * 1000), int((newest or born) * 1000)


def local_folders(server: str) -> dict:
    """This computer's synced folders on `server`, as a list."""
    from .cli import config_path, load
    doc = empty()
    for d in registry.folders():
        try:
            keys, there = load(d)
            at = int(config_path(d).stat().st_mtime * 1000)
            created, changed = folder_dates(d)
        except (OSError, ValueError, KeyError):
            continue
        if there.rstrip("/") == server.rstrip("/"):
            doc["projects"][keys.project] = {"f": keys.fragment(), "n": d.name, "at": at, "w": keys.writable, "c": created, "m": changed}
    return doc


def not_here(doc: dict, server: str) -> list[tuple[str, dict]]:
    """Entries of the list with no folder on this computer, most recently opened first."""
    here = local_folders(server)["projects"]
    return sorted(((p, e) for p, e in doc["projects"].items() if p not in here), key=lambda x: -x[1].get("at", 0))


def sync_this_computer(code: str | None = None) -> tuple[str, dict] | None:
    """Merge this computer's folders into its saved list: (server, the list), or None if it has none."""
    link = saved_link()
    if not link:
        return None
    key, server = ListKey.from_link(link)
    doc = sync(key, server, local_folders(server), code)
    _sync_my_tags(key, server, doc, code)
    _sync_my_settings(key, server, code)
    return server, doc


_tags_error: str | None = None


def tags_problem() -> str | None:
    """Why tags aren't reaching the server, if they aren't; the list itself may be fine."""
    return _tags_error


def _known_code(server: str) -> str | None:
    """A creation code this computer already uses on `server`."""
    from .cli import load, load_config
    for d in registry.folders():
        try:
            if load(d)[1].rstrip("/") == server.rstrip("/") and load_config(d).get("create_code"):
                return load_config(d)["create_code"]
        except (OSError, ValueError, KeyError):
            continue
    return None


def _sync_my_tags(key: ListKey, server: str, doc: dict, code: str | None) -> None:
    global _tags_error
    from . import tags
    from .cli import load
    listed = set(doc["projects"])
    here = set(listed)  # a folder on another server isn't on this list, but keeps its tags here
    for d in registry.folders():
        try:
            here.add(load(d)[0].project)
        except (OSError, ValueError, KeyError):
            continue
    try:
        tags.apply(sync_tags(key, server, tags.doc(), listed, code or _known_code(server)), here)
        _tags_error = None
    except ListError as e:
        _tags_error = e.code


def _sync_my_settings(key: ListKey, server: str, code: str | None) -> None:
    """The creation code and your name follow the list: this computer's latest ones go on it, and
    later ones set elsewhere (say in the web app) come back here."""
    saved = _saved_codes().get(server.rstrip("/"))
    local = empty_settings()
    if isinstance(saved, dict) and isinstance(saved.get("v"), str) and isinstance(saved.get("at"), int):
        local["s"]["code"] = {"v": saved["v"], "at": saved["at"]}
    elif known := _known_code(server):
        local["s"]["code"] = {"v": known, "at": 1}  # from before codes were dated: older than any that is
    name, name_at = my_name()
    if name_at:
        local["s"]["name"] = {"v": name, "at": name_at}
    try:
        merged = sync_settings(key, server, local, code or code_for(server) or None)
    except ListError as e:
        log.debug("list settings: %s", e.code)
        return
    got = merged["s"].get("code")
    if got and got != local["s"].get("code") and isinstance(got.get("v"), str) and isinstance(got.get("at"), int):
        remember_code(server, got["v"], got["at"])
    got = merged["s"].get("name")
    if got and got != local["s"].get("name") and isinstance(got.get("v"), str) and isinstance(got.get("at"), int):
        remember_name(got["v"], got["at"])
