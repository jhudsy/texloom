#!/usr/bin/env python3
"""Comments for people working in a synced folder, against a real server: the folder's
.texloom/comments.md overview, and `texloom comments` listing, replying and resolving.

A collaborator (the desktop's sync engine standing in for the web app) comments on a project; a
folder synced with `texloom run --once` then has the overview, with the line each comment is on
and the ones that mention the folder's person marked; `texloom comments DIR` lists them, and
`reply` and `resolve` reach the collaborator, signed with the folder's link.

    uv run --extra server python scripts/check_comments.py
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import testdb  # noqa: E402

from pycrdt import Text  # noqa: E402

from texloom.crypto import Keys  # noqa: E402
from texloom.mcp import Project  # noqa: E402
from texloom.replica import Replica  # noqa: E402

SERVER = "import sys; from server.cli import main; sys.argv[0] = 'texloom-server'; main()"
MAIN = "\\documentclass{article}\n\\begin{document}\nFirst line.\nSecond line, with a typo.\nThird line.\n\\end{document}\n"

ok = True


def check(what: str, cond: bool, detail: object = "") -> None:
    global ok
    ok &= bool(cond)
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def texloom(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TEXLOOM_HOME": str(home), "XDG_CACHE_HOME": str(home / "cache"), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "texloom.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


async def until(cond, secs: float = 20) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="texloom-comments-"))
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    database = testdb.start(tmp / "db")
    server = subprocess.Popen([sys.executable, "-c", SERVER, "--port", str(port), "--database", database, "--data", str(tmp / "relay")],
                              cwd=ROOT, stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
    tasks: list[asyncio.Task] = []
    try:
        for _ in range(300):
            try:
                if urllib.request.urlopen(url + "/api/info", timeout=2).status == 200:
                    break
            except OSError:
                await asyncio.sleep(0.1)
        keys = Keys.generate()
        web = Replica(tmp / "web", keys, url)
        tasks.append(asyncio.create_task(web.run()))
        await asyncio.wait_for(web.synced.wait(), 20)
        web.local_change(lambda: web.files.__setitem__("main.tex", Text(MAIN)))
        web.local_change(lambda: web.meta.__setitem__("name", "Check paper"))
        alice = Project(web, "Alice")
        i = MAIN.index("typo")
        typo = alice._add_item("comment", "main.tex", i, i + 4, "@Dana please fix this typo")
        j = MAIN.index("Third")
        other = alice._add_item("comment", "main.tex", j, j + 5, "Fine as it is.")
        await until(lambda: not web.outbox)

        # a folder on "Dana's" computer: cloned, named, synced once more
        home, d = tmp / "home", tmp / "folder"
        res = texloom(home, "clone", keys.link(url), str(d))
        check("the folder is cloned", res.returncode == 0 and (d / "main.tex").exists(), res.stderr[-800:])
        once = ("import asyncio, json, sys; from pathlib import Path; from texloom.cli import run, config_path; "
                "d = Path(sys.argv[1]); c = json.loads(config_path(d).read_text()); c['name'] = 'Dana'; "
                "config_path(d).write_text(json.dumps(c)); asyncio.run(run(d, False, once=True))")
        env = {**os.environ, "TEXLOOM_HOME": str(home), "XDG_CACHE_HOME": str(home / "cache")}
        res = subprocess.run([sys.executable, "-c", once, str(d)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        check("and synced again as Dana", res.returncode == 0, res.stderr[-800:])

        md = (d / ".texloom" / "comments.md").read_text("utf-8") if (d / ".texloom" / "comments.md").exists() else ""
        check("the overview is in .texloom/comments.md", "# Comments on “Check paper”" in md, md[:300])
        check("it gives each comment's file and line", "### main.tex:4 · comment by Alice" in md and "### main.tex:5" in md, md)
        check("it says how many mention you, and marks them", "1 mention you (@Dana)" in md and md.count("**mentions you**") == 1, md)
        check("the other open comment is listed too", "Fine as it is." in md)

        res = texloom(home, "comments", str(d))
        check("`texloom comments DIR` lists them", res.returncode == 0 and "main.tex:4: comment by Alice" in res.stdout and typo in res.stdout, res.stdout + res.stderr)
        res = texloom(home, "comments", str(d), "--mine")
        check("--mine lists only the ones that mention you", "typo" in res.stdout and "Fine as it is" not in res.stdout, res.stdout)

        res = texloom(home, "comments", "reply", str(d), typo, "Fixed,", "thanks")
        check("`reply` is confirmed", res.returncode == 0 and "Replied." in res.stdout, res.stdout + res.stderr)
        res = texloom(home, "comments", "resolve", str(d), other)
        check("`resolve` is confirmed", res.returncode == 0 and "Resolved." in res.stdout, res.stdout + res.stderr)

        from texloom.comments import read
        got = await until(lambda: any(rp.text == "Fixed, thanks" for c in read(web, True) if c.id == typo for rp in c.replies)
                          and any(c.resolved for c in read(web, True) if c.id == other))
        items = {c.id: c for c in read(web, True)}
        check("the collaborator gets the reply, as from Dana", got and items[typo].replies and items[typo].replies[0].by == "Dana",
              [(c.id, c.replies, c.resolved) for c in items.values()])
        check("and the comment resolved", items[other].resolved)
        check("the reply is signed with the folder's link (the shared one: not a personal ✓)", not items[typo].replies[0].verified)

        res = texloom(home, "comments", "resolve", str(d), "no-such-id")
        check("an unknown id is refused", res.returncode != 0 and "no comment with id" in res.stderr, res.stderr)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.terminate()
        server.wait(10)
        testdb.stop(tmp / "db")
        if ok and not os.environ.get("KEEP"):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"server log, folders and database: {tmp}")
    print("comments in a folder: " + ("all checks passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
