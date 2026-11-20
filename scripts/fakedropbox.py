"""A stand-in for Dropbox's API (www, api and content hosts on one server), for
scripts/check_web_dropbox.py: only what web/src/dropbox.ts calls, behaving as Dropbox does where
it matters.

Sign-in is OAuth with PKCE (the authorize page approves at once and sends the browser back with a
code; the token endpoint checks the verifier against the challenge). Files have revisions, and old
revisions can be downloaded ("rev:…"); listings come a few entries a page; an upload in "update"
mode is refused if the file has changed since that revision, and in "add" mode if one is there; a
deletion names the revision it deletes. Content hashes are Dropbox's (SHA-256 of 4 MiB blocks).
Two accounts: "ann", and "bob", who sees ann's /Papers/paper as /Shared/paper (a shared folder
sits at a different path in each member's Dropbox).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import secrets
from urllib.parse import urlencode

from aiohttp import web

PAGE = 3  # entries per listing page: checks the paging


def content_hash(d: bytes) -> str:
    return hashlib.sha256(b"".join(hashlib.sha256(d[i:i + 4 << 20]).digest() for i in range(0, len(d), 4 << 20))).hexdigest()


class Store:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}  # lower path -> {display, data, rev}
        self.folders: set[str] = set()
        self.revs: dict[str, bytes] = {}  # every revision's content
        self.n = itertools.count(1)
        self.codes: dict[str, tuple[str, str]] = {}  # code -> (challenge, account)
        self.tokens: dict[str, str] = {}  # access or refresh token -> account
        self.next_account = "ann"
        self.calls: list[str] = []

    def put(self, path: str, data: bytes) -> dict:
        rev = f"{next(self.n):09x}"
        self.revs[rev] = data
        e = self.files[path.lower()] = {"display": path, "data": data, "rev": rev}
        parts = path.split("/")
        for i in range(2, len(parts)):
            self.folders.add("/".join(parts[:i]).lower())
        return e

    def meta(self, e: dict) -> dict:
        return {".tag": "file", "name": e["display"].rsplit("/", 1)[-1], "path_display": e["display"], "path_lower": e["display"].lower(),
                "rev": e["rev"], "size": len(e["data"]), "content_hash": content_hash(e["data"]), "id": "id:" + e["rev"]}


S = Store()
VIEW = {"bob": ("/shared/paper", "/papers/paper")}  # bob's path -> where it is in the store


def real(account: str, path: str) -> str:
    if account in VIEW and path.lower().startswith(VIEW[account][0]):
        return VIEW[account][1] + path[len(VIEW[account][0]):]
    return path


def shown(account: str, path: str) -> str:
    if account in VIEW and path.lower().startswith(VIEW[account][1]):
        return "/Shared/paper" + path[len(VIEW[account][1]):]
    return path


def err(summary: str, status: int = 409) -> web.Response:
    return web.json_response({"error_summary": summary, "error": {".tag": summary.split("/")[0]}}, status=status)


@web.middleware
async def cors(req: web.Request, handler):
    if req.method == "OPTIONS":
        resp = web.Response()
    else:
        S.calls.append(f"{req.method} {req.path}")
        try:
            resp = await handler(req)
        except web.HTTPException as e:
            resp = e
    resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                         "Access-Control-Allow-Headers": "authorization, content-type, dropbox-api-arg"})
    return resp


def who(req: web.Request) -> str:
    acct = S.tokens.get(req.headers.get("Authorization", "").removeprefix("Bearer "))
    if not acct:
        raise web.HTTPUnauthorized(text=json.dumps({"error_summary": "expired_access_token/"}), content_type="application/json")
    return acct


async def authorize(req: web.Request) -> web.Response:
    q = req.query
    if q.get("code_challenge_method") != "S256" or q.get("response_type") != "code" or not q.get("client_id"):
        return web.Response(status=400, text="bad request")
    code = secrets.token_urlsafe(16)
    S.codes[code] = (q["code_challenge"], S.next_account)
    raise web.HTTPFound(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")


async def token(req: web.Request) -> web.Response:
    f = await req.post()
    if f.get("grant_type") == "authorization_code":
        challenge, acct = S.codes.pop(str(f.get("code")), ("", ""))
        verifier = str(f.get("code_verifier", ""))
        if not acct or base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() != challenge:
            return web.json_response({"error": "invalid_grant"}, status=400)
        refresh = "r-" + secrets.token_hex(8)
        S.tokens[refresh] = acct
    elif f.get("grant_type") == "refresh_token":
        refresh = str(f.get("refresh_token"))
        acct = S.tokens.get(refresh, "")
        if not acct:
            return web.json_response({"error": "invalid_grant"}, status=400)
    else:
        return web.json_response({"error": "unsupported_grant_type"}, status=400)
    access = "a-" + secrets.token_hex(8)
    S.tokens[access] = acct
    return web.json_response({"access_token": access, "expires_in": 14400, "refresh_token": refresh, "account_id": f"dbid:{acct}", "token_type": "bearer"})


async def account(req: web.Request) -> web.Response:
    acct = who(req)
    return web.json_response({"account_id": f"dbid:{acct}", "name": {"display_name": acct.capitalize() + " Example"}})


async def list_folder(req: web.Request) -> web.Response:
    acct = who(req)
    j = await req.json()
    if req.path.endswith("/continue"):
        root, recursive, start = json.loads(base64.b64decode(j["cursor"]))
    else:
        root, recursive, start = real(acct, j["path"]).lower(), j.get("recursive", False), 0
        if root and root not in S.folders:
            return err("path/not_found/")
    items = []
    for lower, e in sorted(S.files.items()):
        if lower.startswith(root + "/") and (recursive or "/" not in lower[len(root) + 1:]):
            m = S.meta(e)
            m["path_display"], m["path_lower"] = shown(acct, m["path_display"]), shown(acct, m["path_lower"]).lower()
            items.append(m)
    for f in sorted(S.folders):
        if f.startswith(root + "/") and (recursive or "/" not in f[len(root) + 1:]) or (root == "" and "/" not in f[1:]):
            d = shown(acct, f)
            items.append({".tag": "folder", "name": d.rsplit("/", 1)[-1], "path_display": d, "path_lower": d.lower()})
    page = items[start:start + PAGE]
    more = start + PAGE < len(items)
    return web.json_response({"entries": page, "has_more": more, "cursor": base64.b64encode(json.dumps([root, recursive, start + PAGE]).encode()).decode()})


async def download(req: web.Request) -> web.Response:
    acct = who(req)
    path = json.loads(req.headers["Dropbox-API-Arg"])["path"]
    if path.startswith("rev:"):
        data = S.revs.get(path[4:])
        if data is None:
            return err("path/not_found/")
        return web.Response(body=data, content_type="application/octet-stream")
    e = S.files.get(real(acct, path).lower())
    if not e:
        return err("path/not_found/")
    return web.Response(body=e["data"], content_type="application/octet-stream")


async def upload(req: web.Request) -> web.Response:
    acct = who(req)
    arg = json.loads(req.headers["Dropbox-API-Arg"])
    path, mode = real(acct, arg["path"]), arg.get("mode", "add")
    data = await req.read()
    e = S.files.get(path.lower())
    if mode == "add" and e:
        return err("path/conflict/file/")
    if isinstance(mode, dict) and mode.get(".tag") == "update" and (not e or e["rev"] != mode["update"]):
        return err("path/conflict/file/")
    S.before_write()
    return web.json_response(S.meta(S.put(e["display"] if e else path, data)) | {"path_display": shown(acct, path)})


async def delete(req: web.Request) -> web.Response:
    acct = who(req)
    j = await req.json()
    path = real(acct, j["path"]).lower()
    e = S.files.get(path)
    if not e:
        return err("path_lookup/not_found/")
    if j.get("parent_rev") and j["parent_rev"] != e["rev"]:
        return err("path_lookup/conflict/")  # (Dropbox: the revision isn't the latest)
    del S.files[path]
    return web.json_response({"metadata": S.meta(e)})


async def create_folder(req: web.Request) -> web.Response:
    acct = who(req)
    path = real(acct, (await req.json())["path"])
    if path.lower() in S.folders or path.lower() in S.files:
        return err("path/conflict/folder/")
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        S.folders.add("/".join(parts[:i]).lower())
    return web.json_response({"metadata": {".tag": "folder", "path_display": path}})


S.before_write = lambda: None  # set by a check to have "someone else" write just before an upload lands


def make_fake() -> web.Application:
    app = web.Application(middlewares=[cors])
    app.router.add_get("/oauth2/authorize", authorize)
    app.router.add_post("/oauth2/token", token)
    app.router.add_post("/2/users/get_current_account", account)
    app.router.add_post("/2/files/list_folder", list_folder)
    app.router.add_post("/2/files/list_folder/continue", list_folder)
    app.router.add_post("/2/files/download", download)
    app.router.add_post("/2/files/upload", upload)
    app.router.add_post("/2/files/delete_v2", delete)
    app.router.add_post("/2/files/create_folder_v2", create_folder)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
    return app
