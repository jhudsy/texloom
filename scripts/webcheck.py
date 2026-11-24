"""What the browser checks of the web app share (scripts/check_web_*.py): a throwaway TeXLoom
server serving web/dist (build it first), headless Chromium, and a new project to work in.

    with webcheck.Session() as s:          # the server, its database, and a browser
        page = s.project({"main.tex": "…"}) # a new project in its own browser profile, with these files
        s.check("what", ok)                # PASS/FAIL lines; s.failed at the end

Pages run with the web app's test hooks on (window.__texloom: the replica, the editor view, open a
file). Needs Playwright's Chromium (and, with TEXLOOM_CHECK_DATABASE=postgresql, PostgreSQL's programs: scripts/testdb.py):
    uv run --with playwright python -m playwright install chromium

Against a real deployment instead (a staging server, issue 23), from a computer that can reach it:
    TEXLOOM_CHECK_SERVER=https://staging.example.org TEXLOOM_CHECK_CODE_FILE=~/staging-code \
      uv run --extra server --with playwright python scripts/check_web_review.py
The browser opens it through a pass-through on 127.0.0.1 (the web app's test hooks are only for pages
from localhost; everything past the pass-through is the deployment's own: TLS, the web server in
front, the server). The stand-ins (git hosts, Dropbox) still run here. The file holds one of that server's creation codes, if it asks for one: the checks store their
projects and a project list there, as a person would. Use a server meant for testing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import pg  # noqa: E402
import testdb

MOD = "Meta" if sys.platform == "darwin" else "Control"


class Session:
    """A server and a browser, for the length of a `with`. `extra` adds aiohttp apps of stand-ins,
    each on its own port: `self.urls[name]` is where."""

    def __init__(self, extra: dict | None = None) -> None:
        self.extra = extra or {}
        self.urls: dict[str, str] = {}
        self.failed = False
        self.loop = asyncio.new_event_loop()

    # --- the server, on its own thread ---
    def __enter__(self) -> Session:
        from aiohttp import web

        from server.app import Settings, make_app
        self.remote = os.environ.get("TEXLOOM_CHECK_SERVER", "").rstrip("/")
        code_file = os.environ.get("TEXLOOM_CHECK_CODE_FILE")
        self.code = Path(code_file).expanduser().read_text().strip() if code_file else ""
        self._tmp = tempfile.TemporaryDirectory()
        apps = list(self.extra.items())
        if self.remote:
            apps.insert(0, ("texloom", passthrough(self.remote)))
            print(f"against {self.remote}" + (" (with a creation code)" if self.code else ""))
        else:
            dist = ROOT / "web" / "dist"
            if not (dist / "index.html").exists():
                sys.exit("Build the web app first: cd web && npm run build")
            self._pg = testdb.running()
            db = self._pg.__enter__()
            apps.insert(0, ("texloom", make_app(Settings(db, dist, data=Path(self._tmp.name)))))

        async def start():
            runners = []
            for name, a in apps:
                r = web.AppRunner(a)
                await r.setup()
                port = pg.free_port()
                await web.TCPSite(r, "127.0.0.1", port).start()
                self.urls[name] = f"http://127.0.0.1:{port}"
                runners.append(r)
            return runners

        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._runners = asyncio.run_coroutine_threadsafe(start(), self.loop).result(60)
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.browser.close()
            self._pw.stop()
        finally:
            async def stop():
                for r in self._runners:
                    await r.cleanup()
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(60)
            self.loop.call_soon_threadsafe(self.loop.stop)
            if not self.remote:
                self._pg.__exit__(None, None, None)
            self._tmp.cleanup()

    # --- pages ---
    def page(self, storage: dict[str, str] | None = None, **kw):
        """A page in a new browser profile, with the test hooks and these localStorage entries set."""
        p = self.browser.new_page(viewport={"width": 1400, "height": 950}, **kw)
        p.on("pageerror", lambda e: print("  page error:", e))
        items = {"texloom.testHooks": "1", **({"texloom.code": self.code} if self.code else {}), **(storage or {})}
        p.add_init_script(f"try {{ for (const [k, v] of Object.entries({json.dumps(items)})) localStorage.setItem(k, v) }} catch {{}}")  # (none on about:blank)
        return p

    def project(self, files: dict[str, str] | None = None, storage: dict[str, str] | None = None, name: str = "Tester"):
        """A new project (not compiled in the browser), with these files, open in a new page."""
        from texloom.crypto import Keys
        p = self.page(storage)
        p.link = Keys.generate().link(self.urls["texloom"])  # (for a collaborator to open)
        p.goto(p.link)
        p.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)
        p.fill("dialog[open] input[placeholder='Shown to collaborators']", name)
        p.locator("dialog[open] label.radio", has_text="Don’t compile here").last.click()
        p.click("dialog[open] button:has-text('Start')")
        p.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        if files:
            set_files(p, files)
        return p

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        print(("PASS " if ok else "FAIL ") + what + ("" if ok or not detail else f"\n     {detail}"))
        self.failed |= not ok
        return ok

    def done(self) -> None:
        print("FAILED" if self.failed else "ALL PASS")
        sys.exit(1 if self.failed else 0)


def passthrough(target: str):
    """An aiohttp app that hands every request, websockets too, to `target` and its answers back."""
    import aiohttp
    from aiohttp import web
    SKIP = {"host", "content-length", "content-encoding", "transfer-encoding", "connection", "upgrade",
            "strict-transport-security"}
    session: list[aiohttp.ClientSession] = []

    def client() -> aiohttp.ClientSession:
        if not session:
            session.append(aiohttp.ClientSession(auto_decompress=True))
        return session[0]

    async def handle(request: web.Request) -> web.StreamResponse:
        url = target + str(request.rel_url)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP | {"origin", "referer", "accept-encoding"}
                   and not k.lower().startswith("sec-websocket")}
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ours = web.WebSocketResponse(max_msg_size=0)
            await ours.prepare(request)
            async with client().ws_connect(url.replace("https:", "wss:", 1).replace("http:", "ws:", 1), headers=headers,
                                           max_msg_size=0) as theirs:
                async def pump(a, b):
                    async for m in a:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            await b.send_str(m.data)
                        elif m.type == aiohttp.WSMsgType.BINARY:
                            await b.send_bytes(m.data)
                    await b.close()
                await asyncio.gather(pump(ours, theirs), pump(theirs, ours), return_exceptions=True)
            return ours
        async with client().request(request.method, url, headers=headers, data=await request.read(),
                                    allow_redirects=False) as r:
            body = await r.read()
            out = web.Response(status=r.status, body=body)
            for k, v in r.headers.items():
                if k.lower() not in SKIP:
                    out.headers.add(k, v)
            return out

    async def close(_app):
        if session:
            await session[0].close()

    app = web.Application(client_max_size=1 << 30)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_cleanup.append(close)
    return app


def set_files(page, files: dict[str, str]) -> None:
    """Put text files into the open project (replacing any of the same name), in one change."""
    page.evaluate("""(files) => { const r = __texloom.r; const T = r.doc.getText('__t').constructor
      r.doc.transact(() => { for (const [k, v] of Object.entries(files)) r.files.set(k, new T(v)) }) }""", files)


def files_of(page) -> dict[str, str]:
    """The project's files: text as it is, others as 'BIN:<size>'."""
    return page.evaluate("""() => { const r = __texloom.r
      return Object.fromEntries([...r.files.keys()].map(k => [k, r.files.get(k).toString()])
        .concat([...r.bin.keys()].map(k => [k, 'BIN:' + r.bin.get(k).size]))) }""")


def select(page, file: str, start: int, end: int) -> None:
    """Open a file in the editor and select [start, end)."""
    page.evaluate(f"() => __texloom.open({json.dumps(file)})")
    page.wait_for_timeout(300)
    page.evaluate(f"() => {{ const v = __texloom.view(); v.focus(); v.dispatch({{ selection: {{ anchor: {start}, head: {end} }} }}) }}")


def open_card(page, name: str):
    """Unfold an integration's card in the open Settings (integrationcard.ts), as a person would by
    clicking its name; returns the card."""
    card = page.locator("dialog[open] details.icard", has=page.locator(f"summary .iname:text-is('{name}')"))
    card.wait_for(timeout=10000)
    if card.get_attribute("open") is None:
        card.locator("summary").click()
    return card
