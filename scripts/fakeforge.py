"""Stand-ins for the GitHub, GitLab and Gitea/Forgejo APIs the web app syncs repositories through
(web/src/forges.ts), over one in-memory store of repositories, for scripts/check_web_git.py.

Only what TeXLoom calls, behaving as the real ones do where it matters: blob ids are git's, GitHub
refuses a ref update that isn't a fast-forward and git-API writes to an empty repository, GitLab
lists trees a few entries a page with a Link header and gives no sizes, Gitea pages trees with
`truncated` and refuses an update or deletion whose blob id isn't the current one. Every request
needs the token TOKEN. Served under /gh (api.github.com), /gl (a GitLab) and /gt (a Gitea).
"""
import base64
import hashlib
import itertools
import json
from urllib.parse import unquote
from aiohttp import web

def blob_sha(d: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest()

class Repo:
    def __init__(self):
        self.blobs = {}       # sha -> bytes
        self.commits = {}     # sha -> {parent, files: {path: (sha, mode)}, msg}
        self.branches = {}    # name -> sha
        self.trees = {}       # tree id -> files
        self.default = "main"
        self.n = itertools.count()
    def put(self, d: bytes) -> str:
        s = blob_sha(d); self.blobs[s] = d; return s
    def commit(self, parent, files, msg="c"):
        sha = hashlib.sha1(f"commit {next(self.n)} {msg}".encode()).hexdigest()
        tree = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()
        self.trees[tree] = dict(files)
        self.commits[sha] = {"parent": parent, "files": dict(files), "msg": msg, "tree": tree}
        return sha
    def files(self, branch):
        c = self.branches.get(branch)
        return {p: self.blobs[s] for p, (s, m) in self.commits[c]["files"].items()} if c else {}
    def push(self, branch, changes, msg="outside"):  # a commit made outside TeXLoom
        parent = self.branches.get(branch)
        files = dict(self.commits[parent]["files"]) if parent else {}
        for p, d in changes.items():
            if d is None: files.pop(p, None)
            else: files[p] = (self.put(d), "100644")
        self.branches[branch] = self.commit(parent, files, msg)

REPOS: dict[str, Repo] = {}
BEFORE = []  # run once just before the next commit lands (someone pushing at the same moment)
def before():
    while BEFORE: BEFORE.pop()()
LOG: list[str] = []
TOKEN = "tok_" + "x" * 30

@web.middleware
async def cors(req, handler):
    if req.method == "OPTIONS":
        resp = web.Response()
    else:
        LOG.append(f"{req.method} {req.path_qs}")
        if req.headers.get("Authorization", "") not in (f"Bearer {TOKEN}", f"token {TOKEN}"):
            resp = web.json_response({"message": "Bad credentials"}, status=401)
        else:
            try: resp = await handler(req)
            except web.HTTPException as e: resp = web.json_response({"message": e.reason}, status=e.status)
    resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization,content-type,accept",
                         "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE", "Access-Control-Expose-Headers": "Link"})
    return resp

def repo_of(name) -> Repo:
    r = REPOS.get(unquote(name))
    if not r: raise web.HTTPNotFound(reason="Not Found")
    return r

def e404(): raise web.HTTPNotFound(reason="Not Found")

# ---- GitHub (/gh) ----
gh = web.RouteTableDef()
@gh.get("/gh/user")
async def gh_user(req): return web.json_response({"login": "ghuser"})
@gh.get("/gh/repos/{o}/{n}")
async def gh_repo(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}")
    return web.json_response({"default_branch": r.default, "permissions": {"push": True}, "size": 1 if r.branches else 0})
@gh.get("/gh/repos/{o}/{n}/git/ref/heads/{b:.+}")
async def gh_ref(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); c = r.branches.get(unquote(req.match_info["b"]))
    if not c: raise web.HTTPConflict(reason="Git Repository is empty.") if not r.branches else web.HTTPNotFound(reason="Not Found")
    return web.json_response({"object": {"sha": c}})
