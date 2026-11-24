"""The relay's HTTP endpoints: stored files, the server's info, encrypted project lists and
checking a creation code; and the headers every response gets. The storage rules they follow are
in policy.py (Policy)."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from typing import TYPE_CHECKING

from aiohttp import web

from texloom.crypto import signer

from .files import FilesError
from .lists import MAX_LIST, RETIRED, Lists
from .policy import UNLIMITED, KiB, MiB, Policy
from .wire import BLOB_RE, PROJECT_RE, b64d, verify

if TYPE_CHECKING:
    from .relay import Budget
    from .store import Store
    from .texlive import TexLive

log = logging.getLogger("texloom.relay")

MAX_BLOB = 50 * MiB  # uploaded over HTTP
UPLOAD_TIME = 600  # seconds an upload may take; slow ones use only a connection and a temporary file


def blob_ids(request: web.Request) -> tuple[str, str]:
    """The project and stored file a /blob/{project}/{id} request names."""
    project, bid = request.match_info["project"], request.match_info["id"]
    if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
        raise web.HTTPBadRequest()
    return project, bid


def x_sig(request: web.Request) -> bytes:
    """The signature a request carries (X-Sig); one that isn't base64 is refused."""
    try:
        return b64d(request.headers.get("X-Sig", ""))
    except ValueError:  # (binascii.Error is one)
        raise web.HTTPForbidden(text="bad signature") from None


