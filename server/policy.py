"""The relay's storage rules: sizes and limits, creation codes and their allowances (CodeBook),
and who may use the server and what it stores for them (Policy). They decide on top of the Store
(store.py), which only keeps things; the WebSocket protocol (relay.py), the HTTP endpoints
(endpoints.py) and the command line ask them. See the server's help (cli.py) for how allowances
are set."""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import logging
import math
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from texloom import __version__

from .lists import Lists
from .store import key
from .wire import APPS, version_tuple

if TYPE_CHECKING:
    from .store import Store, Tx

log = logging.getLogger("texloom.relay")

KiB, MiB, GiB = 1 << 10, 1 << 20, 1 << 30
UNLIMITED = math.inf
# Memory for big work at once, shared (see Budget): catch-up replies, large frames being handled,
# uploads being checked and stored, arXiv imports. Work that doesn't fit waits its turn (a large
# frame is answered "server busy" instead). With HELD_MAX, OUT_MAX, the arXiv cache and the
# process itself (about 120 MB), the default stays under the service's MemoryMax
# (deploy/install.sh: 1G); `--memory` (setting `memory`) changes it, and MemoryMax with it.
MEMORY = 512 * MiB
PURGE_PAUSE = 0.01  # seconds between deleted projects: lets live editing through


def parse_size(v: str) -> float:
    """'0', '500M', '2G', '1.5G', 'unlimited' -> bytes (or inf)."""
    v = v.strip().lower()
    if v in ("unlimited", "inf", "none"):
        return UNLIMITED
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?", v)
    if not m:
        raise ValueError(f"not a size: {v!r}")
    return int(float(m[1]) * {"": 1, "k": KiB, "m": MiB, "g": GiB, "t": 1 << 40}[m[2]])


def fmt_size(n: float) -> str:
    if n == UNLIMITED:
        return "unlimited"
    for unit, size in (("G", GiB), ("M", MiB), ("K", KiB)):
        if n >= size:
            return f"{n / size:.1f}{unit}".replace(".0", "")
    return str(int(n))


UNTIL_RE = re.compile(r"^until=(\S*)$", re.IGNORECASE)


def today() -> dt.date:
    """Today in UTC: codes expire at the end of their `until` day, the same everywhere."""
    return dt.datetime.now(dt.UTC).date()


def parse_date(v: str) -> dt.date:
    """'2027-11-07' -> a date."""
    try:
        return dt.date.fromisoformat(v.strip())
    except ValueError:
        raise ValueError(f"not a date (YYYY-MM-DD): {v!r}") from None


