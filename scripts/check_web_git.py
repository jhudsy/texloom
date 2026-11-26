#!/usr/bin/env python3
"""The web app keeping a project in step with a git repository, on GitHub, GitLab and Gitea
(stand-ins: scripts/fakeforge.py), through its real screens in headless Chromium.

For each host: connect an account and link a repository (its files come in, the branch is filled
in); the project's edits, a new file and a deletion go up as a commit; a commit made outside merges
with the project's own edit to the same file; a clash is reported and "Keep the project's version"
sends it; someone pushing just as a change goes up loses nothing; an empty repository and a branch
that doesn't exist yet get the project. Also: an approval saved before there were other hosts (no
host in it) still counts. GitLab's and Gitea's tree listings come in pages. And Create project ▸
From a git repository… ("new"): one folder of a larger repository, nothing else of it.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_web_git.py [github gitlab gitea new]
"""

from __future__ import annotations

import sys

import fakeforge as F
import webcheck
from webcheck import files_of

PNG = bytes(range(256)) * 4
REPO = {"github": "me/paper", "gitlab": "grp/sub/paper", "gitea": "me/paper"}
PATH = {"github": "gh", "gitlab": "gl", "gitea": "gt"}


def run(s: webcheck.Session, kind: str) -> None:
    fake = s.urls["forge"]
    name = REPO[kind]
    F.REPOS.clear()
    repo = F.REPOS[name] = F.Repo()
    repo.push("main", {"main.tex": b"\\documentclass{article}\n\\begin{document}\nRepo line one.\nRepo line two.\n\\end{document}\n",
                       "sec/intro.tex": b"Intro from the repo.\n", "fig.png": PNG, "notes.tex": b"to be deleted\n"}, "first")
    storage = {"texloom.githubApi": f"{fake}/gh", "texloom.settingsTab": "integrations"}
    page = s.project({"main.tex": "local main\n"}, storage)
    head = lambda p=page: p.evaluate("() => __texloom.r.meta.get('github').head")  # noqa: E731

    # connect and link
    page.click("button.gearbtn")
    webcheck.open_card(page, "Git repositories")
    dlg = page.locator("dialog[open]")
    box = dlg.locator(".git-accounts")
    box.locator("select").select_option(kind)
    if kind != "github":
        box.locator("input[aria-label='Server']").fill(f"{fake}/{PATH[kind]}")
    box.locator("input[type=password]").fill(F.TOKEN)
    box.locator("button:has-text('Connect')").click()
    dlg.locator("text=connected as").wait_for(timeout=10000)
    dlg.locator("button:has-text('Link a repository…')").click()
    ld = page.locator("dialog.zlinkdlg[open]")
    # Gitea: the repository's web address; the others: owner/name (GitLab: with subgroups)
    ld.locator("input[aria-label='Repository']").fill(f"{fake}/gt/{name}.git" if kind == "gitea" else name)
    ld.locator("input[aria-label='Repository']").dispatch_event("change")
    page.wait_for_timeout(500)
    s.check(f"{kind}: the branch is filled in", ld.locator("input[aria-label='Branch']").input_value() == "main")
    ld.locator("label.radio", has_text="the repository’s version").click()
    ld.locator("button:has-text('Link')").click()
    page.wait_for_selector("dialog.zlinkdlg", state="detached", timeout=15000)
    f = files_of(page)
    s.check(f"{kind}: the repository's files come in", "Repo line one" in f.get("main.tex", "") and f.get("sec/intro.tex") == "Intro from the repo.\n"
            and f.get("fig.png") == f"BIN:{len(PNG)}", str(f))
    link = page.evaluate("() => __texloom.r.meta.get('github')")
    s.check(f"{kind}: the link names its host", link.get("kind") == (None if kind == "github" else kind) and link["head"] == repo.branches["main"], str(link))

    def sync(wait=2500):
        dlg.locator("button:has-text('Sync now')").click()
        page.wait_for_timeout(wait)

    # the project's changes go up
    page.evaluate("""() => { const r = __texloom.r; const yt = r.files.get('main.tex'); yt.insert(yt.toString().indexOf('Repo line two'), 'Edited in TeXLoom. ')
      r.files.delete('notes.tex'); const T = r.doc.getText('__t').constructor; r.files.set('new.tex', new T('brand new\\n')) }""")
    page.wait_for_timeout(300)
    sync()
    rf = repo.files("main")
    s.check(f"{kind}: edits, a new file and a deletion go up", b"Edited in TeXLoom." in rf["main.tex"] and "notes.tex" not in rf
            and rf.get("new.tex") == b"brand new\n" and rf["fig.png"] == PNG, str(list(rf)))
    s.check(f"{kind}: the link moves on to that commit", head() == repo.branches["main"])

    # a commit from outside, while the project changes the same file elsewhere
    repo.push("main", {"main.tex": rf["main.tex"].replace(b"Repo line one.", b"Repo line one, changed outside."), "sec/intro.tex": None, "sec/new.tex": b"outside file\n"})
    page.evaluate("() => { const yt = __texloom.r.files.get('main.tex'); yt.insert(yt.toString().indexOf('\\\\end{document}'), 'Added in TeXLoom.\\n') }")
    page.wait_for_timeout(300)
    sync(3000)
    f, rf = files_of(page), repo.files("main")
    s.check(f"{kind}: an outside commit merges with the project's edit", "changed outside" in f["main.tex"] and "Added in TeXLoom" in f["main.tex"]
            and "sec/intro.tex" not in f and f.get("sec/new.tex") == "outside file\n", f["main.tex"])
    s.check(f"{kind}: the merged file goes up", b"changed outside" in rf["main.tex"] and b"Added in TeXLoom" in rf["main.tex"])

    # a clash, then keeping the project's version
    repo.push("main", {"main.tex": rf["main.tex"].replace(b"Added in TeXLoom.", b"Clash outside.")})
    page.evaluate("() => { const yt = __texloom.r.files.get('main.tex'); const i = yt.toString().indexOf('Added in TeXLoom.'); yt.delete(i, 17); yt.insert(i, 'Clash inside.') }")
    sync()
    s.check(f"{kind}: a clash is reported and nothing comes in", dlg.locator(".risk").count() == 1 and "Clash inside" in files_of(page)["main.tex"])
    page.once("dialog", lambda d: d.accept())
    dlg.locator("button:has-text('Keep the project’s version')").click()
    page.wait_for_timeout(2500)
    rf = repo.files("main")
    s.check(f"{kind}: Keep the project's version sends it", b"Clash inside." in rf["main.tex"] and b"Clash outside" not in rf["main.tex"])

    # someone pushes another file just as the project's change goes up
    F.BEFORE.append(lambda: repo.push("main", {"race.tex": b"raced\n"}))
    page.evaluate("() => __texloom.r.files.get('new.tex').insert(0, 'Race edit. ')")
    sync()
    sync()
    f, rf = files_of(page), repo.files("main")
    s.check(f"{kind}: a push at the same moment: both kept", f.get("race.tex") == "raced\n" and rf.get("race.tex") == b"raced\n"
            and rf.get("new.tex", b"").startswith(b"Race edit.") and f["new.tex"].startswith("Race edit."))
    s.check(f"{kind}: a push at the same moment: in step after", head() == repo.branches["main"])

    if kind == "github":  # an approval saved before there were other hosts has no host in it
        page.evaluate("""() => { const k = 'texloom.githubApproved.' + __texloom.r.keys.project; const v = JSON.parse(localStorage.getItem(k))
          localStorage.setItem(k, JSON.stringify({ repo: v.repo, branch: v.branch, dir: v.dir })) }""")
        page.reload()
        page.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        page.click("button.gearbtn")
        webcheck.open_card(page, "Git repositories")
        sec = page.locator("dialog[open] .github")
        try:
            sec.locator("text=keeps it in step").wait_for(timeout=20000)  # once it has taken its turn again
        except Exception:
            pass
        shown = sec.inner_text()
        s.check("github: an approval from before other hosts still counts",
                "keeps it in step" in shown and sec.locator("button:has-text('Approve and sync')").count() == 0, shown)

    # an empty repository, and a branch that doesn't exist yet
    accounts = page.evaluate("() => ({ 'texloom.github': localStorage.getItem('texloom.github') ?? '', 'texloom.gitAccounts': localStorage.getItem('texloom.gitAccounts') ?? '' })")
    for which in ("empty", "new branch"):
        r2 = F.REPOS[name + "2"] = F.Repo()
        if which == "new branch":
            r2.push("main", {"main.tex": b"on main\n", "extra.tex": b"extra\n"})
        p2 = s.project({"main.tex": "second project\n", "b.tex": "bee\n"}, {**storage, **{k: v for k, v in accounts.items() if v}})
        p2.click("button.gearbtn")
        webcheck.open_card(p2, "Git repositories")
        d2 = p2.locator("dialog[open]")
        d2.locator("button:has-text('Link a repository…')").click()
        l2 = p2.locator("dialog.zlinkdlg[open]")
        l2.locator("input[aria-label='Repository']").fill(name + "2")
        branch = "main" if which == "empty" else "texloom"
        l2.locator("input[aria-label='Branch']").fill(branch)
        l2.locator("label.radio", has_text="the project’s version").click()
        l2.locator("button:has-text('Link')").click()
        p2.wait_for_selector("dialog.zlinkdlg", state="detached", timeout=15000)
        p2.wait_for_timeout(2500)
        d2.locator("button:has-text('Sync now')").click()
        p2.wait_for_timeout(2500)
        rf2 = r2.files(branch)
        s.check(f"{kind}: {which}: the project goes up", rf2.get("main.tex") == b"second project\n" and rf2.get("b.tex") == b"bee\n"
                and (which == "empty" or (rf2.get("extra.tex") == b"extra\n" and r2.files("main")["main.tex"] == b"on main\n")),
                f"{rf2} | {d2.locator('.github').inner_text()}")
        s.check(f"{kind}: {which}: in step", p2.evaluate("() => __texloom.r.meta.get('github').head") == r2.branches[branch])
        p2.close()
    page.close()


