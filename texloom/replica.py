"""A full local replica of a project, synced through the relay.

Owns the Y.Doc, its persistence, the outbox of unacknowledged local updates and the
WebSocket connection. Folder syncing is layered on top (see folder.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from cryptography.exceptions import InvalidTag
from pycrdt import Doc, Map, merge_updates

from .crypto import Keys, b64d, b64e, revoke_msg, signer

log = logging.getLogger("texloom")

COMPACT_AFTER = 300  # log entries before a writer uploads a snapshot
BLOB_RE = re.compile(r"^[0-9a-f]{32}$")  # blob ids are HMAC hex digests, never paths


class Replica:
    def __init__(self, state_dir: Path, keys: Keys, server: str, create_code: str | None = None):
        self.dir = state_dir
        self.keys = keys
        self.server = server.rstrip("/")
        self.create_code = create_code  # needed only to store a *new* project on some servers
        self._warned: set[str] = set()
        self.doc = Doc(skip_gc=True)  # keep deleted text so past versions can be reconstructed
        self.files: Map = self.doc.get("files", type=Map)
        self.bin: Map = self.doc.get("bin", type=Map)
        self.dirs: Map = self.doc.get("dirs", type=Map)
        self.clients: Map = self.doc.get("clients", type=Map)  # client id -> name (who wrote what)
        self.meta: Map = self.doc.get("meta", type=Map)
        self.out: Map = self.doc.get("out", type=Map)
        self.since = 0
        self.outbox: dict[str, bytes] = {}
        self._seen: set[int] = set()  # seqs received above `since` (out of order)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.http: aiohttp.ClientSession | None = None
        self.on_remote: Callable[[], Awaitable[None]] | None = None
        self.on_compile_request: Callable[[int], Awaitable[None]] | None = None
        self.server_mode = "server"  # what the server says: "server" (stores) or "relay"
        self.pending_mode: str | None = None  # storage change not yet sent to the server
        self._blob_waiters: dict[str, list[asyncio.Future]] = {}
        self.before_remote: Callable[[], Awaitable[None]] | None = None
        self.lock = asyncio.Lock()  # serialises remote applies with local folder ingests
        self.connected = asyncio.Event()
        self.synced = asyncio.Event()  # first catch-up complete
        self.load()

    @property
    def storage(self) -> str:
        return "relay" if self.meta.get("storage") == "relay" else "server"

    # --- persistence -------------------------------------------------------
    def load(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)  # holds the plaintext document and project keys
        if (p := self.dir / "doc.bin").exists():
            self.doc.apply_update(p.read_bytes())
        if (p := self.dir / "state.json").exists():
            s = json.loads(p.read_text())
            self.since = s.get("since", 0)
            self.pending_mode = s.get("pending_mode")
            self.outbox = {k: b64d(v) for k, v in s.get("outbox", {}).items()}

    def save(self) -> None:
        (self.dir / "doc.bin.tmp").write_bytes(self.doc.get_update())
        (self.dir / "doc.bin.tmp").replace(self.dir / "doc.bin")
        state = {"since": self.since, "pending_mode": self.pending_mode,
                 "outbox": {k: b64e(v) for k, v in self.outbox.items()}}
        (self.dir / "state.json.tmp").write_text(json.dumps(state))
        (self.dir / "state.json.tmp").replace(self.dir / "state.json")

    # --- local changes -------------------------------------------------------
    def local_change(self, fn: Callable[[], None]) -> None:
        """Run fn (which mutates the doc) and queue the resulting update for sending."""
        if not self.keys.writable:
            raise PermissionError("read-only link")
        before = self.doc.get_state()
        with self.doc.transaction(origin="local"):
            fn()
        update = self.doc.get_update(before)
        if len(update) <= 2:  # empty update
            return
        uid = secrets.token_hex(8)
        self.outbox[uid] = update
        self.save()
        try:
            asyncio.get_running_loop().create_task(self._send_update(uid, update))
        except RuntimeError:
            pass  # no loop yet: sent from the outbox on connect

    async def _send_update(self, uid: str, update: bytes) -> None:
        if self.ws is None or self.ws.closed:
            return  # stays in outbox; flushed on reconnect
        data = self.keys.encrypt(update)
        sig = self.keys.sign(b"upd:" + data)
        try:
            await self.ws.send_json({"t": "up", "id": uid, "d": b64e(data), "s": b64e(sig)})
        except ConnectionError:
            pass

    async def flush_outbox(self) -> None:
        if not self.outbox or not self.keys.writable:
            return
        # Merge everything pending into one update to send.
        merged = merge_updates(*self.outbox.values())
        uid = secrets.token_hex(8)
        self.outbox = {uid: merged}
        self.save()
        await self._send_update(uid, merged)

    # --- remote changes ------------------------------------------------------
    def _verify(self, prefix: bytes, data: bytes, sig: bytes, fresh: bool = False) -> bool:
        return self.keys.verify(prefix + data, sig, fresh)

    def _revoked(self, items: list) -> None:
        """Personal keys the owner revoked (each signed by the owner)."""
        for it in items or []:
            try:
                pub, sig = str(it["p"]), b64d(str(it["s"]))
                if signer(self.keys.project, revoke_msg(pub), sig) == "owner":
                    if pub == self.keys.member_pub and pub not in self.keys.revoked:
                        log.warning("the owner has revoked this personal edit link: your changes are no longer accepted")
                    self.keys.revoked.add(pub)
            except (KeyError, ValueError, TypeError):
                continue

    async def _apply_entries(self, entries: list[dict]) -> None:
        if not entries:
            return
        async with self.lock:
            await self._apply_locked(entries)

    async def _apply_locked(self, entries: list[dict]) -> None:
        if self.before_remote:
            await self.before_remote()
        for e in entries:
            data, sig = b64d(e["d"]), b64d(e["s"])
            prefix = f"snap:{e['q']}:".encode() if e.get("snap") else b"upd:"
            if not self._verify(prefix, data, sig):
                log.warning("dropping update %s with bad signature", e["q"])
                continue
            try:
                update = self.keys.decrypt(data)
            except InvalidTag:
                log.warning("dropping update %s that fails to decrypt", e["q"])
                continue
            with self.doc.transaction(origin="remote"):
                self.doc.apply_update(update)
        self.save()
        if self.on_remote:
            await self.on_remote()

    def _mark_seen(self, q: int) -> None:
        """Advance `since` only over contiguous sequence numbers, so nothing is skipped."""
        if q > self.since:
            self._seen.add(q)
        while self.since + 1 in self._seen:
            self.since += 1
            self._seen.discard(self.since)

    async def _maybe_compact(self, count: int) -> None:
        if count < COMPACT_AFTER or not self.keys.writable or self.ws is None or self.server_mode == "relay":
            return
        data = self.keys.encrypt(self.doc.get_update())
        sig = self.keys.sign(f"snap:{self.since}:".encode() + data)
        await self.ws.send_json({"t": "snap", "upto": self.since, "d": b64e(data), "s": b64e(sig)})
        log.info("uploaded snapshot (log had %d entries)", count)

    # --- blobs ---------------------------------------------------------------
    async def put_blob(self, plaintext: bytes) -> str:
        """Store a blob locally and upload it (now if online, else on reconnect)."""
        bid = self.keys.blob_id(plaintext)
        cache = self.dir / "blobs" / bid
        if not cache.exists():
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(plaintext)
            pending = self.dir / "blobs-pending"
            pending.mkdir(exist_ok=True)
            (pending / bid).touch()
        if self.connected.is_set():
            await self.upload_pending()
        return bid

    async def upload_pending(self) -> None:
        pending = self.dir / "blobs-pending"
        if not pending.exists() or not self.keys.writable or self.http is None:
            return
        if self.server_mode == "relay" or self.storage == "relay":
            return  # blobs travel peer to peer instead
        for marker in list(pending.iterdir()):
            bid = marker.name
            data = self.keys.encrypt((self.dir / "blobs" / bid).read_bytes())
            sig = self.keys.sign(f"blob:{bid}:".encode() + data)
            headers = {"X-Sig": b64e(sig)}
            if self.create_code:
                headers["X-Create-Code"] = self.create_code
            try:
                async with self.http.put(
                    f"{self.server}/blob/{self.keys.project}/{bid}", data=data, headers=headers,
                ) as r:
                    if r.status in (403, 413, 507):
                        self._storage_warning((await r.json(content_type=None) or {}).get("code", str(r.status)))
                        return
                    r.raise_for_status()
                marker.unlink()
            except aiohttp.ClientError as e:
                log.warning("blob upload failed (%s); will retry", e)
                return

    async def get_blob(self, bid: str) -> bytes:
        if not isinstance(bid, str) or not BLOB_RE.match(bid):
            raise ValueError(f"malformed blob id {bid!r}")
        cache = self.dir / "blobs" / bid
        if cache.exists():
            return cache.read_bytes()
        plaintext = None
        if self.server_mode == "server" and self.http:
            async with self.http.get(f"{self.server}/blob/{self.keys.project}/{bid}") as r:
                if r.status == 200:
                    plaintext = self.keys.decrypt(await r.read())
                    if self.keys.blob_id(plaintext) != bid:  # the server mustn't swap contents
                        log.warning("server returned the wrong content for blob %s", bid)
                        plaintext = None
        if plaintext is None:  # ask connected peers
            fut = asyncio.get_running_loop().create_future()
            self._blob_waiters.setdefault(bid, []).append(fut)
            await self.p2p({"k": "breq", "id": bid})
            plaintext = await asyncio.wait_for(fut, 20)
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(plaintext)
        return plaintext

    # --- peer to peer (relayed by the server, never stored) -------------------------
    async def p2p(self, payload: dict, to: int | None = None) -> None:
        if self.ws is None or self.ws.closed:
            return
        data = self.keys.encrypt(json.dumps(payload).encode())
        msg: dict = {"t": "p2p", "d": b64e(data)}
        if payload.get("k") == "upd":
            msg["s"] = b64e(self.keys.sign(b"p2p:" + data))
        if to is not None:
            msg["to"] = to
        await self.ws.send_json(msg)

    async def _on_p2p(self, m: dict) -> None:
        data = b64d(m["d"])
        try:
            p = json.loads(self.keys.decrypt(data))
        except (InvalidTag, ValueError):
            return
        k, frm = p.get("k"), m.get("from")
        if k in ("sv1", "sv2"):
            if self.keys.writable:
                diff = self.doc.get_update(b64d(p["sv"]))
                if len(diff) > 2:
                    await self.p2p({"k": "upd", "u": b64e(diff)}, frm)
            if k == "sv1":
                await self.p2p({"k": "sv2", "sv": b64e(self.doc.get_state())}, frm)
        elif k == "upd":
            if "s" not in m or not self._verify(b"p2p:", data, b64d(m["s"]), fresh=True):
                log.warning("ignoring unsigned peer update")
                return
            async with self.lock:
                if self.before_remote:
                    await self.before_remote()
                with self.doc.transaction(origin="remote"):
                    self.doc.apply_update(b64d(p["u"]))
                self.save()
                if self.on_remote:
                    await self.on_remote()
        elif k == "breq":
            bid = p.get("id")
            if not isinstance(bid, str) or not BLOB_RE.match(bid):
                return
            cache = self.dir / "blobs" / bid
            if cache.exists():
                await self.p2p({"k": "blob", "id": p["id"], "b": b64e(cache.read_bytes())}, frm)
        elif k == "blob":
            blob = b64d(p["b"])
            if self.keys.blob_id(blob) != p["id"]:
                return
            for fut in self._blob_waiters.pop(p["id"], []):
                if not fut.done():
                    fut.set_result(blob)
        elif k == "creq" and self.on_compile_request:
            await self.on_compile_request(frm)

    async def send_mode(self, mode: str) -> None:
        if self.ws is None or not self.keys.writable:
            return
        ts = int(time.time())
        sig = self.keys.sign(f"mode:{mode}:{ts}".encode())
        await self.ws.send_json({"t": "mode", "m": mode, "ts": ts, "s": b64e(sig)})
        self.server_mode = mode
        self.pending_mode = None
        self.save()
        if mode == "server":  # give the server a complete copy again
            uid = secrets.token_hex(8)
            self.outbox[uid] = self.doc.get_update()
            await self._send_update(uid, self.outbox[uid])
            pending = self.dir / "blobs-pending"
            pending.mkdir(exist_ok=True)
            for b in (self.dir / "blobs").glob("*") if (self.dir / "blobs").exists() else []:
                (pending / b.name).touch()
            await self.upload_pending()

    def set_storage(self, mode: str) -> None:
        """User changed the project's storage mode; tell the server on next connect."""
        self.local_change(lambda: self.meta.__setitem__("storage", mode))
        self.pending_mode = mode
        self.save()

    def _storage_warning(self, code: str) -> None:
        if code in self._warned:
            return
        self._warned.add(code)
        log.warning({
            "needs-code": "NOT SAVED ON SERVER: this server needs a creation code to store new projects. "
                          "Add \"create_code\" to .texloom/config.json (or set TEXLOOM_CODE) and restart. "
                          "Changes still reach collaborators who are online.",
            "quota-full": "NOT SAVED ON SERVER: the storage allowance of this project's creation code is used up.",
            "project-full": "NOT SAVED ON SERVER: this project has reached the server's size limit.",
            "server-full": "NOT SAVED ON SERVER: the server is out of space; changes are kept here and retried.",
        }.get(code, f"not saved on server: {code}"))

    # --- connection loop -------------------------------------------------------
    async def run(self) -> None:
        ws_url = self.server.replace("http", "ws", 1) + f"/ws/{self.keys.project}"
        backoff = 1
        async with aiohttp.ClientSession() as http:
            self.http = http
            while True:
                try:
                    async with http.ws_connect(ws_url, heartbeat=30, max_msg_size=0) as ws:
                        self.ws = ws
                        backoff = 1
                        log.info("connected to %s", self.server)
                        self.connected.set()
                        hello = {"t": "hello", "since": self.since}
                        if self.create_code:
                            hello["code"] = self.create_code
                        await ws.send_json(hello)
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                await self._handle(json.loads(msg.data))
                            except Exception as e:  # noqa: BLE001 - never let one bad frame kill the helper
                                log.warning("ignored a malformed message (%s: %s)", type(e).__name__, str(e)[:120])
                except (aiohttp.ClientError, OSError) as e:
                    log.info("offline (%s); retrying in %ds", e.__class__.__name__, backoff)
                finally:
                    self.ws = None
                    self.connected.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle(self, m: dict) -> None:
        t = m.get("t")
        if t == "log":
            self.server_mode = m.get("mode", "server")
            self._revoked(m.get("rv", []))
            await self._apply_entries(m["entries"])
            if self.keys.writable and self.pending_mode:  # setting changed while offline
                await self.send_mode(self.pending_mode)
            await self.upload_pending()  # blobs first, so peers can fetch what updates reference
            self.since = max(self.since, m["head"])
            self._seen = {q for q in self._seen if q > self.since}
            self._mark_seen(self.since)
            await self.flush_outbox()
            await self.p2p({"k": "sv1", "sv": b64e(self.doc.get_state())})
            self.synced.set()
            await self._maybe_compact(m.get("count", 0))
        elif t == "up":
            await self._apply_entries([m])
            if m["q"]:
                self._mark_seen(m["q"])
            self.save()
        elif t == "p2p":
            await self._on_p2p(m)
        elif t == "mode":
            self.server_mode = m["m"]
        elif t == "rv":
            self._revoked([m])
        elif t == "ack":
            if m["q"]:
                self._mark_seen(m["q"])
            self.outbox.pop(m.get("id"), None)
            self.save()
        elif t == "err":
            if m.get("code") in ("needs-code", "quota-full", "project-full", "server-full"):
                self._storage_warning(m["code"])  # kept in the outbox; retried on reconnect
            else:
                log.warning("server: %s", m.get("m"))