def add_months(d: dt.date, months: int) -> dt.date:
    """The same day `months` later, or the end of that month when it is shorter (31 Jan + 1 = 28/29 Feb)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return min(d.replace(year=year, month=month, day=1) + dt.timedelta(days=d.day - 1),
               nxt - dt.timedelta(days=1))


@dataclass
class Limits:
    max_bytes: float = UNLIMITED
    min_free: float = 2 * GiB
    project_max: float = 200 * MiB
    memory: float = MEMORY  # see MEMORY


class CodeBook:
    """Storage allowances per creation code, from a text file (see module docstring)."""

    ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def __init__(self, path: Path | list[Path] | None):
        # Several files can be given (e.g. one kept by hand and one kept by a sales service):
        # their codes are combined; "default" comes from the first file that sets it. New
        # codes (--add-code) go into the first file.
        self.paths: list[Path] = [] if path is None else list(path) if isinstance(path, list) else [path]
        self.path = self.paths[0] if self.paths else None
        self._mtime: tuple[float, ...] = ()
        self.codes: dict[str, tuple[float, str]] = {}  # normalized code -> (allowance, label)
        self.until: dict[str, dt.date] = {}  # normalized code -> last day it is valid (if it expires)
        self.default: float = UNLIMITED if self.path is None else 0

    @staticmethod
    def normalize(code: object) -> str:
        return re.sub(r"[^0-9A-Z]", "", str(code).upper())[:32]

    @staticmethod
    def _line_fields(parts: list[str]) -> tuple[str, list[str], str | None]:
        """The size, label words and until= date after the code on a line of a codes file
        (`CODE SIZE [label…] [until=YYYY-MM-DD]`). ValueError if the size is missing or wrong."""
        rest = [p for p in parts[1:] if not UNTIL_RE.match(p)]
        dates = [UNTIL_RE.match(p)[1] for p in parts[1:] if UNTIL_RE.match(p)]  # the last day the code is valid
        if not rest:
            raise ValueError("no allowance")
        parse_size(rest[0])
        return rest[0], rest[1:], dates[-1] if dates else None

    def load(self) -> None:
        if not self.path:
            return
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except FileNotFoundError:
                return -1.0
        mtimes = tuple(mtime(p) for p in self.paths)
        if mtimes == self._mtime:
            return
        codes, until, default = {}, {}, None
        lines = [(p, n, line) for p, m in zip(self.paths, mtimes, strict=True) if m >= 0
                 for n, line in enumerate(p.read_text().splitlines(), 1)]
        for path, n, line in lines:
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            try:
                size, label, until_text = self._line_fields(parts)
            except ValueError as e:
                log.warning("%s line %d: %s (ignored: a line is `code allowance [until=YYYY-MM-DD] [label]`)", path, n, e)
                continue
            allowance, label = parse_size(size), " ".join(label)
            expires = None
            if until_text:
                try:
                    expires = parse_date(until_text)
                except ValueError as e:
                    log.warning("%s line %d: %s (ignored: the code doesn't expire)", path, n, e)
            if parts[0].lower() == "default":
                default = allowance if default is None else default
            else:
                c = self.normalize(parts[0])
                if c in codes:
                    continue  # the first file listing a code decides
                codes[c] = (allowance, label)
                if expires:
                    until[c] = expires
        default = default or 0.0
        self.codes, self.until, self.default, self._mtime = codes, until, default, mtimes
        expired = sum(1 for c in codes if self.expired(c))
        log.info("codes file: %d code(s)%s, default allowance %s", len(codes),
                 f" ({expired} expired)" if expired else "", fmt_size(default))

    def expired(self, code: str) -> bool:
        """True once a code's `until` day has passed. An expired code counts as removed: its
        projects fall back to the default allowance and it can't store new projects."""
        u = self.until.get(code)
        return u is not None and today() > u

    def lapsed(self, owner: str) -> bool:
        """True if `owner` was a creation code that has since expired or been removed."""
        self.load()
        return bool(self.path) and owner != "default" and (owner not in self.codes or self.expired(owner))

    def lapsed_since(self, code: str) -> int | None:
        """Whole days since an expired code's last valid day (None if it hasn't expired)."""
        return (today() - self.until[code]).days if self.expired(code) else None

    @property
    def open_creation(self) -> bool:
        self.load()
        return self.default > 0

    def check(self, code: object) -> str | None:
        """The normalized code if valid (constant-time comparison), else None."""
        if not code:
            return None
        self.load()
        given = self.normalize(code)
        found = None
        for c in self.codes:
            if hmac.compare_digest(c, given):
                found = c
        return None if found is None or self.expired(found) else found

    def allowance(self, owner: str) -> float:
        """Allowance for an owner: a code, or 'default'."""
        self.load()
        if owner in self.codes and not self.expired(owner):
            return self.codes[owner][0]
        # 'default' (unlimited without a codes file), or a code since removed or expired
        return self.default

    def label(self, owner: str) -> str:
        """How the logs name a code: its label, else only its start (logs mustn't hold whole codes)."""
        if owner == "default":
            return owner
        return self.codes.get(owner, (0, ""))[1] or f"code {owner[:4]}…"

    @staticmethod
    def display(code: str) -> str:
        """A normalized code as people see it: XXXX-XXXX-XXXX."""
        return f"{code[:4]}-{code[4:8]}-{code[8:]}"

    def add(self, label: str, allowance: str, until: dt.date | None = None) -> str:
        assert self.path
        parse_size(allowance)
        code = self.display("".join(secrets.choice(self.ALPHABET) for _ in range(12)))  # 60 bits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        with os.fdopen(fd, "a") as f:
            if new:
                f.write("# TeXLoom storage allowances:  <code|default>  <0|size|unlimited>  [until=YYYY-MM-DD]  [label]\n"
                        "default  0\n")
            fields = [code, allowance, *([f"until={until.isoformat()}"] if until else []), *([label] if label else [])]
            f.write("  ".join(fields) + "\n")
        return code

    def renew(self, code: str, until: dt.date | None = None, months: int = 0,
              allowance: str | None = None) -> tuple[str, dt.date | None, str]:
        """Set a code's expiry (`until`, or `months` after the later of today and its current
        expiry) and optionally its allowance, rewriting its line in place (in whichever file
        lists it). Returns the label, the new expiry and the allowance as written."""
        assert self.path
        if allowance is not None:
            parse_size(allowance)
        for path in self.paths:
            try:
                return self._renew_in(path, code, until, months, allowance)
            except (KeyError, FileNotFoundError):
                continue
        raise KeyError(f"no such code: {code}")

    def _renew_in(self, path: Path, code: str, until: dt.date | None, months: int,
                  allowance: str | None) -> tuple[str, dt.date | None, str]:
        want = self.normalize(code)
        lines = path.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            body, hash_, comment = line.rstrip("\n").partition("#")
            parts = body.split()
            if not parts or parts[0].lower() == "default" or self.normalize(parts[0]) != want:
                continue
            size, label, until_text = self._line_fields(parts)
            try:
                current = parse_date(until_text) if until_text else None
            except ValueError:
                current = None
            if months:
                until = add_months(max(today(), current or today()), months)
            elif until is None:
                until = current
            size = allowance or size
            new = "  ".join([parts[0], size, *([f"until={until.isoformat()}"] if until else []), *([" ".join(label)] if label else [])])
            lines[i] = new + (f"  {hash_}{comment}" if hash_ else "") + "\n"
            # written beside it and renamed over it, keeping its mode and owner (the install
            # script makes it root:texloom 640, so that the server can read it)
            st = path.stat()
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (PermissionError, AttributeError):  # AttributeError: no chown on Windows
                pass  # not root: the file stays the current user's, as it was if they could write it
            os.replace(tmp, path)
            return " ".join(label), until, size
        raise KeyError(f"no such code: {code}")