def from_repository(s: webcheck.Session) -> None:
    """Create project ▸ From a git repository…: one folder of a repository that holds much else, on
    GitLab, with no account connected yet. Only that folder comes in; the project takes its name."""
    fake = s.urls["forge"]
    F.REPOS.clear()
    repo = F.REPOS["grp/big"] = F.Repo()
    repo.push("main", {"README.md": b"the repository\n", "code/x.py": b"print(1)\n", "data/big.bin": PNG,
                       "papers/p1/main.tex": b"\\documentclass{article}\n\\begin{document}\nPaper one.\n\\end{document}\n",
                       "papers/p1/sec/intro.tex": b"Intro.\n", "papers/p2/main.tex": b"another paper\n"}, "first")
    page = s.page()
    page.goto(s.urls["texloom"])
    page.locator("button.primary.big", has_text="Create project").click()
    page.locator("dialog[open] button.choice", has_text="From a git repository…").click()
    page.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)  # the new project's own first dialog
    page.fill("dialog[open] input[placeholder='Shown to collaborators']", "Tester")
    page.locator("dialog[open] label.radio", has_text="Don’t compile here").last.click()
    page.click("dialog[open] button:has-text('Start')")
    dlg = page.locator("dialog[open]")
    box = dlg.locator(".git-accounts")
    box.wait_for(timeout=30000)
    s.check("from a repository: with no account yet, it asks for one first", "From a git repository" in dlg.locator("h3").inner_text())
    box.locator("select").select_option("gitlab")
    box.locator("input[aria-label='Server']").fill(f"{fake}/gl")
    box.locator("input[type=password]").fill(F.TOKEN)
    box.locator("button:has-text('Connect')").click()
    ld = page.locator("dialog.zlinkdlg[open]:has(input[aria-label='Repository'])")
    ld.wait_for(timeout=10000)
    s.check("from a repository: then the repository, with no “whose version” to choose", ld.locator("input[name=gh-prefer]").count() == 0)
    ld.locator("input[aria-label='Repository']").fill("grp/big")
    ld.locator("input[aria-label='Folder in the repository']").fill("papers/p1")
    ld.locator("button:has-text('Bring it in')").click()
    page.wait_for_selector("dialog.zlinkdlg", state="detached", timeout=15000)
    f = files_of(page)
    s.check("from a repository: only the folder's files come in, at the top of the project",
            set(f) == {"main.tex", "sec/intro.tex"} and "Paper one." in f["main.tex"], str(sorted(f)))
    s.check("from a repository: the project is named after the folder", page.evaluate("() => __texloom.r.meta.get('name')") == "p1")
    link = page.evaluate("() => __texloom.r.meta.get('github')")
    s.check("from a repository: and kept in step with that folder", link.get("dir") == "papers/p1" and link.get("head") == repo.branches["main"], str(link))
    page.close()


if __name__ == "__main__":
    with webcheck.Session({"forge": F.make_fake()}) as s:
        for kind in sys.argv[1:] or ["github", "gitlab", "gitea", "new"]:
            from_repository(s) if kind == "new" else run(s, kind)
    s.done()
