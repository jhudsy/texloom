#!/usr/bin/env python3
"""The web app keeping a project in step with a Dropbox folder (a stand-in: scripts/fakedropbox.py),
through its real screens in headless Chromium.

Checked: connecting (to Dropbox and back in the same tab, PKCE); linking a folder (its files come in, and it gets a
folder id); the project's edits, a new file and a deletion go to Dropbox; a change made in Dropbox
merges with the project's own edit to the same file; a clash is reported and "Keep the project's
version" sends it; someone saving in Dropbox just as a change goes up loses nothing; a collaborator
whose Dropbox has the shared folder at another path approves the link with that path (and a wrong
folder is refused); while the TeXLoom app on someone's computer keeps the same folder, browsers
stand by.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_web_dropbox.py
"""

from __future__ import annotations

import json

import fakedropbox as D
import webcheck
from webcheck import files_of

PNG = bytes(range(256)) * 4
ROOT = "/Papers/paper"


def connect(page) -> None:
    """Connect Dropbox from the open Settings: the tab goes to Dropbox and comes back to the project."""
    back = page.url
    page.locator("dialog[open] .dropbox-account button:has-text('Connect Dropbox')").click()
    page.wait_for_url(lambda u: u == back, timeout=20000)
    page.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
    page.click("button.gearbtn")
    page.locator("dialog[open] .dropbox >> text=Dropbox").first.wait_for(timeout=10000)


