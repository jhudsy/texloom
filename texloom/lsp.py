"""A language server for the Review tab's comments (`texloom lsp`), so a synced folder's comments
show in the editor beside the text they're about: Neovim, Emacs, Helix, Zed, Sublime Text, Kate,
and VS Code through its extension (editors/vscode).

Over stdio, as the other language servers are, written without a framework (like texloom/mcp.py).
It keeps a copy of the project of its own, as `texloom mcp` and `texloom comments` do, so new
comments arrive while it runs and it works beside the app. What it offers:

- each open comment or suggestion as a diagnostic on its text (Warning: mentions you; Information:
  a comment; Hint: a suggested change), with the author (✓: a personal link vouches for the name);
- hover: the whole thread;
- code actions: Resolve, and Open in the browser's Review tab;
- commands: texloom.resolve [id], texloom.reply [id, text] (an editor that can ask for text, such
  as the VS Code extension), texloom.openReview [];
- for the VS Code extension: the request texloom/comments {uri} (a file's comments with their
  ranges and threads) and the notification texloom/changed.

Where a buffer has edits that aren't saved, a comment follows its quoted text in the buffer.
Positions are UTF-16, as LSP says by default.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from .cli import DEFAULT_CONFIG, browser_link, config_path, load, load_config
from .comments import Comment, read, reply, resolve
from .listsync import my_name
from .replica import Replica
from .textpos import length_u16

log = logging.getLogger("texloom")

POLL = 2  # seconds between looks for new comments
SOURCE = "texloom"
COMMANDS = ["texloom.resolve", "texloom.reply", "texloom.openReview"]
ERROR, WARNING, INFORMATION, HINT = 1, 2, 3, 4


def folder_of(start: Path) -> Path | None:
    """The synced folder that holds `start`, or that it is in."""
    start = start.resolve()
    for p in (start, *start.parents):
        if config_path(p).exists():
            return p
    return None


def path_of(uri: str) -> Path | None:
    u = urlparse(uri)
    return Path(url2pathname(u.path)) if u.scheme == "file" else None


def position(text: str, i: int) -> dict:
    """A character offset as an LSP position (line, UTF-16 column)."""
    i = max(0, min(i, len(text)))
    line = text.count("\n", 0, i)
    return {"line": line, "character": length_u16(text[text.rfind("\n", 0, i) + 1:i])}


def offset(text: str, pos: dict) -> int:
    """An LSP position as a character offset (clamped to the line)."""
    at = 0
    for _ in range(int(pos.get("line", 0))):
        nl = text.find("\n", at)
        if nl < 0:
            return len(text)
        at = nl + 1
    end = text.find("\n", at)
    end = len(text) if end < 0 else end
    units = 0
    for k in range(at, end):
        if units >= pos.get("character", 0):
            return k
        units += 2 if ord(text[k]) > 0xFFFF else 1
    return end


def who(name: str, verified: bool) -> str:
    return f"{name} ✓" if verified else name


class Lsp:
    def __init__(self, folder: Path | None = None):
        self.folder = folder
        self.replica: Replica | None = None
        self.name = ""
        self.docs: dict[str, str] = {}  # open buffers by uri
        self.seen: object = None
        self.tasks: list[asyncio.Task] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.next_id = 1
        self.waiting: dict[int, asyncio.Future] = {}
        self.stop = asyncio.Event()

    # --- wire
    def send(self, msg: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False).encode()
        out = sys.stdout.buffer
        out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        out.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    async def ask(self, method: str, params: dict) -> object:
        """A request to the editor, and its answer."""
        n, self.next_id = self.next_id, self.next_id + 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiting[n] = fut
        self.send({"id": n, "method": method, "params": params})
        return await asyncio.wait_for(fut, 30)

    def message(self, text: str, kind: int = 3) -> None:
        self.notify("window/showMessage", {"type": kind, "message": text})

    # --- the project
    def start(self) -> None:
        if self.replica or not self.folder:
            return
        keys, server = load(self.folder)
        cfg = {**DEFAULT_CONFIG, **load_config(self.folder)}
        self.name = cfg.get("name") or my_name()[0] or getpass.getuser()
        state = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "texloom" / "lsp" / keys.project
        self.replica = Replica(state, keys, server, cfg["create_code"] or None)
        self.tasks += [asyncio.create_task(self.replica.run()), asyncio.create_task(self.watch())]

    async def watch(self) -> None:
        """Tell the editor when the project's comments change."""
        assert self.replica
        await self.replica.synced.wait()
        while True:
            try:
                key = [(c.id, c.resolved, len(c.replies), c.span, c.text) for c in read(self.replica)]
                if key != self.seen:
                    self.seen = key
                    self.publish_all()
                    self.notify("texloom/changed", {})
            except Exception:  # noqa: BLE001 - a bad item mustn't stop the rest
                log.exception("comments not read")
            await asyncio.sleep(POLL)

    def rel(self, uri: str) -> str | None:
        """A file's path in the project."""
        p = path_of(uri)
        if p is None or not self.folder:
            return None
        try:
            return p.resolve().relative_to(self.folder.resolve()).as_posix()
        except ValueError:
            return None

    def uri_of(self, rel: str) -> str:
        assert self.folder
        return (self.folder / rel).resolve().as_uri()

    def buffer(self, uri: str, rel: str) -> str | None:
        """What the editor shows, else what the project has."""
        if uri in self.docs:
            return self.docs[uri]
        if self.replica and rel in self.replica.files:
            return str(self.replica.files[rel])
        return None

    def place(self, c: Comment, text: str | None) -> tuple[int, int]:
        """Where a comment is in `text`: the project's range, or its quote if the buffer has other text."""
        if text is None:
            return c.span or (0, 0)
        proj = str(self.replica.files[c.file]) if self.replica and c.file in self.replica.files else None
        if c.span and text == proj:
            return c.span
        if c.quote and c.quote in text:  # unsaved edits: the quote nearest where it was
            at = c.span[0] if c.span else 0
            best, i = None, text.find(c.quote)
            while i >= 0:
                if best is None or abs(i - at) < abs(best - at):
                    best = i
                i = text.find(c.quote, i + 1)
            assert best is not None
            return best, best + len(c.quote)
        a, b = c.span or (0, 0)
        return min(a, len(text)), min(b, len(text))

    def comments_in(self, rel: str) -> list[Comment]:
        return [c for c in read(self.replica) if c.file == rel] if self.replica else []

    def range_of(self, c: Comment, text: str | None) -> dict:
        a, b = self.place(c, text)
        t = text or ""
        return {"start": position(t, a), "end": position(t, b)}

    def diagnostic(self, c: Comment, text: str | None) -> dict:
        mine = c.mentions(self.name)
        first = (c.text.strip().splitlines() or [""])[0][:160]
        if c.kind == "suggestion":
            msg = f"{who(c.by, c.verified)} suggests: " + (f"“{c.quote[:60]}” → “{first[:60]}”" if c.quote else f"insert “{first[:60]}”")
        else:
            msg = f"{who(c.by, c.verified)}: {first}"
        if c.replies:
            msg += f" (+{len(c.replies)} {'reply' if len(c.replies) == 1 else 'replies'})"
        if mine:
            msg = f"Mentions you · {msg}"
        if not c.span:
            msg += " (the text it was on has changed)"
        sev = WARNING if mine else HINT if c.kind == "suggestion" else INFORMATION
        return {"range": self.range_of(c, text), "severity": sev, "source": SOURCE, "code": c.id, "message": msg, "data": {"id": c.id}}

    def publish(self, uri: str) -> None:
        rel = self.rel(uri)
        if rel is None:
            return
        text = self.buffer(uri, rel)
        self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": [self.diagnostic(c, text) for c in self.comments_in(rel)]})

    def publish_all(self) -> None:
        for uri in list(self.docs):
            self.publish(uri)

    def thread(self, c: Comment) -> str:
        head = f"**{who(c.by, c.verified)}** · {'suggested change' if c.kind == 'suggestion' else 'comment'}"
        if c.kind == "suggestion":
            body = (f"Replace `{c.quote}` with `{c.text}`" if c.quote else f"Insert `{c.text}`")
        else:
            body = c.text
        replies = "".join(f"\n\n---\n**{who(rp.by, rp.verified)}**: {rp.text}" for rp in c.replies)
        return f"{head}\n\n{body}{replies}"

    def describe(self, c: Comment, text: str | None) -> dict:
        return {"id": c.id, "kind": c.kind, "by": c.by, "verified": c.verified, "at": c.at, "text": c.text, "quote": c.quote,
                "mentionsMe": c.mentions(self.name), "range": self.range_of(c, text), "placed": c.span is not None,
                "replies": [{"by": rp.by, "verified": rp.verified, "at": rp.at, "text": rp.text} for rp in c.replies]}

    # --- requests
    async def settle(self) -> None:
        """Wait until the server has what we changed."""
        assert self.replica
        for _ in range(100):
            if not self.replica.outbox:
                return
            await asyncio.sleep(0.1)

    async def command(self, name: str, args: list) -> object:
        r = self.replica
        if name == "texloom.openReview":
            if not self.folder:
                return None
            await self.ask("window/showDocument", {"uri": browser_link(self.folder, "tab=review"), "external": True})
            return None
        if not r:
            raise ValueError("this folder isn't a TeXLoom folder")
        await asyncio.wait_for(r.synced.wait(), 30)
        if not r.can_write:
            raise ValueError("this folder has a read-only link: it can't reply or resolve")
        try:
            if name == "texloom.resolve":
                resolve(r, str(args[0]))
            elif name == "texloom.reply":
                reply(r, str(args[0]), str(args[1]), self.name)
            else:
                raise ValueError(f"unknown command {name}")
        except KeyError as e:
            raise ValueError(f"no comment with id {e.args[0]}") from None
        self.seen = None
        await self.settle()
        return None

    def actions(self, uri: str, rng: dict) -> list[dict]:
        rel = self.rel(uri)
        if rel is None or not self.replica:
            return []
        text = self.buffer(uri, rel) or ""
        lo, hi = offset(text, rng["start"]), offset(text, rng["end"])
        out: list[dict] = []
        for c in self.comments_in(rel):
            a, b = self.place(c, text)
            if not (a <= hi and lo <= max(b, a)):
                continue
            diag = self.diagnostic(c, text)
            if c.kind == "comment" and self.replica.can_write:
                out.append({"title": f"TeXLoom: resolve {c.by}’s comment", "kind": "quickfix", "diagnostics": [diag],
                            "command": {"title": "Resolve", "command": "texloom.resolve", "arguments": [c.id]}})
            out.append({"title": "TeXLoom: open the Review tab in the browser", "kind": "quickfix", "diagnostics": [diag],
                        "command": {"title": "Open", "command": "texloom.openReview", "arguments": []}})
        return out

    def hover(self, uri: str, pos: dict) -> dict | None:
        rel = self.rel(uri)
        if rel is None or not self.replica:
            return None
        text = self.buffer(uri, rel) or ""
        at = offset(text, pos)
        found = [c for c in self.comments_in(rel) if (p := self.place(c, text))[0] <= at <= max(p[1], p[0])]
        if not found:
            return None
        return {"contents": {"kind": "markdown", "value": "\n\n===\n\n".join(self.thread(c) for c in found)}}

    async def request(self, method: str, p: dict) -> object:
        if method == "initialize":
            if not self.folder:
                root = (p.get("workspaceFolders") or [{}])[0].get("uri") or p.get("rootUri")
                if root and (rp := path_of(root)):
                    self.folder = folder_of(rp)
            self.start()
            return {"capabilities": {"textDocumentSync": {"openClose": True, "change": 1},
                                     "hoverProvider": True, "codeActionProvider": {"codeActionKinds": ["quickfix"]},
                                     "executeCommandProvider": {"commands": COMMANDS}},
                    "serverInfo": {"name": "texloom"}}
        if method == "shutdown":
            return None
        if method == "textDocument/hover":
            return self.hover(p["textDocument"]["uri"], p["position"])
        if method == "textDocument/codeAction":
            return self.actions(p["textDocument"]["uri"], p["range"])
        if method == "workspace/executeCommand":
            return await self.command(p["command"], p.get("arguments") or [])
        if method == "texloom/comments":
            uri = p["textDocument"]["uri"] if "textDocument" in p else p["uri"]
            rel = self.rel(uri)
            if rel is None:
                return {"comments": [], "name": self.name, "canWrite": False}
            text = self.buffer(uri, rel)
            return {"comments": [self.describe(c, text) for c in self.comments_in(rel)], "name": self.name,
                    "canWrite": bool(self.replica and self.replica.can_write)}
        raise LookupError(method)

    async def notification(self, method: str, p: dict) -> None:
        if method == "textDocument/didOpen":
            d = p["textDocument"]
            self.docs[d["uri"]] = d["text"]
            self.publish(d["uri"])
        elif method == "textDocument/didChange":
            uri = p["textDocument"]["uri"]
            if p.get("contentChanges"):
                self.docs[uri] = p["contentChanges"][-1]["text"]  # (whole text: sync kind 1)
                self.publish(uri)
        elif method == "textDocument/didClose":
            self.docs.pop(p["textDocument"]["uri"], None)
            self.notify("textDocument/publishDiagnostics", {"uri": p["textDocument"]["uri"], "diagnostics": []})
        elif method == "exit":
            self.stop.set()

    async def handle(self, m: dict) -> None:
        method, mid = m.get("method"), m.get("id")
        if method is None:  # an answer to one of ours
            fut = self.waiting.pop(mid, None) if isinstance(mid, int) else None
            if fut and not fut.done():
                fut.set_result(m.get("result")) if "error" not in m else fut.set_exception(RuntimeError(str(m["error"])))
            return
        params = m.get("params") or {}
        if mid is None:
            try:
                await self.notification(method, params)
            except Exception:  # noqa: BLE001
                log.exception("%s", method)
            return
        try:
            self.send({"id": mid, "result": await self.request(method, params)})
        except LookupError:
            self.send({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        except ValueError as e:  # (something to tell the person)
            self.message(str(e), 1)
            self.send({"id": mid, "error": {"code": -32803, "message": str(e)}})
        except Exception as e:  # noqa: BLE001
            log.exception("%s", method)
            self.send({"id": mid, "error": {"code": -32603, "message": str(e)}})

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()

        def read_stdin() -> None:  # a thread, as a pipe can't be watched on Windows
            inp = sys.stdin.buffer
            try:
                while True:
                    length = 0
                    while line := inp.readline():
                        line = line.strip()
                        if not line:
                            break
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1])
                    else:
                        return
                    if length:
                        loop.call_soon_threadsafe(self.queue.put_nowait, json.loads(inp.read(length)))
            except (ValueError, OSError):
                pass
            finally:
                loop.call_soon_threadsafe(self.stop.set)
        threading.Thread(target=read_stdin, name="lsp-stdin", daemon=True).start()
        pending: set[asyncio.Task] = set()
        try:
            while not self.stop.is_set():
                got = asyncio.ensure_future(self.queue.get())
                done, _ = await asyncio.wait({got, asyncio.ensure_future(self.stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
                if got not in done:
                    got.cancel()
                    break
                t = asyncio.create_task(self.handle(got.result()))  # (a command may wait for the server: the rest goes on)
                pending.add(t)
                t.add_done_callback(pending.discard)
        finally:
            if self.replica:
                await self.settle()
                self.replica.save()
            for t in self.tasks:
                t.cancel()


async def serve(folder: Path | None) -> None:
    await Lsp(folder_of(folder) if folder else None).serve()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # (the thread reading stdin is blocked in it: leaving the usual way aborts)
