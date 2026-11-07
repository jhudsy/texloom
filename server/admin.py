"""A read-only admin page: storage, creation codes and projects, at /admin.

Protected with HTTP Basic authentication (any user name; the password is the admin
token from --admin-token-file or TEXLOOM_ADMIN_TOKEN). The server only ever sees
ciphertext, so this shows sizes, owners and activity, never names or contents.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import html
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .relay import Relay


def _size(n: float) -> str:
    if n == float("inf"):
        return "unlimited"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _ago(t: float) -> str:
    if not t:
        return "never"
    d = time.time() - t
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


STYLE = """
body{font:14px system-ui,sans-serif;margin:24px;color:#222;background:#fff}
h1{font-size:20px} h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;width:100%;max-width:1100px}
td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;white-space:nowrap}
td.n,th.n{text-align:right} code{font-size:12px}
details summary{cursor:pointer;list-style:none} details[open] summary{display:none}
.muted{color:#777} .grid{display:grid;grid-template-columns:max-content max-content;gap:4px 16px}
@media (prefers-color-scheme:dark){body{background:#1b1b1b;color:#ddd} td,th{border-color:#333}}
"""


class Admin:
    def __init__(self, relay: Relay, token: str, evict_days: float, purge_days: float | None = None):
        self.relay = relay
        self.token = token.encode()
        self.evict_days = evict_days
        self.purge_days = purge_days

    def _authorized(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(pw.encode(), self.token)

    async def page(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            await asyncio.sleep(1)  # slow down guessing
            return web.Response(status=401, text="admin token required",
                                headers={"WWW-Authenticate": 'Basic realm="TeXLoom admin"'})
        r, st = self.relay, self.relay.store
        r.codes._load()
        e = html.escape
        modes = dict(st.db.execute("SELECT project, relay FROM mode"))
        rows = st.db.execute("SELECT project, created, owner, seen, evicted FROM projects").fetchall()
        online = {p: len(ws) for p, ws in r.rooms.items() if ws}
        label = {c: lab for c, (_, lab) in r.codes.codes.items()}
        label.update({"default": "default (no code)", "existing": "stored before allowances"})

        def code_name(c: str) -> str:
            name = label.get(c, f"{c[:4]}… (code removed)")
            return f"{name} (expired)" if r.codes.expired(c) else name

        def expiry_cell(c: str) -> str:
            u = r.codes.until.get(c)
            if u is None:
                return "<td class=muted>—</td>"
            days = r.codes.lapsed_since(c)
            return f"<td>{u.isoformat()}</td>" if days is None else f"<td><b>expired</b> {days} day{'s' * (days != 1)} ago</td>"

        projects = sorted(rows, key=lambda x: -st.usage.get(x[0], 0))
        prow = "".join(
            f"<tr><td><code>{e(p[:10])}…</code></td><td>{e(code_name(o))}</td>"
            f"<td class=n>{_size(st.usage.get(p, 0))}</td><td>{'relay-only' if modes.get(p) else ('evicted' if ev and not st.usage.get(p) else 'stored')}</td>"
            f"<td>{_ago(created)}</td><td>{_ago(st.seen.get(p, seen))}</td><td class=n>{online.get(p, 0) or ''}</td></tr>"
            for p, created, o, seen, ev in projects[:500])
        owners: dict[str, list[str]] = {}
        for p, _c, o, _s, _e in rows:
            owners.setdefault(o, []).append(p)
        def code_cell(c: str) -> str:
            """The code itself, for handing out: the start shown, the rest on click (no script needed)."""
            if c not in r.codes.codes:
                return "<td class=muted>—</td>"
            full = f"{c[:4]}-{c[4:8]}-{c[8:]}"
            return f"<td><details><summary><code>{e(full[:4])}-…</code></summary><code>{e(full)}</code></details></td>"

        crow = "".join(
            f"<tr><td>{e(code_name(c))}</td>{code_cell(c)}{expiry_cell(c)}<td class=n>{_size(r.codes.allowance(c))}</td>"
            f"<td class=n>{_size(st.owner_usage(c))}</td><td class=n>{len(owners.get(c, []))}</td></tr>"
            for c in ["default", *r.codes.codes.keys(), *(["existing"] if "existing" in owners else [])])
        lim = r.limits
        body = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>TeXLoom admin</title><style>{STYLE}</style></head><body>
<h1>TeXLoom admin</h1>
<div class=grid>
<span>Stored</span><b>{_size(st.total)}</b>
<span>Disk free</span><span>{_size(st.free_disk())} (keeps {_size(lim.min_free)} free)</span>
<span>Total cap</span><span>{_size(lim.max_bytes)}</span>
<span>Per project</span><span>{_size(lim.project_max)} (unlimited codes exempt)</span>
<span>Projects</span><span>{len(rows)} registered, {sum(1 for p in rows if st.usage.get(p[0]))} with stored data</span>
<span>Connected now</span><span>{sum(online.values())} connection(s) in {len(online)} project(s)</span>
<span>Idle eviction</span><span>{f"after {self.evict_days:g} days unopened" if self.evict_days else "off"}</span>
<span>New projects</span><span>{"anyone" if r.codes.open_creation else "creation code needed"}</span>
</div>
<h2>Creation codes</h2>
<table><tr><th>Label</th><th>Code</th><th>Valid until</th><th class=n>Allowance</th><th class=n>Used</th><th class=n>Projects</th></tr>{crow}</table>
<p class=muted>Click a code to show it all. A code lets people store new projects here up to its allowance; it doesn't
open anyone's project. To withdraw one, delete its line from the codes file or let it expire: its projects then fall back to
the default allowance. Purging expired codes' stored copies: {f"after {self.purge_days:g} days" if self.purge_days is not None else "off"}.</p>
<p class=muted>Add a code: <code>texloom-server --codes-file … --add-code LABEL --allowance 1G --months 12</code>;
renew one: <code>--renew-code CODE --months 12</code>.</p>
<h2>Projects (largest first{", first 500" if len(rows) > 500 else ""})</h2>
<table><tr><th>Project id</th><th>Created with</th><th class=n>Stored</th><th>State</th><th>Created</th><th>Last opened</th><th class=n>Online</th></tr>{prow}</table>
<p class=muted>Contents and names are end-to-end encrypted: the server can’t see them.</p>
</body></html>"""
        return web.Response(text=body, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
