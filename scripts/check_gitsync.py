#!/usr/bin/env python3
"""The desktop app's git bridge (texloom/gitsync.py) against real git: a bare repository as the
remote, the bridge's clone, and another clone playing a co-author who pushes. The project is either
the whole repository or one folder in it (a paper in a repository that holds much else):

- check: a folder in a repository is accepted, and one in none is refused with a reason;
- TeXLoom -> git: the project's changed files are committed and pushed, and nothing else: not
  .texloom, and, in a folder, nothing outside it, not what was changed there and not what was
  staged there (work of your own stays as it was);
- git -> TeXLoom: a co-author's push is merged, and the project's new files are shared;
- what mustn't come in doesn't: a change to .texloom in the project, or conflict markers, leave
  the project as it was (in a folder, a .texloom elsewhere in the repository isn't the project's).

    uv run python scripts/check_gitsync.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from checklib import check, summary  # noqa: E402

from texloom.folderrules import not_project_file  # noqa: E402
from texloom.gitsync import GitBridge, repo_top  # noqa: E402


class Meta(dict):
    def observe(self, _f) -> None:
        pass


class Replica:
    """What the bridge uses of a replica: no link to a repository from the web app, so no turns to take."""

    def __init__(self) -> None:
        self.meta = Meta()
        self.lock = asyncio.Lock()
        self.ws = None
        self.synced = asyncio.Event()


class Folder:
    """What the bridge uses of FolderSync: which files are the project's, and sharing them."""

    def __init__(self) -> None:
        self.r = Replica()
        self.shared = 0

    def ignored(self, rel: str) -> bool:
        return not_project_file(rel, "main.tex")

    async def ingest_all(self) -> None:
        self.shared += 1


