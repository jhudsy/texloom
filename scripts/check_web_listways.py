#!/usr/bin/env python3
"""The project list's ways in, across the desktop app and the web app: a password set by one opens
the list in the other, setting a new one retires the old, removing one stops it, and replacing or
deleting the list retires it, with every way in, on the server.

The desktop side is texloom/listsync.py, called here directly (with a throwaway TEXLOOM_HOME); the
web side is the list dialog in headless Chromium (scripts/webcheck.py), against a throwaway server.
Build web/dist first.
"""

from __future__ import annotations

import os
import tempfile

import webcheck
from webcheck import ROOT  # noqa: F401  (sets up the import path)

os.environ["TEXLOOM_HOME"] = tempfile.mkdtemp(prefix="texloom-listways-")  # before texloom reads it

from texloom import listsync  # noqa: E402
from texloom.crypto import b64e  # noqa: E402

DESK = ("ada-desk", "correct horse desk one")
WEB = ("ada-web", "correct horse web two")
THIRD = ("ada-third", "correct horse third three")


def password_dialog(p, username: str, password: str, again: bool) -> None:
    form = p.locator("dialog[open] form").last
    form.locator("input[autocomplete='username']").fill(username)
    pw = form.locator("input[type='password']")
    pw.nth(0).fill(password)
    if again:
        pw.nth(1).fill(password)
    form.locator("button[type='submit']").click()


def opens(server: str, who: tuple[str, str]) -> str:
    """What a username and password do on the desktop: 'opens <id>', or the error's code."""
    try:
        return "opens " + listsync.unlock_with_password(server, *who).id
    except listsync.ListError as e:
        return e.code


def _code(work) -> str | None:
    try:
        work()
    except listsync.ListError as e:
        return e.code
    return None


def main() -> None:
    with webcheck.Session() as s:
        url = s.urls["texloom"]
        if s.code:
            listsync.remember_code(url, s.code)

        # the desktop app starts a list, and gives it a password
        key = listsync.ListKey.generate()
        listsync.save_link(key.link(url))
        listsync.sync_this_computer()
        listsync.set_password(key, url, *DESK)
        slots = listsync.read_slots(key, url)
        s.check("the desktop app sets a password", [v["kind"] for v in slots.values()] == ["password"], str(slots))
        s.check("which opens the list on the desktop", opens(url, DESK) == "opens " + key.id, opens(url, DESK))

        # a browser with no list opens it with that password
        p = s.page()
        p.on("dialog", lambda d: d.accept())  # "Use this list here?"
        p.goto(url + "/")
        p.click("button:has-text('Sync between your devices')")
        p.locator("dialog[open] button", has_text="Password").last.click()
        password_dialog(p, *DESK, again=False)
        p.wait_for_function("() => localStorage.getItem('texloom.listKey')", timeout=30000)
        s.check("the web app opens it with the desktop's password", p.evaluate("localStorage.getItem('texloom.listKey')") == b64e(key.secret))

        # the browser sets a new password: it replaces the desktop's
        p.locator("dialog[open] button", has_text="Set a password").click()
        password_dialog(p, *WEB, again=True)
        p.wait_for_selector("dialog[open] .ok:has-text('Password set')", timeout=30000)
        s.check("the web app's password opens it on the desktop", opens(url, WEB) == "opens " + key.id, opens(url, WEB))
        s.check("and the desktop's old one no longer does", opens(url, DESK) == "wrong-password", opens(url, DESK))
        slots = listsync.read_slots(key, url)
        s.check("the desktop app sees one password", [v["kind"] for v in slots.values()] == ["password"], str(slots))

        # the desktop app removes it
        sid, slot = next(iter(slots.items()))
        listsync.remove_slot(key, url, sid, slot)
        s.check("removed on the desktop, it opens nothing", opens(url, WEB) == "wrong-password", opens(url, WEB))
        s.check("and the record has no ways in", listsync.read_slots(key, url) == {})

        s.check("a retired password can't be used again", _code(lambda: listsync.set_password(key, url, *DESK)) == "password-taken")

        # replacing the list: the old link is retired, the new one has the same projects
        listsync.set_password(key, url, *THIRD)
        before = listsync.pull(key, url)[1]
        new_link = listsync.replace_list()
        fresh = listsync.ListKey.from_link(new_link)[0]
        try:
            listsync.pull(key, url)
            retired = False
        except listsync.ListError as e:
            retired = e.code == "list-retired"
        s.check("replacing the list retires the old one", retired)
        s.check("its password went with it", opens(url, THIRD) == "wrong-password", opens(url, THIRD))
        s.check("the new list has the same projects", listsync.pull(fresh, url)[1]["projects"] == before["projects"])
        s.check("and this computer uses the new link", listsync.saved_link() == new_link)
        p.goto(url + "/")
        p.wait_for_timeout(3000)
        p.click("button:has-text('Manage sync')")
        s.check("the browser on the old list is told", p.locator("dialog[open]", has_text="replaced or deleted").count() > 0)

        # deleting it
        listsync.delete_list()
        try:
            listsync.pull(fresh, url)
            gone = False
        except listsync.ListError as e:
            gone = e.code == "list-retired"
        s.check("deleting the list retires it on the server", gone)
        s.check("and this computer stops using it", listsync.saved_link() is None)
        s.done()


if __name__ == "__main__":
    main()
