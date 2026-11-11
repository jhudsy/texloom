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

from . import tls
from .crypto import Keys, b64d, b64e, canonical_pub, revoke_msg, signer

log = logging.getLogger("texloom")

COMPACT_AFTER = 300  # log entries before a writer uploads a snapshot
BLOB_RE = re.compile(r"^[0-9a-f]{32}$")  # blob ids are HMAC hex digests, never paths
# Keep in sync with server/relay.py: frames are at most 16 MiB (base64 adds a third).
MAX_SNAPSHOT = 12 * 1024 * 1024
MAX_UPDATE = 8 * 1024 * 1024  # one change, after encryption
UPDATE_BATCH = 6 * 1024 * 1024  # pending changes are merged up to this much per message
# A bigger change (a whole project, say) or snapshot goes to the server as an encrypted upload,
# like a file, and what's sent is a small reference to it: REF + {"b": upload id}. The server
# can't tell the difference (it's all encrypted). Uploads go as "output", so the writer whose
# snapshot replaces them may delete them. See web/src/replica.ts.
REF = b"\x00texloom-ref\x00"
P2P_BLOB = 50 * 1024 * 1024  # files sent peer to peer (relay-only projects): as big as any synced file
# A peer-to-peer message bigger than this (a whole project for a computer catching up, a file)
# goes in pieces: the server takes frames of at most 16 MiB. Pieces are put together again
# up to P2P_PARTS of them, for a few messages at a time.
P2P_PIECE = 4 * 1024 * 1024
P2P_PARTS, P2P_ASSEMBLING = 32, 4
# Unfinished messages are held in memory, so anyone with a link could send pieces that never
# end: at most this much in all (oldest dropped first), two at a time from one sender, and
# none that has gone quiet for P2P_STALE seconds.
P2P_HELD, P2P_PER_SENDER, P2P_STALE = 128 * 1024 * 1024, 2, 60.0
# The server closes a connection sending more than 160 MiB in 10 s: pieces are paced below that.
P2P_WINDOW, P2P_RATE = 10.0, 96 * 1024 * 1024


