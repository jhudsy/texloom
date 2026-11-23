#!/usr/bin/env python3
"""The comments language server (`texloom lsp`), against a real server and a real stdio client.

A collaborator (the desktop's sync engine standing in for the web app) comments on a project; a
folder synced for "Dana" is opened in the server like an editor would: comments arrive as
diagnostics (the one that mentions Dana as a warning), hover shows the thread, code actions resolve,
reply and resolve reach the collaborator, and unsaved edits move the ranges.

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


async def until(cond, secs: float = 20) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False



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


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="texloom-lsp-"))
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    database = testdb.start(tmp / "db")
    server = subprocess.Popen([sys.executable, "-c", SERVER, "--port", str(port), "--database", database, "--data", str(tmp / "relay")],
                              cwd=ROOT, stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
    tasks: list[asyncio.Task] = []
    lsp = None
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

        home, d = tmp / "home", tmp / "folder"
        env = {**os.environ, "TEXLOOM_HOME": str(home), "XDG_CACHE_HOME": str(home / "cache"), "PYTHONIOENCODING": "utf-8"}
        res = subprocess.run([sys.executable, "-m", "texloom.cli", "clone", keys.link(url), str(d)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        check("the folder is cloned", res.returncode == 0 and (d / "main.tex").exists(), res.stderr[-800:])
        setname = ("import json, sys; from pathlib import Path; from texloom.cli import config_path; d = Path(sys.argv[1]); "
                   "c = json.loads(config_path(d).read_text()); c['name'] = 'Dana'; config_path(d).write_text(json.dumps(c))")
        subprocess.run([sys.executable, "-c", setname, str(d)], cwd=ROOT, env=env, check=True)

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
        from texloom.comments import read
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
        from texloom.comments import read as read_all
        line4 = edited.splitlines()[4]
        res = await c.call("texloom/comments", {"textDocument": {"uri": uri}})
        check("texloom/comments says the file can take comments", res["result"].get("canComment") is True, res)
        sel = {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": len("Second line")}}
        res = await c.call("workspace/executeCommand", {"command": "texloom.addComment", "arguments": [uri, sel, "  Is this clear?  "]})
        new_id = (res.get("result") or {}).get("id")
        def found(iid):
            return next((x for x in read_all(web, True) if x.id == iid), None)
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
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.terminate()
        server.wait(10)
        testdb.stop(tmp / "db")
        if ok and not os.environ.get("KEEP"):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"logs, folders and database: {tmp}")
    print("comments language server: " + ("all checks passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