class Policy:
    """Who may use a server and what it stores for them, on top of the Store: asked by the
    WebSocket protocol (relay.py), the HTTP endpoints (endpoints.py), the background upkeep
    (app.py) and the command line's purges (cli.py)."""

    def __init__(self, store: Store | None, codes: CodeBook | None = None, limits: Limits | None = None,
                 private: bool = False, lists: bool = True, min_version: str = "",
                 busy: Callable[[str], bool] = lambda project: False):
        self.codes = codes or CodeBook(None)
        self.limits = limits or Limits()
        self.private = private  # only projects created with a valid creation code may use this server
        # the oldest app version allowed (--min-version): older ones, or apps too old to say, are
        # asked to update, so that support for older data and protocols can be dropped
        self.min_version = version_tuple(min_version) if min_version else None
        self.busy = busy  # busy(project): someone has it open, so it isn't evicted or purged now
        self.want_lists = lists
        self.store: Store = store  # type: ignore[assignment]  # (attach, when the server starts)
        self.lists: Lists | None = None
        if store is not None:
            self.attach(store)

    def attach(self, store: Store) -> None:
        self.store = store
        self.lists = Lists(store.db, store.lists_grew) if self.want_lists else None

    # --- private servers -----------------------------------------------------------
    async def denied(self, project: str, code: object, claim: bool = False) -> str | None:
        """On a private server, why this project may not use it (None if it may). A project
        belongs to a creation code, so people with its link need nothing else. One that has no
        code yet gets the first valid one offered with a signed write (`claim`: an upload, once its
        signature is checked; a stored change claims through may_store): anyone can make up a
        project id, so merely connecting with a code binds nothing. One whose code has lapsed
        waits for a renewal."""
        if not self.private:
            return None
        owner = self.store.owner(project)
        if owner and owner != "default":
            # someone with a valid code of their own may still connect: to move the project onto it
            # (the owner's signed `move`), as they could relay a project they created with it anyway
            return "code-expired" if self.codes.lapsed(owner) and not self.codes.check(code) else None
        valid = self.codes.check(code)
        if not valid:
            return "needs-code"
        if not claim:  # asked before anything is verified: say yes, but don't bind the project yet
            return None
        if owner:
            await self.store.claim(project, valid)
        else:
            await self.store.register(project, valid, self.codes.label(valid))
        return None

    def anyone_creates(self) -> bool:
        """Anyone may create projects that are stored here, without a code."""
        return not self.private and self.codes.open_creation

    # --- app versions ------------------------------------------------------------------
    async def note_client(self, app: object, version: object) -> None:
        """Count an app connecting, by kind and version (shown on the admin page)."""
        a = app if app in APPS else "unknown"
        # versions up to this server's own, as they are; anything else (made up, or unknown) as one
        # row, so that connections can't add rows without end
        v = version if (t := version_tuple(version)) and t <= (version_tuple(__version__) or ()) else "other"
        await self.store.note_client(a, v)

    def too_old(self, version: object) -> bool:
        """Below --min-version (an app that doesn't say is older than this check)."""
        if not self.min_version:
            return False
        v = version_tuple(version)
        return v is None or v < self.min_version

    def update_needed(self) -> dict:
        m = ".".join(map(str, self.min_version or ()))
        return {"code": "update-needed", "min": m, "m": f"this server needs TeXLoom {m} or later"}

    # --- storage policy ------------------------------------------------------------
    async def may_store(self, project: str, nbytes: int, code: str | None, c: Tx) -> str | None:
        """None if this write (signed: checked already) may be stored, else a reason code for the
        client. In `c`, a transaction holding the project (Store.locked), which then stores it: a
        limited owner's allowance is checked against what the database says it stores, holding the
        owner too, so writes to its projects through any server can't together pass it. The
        server's total is this server's count (see store.py)."""
        registered = await self.store.owner_of(project, c)
        owner = registered
        claim = self.private and code is not None and owner == "default"
        if claim:  # a private server: the first signed write with a code binds a project made without one
            owner = code
        if owner is None:  # first write: the creator's code (or the default) owns it
            owner = code or "default"
            if self.codes.allowance(owner) <= 0:
                return "needs-code" if owner == "default" else "quota-full"
        allowance = self.codes.allowance(owner)
        if nbytes > 0:
            if allowance != UNLIMITED:  # (an unlimited owner's total isn't needed: none is added up)
                await c.xlock(key("owner", owner))
                used = await self.store.owner_total(owner, c)
                if registered is not None and self.codes.lapsed(owner) and allowance <= used + nbytes:
                    return "code-expired"
                if used + nbytes > allowance:
                    return "quota-full" if registered or owner != "default" else "needs-code"
                if await self.store.project_total(project, c) + nbytes > self.limits.project_max:
                    return "project-full"
            if self.store.total + nbytes > self.limits.max_bytes or self.store.free_disk() - nbytes < self.limits.min_free:
                return "server-full"
        if registered is None:
            await self.store.register(project, owner, self.codes.label(owner) if code else "default", c)
        elif claim:
            await self.store.claim(project, owner, c)
        return None

    async def move(self, project: str, code: str) -> dict | None:
        """Move a stored project to another creation code (its owner asked; see `move` in relay.py).
        None if done, else the refusal to send: the code isn't valid (or has lapsed), or hasn't room."""
        async with self.store.locked(project) as c:
            owner = await self.store.owner_of(project, c)
            if owner is None:
                return {"code": "move-not-stored", "m": "the server doesn't store this project"}
            new = self.codes.check(code)
            if not new:
                return {"code": "move-bad-code", "m": "that code isn't valid on this server"}
            if new == owner:
                return None
            allowance = self.codes.allowance(new)
            if allowance != UNLIMITED:
                need = await self.store.project_total(project, c)
                if need > self.limits.project_max:
                    return {"code": "move-too-big", "m": "the project is larger than a project may be with that code",
                            "need": need, "max": self.limits.project_max}
                await c.xlock(key("owner", new))
                free = allowance - await self.store.owner_total(new, c)
                if need > free:
                    return {"code": "move-no-room", "m": "that code hasn't room for this project", "need": need, "free": max(0, int(free))}
            await self.store.claim(project, new, c)
        log.info("project %s… moved to another creation code (%s)", project[:8], self.codes.label(new))
        return None

    async def purge(self, owner: str, pause: float = 0) -> tuple[int, int, int]:
        """Drop the stored copies of every project created with `owner` (a creation code), as
        idle eviction does, one at a time, pausing `pause` seconds after each. Writers' re-uploads
        are refused while the code is expired or removed, so the projects carry on relay-only, on
        people's devices. Projects someone has open are skipped (and picked up next time).
        Returns (purged, bytes freed, skipped)."""
        if self.lists:
            await self.lists.purge_owner(owner)
        purged = freed = skipped = 0
        for p in self.store.stored_by(owner):
            if self.busy(p):
                skipped += 1
                continue
            freed += await self.store.evict(p)
            purged += 1
            await asyncio.sleep(pause)
        return purged, freed, skipped

    async def purge_expired(self, after_days: float) -> None:
        """Purge the projects of codes that expired more than `after_days` ago, letting live
        editing through between projects (a lapsed plan can hold gigabytes)."""
        self.codes.load()
        for c in list(self.codes.until):
            days = self.codes.lapsed_since(c)
            if days is None or days <= after_days or not (self.store.stored_by(c) or (self.lists and await self.lists.count(c))):
                continue
            n, freed, skipped = await self.purge(c, PURGE_PAUSE)
            log.info("purged %d project(s) of code %s… (%s), expired %d day(s) ago%s", n, c[:4],
                     fmt_size(freed), days, f"; {skipped} open, left for later" if skipped else "")

    async def evict_idle(self, days: float) -> None:
        """Drop the stored copies of projects nobody has opened for `days` (their writers upload
        them again), letting live editing through between projects."""
        for p in self.store.idle(days):
            if self.busy(p):
                continue  # someone is connected right now
            freed = await self.store.evict(p)
            log.info("evicted idle project %s… (%s, unopened for %g days)", p[:8], fmt_size(freed), days)
            await asyncio.sleep(PURGE_PAUSE)

    def store_status(self, project: str, code: str | None) -> dict:
        """What the server keeps of a project, and its limits, for a connection holding creation
        code `code`. `ownerUsed` (all that the project's creation code stores, its other projects
        and project lists included) goes only to a connection that holds that code: to anyone
        else it would tell how much the owner keeps elsewhere."""
        owner = self.store.owner(project)
        allowance = self.codes.allowance(owner or code or "default")
        return {
            "registered": owner is not None,
            "creation": "open" if self.anyone_creates() else "code",
            "canCreate": self.anyone_creates() or bool(code),
            "used": self.store.usage.get(project, 0),
            "projectMax": self.limits.project_max if allowance != UNLIMITED else None,
            "allowance": None if allowance == UNLIMITED else allowance,
            "ownerUsed": self.store.owner_usage(owner) if owner is not None and code == owner else None,
        }
