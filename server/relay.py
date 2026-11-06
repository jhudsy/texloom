"""texloom relay server.

Stores and relays opaque, encrypted Yjs updates. Never sees keys or plaintext.
Verifies Ed25519 signatures so that read-only link holders cannot write.

Storage safeguards (the server can't see what it stores, and anyone with the site
can make a project, so limits are by size and by who may *create* stored projects):
  --max-bytes          total bytes texloom may store                  (default 2 GiB)
  --min-free           stop storing when free disk drops below this   (default 2 GiB)
  --project-max-bytes  per-project cap                                (default 200 MiB)
  --codes-file         if given, storing a *new* project needs a creation code from
                       this file ("code  label" per line; re-read when it changes)
A refused write is still relayed live to connected collaborators but not
acknowledged, so the sender keeps it and retries later: nobody loses work.

Run:  uv run texloom-server --port 8787 --db data/texloom.db
Add a creation code:  uv run texloom-server --codes-file data/codes.txt --add-code alice
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
import re
import secrets
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger("texloom.relay")

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url(32-byte pubkey)
BLOB_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_UPDATE = 8 * 1024 * 1024
MAX_BLOB = 50 * 1024 * 1024
MAX_P2P = 72 * 1024 * 1024  # peer-to-peer messages may carry (base64) blobs
MODE_SKEW = 600  # seconds a signed mode-change command stays valid
GiB, MiB = 1 << 30, 1 << 20


@dataclass
class Limits:
    max_bytes: int = 2 * GiB
    min_free: int = 2 * GiB
    project_max: int = 200 * MiB


class CodeBook:
    """Creation codes, one per line: `code  label`. No file configured = open creation."""

    ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def __init__(self, path: Path | None):
        self.path = path
        self._mtime = -1.0
        self._codes: dict[str, str] = {}

    @property
    def required(self) -> bool:
        return self.path is not None

    def _load(self) -> None:
        if not self.path:
            return
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._codes, self._mtime = {}, -1.0
            return
        if mtime == self._mtime:
            return
        codes = {}
        for line in self.path.read_text().splitlines():
            parts = line.split(None, 1)
            if parts and not parts[0].startswith("#"):
                codes[self.normalize(parts[0])] = parts[1].strip() if len(parts) > 1 else ""
        self._codes, self._mtime = codes, mtime

    @staticmethod
    def normalize(code: str) -> str:
        return re.sub(r"[^0-9A-Z]", "", code.upper())

    def check(self, code: str | None) -> str | None:
        """Return the code's label if valid (constant-time compare), else None."""
        if not code:
            return None
        self._load()
        given = self.normalize(code)
        found = None
        for c, label in self._codes.items():
            if hmac.compare_digest(c, given):
                found = label or "(unlabelled)"
        return found

    def add(self, label: str) -> str:
        assert self.path
        raw = "".join(secrets.choice(self.ALPHABET) for _ in range(12))  # 60 bits
        code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(f"{code}  {label}\n")
        self.path.chmod(0o640)
        return code


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verify(project: str, msg: bytes, sig: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(project)).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS log (
              project TEXT NOT NULL, seq INTEGER NOT NULL, snap INTEGER NOT NULL DEFAULT 0,
              data BLOB NOT NULL, sig BLOB NOT NULL, PRIMARY KEY (project, seq));
            CREATE TABLE IF NOT EXISTS head (project TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS mode (project TEXT PRIMARY KEY, relay INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS blob (
              project TEXT NOT NULL, id TEXT NOT NULL, data BLOB NOT NULL,
              PRIMARY KEY (project, id));
            CREATE TABLE IF NOT EXISTS projects (
              project TEXT PRIMARY KEY, created REAL NOT NULL, label TEXT NOT NULL DEFAULT '');
            """
        )
        with self.db:  # projects stored before registration existed are grandfathered in
            self.db.execute(
                "INSERT OR IGNORE INTO projects(project, created, label) "
                "SELECT project, ?, 'existing' FROM (SELECT project FROM log UNION SELECT project FROM blob)",
                (time.time(),),
            )
        self.usage: dict[str, int] = defaultdict(int)
        for q in ("SELECT project, SUM(length(data) + length(sig)) FROM log GROUP BY project",
                  "SELECT project, SUM(length(data)) FROM blob GROUP BY project"):
            for project, n in self.db.execute(q):
                self.usage[project] += n or 0
        self.total = sum(self.usage.values())
        self.dir = path.parent

    # --- registration & accounting -------------------------------------------
    def registered(self, project: str) -> bool:
        return self.db.execute("SELECT 1 FROM projects WHERE project=?", (project,)).fetchone() is not None

    def register(self, project: str, label: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO projects(project, created, label) VALUES (?,?,?)",
                            (project, time.time(), label))
        log.info("registered project %s… (%s)", project[:8], label)

    def _recount(self, project: str) -> None:
        n = self.db.execute("SELECT COALESCE(SUM(length(data) + length(sig)), 0) FROM log WHERE project=?",
                            (project,)).fetchone()[0]
        n += self.db.execute("SELECT COALESCE(SUM(length(data)), 0) FROM blob WHERE project=?",
                             (project,)).fetchone()[0]
        self.total += n - self.usage[project]
        self.usage[project] = n

    def free_disk(self) -> int:
        return shutil.disk_usage(self.dir).free

    def head(self, project: str) -> int:
        row = self.db.execute("SELECT seq FROM head WHERE project=?", (project,)).fetchone()
        return row[0] if row else 0

    def append(self, project: str, data: bytes, sig: bytes) -> int:
        seq = self.head(project) + 1
        with self.db:
            self.db.execute(
                "INSERT INTO log(project, seq, data, sig) VALUES (?,?,?,?)",
                (project, seq, data, sig),
            )
            self.usage[project] += len(data) + len(sig)
            self.total += len(data) + len(sig)
            self.db.execute(
                "INSERT INTO head(project, seq) VALUES (?,?) "
                "ON CONFLICT(project) DO UPDATE SET seq=excluded.seq",
                (project, seq),
            )
        return seq

    def snapshot(self, project: str, upto: int, data: bytes, sig: bytes) -> None:
        with self.db:
            self.db.execute("DELETE FROM log WHERE project=? AND seq<=?", (project, upto))
            self.db.execute(
                "INSERT INTO log(project, seq, snap, data, sig) VALUES (?,?,1,?,?)",
                (project, upto, data, sig),
            )
        self._recount(project)

    def since(self, project: str, since: int) -> list[dict]:
        # If the client is behind the latest snapshot it needs the snapshot too.
        snap = self.db.execute(
            "SELECT seq FROM log WHERE project=? AND snap=1 ORDER BY seq DESC LIMIT 1",
            (project,),
        ).fetchone()
        start = since
        if snap and since < snap[0]:
            start = snap[0] - 1
        rows = self.db.execute(
            "SELECT seq, snap, data, sig FROM log WHERE project=? AND seq>? ORDER BY seq",
            (project, start),
        ).fetchall()
        return [
            {"q": q, "snap": bool(sn), "d": b64e(d), "s": b64e(s)} for q, sn, d, s in rows
        ]

    def count(self, project: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM log WHERE project=?", (project,)
        ).fetchone()[0]

    def relay_only(self, project: str) -> bool:
        row = self.db.execute("SELECT relay FROM mode WHERE project=?", (project,)).fetchone()
        return bool(row and row[0])

    def set_mode(self, project: str, relay_only: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO mode(project, relay) VALUES (?,?) "
                "ON CONFLICT(project) DO UPDATE SET relay=excluded.relay",
                (project, int(relay_only)),
            )
            if relay_only:  # forget everything we stored for this project
                self.db.execute("DELETE FROM log WHERE project=?", (project,))
                self.db.execute("DELETE FROM blob WHERE project=?", (project,))
        self._recount(project)

    def has_blob(self, project: str, bid: str) -> bool:
        return self.db.execute("SELECT 1 FROM blob WHERE project=? AND id=?", (project, bid)).fetchone() is not None

    def put_blob(self, project: str, bid: str, data: bytes) -> None:
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO blob(project, id, data) VALUES (?,?,?)",
                (project, bid, data),
            )
        if cur.rowcount:
            self.usage[project] += len(data)
            self.total += len(data)

    def get_blob(self, project: str, bid: str) -> bytes | None:
        row = self.db.execute(
            "SELECT data FROM blob WHERE project=? AND id=?", (project, bid)
        ).fetchone()
        return row[0] if row else None


class Relay:
    def __init__(self, store: Store, limits: Limits | None = None, codes: CodeBook | None = None):
        self.store = store
        self.limits = limits or Limits()
        self.codes = codes or CodeBook(None)
        self.rooms: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
        self.peer_ids: dict[web.WebSocketResponse, int] = {}
        self.code_label: dict[web.WebSocketResponse, str | None] = {}  # valid creation code per connection
        self._next_id = 1

    def may_store(self, project: str, nbytes: int, code_label: str | None) -> str | None:
        """None if this write may be stored, else a reason code for the client."""
        if not self.store.registered(project):
            if self.codes.required and not code_label:
                return "needs-code"
        if self.store.usage[project] + nbytes > self.limits.project_max:
            return "project-full"
        if self.store.total + nbytes > self.limits.max_bytes or self.store.free_disk() - nbytes < self.limits.min_free:
            return "server-full"
        if not self.store.registered(project):
            self.store.register(project, code_label or "open")
        return None

    def store_status(self, project: str, ws) -> dict:
        return {
            "registered": self.store.registered(project),
            "creation": "code" if self.codes.required else "open",
            "canCreate": not self.codes.required or bool(self.code_label.get(ws)),
            "used": self.store.usage.get(project, 0),
            "projectMax": self.limits.project_max,
        }

    async def broadcast(self, project: str, msg: dict, exclude=None) -> None:
        text = json.dumps(msg)
        for ws in list(self.rooms[project]):
            if ws is not exclude and not ws.closed:
                try:
                    await ws.send_str(text)
                except ConnectionError:
                    pass

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        project = request.match_info["project"]
        if not PROJECT_RE.match(project):
            raise web.HTTPBadRequest(text="bad project id")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_P2P)
        await ws.prepare(request)
        room = self.rooms[project]
        room.add(ws)
        self.peer_ids[ws] = self._next_id
        self._next_id += 1
        self.code_label[ws] = None
        log.info("join %s… (%d peers)", project[:8], len(room))
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(raw.data)
                    await self.handle(project, ws, msg)
                except (ValueError, KeyError, TypeError) as e:
                    await ws.send_json({"t": "err", "m": f"bad message: {e}"})
        finally:
            room.discard(ws)
            self.peer_ids.pop(ws, None)
            self.code_label.pop(ws, None)
            if not room:
                self.rooms.pop(project, None)
            log.info("leave %s…", project[:8])
        return ws

    async def handle(self, project: str, ws, msg: dict) -> None:
        t = msg["t"]
        relay_only = self.store.relay_only(project)
        if t == "hello":
            if msg.get("code"):
                self.code_label[ws] = self.codes.check(str(msg["code"]))
            entries = self.store.since(project, int(msg.get("since", 0)))
            await ws.send_json(
                {"t": "log", "entries": entries, "head": self.store.head(project),
                 "count": self.store.count(project), "you": self.peer_ids[ws],
                 "mode": "relay" if relay_only else "server", "peers": len(self.rooms[project]) - 1,
                 "store": self.store_status(project, ws)}
            )
        elif t == "up":
            data, sig = b64d(msg["d"]), b64d(msg["s"])
            if len(data) > MAX_UPDATE or not verify(project, b"upd:" + data, sig):
                await ws.send_json({"t": "err", "m": "rejected update (bad signature?)"})
                return
            if relay_only:  # pass it on, but keep nothing
                await ws.send_json({"t": "ack", "q": 0, "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": msg["d"], "s": msg["s"]}, ws)
                return
            refused = self.may_store(project, len(data) + len(sig), self.code_label.get(ws))
            if refused:
                # Collaborators online still get it; the sender keeps it (no ack) and retries later.
                await ws.send_json({"t": "err", "code": refused, "m": f"not stored: {refused}", "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": msg["d"], "s": msg["s"]}, ws)
                return
            seq = self.store.append(project, data, sig)
            await ws.send_json({"t": "ack", "q": seq, "id": msg.get("id")})
            await self.broadcast(project, {"t": "up", "q": seq, "d": msg["d"], "s": msg["s"]}, ws)
        elif t == "snap":
            upto, data, sig = int(msg["upto"]), b64d(msg["d"]), b64d(msg["s"])
            if relay_only or not self.store.registered(project):
                return
            if upto > self.store.head(project) or not verify(
                project, f"snap:{upto}:".encode() + data, sig
            ):
                await ws.send_json({"t": "err", "m": "rejected snapshot"})
                return
            self.store.snapshot(project, upto, data, sig)
            log.info("compacted %s… up to %d", project[:8], upto)
        elif t == "aw":
            await self.broadcast(project, {"t": "aw", "d": msg["d"]}, ws)
        elif t == "p2p":
            # Opaque peer-to-peer payload (state-vector sync, blob transfer). Never stored.
            out = {"t": "p2p", "d": msg["d"], "from": self.peer_ids[ws]}
            if "s" in msg:
                out["s"] = msg["s"]
            to = msg.get("to")
            if to is None:
                await self.broadcast(project, out, ws)
            else:
                for peer, pid in list(self.peer_ids.items()):
                    if pid == to and peer in self.rooms[project] and not peer.closed:
                        await peer.send_json(out)
        elif t == "mode":
            mode, ts, sig = msg["m"], int(msg["ts"]), b64d(msg["s"])
            if mode not in ("server", "relay") or abs(time.time() - ts) > MODE_SKEW or not verify(
                project, f"mode:{mode}:{ts}".encode(), sig
            ):
                await ws.send_json({"t": "err", "m": "rejected mode change"})
                return
            self.store.set_mode(project, mode == "relay")
            log.info("project %s… storage mode -> %s", project[:8], mode)
            await self.broadcast(project, {"t": "mode", "m": mode})

    async def put_blob(self, request: web.Request) -> web.Response:
        project, bid = request.match_info["project"], request.match_info["id"]
        if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
            raise web.HTTPBadRequest()
        data = await request.read()
        sig = b64d(request.headers.get("X-Sig", ""))
        if len(data) > MAX_BLOB or not verify(project, f"blob:{bid}:".encode() + data, sig):
            raise web.HTTPForbidden(text="bad signature")
        if self.store.relay_only(project):
            raise web.HTTPConflict(text="project is relay-only: blobs are exchanged peer to peer")
        if self.store.has_blob(project, bid):
            return web.Response(status=200)
        refused = self.may_store(project, len(data), self.codes.check(request.headers.get("X-Create-Code")))
        if refused:
            status = {"needs-code": 403, "project-full": 413, "server-full": 507}[refused]
            return web.json_response({"code": refused}, status=status)
        self.store.put_blob(project, bid, data)
        return web.Response(status=201)

    async def info(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "creation": "code" if self.codes.required else "open",
            "projectMaxBytes": self.limits.project_max,
        })

    async def check_code(self, request: web.Request) -> web.Response:
        try:
            code = str((await request.json()).get("code", ""))[:64]
        except ValueError:
            raise web.HTTPBadRequest()
        return web.json_response({"ok": bool(self.codes.check(code)) or not self.codes.required})

    async def get_blob(self, request: web.Request) -> web.Response:
        project, bid = request.match_info["project"], request.match_info["id"]
        data = self.store.get_blob(project, bid)
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )


@web.middleware
async def cors(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "X-Sig, X-Create-Code, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, OPTIONS"
    return resp


def make_app(db: Path, static: Path | None, tex: Path | None = None,
             limits: Limits | None = None, codes: CodeBook | None = None) -> web.Application:
    relay = Relay(Store(db), limits, codes)
    app = web.Application(middlewares=[cors], client_max_size=MAX_BLOB + 1024)
    app.router.add_get("/ws/{project}", relay.ws_handler)
    app.router.add_put("/blob/{project}/{id}", relay.put_blob)
    app.router.add_get("/blob/{project}/{id}", relay.get_blob)
    app.router.add_route("OPTIONS", "/blob/{project}/{id}", lambda r: web.Response())
    app.router.add_get("/api/info", relay.info)
    app.router.add_post("/api/code", relay.check_code)
    if tex and (tex / "busytex").is_dir():  # WebAssembly TeX Live for in-browser compiles
        app.router.add_static("/core", tex)
    if static and static.is_dir():
        async def index(_):
            return web.FileResponse(static / "index.html")
        app.router.add_get("/", index)
        app.router.add_static("/", static)
    return app


def main() -> None:
    p = argparse.ArgumentParser(description="texloom relay server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", type=Path, default=Path("data/texloom.db"))
    p.add_argument("--static", type=Path, default=Path("web/dist"))
    p.add_argument("--tex-assets", type=Path, default=Path("tex-assets"),
                   help="directory containing busytex/ (in-browser TeX); optional")
    size = lambda v: int(float(v[:-1]) * {"K": 1 << 10, "M": MiB, "G": GiB}[v[-1].upper()]) if v[-1].isalpha() else int(v)
    p.add_argument("--max-bytes", type=size, default=2 * GiB, help="total storage cap, e.g. 2G")
    p.add_argument("--min-free", type=size, default=2 * GiB, help="keep this much disk free, e.g. 2G")
    p.add_argument("--project-max-bytes", type=size, default=200 * MiB, help="per-project cap, e.g. 200M")
    p.add_argument("--codes-file", type=Path, help="require creation codes from this file for new stored projects")
    p.add_argument("--add-code", metavar="LABEL", help="create a new creation code with this label, print it, exit")
    p.add_argument("--list-codes", action="store_true", help="list creation codes and the projects created with each")
    a = p.parse_args()
    codes = CodeBook(a.codes_file)
    if a.add_code is not None or a.list_codes:
        if not a.codes_file:
            sys.exit("--codes-file is required")
        if a.add_code is not None:
            print(codes.add(a.add_code))
        else:
            codes._load()
            store = Store(a.db)
            for c, label in codes._codes.items():
                n = store.db.execute("SELECT COUNT(*) FROM projects WHERE label=?", (label,)).fetchone()[0]
                print(f"{c[:4]}-{c[4:8]}-{c[8:]}  {label:20}  {n} project(s)")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    limits = Limits(a.max_bytes, a.min_free, a.project_max_bytes)
    log.info("limits: total %.1f GiB, keep %.1f GiB free, %.0f MiB per project; creation %s",
             limits.max_bytes / GiB, limits.min_free / GiB, limits.project_max / MiB,
             f"needs a code ({a.codes_file})" if codes.required else "open")
    web.run_app(make_app(a.db, a.static, a.tex_assets, limits, codes), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
