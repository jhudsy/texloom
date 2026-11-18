"""What the browser checks of the web app share (scripts/check_web_*.py): a throwaway TeXLoom
server serving web/dist (build it first), headless Chromium, and a new project to work in.

    with webcheck.Session() as s:          # the server, its database, and a browser
        page = s.project({"main.tex": "…"}) # a new project in its own browser profile, with these files
        s.check("what", ok)                # PASS/FAIL lines; s.failed at the end

Pages run with the web app's test hooks on (window.__texloom: the replica, the editor view, open a
file). Needs PostgreSQL's programs (scripts/pg.py) and Playwright's Chromium:
    uv run --with playwright python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import pg  # noqa: E402

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

        import server.relay as relay
        dist = ROOT / "web" / "dist"
        if not (dist / "index.html").exists():
            sys.exit("Build the web app first: cd web && npm run build")
        self._tmp = tempfile.TemporaryDirectory()
        self._pg = pg.running()
        db = self._pg.__enter__()

        async def start():
            runners = []
            app = relay.make_app(db, dist, data=Path(self._tmp.name))
            for name, a in [("texloom", app), *self.extra.items()]:
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
            self._pg.__exit__(None, None, None)
            self._tmp.cleanup()

    # --- pages ---
    def page(self, storage: dict[str, str] | None = None, **kw):
        """A page in a new browser profile, with the test hooks and these localStorage entries set."""
        p = self.browser.new_page(viewport={"width": 1400, "height": 950}, **kw)
        p.on("pageerror", lambda e: print("  page error:", e))
        items = {"texloom.testHooks": "1", **(storage or {})}
        p.add_init_script(f"for (const [k, v] of Object.entries({json.dumps(items)})) localStorage.setItem(k, v)")
        return p

    def project(self, files: dict[str, str] | None = None, storage: dict[str, str] | None = None, name: str = "Tester"):
        """A new project (not compiled in the browser), with these files, open in a new page."""
        from texloom.crypto import Keys
        p = self.page(storage)
        p.goto(Keys.generate().link(self.urls["texloom"]))
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
