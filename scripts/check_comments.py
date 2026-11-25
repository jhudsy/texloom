#!/usr/bin/env python3
"""Comments for people working in a synced folder, against a real server.

A collaborator (the desktop's sync engine standing in for the web app) comments on a project, and
a folder synced for "Dana" gets them, two ways:

- the folder's .texloom/comments.md overview, after one pass of the folder sync (`texloom.cli.run(..., once=True)`): the line each comment is
  on, the ones that mention Dana marked; `texloom comments DIR` lists them, and `reply` and
  `resolve` reach the collaborator, signed with the folder's link;
- the comments language server (`texloom lsp`), opened like an editor would with a real stdio
  client: comments arrive as diagnostics (the one that mentions Dana as a warning), hover shows the
  thread, code actions resolve, reply and resolve reach the collaborator, unsaved edits move the
  ranges, and comments made in the editor arrive.

    uv run --extra server python scripts/check_comments.py
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import PY, ROOT, background, check, home_env, server, summary, texloom, until  # noqa: E402

from pycrdt import Text  # noqa: E402

from texloom.comments import read  # noqa: E402
from texloom.crypto import Keys  # noqa: E402
from texloom.mcp import Project  # noqa: E402
from texloom.replica import Replica  # noqa: E402

MAIN = "\\documentclass{article}\n\\begin{document}\nFirst line.\nSecond line, with a typo.\nThird line.\n\\end{document}\n"
# Dana's name in a folder's settings
AS_DANA = ("import json, sys; from pathlib import Path; from texloom.cli import config_path; d = Path(sys.argv[1]); "
           "c = json.loads(config_path(d).read_text()); c['name'] = 'Dana'; config_path(d).write_text(json.dumps(c))")


async def commented(url: str, tmp: Path, tasks: list) -> tuple[Replica, str, str, Path, Path]:
    """A project the collaborator (Alice) has commented on twice, the first mentioning @Dana, and
    a folder of it on "Dana's" computer. Returns the collaborator's copy, the two comments' ids,
    and Dana's TEXLOOM_HOME and folder."""
    tmp.mkdir(parents=True)
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

    home, d = tmp / "home", tmp / "folder"
    res = texloom(home, "clone", keys.link(url), str(d))
    check("the folder is cloned", res.returncode == 0 and (d / "main.tex").exists(), res.stderr[-800:])
    subprocess.run(PY + [AS_DANA, str(d)], cwd=ROOT, env=home_env(home), check=True)
    return web, typo, other, home, d


async def overview(url: str, tmp: Path, tasks: list) -> None:
    """The folder's comments overview, and `texloom comments`."""
    web, typo, other, home, d = await commented(url, tmp, tasks)
    once = "import asyncio, sys; from pathlib import Path; from texloom.cli import run; asyncio.run(run(Path(sys.argv[1]), False, once=True))"
    res = subprocess.run(PY + [once, str(d)], cwd=ROOT, env=home_env(home), capture_output=True, text=True, timeout=120)
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

    got = await until(lambda: any(rp.text == "Fixed, thanks" for c in read(web, True) if c.id == typo for rp in c.replies)
                      and any(c.resolved for c in read(web, True) if c.id == other))
    items = {c.id: c for c in read(web, True)}
    check("the collaborator gets the reply, as from Dana", got and items[typo].replies and items[typo].replies[0].by == "Dana",
          [(c.id, c.replies, c.resolved) for c in items.values()])
    check("and the comment resolved", items[other].resolved)
    check("the reply is signed with the folder's link (the shared one: not a personal ✓)", not items[typo].replies[0].verified)

    res = texloom(home, "comments", "resolve", str(d), "no-such-id")
    check("an unknown id is refused", res.returncode != 0 and "no comment with id" in res.stderr, res.stderr)