@gh.post("/gh/repos/{o}/{n}/git/refs")
async def gh_newref(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json()
    if not r.branches: raise web.HTTPConflict(reason="Git Repository is empty.")
    r.branches[j["ref"].removeprefix("refs/heads/")] = j["sha"]; return web.json_response({}, status=201)
@gh.patch("/gh/repos/{o}/{n}/git/refs/heads/{b:.+}")
async def gh_setref(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json(); b = unquote(req.match_info["b"])
    before()
    if r.commits[j["sha"]]["parent"] != r.branches.get(b): raise web.HTTPUnprocessableEntity(reason="Update is not a fast forward")
    r.branches[b] = j["sha"]; return web.json_response({})
@gh.get("/gh/repos/{o}/{n}/git/commits/{c}")
async def gh_commit(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); c = r.commits.get(req.match_info["c"]) or e404()
    return web.json_response({"tree": {"sha": c["tree"]}})
@gh.get("/gh/repos/{o}/{n}/git/trees/{t}")
async def gh_tree(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); f = r.trees.get(req.match_info["t"])
    if f is None: e404()
    return web.json_response({"truncated": False, "tree": [{"path": p, "type": "blob", "mode": m, "sha": s, "size": len(r.blobs[s])} for p, (s, m) in f.items()]})
@gh.get("/gh/repos/{o}/{n}/git/blobs/{s}")
async def gh_blob(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); d = r.blobs.get(req.match_info["s"])
    if d is None: e404()
    return web.json_response({"content": base64.encodebytes(d).decode(), "encoding": "base64"})
@gh.post("/gh/repos/{o}/{n}/git/blobs")
async def gh_newblob(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json()
    return web.json_response({"sha": r.put(base64.b64decode(j["content"]))}, status=201)
@gh.post("/gh/repos/{o}/{n}/git/trees")
async def gh_newtree(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json()
    files = dict(r.trees[j["base_tree"]]) if j.get("base_tree") else {}
    for e in j["tree"]:
        if e["sha"] is None: files.pop(e["path"], None)
        else: files[e["path"]] = (e["sha"], e["mode"])
    tid = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest(); r.trees[tid] = files
    return web.json_response({"sha": tid}, status=201)
@gh.post("/gh/repos/{o}/{n}/git/commits")
async def gh_newcommit(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json()
    return web.json_response({"sha": r.commit(j["parents"][0] if j["parents"] else None, r.trees[j["tree"]], j["message"])}, status=201)
@gh.put("/gh/repos/{o}/{n}/contents/{p:.+}")
async def gh_put(req):
    r = repo_of(f"{req.match_info['o']}/{req.match_info['n']}"); j = await req.json()
    if r.branches: raise web.HTTPUnprocessableEntity(reason="sha wasn't supplied")
    r.push(j["branch"], {unquote(req.match_info["p"]): base64.b64decode(j["content"])}, j["message"])
    return web.json_response({"commit": {"sha": r.branches[j["branch"]]}}, status=201)

# ---- GitLab (/gl/api/v4) ----
gl = web.RouteTableDef()
P = "/gl/api/v4/projects/{id:.+?}"
@gl.get("/gl/api/v4/user")
async def gl_user(req): return web.json_response({"username": "gluser"})
async def gl_proj(req):
    r = repo_of(req.match_info["id"])
    return web.json_response({"default_branch": r.default if r.branches else None, "empty_repo": not r.branches, "permissions": {"project_access": {"access_level": 40}, "group_access": None}})
@gl.get(P + "/repository/branches/{b}")
async def gl_branch(req):
    r = repo_of(req.match_info["id"]); c = r.branches.get(unquote(req.match_info["b"])) or e404()
    return web.json_response({"commit": {"id": c}})
@gl.post(P + "/repository/branches")
async def gl_newbranch(req):
    r = repo_of(req.match_info["id"]); ref = req.query["ref"]
    r.branches[req.query["branch"]] = r.branches.get(ref, ref); return web.json_response({}, status=201)
@gl.get(P + "/repository/tree")
async def gl_tree(req):
    r = repo_of(req.match_info["id"]); c = r.commits.get(req.query["ref"]) or e404()
    assert req.query.get("recursive") == "true"
    items = [{"id": s, "name": p.split("/")[-1], "type": "blob", "path": p, "mode": m} for p, (s, m) in sorted(c["files"].items())]
    dirs = sorted({"/".join(p.split("/")[:i]) for p in c["files"] for i in range(1, p.count("/") + 1)})
    items = sorted(items + [{"id": "d" * 40, "name": d.split("/")[-1], "type": "tree", "path": d, "mode": "040000"} for d in dirs], key=lambda e: e["path"])
    per = min(int(req.query.get("per_page", 20)), 3); start = int(req.query.get("page_token") or 0)
    resp = web.json_response(items[start:start + per])
    if start + per < len(items):
        q = dict(req.query); q["page_token"] = str(start + per)
        resp.headers["Link"] = f'<http://{req.host}{req.path}?{"&".join(f"{k}={v}" for k, v in q.items())}>; rel="next"'
    return resp
@gl.get(P + "/repository/blobs/{s}/raw")
async def gl_raw(req):
    r = repo_of(req.match_info["id"]); d = r.blobs.get(req.match_info["s"])
    if d is None: e404()
    return web.Response(body=d, content_type="text/plain")
@gl.post(P + "/repository/commits")
async def gl_commit(req):
    r = repo_of(req.match_info["id"]); j = await req.json(); b = j["branch"]
    before()
    parent = r.branches.get(b)
    files = dict(r.commits[parent]["files"]) if parent else {}
    for a in j["actions"]:
        p = a["file_path"]
        if a["action"] == "create" and p in files: raise web.HTTPBadRequest(reason="A file with this name already exists")
        if a["action"] in ("update", "delete") and p not in files: raise web.HTTPBadRequest(reason="A file with this name doesn't exist")
        if a["action"] == "delete": files.pop(p)
        else: files[p] = (r.put(base64.b64decode(a["content"]) if a.get("encoding") == "base64" else a["content"].encode()), files.get(p, (0, "100644"))[1])
    sha = r.commit(parent, files, j["commit_message"]); r.branches[b] = sha
    return web.json_response({"id": sha, "parent_ids": [parent] if parent else []}, status=201)

# ---- Gitea (/gt/api/v1) ----
gt = web.RouteTableDef()
R = "/gt/api/v1/repos/{o}/{n}"
rn = lambda req: f"{req.match_info['o']}/{req.match_info['n']}"
@gt.get("/gt/api/v1/user")
async def gt_user(req): return web.json_response({"login": "gtuser"})
@gt.get(R)
async def gt_repo(req):
    r = repo_of(rn(req)); return web.json_response({"default_branch": r.default, "empty": not r.branches, "permissions": {"push": True}})
@gt.get(R + "/branches/{b}")
async def gt_branch(req):
    r = repo_of(rn(req)); c = r.branches.get(unquote(req.match_info["b"])) or e404()
    return web.json_response({"commit": {"id": c}})
@gt.post(R + "/branches")
async def gt_newbranch(req):
    r = repo_of(rn(req)); j = await req.json(); ref = j["old_ref_name"]
    r.branches[j["new_branch_name"]] = r.branches.get(ref, ref); return web.json_response({}, status=201)
@gt.get(R + "/git/trees/{c}")
async def gt_tree(req):
    r = repo_of(rn(req)); c = r.commits.get(req.match_info["c"]) or e404()
    items = [{"path": p, "mode": m, "type": "blob", "size": len(r.blobs[s]), "sha": s} for p, (s, m) in sorted(c["files"].items())]
    per = min(int(req.query.get("per_page", 1000)), 2); page = int(req.query.get("page", 1))  # tiny pages: tests paging
    chunk = items[(page - 1) * per: page * per]
    return web.json_response({"sha": c["tree"], "tree": chunk, "truncated": page * per < len(items), "page": page, "total_count": len(items)})
@gt.get(R + "/git/blobs/{s}")
async def gt_blob(req):
    r = repo_of(rn(req)); d = r.blobs.get(req.match_info["s"])
    if d is None: e404()
    return web.json_response({"content": base64.b64encode(d).decode(), "encoding": "base64", "sha": req.match_info["s"], "size": len(d)})
@gt.post(R + "/contents")
async def gt_change(req):
    r = repo_of(rn(req)); j = await req.json(); b = j.get("branch") or r.default
    before()
    parent = r.branches.get(b)
    files = dict(r.commits[parent]["files"]) if parent else {}
    for f in j["files"]:
        p, op = f["path"], f["operation"]
        if op == "create" and p in files: raise web.HTTPUnprocessableEntity(reason="repository file already exists")
        if op in ("update", "delete") and (p not in files or files[p][0] != f.get("sha")): raise web.HTTPConflict(reason="sha does not match")
        if op == "delete": files.pop(p)
        else: files[p] = (r.put(base64.b64decode(f["content"])), files.get(p, (0, "100644"))[1])
    sha = r.commit(parent, files, j["message"]); r.branches[b] = sha
    return web.json_response({"commit": {"sha": sha, "parents": [{"sha": parent}] if parent else []}}, status=201)

def make_fake():
    app = web.Application(middlewares=[cors])
    gl.get(P)(gl_proj)
    for t in (gh, gl, gt): app.add_routes(t)
    async def opt(req): return web.Response()
    app.router.add_route("OPTIONS", "/{tail:.*}", opt)
    return app