# this computer's own git settings (commit signing, hooks) stay out of it: for the bridge's git too
os.environ.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"})


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def repository(tmp: Path) -> tuple[Path, Path, Path]:
    """A remote with a paper in papers/p1 and much else, the bridge's clone, and a co-author's."""
    remote, seed = tmp / "remote.git", tmp / "seed"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp, "init", "-q", "-b", "main", str(seed))
    write(seed / "README.md", "the repository\n")
    write(seed / "code" / "x.py", "print(1)\n")
    write(seed / "papers" / "p1" / "main.tex", "\\documentclass{article}\n")
    write(seed / "papers" / "p2" / "main.tex", "another paper\n")
    for c in (["add", "-A"], ["commit", "-qm", "start"], ["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "main"]):
        git(seed, *c)
    mine, theirs = tmp / "mine", tmp / "theirs"
    for d in (mine, theirs):
        git(tmp, "clone", "-q", str(remote), str(d))
    return remote, mine, theirs


def bridge(folder: Path) -> tuple[GitBridge, Folder]:
    fs = Folder()
    return GitBridge(folder, fs), fs  # (its device id goes in the folder's state folder)


def at_remote(remote: Path, path: str) -> str | None:
    try:
        return git(remote, "show", f"main:{path}")
    except subprocess.CalledProcessError:
        return None


async def in_a_folder(tmp: Path) -> None:
    print("a folder in a repository (papers/p1):")
    remote, mine, theirs = repository(tmp)
    p1 = mine / "papers" / "p1"
    b, fs = bridge(p1)
    check("the bridge takes a folder inside a clone", await b.check() is None, await b.check())
    check("and knows where it is in the repository", (b.top, b.sub) == (mine.resolve(), "papers/p1"), (b.top, b.sub))
    check("the window can tell it's in one", repo_top(p1.resolve()) == mine.resolve())

    # TeXLoom -> git: the project's files, and nothing else
    write(p1 / "main.tex", "\\documentclass{article}\n% edited in TeXLoom\n")
    write(p1 / "figs" / "a[1].txt", "a figure\n")  # (a name that is also a pattern)
    write(p1 / ".texloom" / "config.json", "{}\n")
    write(mine / "code" / "x.py", "print(2)  # my own work, not ready\n")
    write(mine / "notes.txt", "staged by me\n")
    git(mine, "add", "notes.txt")
    await b.cycle(force=True)
    check("the project's edits are committed and pushed", "edited in TeXLoom" in (at_remote(remote, "papers/p1/main.tex") or ""))
    check("its new files too (one named like a pattern)", at_remote(remote, "papers/p1/figs/a[1].txt") == "a figure\n")
    check("but not its .texloom", at_remote(remote, "papers/p1/.texloom/config.json") is None)
    check("nor what was changed outside the folder", "print(1)" in (at_remote(remote, "code/x.py") or ""))
    check("nor what was staged outside it", at_remote(remote, "notes.txt") is None)
    status = git(mine, "status", "--porcelain=v1")
    check("which stay as they were: staged, and changed", "A  notes.txt" in status and " M code/x.py" in status, status)

    # git -> TeXLoom: a co-author's push
    git(theirs, "pull", "-q")
    write(theirs / "papers" / "p1" / "intro.tex", "\\section{Intro}\n")
    write(theirs / "papers" / "p2" / "main.tex", "the other paper, changed\n")
    git(theirs, "add", "-A")
    git(theirs, "commit", "-qm", "co-author")
    git(theirs, "push", "-q")
    shared = fs.shared
    await b.cycle()
    check("a co-author's push comes in", (p1 / "intro.tex").exists())
    check("and is shared with the project", fs.shared == shared + 1)
    check("the rest of the branch comes in too, as a pull would", "changed" in (mine / "papers" / "p2" / "main.tex").read_text())
    check("and your own work is still there", "my own work" in (mine / "code" / "x.py").read_text())

    # what mustn't come in
    git(theirs, "pull", "-q")
    write(theirs / "papers" / "p1" / ".texloom" / "config.json", '{"compile": true}\n')
    git(theirs, "add", "-A", "-f")
    git(theirs, "commit", "-qm", "sneaky")
    git(theirs, "push", "-q")
    before = git(mine, "rev-parse", "HEAD")
    await b.cycle()
    check("a change to the project's .texloom isn't merged", git(mine, "rev-parse", "HEAD") == before and b._blocked)
    git(theirs, "rm", "-q", "-r", "papers/p1/.texloom")
    write(theirs / "other" / ".texloom" / "notes", "not the project's\n")
    git(theirs, "add", "-A", "-f")
    git(theirs, "commit", "-qm", "outside")
    git(theirs, "push", "-q")
    await b.cycle()
    check("a .texloom elsewhere in the repository isn't the project's, so it comes in", (mine / "other" / ".texloom" / "notes").exists())
    write(theirs / "papers" / "p1" / "intro.tex", "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> x\n")
    git(theirs, "commit", "-qam", "markers")
    git(theirs, "push", "-q")
    before = git(mine, "rev-parse", "HEAD")
    await b.cycle()
    check("conflict markers in the project aren't merged", git(mine, "rev-parse", "HEAD") == before
          and "<<<<<<<" not in (p1 / "intro.tex").read_text())


async def whole(tmp: Path) -> None:
    print("the whole repository:")
    remote, mine, _theirs = repository(tmp)
    b, _fs = bridge(mine)
    check("the bridge takes the top of a clone", await b.check() is None and b.sub == "")
    write(mine / "papers" / "p1" / "main.tex", "edited\n")
    write(mine / ".texloom" / "config.json", "{}\n")
    await b.cycle(force=True)
    check("edits anywhere in it are committed and pushed", at_remote(remote, "papers/p1/main.tex") == "edited\n")
    check("but not .texloom", at_remote(remote, ".texloom/config.json") is None)


async def nowhere(tmp: Path) -> None:
    print("not in a repository:")
    d = tmp / "plain"
    d.mkdir()
    b, _fs = bridge(d)
    why = await b.check()
    check("refused, saying why", bool(why) and "isn't in a git repository" in why, why)
    check("and the window can tell", repo_top(d.resolve()) is None)


async def main() -> int:
    for case in (in_a_folder, whole, nowhere):
        with tempfile.TemporaryDirectory() as tmp:
            await case(Path(tmp).resolve())
    return summary("git bridge")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