class Client:
    """The editor's side of a language server's stdio."""
    def __init__(self, proc: subprocess.Popen):
        self.proc, self.n, self.notes, self.waiting = proc, 0, [], {}
        self.reader = asyncio.get_running_loop().run_in_executor(None, self._read)
        self.loop = asyncio.get_running_loop()

    def _read(self) -> None:
        out = self.proc.stdout
        while True:
            length = 0
            while line := out.readline():
                if not line.strip():
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            else:
                return
            msg = json.loads(out.read(length))
            self.loop.call_soon_threadsafe(self._got, msg)

    def _got(self, m: dict) -> None:
        if "method" in m and "id" in m:  # a request to us (showDocument)
            self.notes.append(m)
            self.write({"id": m["id"], "result": {"success": True}})
        elif "method" in m:
            self.notes.append(m)
        elif m.get("id") in self.waiting:
            self.waiting.pop(m["id"]).set_result(m)

    def write(self, m: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **m}).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        fut = self.loop.create_future()
        self.waiting[self.n] = fut
        self.write({"id": self.n, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, 30)

    def tell(self, method: str, params: dict) -> None:
        self.write({"method": method, "params": params})

    def diagnostics(self, uri: str) -> list | None:
        last = [n for n in self.notes if n.get("method") == "textDocument/publishDiagnostics" and n["params"]["uri"] == uri]
        return last[-1]["params"]["diagnostics"] if last else None


async def language_server(url: str, tmp: Path, tasks: list) -> None:
    """The comments language server, `texloom lsp`, with a stdio client."""
    web, typo, other, home, d = await commented(url, tmp, tasks)
    env = home_env(home)
    lsp = None
    try:
        lsp = subprocess.Popen([sys.executable, "-m", "texloom.cli", "lsp"], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=open(tmp / "lsp.log", "w"))  # noqa: SIM115
        c = Client(lsp)
        res = await c.call("initialize", {"processId": None, "rootUri": d.as_uri(), "capabilities": {}})
        caps = res["result"]["capabilities"]
        check("it starts, with hover, code actions and commands", caps.get("hoverProvider") and "texloom.resolve" in caps["executeCommandProvider"]["commands"], res)
        c.tell("initialized", {})
        uri = (d / "main.tex").as_uri()
        c.tell("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "latex", "version": 1, "text": MAIN}})
        got = await until(lambda: len(c.diagnostics(uri) or []) == 2, 30)
        diags = c.diagnostics(uri) or []
        check("each open comment is a diagnostic", got, diags)
        by_id = {x["code"]: x for x in diags}
        mine, fine = by_id.get(typo, {}), by_id.get(other, {})
        check("on the text it's about", mine.get("range", {}).get("start") == {"line": 3, "character": MAIN.splitlines()[3].index("typo")}, mine)
        check("the one that mentions Dana is a warning, saying so", mine.get("severity") == 2 and "Mentions you" in mine.get("message", ""), mine)
        check("the other is information, with its author", fine.get("severity") == 3 and "Alice: Fine as it is." in fine.get("message", ""), fine)

        res = await c.call("textDocument/hover", {"textDocument": {"uri": uri}, "position": mine["range"]["start"]})
        check("hover shows the thread", "@Dana please fix this typo" in json.dumps(res.get("result")), res)
        res = await c.call("textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 3}})
        check("and nothing where there is no comment", res.get("result") is None, res)
        res = await c.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": mine["range"], "context": {"diagnostics": []}})
        titles = [a["title"] for a in res["result"]]
        check("code actions: resolve, and open the Review tab", any("resolve" in t for t in titles) and any("Review tab" in t for t in titles), titles)

        res = await c.call("workspace/executeCommand", {"command": "texloom.reply", "arguments": [typo, "Fixed, thanks"]})
        ok_reply = await until(lambda: any(rp.text == "Fixed, thanks" and rp.by == "Dana" for x in read(web, True) if x.id == typo for rp in x.replies), 20)
        check("reply reaches the collaborator, as Dana", "error" not in res and ok_reply, res)
        res = await c.call("workspace/executeCommand", {"command": "texloom.resolve", "arguments": [other]})
        ok_res = await until(lambda: any(x.resolved for x in read(web, True) if x.id == other), 20)
        check("resolve reaches the collaborator", "error" not in res and ok_res, res)
        gone = await until(lambda: [x["code"] for x in c.diagnostics(uri) or []] == [typo], 15)
        check("the resolved comment leaves the diagnostics, and the reply is counted", gone and "(+1 reply)" in (c.diagnostics(uri) or [{}])[0].get("message", ""), c.diagnostics(uri))

        res = await c.call("texloom/comments", {"textDocument": {"uri": uri}})
        cs = res["result"]["comments"]
        check("texloom/comments gives the thread for an extension", len(cs) == 1 and cs[0]["replies"][0]["text"] == "Fixed, thanks" and cs[0]["mentionsMe"], res)

        # unsaved edits above the comment: the range follows its text in the buffer
        edited = "% a new first line\n" + MAIN
        c.tell("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": edited}]})
        moved = await until(lambda: (c.diagnostics(uri) or [{}])[0].get("range", {}).get("start", {}).get("line") == 4, 10)
        check("with unsaved edits the comment follows its text", moved, c.diagnostics(uri))

        # a new comment from the editor, on text chosen in the buffer (which has an unsaved line above it)
        line4 = edited.splitlines()[4]
        res = await c.call("texloom/comments", {"textDocument": {"uri": uri}})
        check("texloom/comments says the file can take comments", res["result"].get("canComment") is True, res)
        sel = {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": len("Second line")}}
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [uri, sel, "  Is this clear?  "]})
        new_id = (res.get("result") or {}).get("id")
        def found(iid):
            return next((x for x in read(web, True) if x.id == iid), None)
        arrived = await until(lambda: found(new_id) is not None, 20)
        x = found(new_id)
        check("a comment from the editor reaches the collaborator, as Dana, on the selected text", arrived and x and x.by == "Dana" and x.quote == "Second line"
              and x.text == "Is this clear?" and x.span == (MAIN.index("Second line"), MAIN.index("Second line") + 11), (res, x))
        shown_new = await until(lambda: new_id in [d["code"] for d in c.diagnostics(uri) or []], 15)
        check("and comes back to the editor as a comment there", shown_new, c.diagnostics(uri))
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [uri, {"start": {"line": 5, "character": 2}, "end": {"line": 5, "character": 2}}, "Whole line"]})
        await until(lambda: found((res.get("result") or {}).get("id")) is not None, 20)
        x = found((res.get("result") or {}).get("id"))
        check("with nothing selected, it's on the line's text", x is not None and x.quote == "Third line.", (res, x))
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [uri, {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 18}}, "On unsaved text"]})
        check("text that isn't in the synced file is refused, saying why", "error" in res and "isn't in the synced file" in res["error"].get("message", ""), res)
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [uri, sel, "   "]})
        check("an empty comment is refused", "error" in res, res)
        assert line4.startswith("Second line")

        # non-ASCII text: positions are UTF-16, and the comment is placed by its quote
        NOTES = "Café 😀 résumé — naïve.\n"
        web.local_change(lambda: web.files.__setitem__("notes.tex", Text(NOTES)))
        nuri = (d / "notes.tex").as_uri()
        c.tell("textDocument/didOpen", {"textDocument": {"uri": nuri, "languageId": "latex", "version": 1, "text": NOTES}})
        async def can_comment() -> bool:
            return (await c.call("texloom/comments", {"textDocument": {"uri": nuri}}))["result"].get("canComment") is True
        for _ in range(100):
            if await can_comment():
                break
            await asyncio.sleep(0.2)
        start16 = len("Café 😀 ".encode("utf-16-le")) // 2
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [nuri, {"start": {"line": 0, "character": start16}, "end": {"line": 0, "character": start16 + 6}}, "Accents?"]})
        nid = (res.get("result") or {}).get("id")
        await until(lambda: found(nid) is not None, 20)
        x = found(nid)
        check("in non-ASCII text the comment is on the right characters", x is not None and x.quote == "résumé" and x.span == (NOTES.index("résumé"), NOTES.index("résumé") + 6), (res, x))
        res = await c.call("texloom/comments", {"textDocument": {"uri": nuri}})
        r0 = next((y["range"] for y in res["result"]["comments"] if y["id"] == nid), None)
        check("and its range in the editor is in UTF-16", r0 == {"start": {"line": 0, "character": start16}, "end": {"line": 0, "character": start16 + 6}}, res)

        res = await c.call("workspace/executeCommand", {"command": "texloom.openReview", "arguments": []})
        shown = [n for n in c.notes if n.get("method") == "window/showDocument"]
        check("open in the browser asks the editor to show the Review tab's link", shown and "tab=review" in shown[-1]["params"]["uri"] and "#" in shown[-1]["params"]["uri"], c.notes[-3:])

        res = await c.call("workspace/executeCommand", {"command": "texloom.resolve", "arguments": ["no-such-id"]})
        check("an unknown id is an error, said to the person", "error" in res and any(n.get("method") == "window/showMessage" for n in c.notes), res)

        await c.call("shutdown")
        c.tell("exit", {})
        code = await asyncio.get_running_loop().run_in_executor(None, lambda: lsp.wait(20))
        check("it ends when told to", code == 0, code)
    finally:
        if lsp and lsp.poll() is None:
            lsp.kill()


async def main() -> int:
    with server("comments") as srv:
        async with background() as tasks:
            print("A folder's comments overview, and texloom comments:", flush=True)
            await overview(srv.url, srv.tmp / "overview", tasks)
            print("The comments language server (texloom lsp):", flush=True)
            await language_server(srv.url, srv.tmp / "lsp", tasks)
    return summary("comments in a folder")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