def main() -> None:
    with webcheck.Session({"dropbox": D.make_fake()}) as s:
        fake = s.urls["dropbox"]
        storage = {"texloom.dropboxApi": json.dumps({"api": fake, "content": fake, "www": fake}), "texloom.dropboxKey": "checkkey123",
                   "texloom.settingsTab": "integrations"}
        D.S.put(f"{ROOT}/main.tex", b"\\documentclass{article}\n\\begin{document}\nDropbox line one.\nDropbox line two.\n\\end{document}\n")
        D.S.put(f"{ROOT}/sec/intro.tex", b"Intro from Dropbox.\n")
        D.S.put(f"{ROOT}/fig.png", PNG)
        D.S.put(f"{ROOT}/notes.tex", b"to be deleted\n")
        D.S.put(f"{ROOT}/main (Ann's conflicted copy 2026-11-20).tex", b"an old version\n")
        D.S.put("/Other/elsewhere.tex", b"another folder\n")
        dbx = lambda rel: D.S.files.get(f"{ROOT}/{rel}".lower(), {}).get("data")  # noqa: E731

        page = s.project({"main.tex": "local main\n"}, storage)
        page.click("button.gearbtn")
        dlg = page.locator("dialog[open]")
        connect(page)
        s.check("connecting: to Dropbox and back to the project, signed in", "Ann Example" in dlg.locator(".dropbox-account").inner_text())
        acc = json.loads(page.evaluate("() => localStorage.getItem('texloom.dropbox')"))
        s.check("the connection keeps a refresh token, not the app secret", acc["refresh"].startswith("r-") and "secret" not in json.dumps(acc))

        dlg.locator("button:has-text('Link a Dropbox folder…')").click()
        ld = page.locator("dialog.zlinkdlg[open]")
        ld.locator("input[aria-label='Folder in your Dropbox']").fill(ROOT)
        ld.locator("label.radio", has_text="the Dropbox folder’s version").click()
        ld.locator("button:has-text('Link')").click()
        page.wait_for_selector("dialog.zlinkdlg", state="detached", timeout=15000)
        f = files_of(page)
        s.check("linking: the folder's files come in", "Dropbox line one" in f.get("main.tex", "") and f.get("sec/intro.tex") == "Intro from Dropbox.\n"
                and f.get("fig.png") == f"BIN:{len(PNG)}", str(f))
        s.check("a conflicted copy in the folder isn't brought in", not any("conflicted" in k for k in f))
        fid = (dbx(".texloom/folder-id") or b"").decode()
        link = page.evaluate("() => __texloom.r.meta.get('dropbox')")
        s.check("the folder gets an id, and the link names it", len(fid) == 32 and link["folder"] == fid, str(link))

        def sync(p=page, d=None, wait=2500):
            (d or dlg).locator("button:has-text('Sync now')").click()
            p.wait_for_timeout(wait)

        page.evaluate("""() => { const r = __texloom.r; const yt = r.files.get('main.tex'); yt.insert(yt.toString().indexOf('Dropbox line two'), 'Edited in TeXLoom. ')
          r.files.delete('notes.tex'); const T = r.doc.getText('__t').constructor; r.files.set('new.tex', new T('brand new\\n')) }""")
        page.wait_for_timeout(300)
        sync()
        s.check("edits, a new file and a deletion go to Dropbox", b"Edited in TeXLoom." in (dbx("main.tex") or b"") and dbx("notes.tex") is None
                and dbx("new.tex") == b"brand new\n" and dbx("fig.png") == PNG, str(sorted(D.S.files)))

        D.S.put(f"{ROOT}/main.tex", dbx("main.tex").replace(b"Dropbox line one.", b"Dropbox line one, changed in Dropbox."))
        D.S.put(f"{ROOT}/sec/new.tex", b"made in Dropbox\n")
        page.evaluate("() => { const yt = __texloom.r.files.get('main.tex'); yt.insert(yt.toString().indexOf('\\\\end{document}'), 'Added in TeXLoom.\\n') }")
        page.wait_for_timeout(300)
        sync(wait=3000)
        f = files_of(page)
        s.check("a change made in Dropbox merges with the project's edit", "changed in Dropbox" in f["main.tex"] and "Added in TeXLoom" in f["main.tex"]
                and f.get("sec/new.tex") == "made in Dropbox\n", f["main.tex"])
        s.check("the merged file goes to Dropbox", b"changed in Dropbox" in dbx("main.tex") and b"Added in TeXLoom" in dbx("main.tex"))

        D.S.put(f"{ROOT}/main.tex", dbx("main.tex").replace(b"Added in TeXLoom.", b"Clash in Dropbox."))
        page.evaluate("() => { const yt = __texloom.r.files.get('main.tex'); const i = yt.toString().indexOf('Added in TeXLoom.'); yt.delete(i, 17); yt.insert(i, 'Clash in TeXLoom.') }")
        sync()
        s.check("a clash is reported, and nothing comes in", dlg.locator(".dropbox .risk").count() == 1 and "Clash in TeXLoom" in files_of(page)["main.tex"])
        page.once("dialog", lambda d: d.accept())
        dlg.locator(".dropbox button:has-text('Keep the project’s version')").click()
        page.wait_for_timeout(2500)
        s.check("Keep the project's version sends it", b"Clash in TeXLoom." in dbx("main.tex") and b"Clash in Dropbox" not in dbx("main.tex"))

        # someone saves another file in Dropbox just as the project's change goes up, and one to the same file
        once = [True]
        def meanwhile():
            if once[0]:
                once[0] = False
                D.S.put(f"{ROOT}/race.tex", b"raced\n")
        D.S.before_write = meanwhile
        page.evaluate("() => __texloom.r.files.get('new.tex').insert(0, 'Race edit. ')")
        sync()
        D.S.before_write = lambda: None
        D.S.put(f"{ROOT}/new.tex", dbx("new.tex").replace(b"brand new", b"brand new, from Dropbox"))
        page.evaluate("() => __texloom.r.files.get('sec/intro.tex').insert(0, 'Project edit. ')")
        sync()
        sync()
        f = files_of(page)
        s.check("a save in Dropbox at the same moment: nothing lost", f.get("race.tex") == "raced\n" and "Race edit." in f["new.tex"]
                and "from Dropbox" in f["new.tex"] and dbx("new.tex") is not None and b"Race edit." in dbx("new.tex") and b"from Dropbox" in dbx("new.tex")
                and dbx("sec/intro.tex").startswith(b"Project edit."), f"{f.get('new.tex')!r} / {dbx('new.tex')!r}")

        # a collaborator with their own Dropbox, where the shared folder is /Shared/paper
        D.S.next_account = "bob"
        p2 = s.page(storage)
        p2.goto(page.link)
        p2.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)
        p2.fill("dialog[open] input[placeholder='Shown to collaborators']", "Bob")
        p2.locator("dialog[open] label.radio", has_text="Don’t compile here").last.click()
        p2.click("dialog[open] button:has-text('Start')")
        p2.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        p2.click("button.gearbtn")
        d2 = p2.locator("dialog[open]")
        connect(p2)
        d2.locator(".dropbox >> text=Approve and sync").wait_for(timeout=10000)
        s.check("a collaborator's browser asks before syncing the link", "linked this project to the Dropbox folder" in d2.locator(".dropbox").inner_text())
        d2.locator(".dropbox button:has-text('Approve and sync')").click()
        ad = p2.locator("dialog.zlinkdlg[open]")
        ad.locator("input[aria-label='Folder in your Dropbox']").fill("/Other")
        ad.locator("button:has-text('Approve and sync')").click()
        p2.wait_for_timeout(1500)
        s.check("approving with a folder that isn't the linked one is refused", "isn’t the folder this project is linked to" in ad.inner_text())
        ad.locator("input[aria-label='Folder in your Dropbox']").fill("/Shared/paper")
        ad.locator("button:has-text('Approve and sync')").click()
        p2.wait_for_selector("dialog.zlinkdlg", state="detached", timeout=15000)
        page.close()  # so Bob's browser takes over syncing
        p2.wait_for_timeout(1000)
        D.S.put(f"{ROOT}/chapter.tex", b"A chapter, made in Dropbox.\n")
        for _ in range(70):  # it takes over once Ann's turn lapses (her closed tab may not have given it up)
            if d2.locator(".dropbox button:has-text('Sync now')").count():
                sync(p2, d2, 2000)
                if files_of(p2).get("chapter.tex"):
                    break
            else:
                p2.wait_for_timeout(2000)
        s.check("the collaborator syncs the folder at their own path", files_of(p2).get("chapter.tex") == "A chapter, made in Dropbox.\n",
                d2.locator(".dropbox").inner_text())

        # the TeXLoom app on someone's computer keeps the same folder in step: browsers stand by
        p2.evaluate(f"() => __texloom.r.folders.set('{fid}', {{ at: Date.now(), holding: true, by: \"Ann's laptop\" }})")
        D.S.put(f"{ROOT}/chapter.tex", b"A chapter, changed while the app holds it.\n")
        p2.wait_for_timeout(6000)
        text = d2.locator(".dropbox").inner_text()
        s.check("while the app keeps the folder, the browser stands by", "The TeXLoom app on Ann's laptop keeps this folder" in text
                and "while the app holds" not in files_of(p2).get("chapter.tex", ""), text)
    s.done()


if __name__ == "__main__":
    main()
