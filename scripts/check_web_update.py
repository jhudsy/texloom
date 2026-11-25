#!/usr/bin/env python3
"""The web app says when a new version is ready, in headless Chromium.

A tab that stays open keeps the code it loaded with; the service worker fetches a newer version in the
background but nothing reloads the tab. So when a new service worker takes over a page that was already
under the old one, the page offers "Reload" (never reloading by itself); the first installation, which
also takes over the page, says nothing.

The new version is made by changing web/dist/sw.js for the length of the check (put back afterwards).
The page is opened as localhost, which is where the service worker is registered (not on 127.0.0.1).

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_web_update.py
"""

from __future__ import annotations

import webcheck
from webcheck import ROOT

SW = ROOT / "web" / "dist" / "sw.js"


def main() -> None:
    original = SW.read_text()
    try:
        with webcheck.Session() as s:
            s.urls["texloom"] = s.urls["texloom"].replace("127.0.0.1", "localhost")  # (links and pages use it)
            page = s.project({"main.tex": "\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}\n"})
            page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=30000)  # installed, and in charge
            page.wait_for_timeout(1000)
            s.check("the first installation says nothing", page.locator(".update-notice").count() == 0)
            page.evaluate("() => { window.__loaded = 'old' }")

            SW.write_text(original + "\n// changed by check_web_update.py\n")
            page.evaluate("async () => { const r = await navigator.serviceWorker.getRegistration(); await r.update() }")
            try:
                page.wait_for_selector(".update-notice", timeout=15000)
                shown = True
            except Exception:  # noqa: BLE001
                shown = False
            s.check("a new version taking over a running page is offered", shown)
            s.check("with a Reload button and a way to dismiss it",
                    page.locator(".update-notice button.primary").inner_text() == "Reload" and page.locator(".update-notice button.plain").count() == 1)
            s.check("and the page is not reloaded by itself", page.evaluate("() => window.__loaded") == "old")

            page.locator(".update-notice button.plain").click()
            s.check("dismissing it hides it", page.locator(".update-notice").count() == 0)

            SW.write_text(original + "\n// changed again by check_web_update.py\n")
            page.evaluate("async () => { const r = await navigator.serviceWorker.getRegistration(); await r.update() }")
            page.wait_for_selector(".update-notice", timeout=15000)
            with page.expect_navigation():
                page.locator(".update-notice button.primary").click()
            page.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
            s.check("Reload reloads the page, which comes back with its project", page.evaluate("() => window.__loaded") is None
                    and page.evaluate("() => __texloom.r.files.has('main.tex')"))
            page.wait_for_timeout(1500)
            s.check("and the new page has nothing to announce", page.locator(".update-notice").count() == 0)
            s.done()
    finally:
        SW.write_text(original)


if __name__ == "__main__":
    main()