class Endpoints:
    """HTTP requests."""

    def __init__(self, policy: Policy, memory: Budget, texlive: TexLive | None = None, codes_url: str = "",
                 dropbox_app_key: str = ""):
        self.policy = policy
        self.memory = memory  # shared with the WebSocket protocol (see MEMORY)
        self.texlive = texlive  # TeX Live files on demand, when served (for /api/info)
        self.codes_url = codes_url  # where people learn how to get a creation code (/api/info)
        self.dropbox_app_key = dropbox_app_key  # the server's Dropbox app, for the web app's Dropbox integration

    @property
    def store(self) -> Store:
        return self.policy.store

    async def put_blob(self, request: web.Request) -> web.Response:
        project, bid = blob_ids(request)
        if (request.content_length or 0) > MAX_BLOB:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BLOB, actual_size=request.content_length or 0)
        if await self.store.deleted(project):
            return web.json_response({"code": "deleted"}, status=410)
        # before reading a large body; the project is bound to the code only once the upload is signed
        why = await self.policy.denied(project, request.headers.get("X-Create-Code"), claim=False)
        if why:
            return web.json_response({"code": why}, status=403)
        sig = x_sig(request)
        if not (len(sig) == 64 or 162 <= len(sig) <= 417):  # an owner or personal-link signature
            raise web.HTTPForbidden(text="bad signature")
        # Read to a temporary file, so a slow or stalled upload holds up nobody else. Then it takes
        # its turn in memory (see MEMORY): anyone can send uploads, signed or not, and the signature
        # can only be checked once the whole file is in memory.
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=MiB, dir=self.store.dir) as spool:
            try:
                async with asyncio.timeout(UPLOAD_TIME):
                    async for piece in request.content.iter_chunked(64 * KiB):
                        size += len(piece)
                        if size > MAX_BLOB:
                            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BLOB, actual_size=size)
                        if size > MiB and self.store.free_disk() < self.policy.limits.min_free:
                            return web.json_response({"code": "server-full"}, status=507)
                        spool.write(piece)
            except TimeoutError:
                raise web.HTTPRequestTimeout() from None
            await self.memory.take(size)
            try:
                return await self._store_upload(request, project, bid, sig, spool, size)
            finally:
                self.memory.give(size)

    async def _store_upload(self, request: web.Request, project: str, bid: str, sig: bytes, spool, size: int) -> web.Response:
        """The rest of put_blob, with the file read to `spool` and its memory taken."""
        # one copy in memory: the signed message (prefix + file), with the file a view into it
        prefix = f"blob:{bid}:".encode()
        signed = bytearray(len(prefix) + size)
        signed[: len(prefix)] = prefix
        spool.seek(0)
        # From here on, answers are returned rather than raised: a raised one would keep this frame,
        # and the file in memory, alive (see the middleware `headers`).
        if spool.readinto(memoryview(signed)[len(prefix):]) != size:
            return web.Response(status=400, text="short upload")
        data = memoryview(signed)[len(prefix):]
        # checking the signature reads the whole file: on a worker thread, so live editing carries on
        if not await asyncio.to_thread(verify, project, signed, sig, await self.store.revoked(project)):
            return web.Response(status=403, text="bad signature")
        if await self.store.deleted(project):  # deleted while it was uploading
            return web.json_response({"code": "deleted"}, status=410)
        if await self.policy.denied(project, request.headers.get("X-Create-Code")):  # a genuine upload: now it may claim
            return web.json_response({"code": "needs-code"}, status=403)
        if await self.store.relay_only(project):
            return web.Response(status=409, text="project is relay-only: blobs are exchanged peer to peer")
        kind = "output" if request.headers.get("X-Kind") == "output" else "file"
        async with self.store.lock(project):  # the check and the reservation; not the (maybe slow) write
            if await self.store.has_blob(project, bid):
                if kind == "file":
                    await self.store.mark_file(project, bid)
                return web.Response(status=200)
            refused = await self.policy.may_store(project, len(data), self.policy.codes.check(request.headers.get("X-Create-Code")))
            if refused:
                status = {"needs-code": 403, "code-expired": 403, "quota-full": 507, "project-full": 413, "server-full": 507}[refused]
                return web.json_response({"code": refused}, status=status)
            self.store.reserve(project, len(data))
        try:
            stored = await self.store.put_blob(project, bid, data, kind, reserved=True)
        except (FilesError, OSError):
            log.exception("storing a file")
            return web.json_response({"code": "store-failed"}, status=503)
        return web.Response(status=201 if stored else 200)

    async def delete_blob(self, request: web.Request) -> web.Response:
        """Erase stored content. Any writer may erase compiled output (replaced by every compile);
        only the owner may erase other content (e.g. outputs stored before outputs were marked).
        This isn't deleting a file from the project, which any editor does in the document."""
        project, bid = blob_ids(request)
        sig = x_sig(request)
        who = signer(project, f"del:{bid}".encode(), sig)
        if who is None or who in await self.store.revoked(project):
            raise web.HTTPForbidden(text="bad signature")
        # X-Only-Output: a client that can't check history asks to delete only marked output
        any_kind = who == "owner" and request.headers.get("X-Only-Output") != "1"
        if await self.store.delete_blob(project, bid, any_kind=any_kind):
            return web.Response(status=204)
        # gone already, or a project file that only the owner may delete
        return web.Response(status=404 if not await self.store.has_blob(project, bid) else 403)

    async def get_blob(self, request: web.Request) -> web.Response:
        project, bid = blob_ids(request)
        if await self.store.deleted(project):
            raise web.HTTPGone()
        if await self.policy.denied(project, None):
            raise web.HTTPForbidden()
        size = await self.store.blob_size(project, bid)
        if size is None:
            raise web.HTTPNotFound()
        return web.Response(body=self.store.read_blob(project, bid), content_type="application/octet-stream", headers={
            "Content-Length": str(size),
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "attachment",  # ciphertext; never something to render
        })

    async def info(self, _request: web.Request) -> web.Response:
        p = self.policy
        p.codes.load()
        d = p.codes.default
        return web.json_response({
            "private": p.private,
            "creation": "open" if p.anyone_creates() else "code",
            "defaultAllowance": None if d == UNLIMITED else d,
            "projectMaxBytes": p.limits.project_max,
            "projectLists": p.lists is not None,
            "texlive": self.texlive.info() if self.texlive else None,
            **({"codesUrl": self.codes_url} if self.codes_url else {}),
            **({"dropboxAppKey": self.dropbox_app_key} if self.dropbox_app_key else {}),
            **({"minVersion": ".".join(map(str, p.min_version))} if p.min_version else {}),
        })

    # --- encrypted project lists (see lists.py) -----------------------------------------
    def _list_request(self, request: web.Request) -> tuple[str, Lists]:
        """The list a request names, and the lists (404 if this server keeps none)."""
        p = self.policy
        if not p.lists:
            raise web.HTTPNotFound()
        if p.min_version and p.too_old(request.headers.get("X-TeXLoom", "").partition("/")[2]):  # app/version
            raise web.HTTPUpgradeRequired(text=json.dumps(p.update_needed()), content_type="application/json")
        lid = request.match_info["id"]
        if not PROJECT_RE.match(lid):
            raise web.HTTPBadRequest()
        return lid, p.lists

    async def get_list(self, request: web.Request) -> web.Response:
        lid, lists = self._list_request(request)
        row = await lists.get(lid)
        if row is None:
            raise web.HTTPNotFound()
        if row[0] == RETIRED:
            return web.json_response({"code": "list-retired"}, status=410)
        return web.Response(body=row[1], content_type="application/octet-stream",
                            headers={"X-Seq": str(row[0]), "Cache-Control": "no-store"})

    @staticmethod
    def _list_signed(lid: str, msg: bytes, request: web.Request) -> bool:
        sig = x_sig(request)
        return len(sig) == 64 and signer(lid, msg, sig) == "owner"

    async def put_list(self, request: web.Request) -> web.Response:
        lid, lists = self._list_request(request)
        if (request.content_length or 0) > MAX_LIST:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_LIST, actual_size=request.content_length or 0)
        try:
            seq = int(request.headers.get("X-Seq", ""))
        except ValueError:
            raise web.HTTPBadRequest()
        data = await request.read()
        if len(data) > MAX_LIST or not self._list_signed(lid, f"list:{lid}:{seq}:".encode() + data, request):
            raise web.HTTPForbidden(text="bad signature")
        row = await lists.meta(lid)
        if row and row[0] == RETIRED:
            return web.json_response({"code": "list-retired"}, status=410)
        current = row[0] if row else 0
        if seq != current + 1:  # someone else updated it: the client merges and tries again
            return web.json_response({"seq": current}, status=409)
        codes, limits = self.policy.codes, self.policy.limits
        owner = row[1] if row else (codes.check(request.headers.get("X-Create-Code")) or "default")
        if codes.allowance(owner) <= 0 or (self.policy.private and owner == "default"):
            return web.json_response({"code": "code-expired" if codes.lapsed(owner) else "needs-code"}, status=403)
        more = len(data) - (row[2] if row else 0)  # what this version adds
        allowance = codes.allowance(owner)
        if allowance != UNLIMITED and more > 0 and self.store.owner_usage(owner) + more > allowance:
            return web.json_response({"code": "list-full"}, status=507)  # (the apps know this one)
        if (self.store.free_disk() - len(data) < limits.min_free or self.store.total + more > limits.max_bytes
                or (not row and not await lists.room_for(owner))):
            return web.json_response({"code": "list-full"}, status=507)
        if not await lists.put(lid, seq, data, owner):  # another update got there first (another server, say)
            now = await lists.meta(lid)
            return web.json_response({"seq": now[0] if now else 0}, status=409)
        return web.json_response({"seq": seq})

    async def delete_list(self, request: web.Request) -> web.Response:
        lid, lists = self._list_request(request)
        if not self._list_signed(lid, f"listdel:{lid}".encode(), request):
            raise web.HTTPForbidden(text="bad signature")
        await lists.retire(lid)
        return web.Response(status=204)

    async def check_code(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            code = str(body.get("code", ""))[:64] if isinstance(body, dict) else ""
        except ValueError:
            raise web.HTTPBadRequest()
        c = self.policy.codes.check(code)
        if not c:
            return web.json_response({"ok": False})
        allowance = self.policy.codes.allowance(c)
        return web.json_response({"ok": True, "allowance": None if allowance == UNLIMITED else allowance,
                                  "used": self.store.owner_usage(c)})


# Content-Security-Policy for the web app. Scripts only from this origin (WebAssembly
# allowed for in-browser TeX); network to this origin and https: (LLM providers) and
# localhost (local LLMs); no framing, plugins or base/form tricks.
CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
       "connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; "
       "style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


@web.middleware
async def headers(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            # Without its traceback: that keeps the handler's frame, and so everything it held (an
            # upload of 50 MB, say), alive until the garbage collector finds the cycle.
            resp = e.with_traceback(None)
    resp.headers["Access-Control-Allow-Origin"] = "*"  # sync data is ciphertext; keys never leave the page
    resp.headers["Access-Control-Allow-Headers"] = "X-Sig, X-Seq, X-Create-Code, X-Kind, X-Only-Output, X-TeXLoom, Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "X-Seq"  # project lists kept on several servers are read and written from any page
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if not request.path.startswith(("/ws/", "/blob/", "/api/", "/core/")):
        resp.headers["Content-Security-Policy"] = CSP
    if isinstance(resp, web.HTTPException) and resp.status >= 400:
        raise resp
    return resp
