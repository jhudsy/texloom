#!/usr/bin/env python3
"""The project list in the browser: opening a project while the list is being synced keeps the time it
was opened. The sync used to put back the list as it had sent it, so a project opened while the server
answered (on every page load) kept showing when it was opened before.

Against a throwaway server, in headless Chromium (scripts/webcheck.py); build web/dist first.
"""

from __future__ import annotations

import json
import os
import time

import webcheck
from webcheck import ROOT  # noqa: F401  (sets up the import path)

from texloom.crypto import Keys, b64e

HOURS = 3600_000
CPU_SLOWER = 20
LIST_DELAY = 4  # seconds the list server takes to answer: long enough for the project to open meanwhile


def recent_at(page, project: str) -> int | None:
    return page.evaluate("""(p) => (JSON.parse(localStorage.getItem('texloom.recent') || '[]').find((r) => r.project === p) || {}).at ?? null""", project)


def main() -> None:
    with webcheck.Session() as s:
        url = s.urls["texloom"]
        keys = Keys.generate()
        link = keys.link(url)
        secret = {"texloom.listKey": b64e(os.urandom(32))}

        # A project opened before, in a browser with no project list yet
        p = s.page()
        p.goto(link)
        p.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)
        p.fill("dialog[open] input[placeholder='Shown to collaborators']", "Tester")
        p.locator("dialog[open] label.radio", has_text="Don’t compile here").last.click()
        p.click("dialog[open] button:has-text('Start')")
        p.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        p.goto(url + "/")
        old = int(time.time() * 1000) - 10 * HOURS
        p.evaluate("""([p, at, key]) => {
            const list = JSON.parse(localStorage.getItem('texloom.recent') || '[]')
            for (const r of list) if (r.project === p) r.at = at
            localStorage.setItem('texloom.recent', JSON.stringify(list))
            localStorage.setItem('texloom.listKey', key)
        }""", [keys.project, old, secret["texloom.listKey"]])
        # it starts using a project list: the server's copy now says it was opened ten hours ago
        p.goto(url + "/")
        p.wait_for_timeout(5000)
        s.check("the list on the server has the earlier time", recent_at(p, keys.project) == old, str(recent_at(p, keys.project)))

        # opened again, while the list server is slow to answer the list itself (its settings come
        # first, then the list, then tags: each under an id of its own)
        ids: list[str] = []

        def slow(route):
            lid = route.request.url.rsplit("/", 1)[-1]
            if lid not in ids:
                ids.append(lid)
            if ids.index(lid) == 1:
                time.sleep(LIST_DELAY)
            route.continue_()
        p.goto(url + "/")
        p.wait_for_timeout(5000)  # its own sync done
        p.route("**/api/list/**", slow)
        p.goto("about:blank")  # (so that the link loads the page afresh)
        # a slow device (or a large project): opening it takes longer than the list's first requests
        cdp = p.context.new_cdp_session(p)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWER})
        before = int(time.time() * 1000)
        p.goto(link)
        p.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        # the page notes the time again whenever the project's settings change, which can hide a
        # time put back: so watch it while the sync finishes, not just at the end
        seen = []
        for _ in range(int(LIST_DELAY * 3 / 0.2)):
            seen.append(recent_at(p, keys.project))
            p.wait_for_timeout(200)
        first = next((i for i, a in enumerate(seen) if a is not None and a >= before), None)
        back = first is not None and any(a is None or a < before for a in seen[first:])
        s.check("opening it during a sync keeps the time it was opened", first is not None and not back,
                f"times seen: {sorted(set(seen), key=seen.index)}, opened at {before}")
        p.unroute("**/api/list/**")
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        p.wait_for_timeout(5000)  # the follow-up exchange takes it to the server

        # another browser with the same list sees it too
        q = s.page(secret)
        q.goto(url + "/")
        q.wait_for_timeout(5000)
        at2 = recent_at(q, keys.project)
        s.check("and the server's copy of the list has it", at2 is not None and at2 >= before, f"at {json.dumps(at2)}")

        # and once in step, the list isn't sent again and again
        puts: list[str] = []
        p.on("request", lambda r: "/api/list/" in r.url and r.method == "PUT" and puts.append(r.url))
        p.wait_for_timeout(10000)
        s.check("once in step, the list isn't sent again", not puts, f"{len(puts)} list update(s) in 10 s")
        s.done()


if __name__ == "__main__":
    main()