class Replica:
    def __init__(self, state_dir: Path, keys: Keys, server: str, create_code: str | None = None):
        self.dir = state_dir
        self.keys = keys
        self.server = server.rstrip("/")
        self.create_code = create_code  # needed only to store a *new* project on some servers
        self._warned: set[str] = set()
        self._parts: dict[tuple, dict] = {}  # big peer-to-peer messages coming in pieces
        self._part_at = 0.0  # when a piece last came in
        self._p2p_window: list = [0.0, 0]  # [start, bytes sent] (see P2P_RATE)
        self.doc = Doc(skip_gc=True)  # keep deleted text so past versions can be reconstructed
        self.files: Map = self.doc.get("files", type=Map)
        self.bin: Map = self.doc.get("bin", type=Map)
        self.dirs: Map = self.doc.get("dirs", type=Map)
        self.clients: Map = self.doc.get("clients", type=Map)  # client id -> name (who wrote what)
        self.meta: Map = self.doc.get("meta", type=Map)
        self.out: Map = self.doc.get("out", type=Map)
        self.members: Map = self.doc.get("members", type=Map)  # personal links the owner issued (see sharing.ts)
        self.since = 0
        self.outbox: dict[str, bytes] = {}
        self.refs: dict[int, str] = {}  # stored changes and snapshots that are uploads: seq -> upload id
        self._ref_sent: dict[str, str] = {}  # outbox id -> upload id, until the server stores it
        self._snap_ref: tuple[int, str | None] | None = None  # the snapshot sent, until the server says it took it
        self._resync = 0  # 1: an upload a change refers to couldn't be fetched, so catch up from the start
        self._seen: set[int] = set()  # seqs received above `since` (out of order)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.http: aiohttp.ClientSession | None = None
        self.on_remote: Callable[[], Awaitable[None]] | None = None
        self.on_compile_request: Callable[[int, bool], Awaitable[None]] | None = None
        self.server_mode = "server"  # what the server says: "server" (stores) or "relay"
        self.pending_mode: str | None = None  # storage change not yet sent to the server
        self.store_refusal: str | None = None  # what the server last said
        self.denied: str | None = None  # a private server turned this project away: why
        self.deleted_at: float | None = None  # the owner deleted the project for everyone (seconds): read-only from then on
        self.refused: str | None = None  # the server's reason for rejecting the last change sent
        self._blob_waiters: dict[str, list[asyncio.Future]] = {}
        self.before_remote: Callable[[], Awaitable[None]] | None = None
        self.lock = asyncio.Lock()  # serialises remote applies with local folder ingests
        self.connected = asyncio.Event()
        self.synced = asyncio.Event()  # first catch-up complete
        self._caught_up = False  # this connection's log (and revocation list) has been applied
        self._revocations: list[dict] = []  # owner-signed, kept in state.json
        self.load()

    @property
    def can_write(self) -> bool:
        """An edit link, for a project its owner hasn't deleted for everyone."""
        return self.keys.writable and self.deleted_at is None

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
            self.deleted_at = s.get("deleted")
            self.refs = {int(q): b for q, b in s.get("refs", {}).items() if isinstance(b, str) and BLOB_RE.match(b)}
            self._revoked(s.get("revoked", []), save=False)  # before anything arrives from peers

    def save(self) -> None:
        (self.dir / "doc.bin.tmp").write_bytes(self.doc.get_update())
        (self.dir / "doc.bin.tmp").replace(self.dir / "doc.bin")
        state = {"since": self.since, "pending_mode": self.pending_mode,
                 "outbox": {k: b64e(v) for k, v in self.outbox.items()}, "revoked": self._revocations,
                 **({"refs": self.refs} if self.refs else {}),
                 **({"deleted": self.deleted_at} if self.deleted_at is not None else {})}
        (self.dir / "state.json.tmp").write_text(json.dumps(state))
        (self.dir / "state.json.tmp").replace(self.dir / "state.json")

    # --- local changes -------------------------------------------------------
    def local_change(self, fn: Callable[[], None]) -> None:
        """Run fn (which mutates the doc) and queue the resulting update for sending."""
        if not self.keys.writable:
            raise PermissionError("read-only link")
        if self.deleted_at is not None:
            raise PermissionError("the owner deleted this project for everyone")
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
        if len(update) > UPDATE_BATCH:
            if self.server_mode == "relay" or self.storage == "relay":
                # nothing is stored: give it to the computers online, in pieces (others catch up
                # from a computer that has it when they connect, as always without a server copy)
                await self.p2p({"k": "upd", "u": b64e(update)})
                self.outbox.pop(uid, None)
                self.save()
                return
            ref = await self._as_upload(update)
            if ref is None:
                return  # not uploaded yet: stays in the outbox, and the status says why
            self._ref_sent[uid] = ref
            update = REF + json.dumps({"b": ref}).encode()
        data = self.keys.encrypt(update)
        if len(data) > MAX_UPDATE:  # the server would refuse it on every try
            self.refused = f"a change of {len(data) >> 20} MB is more than the server takes at once (8 MB)"
            if "too-big" not in self._warned:
                self._warned.add("too-big")
                log.warning("NOT SENT: %s", self.refused)
            return
        sig = self.keys.sign(b"upd:" + data)
        try:
            await self.ws.send_json({"t": "up", "id": uid, "d": b64e(data), "s": b64e(sig)})
        except ConnectionError:
            pass

    async def _as_upload(self, content: bytes) -> str | None:
        """Upload a big change or snapshot like a file; its id, or None if it isn't on the server yet."""
        bid = await self.put_blob(content, kind="ref")
        if (self.dir / "blobs-pending" / bid).exists():
            return None
        (self.dir / "blobs" / bid).unlink(missing_ok=True)  # the server has it: no need for a copy here
        return bid

    async def _fetch_ref(self, update: bytes) -> bytes | None:
        """The change a reference points to, from the server (None if it can't be had)."""
        try:
            bid = json.loads(update[len(REF):])["b"]
            if not isinstance(bid, str) or not BLOB_RE.match(bid) or self.http is None:
                return None
            async with self.http.get(f"{self.server}/blob/{self.keys.project}/{bid}") as r:
                if r.status != 200:
                    return None
                content = self.keys.decrypt(await r.read())
        except (ValueError, KeyError, TypeError, InvalidTag, aiohttp.ClientError):
            return None
        return content if self.keys.blob_id(content) == bid else None

    async def _drop_refs(self, upto: int, keep: str | None) -> None:
        """After a snapshot up to `upto`: the uploads of what it replaced aren't needed any more."""
        for q, bid in list(self.refs.items()):
            if q <= upto and bid != keep:
                del self.refs[q]
                await self.drop_output(bid)
        self.save()

    async def flush_outbox(self) -> None:
        if not self.outbox or not self.keys.writable:
            return
        # Merge what's pending into as few updates as fit the server's limit: one update for a
        # whole new folder could be too big, and the server would refuse it every time.
        batches: list[list[bytes]] = []
        size = 0
        for u in self.outbox.values():
            if not batches or size + len(u) > UPDATE_BATCH:
                batches.append([])
                size = 0
            batches[-1].append(u)
            size += len(u)
        self.outbox = {secrets.token_hex(8): merge_updates(*b) if len(b) > 1 else b[0] for b in batches}
        self.save()
        for uid, u in list(self.outbox.items()):
            await self._send_update(uid, u)

    # --- remote changes ------------------------------------------------------
    def _verify(self, prefix: bytes, data: bytes, sig: bytes, fresh: bool = False) -> bool:
        return self.keys.verify(prefix + data, sig, fresh)

    def _revoked(self, items: list, save: bool = True) -> None:
        """Personal keys the owner revoked (each signed by the owner); remembered across restarts."""
        added = False
        for it in items or []:
            try:
                pub, sig = canonical_pub(str(it["p"])), b64d(str(it["s"]))
                if pub is None or pub in self.keys.revoked or signer(self.keys.project, revoke_msg(pub), sig) != "owner":
                    continue
                if pub == self.keys.member_pub:
                    log.warning("the owner has revoked this personal edit link: your changes are no longer accepted")
                self.keys.revoked.add(pub)
                self._revocations.append({"p": pub, "s": b64e(sig)})
                added = True
            except (KeyError, ValueError, TypeError):
                continue
        if added and save:
            self.save()

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
            if update.startswith(REF):
                ref = update
                update = await self._fetch_ref(ref)
                if update is None:
                    log.warning("couldn't fetch the upload change %s refers to", e["q"])
                    self._resync = self._resync or 1
                    continue
                try:
                    self.refs[e["q"]] = json.loads(ref[len(REF):])["b"]
                except (ValueError, KeyError):
                    pass
            with self.doc.transaction(origin="remote"):
                self.doc.apply_update(update)
        self.save()
        if self.on_remote:
            await self.on_remote()

    async def _catch_up_again(self) -> None:
        """A change refers to an upload that's gone (a snapshot replaced it meanwhile): catch up
        from the start, which brings the snapshot instead. Once per run."""
        if self._resync != 1:
            return
        self._resync = 2
        log.info("catching up from the start")
        self.since, self._seen = 0, set()
        self.save()
        if self.ws is not None:
            await self.ws.close()

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
        full = self.doc.get_update()
        data, ref = self.keys.encrypt(full), None
        if len(data) > MAX_SNAPSHOT:  # too big for one message: as an upload
            ref = await self._as_upload(full)
            if ref is None or self.ws is None:
                return  # not uploaded: the log stays as it is for now
            data = self.keys.encrypt(REF + json.dumps({"b": ref}).encode())
        sig = self.keys.sign(f"snap:{self.since}:".encode() + data)
        self._snap_ref = (self.since, ref)
        await self.ws.send_json({"t": "snap", "upto": self.since, "d": b64e(data), "s": b64e(sig)})
        log.info("uploaded snapshot (log had %d entries)", count)

    # --- blobs ---------------------------------------------------------------
    async def put_blob(self, plaintext: bytes, kind: str = "file") -> str:
        """Store a blob locally and upload it (now if online, else on reconnect). kind "output"
        marks compiled PDFs/logs/SyncTeX: replaced by the next compile, so deletable by writers."""
        bid = self.keys.blob_id(plaintext)
        cache = self.dir / "blobs" / bid
        cached = cache.exists()
        if not cached:
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(plaintext)
        if not cached or kind == "file":  # a file is re-sent in case the server dropped it as output
            pending = self.dir / "blobs-pending"
            pending.mkdir(exist_ok=True)
            (pending / bid).write_text("output" if kind in ("output", "ref") else "")
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
            try:
                if marker.read_text() == "output":
                    headers["X-Kind"] = "output"
            except OSError:
                pass
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
                self.store_refusal = None
            except aiohttp.ClientError as e:
                log.warning("blob upload failed (%s); will retry", e)
                return

    async def drop_output(self, bid: str) -> None:
        """Delete compiled output a newer compile replaced: here, and on the server (only if
        the server marked it as output, so a project file can never be removed this way)."""
        if not isinstance(bid, str) or not BLOB_RE.match(bid):
            return
        for p in (self.dir / "blobs" / bid, self.dir / "blobs-pending" / bid):
            p.unlink(missing_ok=True)
        if not self.keys.writable or self.http is None or self.server_mode == "relay" or self.storage == "relay":
            return
        sig = self.keys.sign(f"del:{bid}".encode())
        try:
            async with self.http.delete(f"{self.server}/blob/{self.keys.project}/{bid}",
                                        headers={"X-Sig": b64e(sig), "X-Only-Output": "1"}):
                pass
        except aiohttp.ClientError:
            pass  # stays on the server; harmless

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
            asked = time.monotonic()
            while True:  # a big file comes in pieces: wait while they keep coming
                try:
                    plaintext = await asyncio.wait_for(asyncio.shield(fut), 5)
                    break
                except asyncio.TimeoutError:
                    if time.monotonic() - max(asked, self._part_at) > 20:
                        fut.cancel()
                        raise
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(plaintext)
        return plaintext

    # --- peer to peer (relayed by the server, never stored) -------------------------
    async def p2p(self, payload: dict, to: int | None = None) -> None:
        if self.ws is None or self.ws.closed:
            return
        data = self.keys.encrypt(json.dumps(payload).encode())
        d = b64e(data)
        sig = b64e(self.keys.sign(b"p2p:" + data)) if payload.get("k") == "upd" else None
        if len(d) <= P2P_PIECE:
            await self._send_p2p(d, sig, to)
            return
        pid, n = secrets.token_hex(8), -(-len(d) // P2P_PIECE)
        for i in range(n):
            part = {"k": "part", "id": pid, "i": i, "n": n, "c": d[i * P2P_PIECE:(i + 1) * P2P_PIECE]}
            if sig and i == n - 1:
                part["s"] = sig  # checked once the message is whole
            await self._send_p2p(b64e(self.keys.encrypt(json.dumps(part).encode())), None, to)

    async def _send_p2p(self, d: str, sig: str | None, to: int | None) -> None:
        now = time.monotonic()
        if now - self._p2p_window[0] > P2P_WINDOW:
            self._p2p_window = [now, 0]
        if self._p2p_window[1] + len(d) > P2P_RATE:
            await asyncio.sleep(max(0.0, P2P_WINDOW - (now - self._p2p_window[0])))
            self._p2p_window = [time.monotonic(), 0]
        self._p2p_window[1] += len(d)
        if self.ws is None or self.ws.closed:
            return
        msg: dict = {"t": "p2p", "d": d}
        if sig:
            msg["s"] = sig
        if to is not None:
            msg["to"] = to
        await self.ws.send_json(msg)

    async def _on_part(self, frm, p: dict) -> None:
        """A piece of a big peer-to-peer message: once all are in, handle it as one."""
        pid, i, n, c = p.get("id"), p.get("i"), p.get("n"), p.get("c")
        if not (isinstance(pid, str) and isinstance(i, int) and isinstance(n, int) and isinstance(c, str)
                and 0 <= i < n <= P2P_PARTS and len(c) <= P2P_PIECE):
            return
        key, now = (frm, pid), time.monotonic()
        for k in [k for k, w in self._parts.items() if now - w["at"] > P2P_STALE]:
            del self._parts[k]
        if key not in self._parts:
            while True:  # the oldest goes: this sender's, else anyone's
                mine = [k for k in self._parts if k[0] == frm]
                if len(mine) >= P2P_PER_SENDER:
                    del self._parts[mine[0]]
                elif len(self._parts) >= P2P_ASSEMBLING:
                    del self._parts[next(iter(self._parts))]
                else:
                    break
            self._parts[key] = {"n": n, "got": {}, "s": None, "at": now, "size": 0}
        whole = self._parts[key]
        if whole["n"] != n or i in whole["got"]:
            return
        while sum(w["size"] for w in self._parts.values()) + len(c) > P2P_HELD:
            old = next(k for k in self._parts if k != key) if len(self._parts) > 1 else key
            del self._parts[old]
            if old == key:
                return  # this one alone is more than may be held
        whole["got"][i] = c
        whole["size"] += len(c)
        whole["at"] = self._part_at = now
        if isinstance(p.get("s"), str):
            whole["s"] = p["s"]
        if len(whole["got"]) == n:
            del self._parts[key]
            m = {"d": "".join(whole["got"][j] for j in range(n)), "from": frm}
            if whole["s"]:
                m["s"] = whole["s"]
            await self._on_p2p(m)

    async def _on_p2p(self, m: dict) -> None:
        data = b64d(m["d"])
        try:
            p = json.loads(self.keys.decrypt(data))
        except (InvalidTag, ValueError):
            return
        k, frm = p.get("k"), m.get("from")
        if k == "part":
            await self._on_part(frm, p)
        elif k in ("sv1", "sv2"):
            if self.keys.writable:
                diff = self.doc.get_update(b64d(p["sv"]))
                if len(diff) > 2:
                    await self.p2p({"k": "upd", "u": b64e(diff)}, frm)
            if k == "sv1":
                await self.p2p({"k": "sv2", "sv": b64e(self.doc.get_state())}, frm)
        elif k == "upd":
            if not self._caught_up:
                return  # not before the server's revocation list is in; peers resend after our sv1
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
            if cache.exists() and cache.stat().st_size <= P2P_BLOB:
                await self.p2p({"k": "blob", "id": p["id"], "b": b64e(cache.read_bytes())}, frm)
        elif k == "blob":
            if p.get("id") not in self._blob_waiters:
                return  # only files we asked for
            blob = b64d(p["b"])
            if self.keys.blob_id(blob) != p["id"]:
                return
            for fut in self._blob_waiters.pop(p["id"], []):
                if not fut.done():
                    fut.set_result(blob)
        elif k == "creq" and self.on_compile_request:
            await self.on_compile_request(frm, p.get("clean") is True)

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

    async def _sync_only(self) -> None:
        """The server keeps projects only with a creation code, and this one has none: rather than
        changes it refuses and files nobody can fetch, pass everything straight to the other
        computers, while they're online (the server's copy, if any, is nothing to lose)."""
        log.warning("This server keeps projects only with a creation code. Without one, this project now "
                    "syncs only between computers that are online at the same time.")
        self.store_refusal = None
        self.set_storage("relay")
        await self.send_mode("relay")
        await self.flush_outbox()

    def pending_count(self) -> int:
        """Changes and files waiting for the server. Files of a project the server doesn't
        store (relay mode) go to collaborators on request instead, so they don't count."""
        blobs = self.dir / "blobs-pending"
        relay = self.server_mode == "relay" or self.storage == "relay"
        return len(self.outbox) + (0 if relay or not blobs.is_dir() else sum(1 for _f in blobs.iterdir()))

    @property
    def store_problem(self) -> str | None:
        """Why the server isn't storing changes (needs-code, quota-full, code-expired, …)."""
        return self.store_refusal

    def _storage_warning(self, code: str) -> None:
        if code == "needs-code" and (self.storage == "relay" or (self.keys.seed and not self.create_code)):
            return  # sync-only, or about to be (see _sync_only): the server isn't meant to keep it
        self.store_refusal = code
        code = self.store_problem
        if code in self._warned:
            return
        self._warned.add(code)
        log.warning({
            "needs-code": "NOT SAVED ON SERVER: this server keeps projects only with a creation code, and this "
                          "project has none. Its owner can add one, or make it sync-only. "
                          "Changes still reach collaborators who are online.",
            "code-expired": "NOT SAVED ON SERVER: this project's creation code has expired; renewing it restores storage.",
            "quota-full": "NOT SAVED ON SERVER: the storage allowance of this project's creation code is used up.",
            "project-full": "NOT SAVED ON SERVER: this project has reached the server's size limit.",
            "server-full": "NOT SAVED ON SERVER: the server is out of space; changes are kept here and retried.",
        }.get(code, f"not saved on server: {code}"))

    # --- connection loop -------------------------------------------------------
    async def run(self) -> None:
        ws_url = self.server.replace("http", "ws", 1) + f"/ws/{self.keys.project}"
        backoff = 1
        async with aiohttp.ClientSession(connector=tls.connector()) as http:
            self.http = http
            if self.deleted_at is not None:
                self.synced.set()
            while self.deleted_at is None:  # a deleted project stays as it is: nothing to connect to
                try:
                    async with http.ws_connect(ws_url, heartbeat=30, max_msg_size=64 * 1024 * 1024) as ws:
                        self.ws = ws
                        self._caught_up = False
                        self._resync = 0 if self._resync == 1 else self._resync
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
                        log.info("disconnected (%s%s)", ws.close_code, f": {ws.exception()}" if ws.exception() else "")
                except (aiohttp.ClientError, OSError) as e:
                    log.info("offline (%s); retrying in %ds", e.__class__.__name__, backoff)
                finally:
                    self.ws = None
                    self._parts.clear()
                    self.connected.clear()
                if self.deleted_at is not None:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle(self, m: dict) -> None:
        t = m.get("t")
        if t == "deleted":  # the owner deleted the project for everyone: only if the owner signed it
            ts, sig = m.get("ts"), m.get("s")
            try:
                ok = isinstance(ts, int) and isinstance(sig, str) and signer(self.keys.project, f"delete:{ts}".encode(), b64d(sig)) == "owner"
            except (ValueError, TypeError):
                ok = False
            if not ok:
                log.warning("ignored a deletion notice without the owner's signature")
                return
            at = m.get("at")
            self.deleted_at = at / 1000 if isinstance(at, (int, float)) else time.time()
            self.save()
            self.synced.set()  # nothing more will come: don't keep anyone waiting
            log.warning("The owner deleted this project for everyone. This copy stays as it is; nothing syncs any more.")
            if self.ws is not None:
                await self.ws.close()
            return
        if t == "log":
            self.denied = None
            self.server_mode = m.get("mode", "server")
            self._revoked(m.get("rv", []))
            # the server dropped its copy (idle eviction): everything up to `floor` is gone
            floor = m.get("floor") or 0
            lost = m.get("mode") != "relay" and (m["head"] < self.since or 0 < self.since <= floor)
            if lost:
                self.since, self._seen = m["head"], set()
            await self._apply_entries(m["entries"])
            if self._resync == 1:
                await self._catch_up_again()
                return
            if lost and self.keys.writable:
                log.info("the server has no copy of this project any more: re-uploading it")
                pending = self.dir / "blobs-pending"
                pending.mkdir(exist_ok=True)
                for info in self.bin.values():
                    bid = info.get("h") if isinstance(info, dict) else None
                    if isinstance(bid, str) and BLOB_RE.match(bid) and (self.dir / "blobs" / bid).exists():
                        (pending / bid).touch()
                uid = secrets.token_hex(8)
                self.outbox[uid] = self.doc.get_update()
            if self.keys.writable and self.pending_mode:  # setting changed while offline
                await self.send_mode(self.pending_mode)
            await self.upload_pending()  # blobs first, so peers can fetch what updates reference
            self.since = max(self.since, m["head"])
            self._seen = {q for q in self._seen if q > self.since}
            self._mark_seen(self.since)
            await self.flush_outbox()
            self._caught_up = True
            await self.p2p({"k": "sv1", "sv": b64e(self.doc.get_state())})
            self.synced.set()
            await self._maybe_compact(m.get("count", 0))
        elif t == "up":
            await self._apply_entries([m])
            if self._resync == 1:
                await self._catch_up_again()
                return
            if m["q"]:
                self._mark_seen(m["q"])
            self.save()
        elif t == "snapped":  # the server took our snapshot: what it replaced can go
            if self._snap_ref and self._snap_ref[0] == m.get("upto"):
                upto, ref = self._snap_ref
                self._snap_ref = None
                if ref:
                    self.refs[upto] = ref
                await self._drop_refs(upto, ref)
        elif t == "p2p":
            await self._on_p2p(m)
        elif t == "mode":
            self.server_mode = m["m"]
        elif t == "rv":
            self._revoked([m])
        elif t == "ack":
            ref = self._ref_sent.pop(m.get("id"), None)
            if m["q"]:
                self._mark_seen(m["q"])
                self.store_refusal = None  # stored again
                if ref:
                    self.refs[m["q"]] = ref
            self.outbox.pop(m.get("id"), None)
            self.refused = None
            self.save()
        elif t == "err":
            if m.get("denied"):
                self.denied = str(m.get("code"))
                if "denied" not in self._warned:
                    self._warned.add("denied")
                    log.warning("NOT SYNCING: this server is private, and this project's creation code has expired or been removed."
                                if self.denied == "code-expired" else
                                "NOT SYNCING: this server is private, and this project needs a creation code. "
                                "Add \"create_code\" to .texloom/config.json and restart.")
            elif m.get("code") == "needs-code" and self.storage == "relay":
                pass  # sent before the switch to sync-only: it's sent again
            elif m.get("code") == "needs-code" and self.keys.seed and not self.create_code:
                await self._sync_only()
            elif m.get("code") in ("needs-code", "quota-full", "code-expired", "project-full", "server-full"):
                self._storage_warning(m["code"])  # kept in the outbox; retried on reconnect
            else:
                log.warning("server: %s", m.get("m"))
                if m.get("id") in self.outbox or "rejected update" in str(m.get("m")):
                    self.refused = str(m.get("m") or "rejected")
