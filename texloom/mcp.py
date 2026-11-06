"""MCP server: lets an agent (Claude Code, Claude Desktop, any MCP client) work on a
TeXLoom project directly, as a visible collaborator.

    texloom mcp <folder>        # a folder set up with `texloom new` / `texloom clone`
    texloom mcp '<link>'        # or straight from a project link

The agent keeps its own replica (state under ~/.cache/texloom/mcp/<project>), shows up in
collaborators' editors with its own name and cursor, and can edit files directly or
leave comments and suggested changes for people to accept or reject.

Speaks JSON-RPC 2.0 over stdio (newline-delimited), per the MCP specification. Logs go
to stderr; stdout carries only protocol messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

from pycrdt import Array, Assoc, Map, Text

from . import __version__
from .crypto import Keys, b64e
from .folder import apply_text, safe_rel
from .replica import Replica

log = logging.getLogger("texloom")

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
COLOR = "#d97757"


# --- awareness (presence) -------------------------------------------------------------
def _varuint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def awareness_update(client: int, clock: int, state: dict | None) -> bytes:
    """A y-protocols awareness update for one client (state None = gone)."""
    s = json.dumps(state, separators=(",", ":")).encode()
    return _varuint(1) + _varuint(client) + _varuint(clock) + _varuint(len(s)) + s


def u16(s: str, i: int) -> int:
    """Python string index -> UTF-16 offset (what Yjs and sticky indices use)."""
    return len(s[:i].encode("utf-16-le")) // 2


class Project:
    def __init__(self, replica: Replica, name: str):
        self.r = replica
        self.name = name
        self._aw_clock = 0
        self.where: dict | None = None  # {"file", "cursor"} shown to collaborators
        self.review: Map = replica.doc.get("review", type=Map)
        self.versions: Array = replica.doc.get("versions", type=Array)

    # --- presence ----------------------------------------------------------------
    async def announce(self, gone: bool = False) -> None:
        ws = self.r.ws
        if ws is None or ws.closed:
            return
        self._aw_clock += 1
        state = None if gone else {
            "user": {"name": self.name, "color": COLOR, "colorLight": COLOR + "33", "agent": True},
            **(self.where or {}),
        }
        upd = awareness_update(self.r.doc.client_id, self._aw_clock, state)
        try:
            await ws.send_json({"t": "aw", "d": b64e(self.r.keys.encrypt(upd))})
        except ConnectionError:
            pass

    async def presence_loop(self) -> None:
        while True:  # peers drop states not renewed within 30 s
            await self.r.connected.wait()
            await self.announce()
            await asyncio.sleep(12)

    def point_at(self, path: str, start: int, end: int | None = None) -> None:
        """Show the agent's cursor at [start, end) (Python string indices) in `path`."""
        yt = self.r.files.get(path)
        if yt is None:
            return
        s = str(yt)
        start = max(0, min(start, len(s)))
        end = start if end is None else max(start, min(end, len(s)))
        try:
            a = self._sticky(yt, s, start, start == len(s))
            h = self._sticky(yt, s, end, end == len(s) or end > start)
            self.where = {"file": path, "cursor": {"anchor": a.to_json(), "head": h.to_json()}}
        except Exception:  # noqa: BLE001 - presence is best-effort
            self.where = {"file": path}
        asyncio.get_event_loop().create_task(self.announce())

    @staticmethod
    def _sticky(yt: Text, s: str, i: int, before: bool):
        if before and i > 0:
            return yt.sticky_index(u16(s, i), Assoc.BEFORE)
        return yt.sticky_index(u16(s, i), Assoc.AFTER)

    # --- helpers -------------------------------------------------------------------
    def text(self, path: str) -> str:
        yt = self.r.files.get(path)
        if yt is None:
            if path in self.r.bin:
                raise ValueError(f"{path} is a binary file (image/PDF); it can't be read as text.")
            raise ValueError(f"No such file: {path}. Use list_files.")
        return str(yt)

    def main_file(self) -> str | None:
        m = self.r.meta.get("main")
        if m and m in self.r.files:
            return str(m)
        for p in sorted((p for p in self.r.files.keys() if p.endswith(".tex")), key=lambda p: (p != "main.tex", p.count("/"), p)):
            if "\\documentclass" in str(self.r.files[p]):
                return p
        return None

    def find_unique(self, path: str, old: str) -> int:
        s = self.text(path)
        if not old:
            raise ValueError("old_string must not be empty.")
        n = s.count(old)
        if n == 0:
            raise ValueError(f"old_string not found in {path}. Read the file again: it may have changed.")
        if n > 1:
            raise ValueError(f"old_string occurs {n} times in {path}; include more surrounding text to make it unique.")
        return s.index(old)

    def edit(self, fn) -> None:
        if not self.r.keys.writable:
            raise PermissionError("This is a read-only link: the agent can read and comment only with an edit link.")
        self.r.local_change(fn)

    def line_of(self, s: str, i: int) -> int:
        return s.count("\n", 0, i) + 1

    # --- tools -----------------------------------------------------------------------
    def list_files(self) -> str:
        main = self.main_file()
        rows = []
        for p in sorted(self.r.files.keys()):
            t = str(self.r.files[p])
            rows.append(f"{p}  ({t.count(chr(10)) + 1} lines){'  [main]' if p == main else ''}")
        for p in sorted(self.r.bin.keys()):
            info = self.r.bin[p]
            size = info.get("size", 0) if isinstance(info, dict) else 0
            rows.append(f"{p}  (binary, {size / 1024:.0f} KB)")
        dirs = sorted(d for d in self.r.dirs.keys())
        head = f"Project “{self.r.meta.get('name') or 'Untitled'}” — {'edit' if self.r.keys.writable else 'read-only'} access"
        return "\n".join([head, *rows, *(f"{d}/  (empty folder)" for d in dirs if not any(p.startswith(d + '/') for p in [*self.r.files.keys(), *self.r.bin.keys()]))])

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        s = self.text(path)
        lines = s.split("\n")
        a = max(1, start_line or 1)
        b = min(len(lines), end_line or len(lines))
        offset = sum(len(x) + 1 for x in lines[: a - 1])
        self.point_at(path, offset)
        width = len(str(b))
        return "\n".join(f"{str(i).rjust(width)}\t{lines[i - 1]}" for i in range(a, b + 1))

    def write_file(self, path: str, content: str) -> str:
        p = safe_rel(path)
        if not p:
            raise ValueError(f"Unsafe or invalid path: {path!r}")
        existed = p in self.r.files

        def fn():
            if p in self.r.bin:
                del self.r.bin[p]
            yt = self.r.files.get(p)
            if yt is None:
                self.r.files[p] = Text(content)
            else:
                apply_text(yt, str(yt), content)  # small edits: collaborators' cursors survive
        self.edit(fn)
        self.point_at(p, 0)
        return f"{'Updated' if existed else 'Created'} {p}."

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        s = self.text(path)
        if replace_all:
            if old_string not in s:
                raise ValueError(f"old_string not found in {path}.")
            new = s.replace(old_string, new_string)
            count = s.count(old_string)
        else:
            i = self.find_unique(path, old_string)
            new = s[:i] + new_string + s[i + len(old_string):]
            count = 1
        self.edit(lambda: apply_text(self.r.files[path], str(self.r.files[path]), new))
        at = new.find(new_string) if new_string else 0
        self.point_at(path, max(0, at), max(0, at) + len(new_string))
        return f"Replaced {count} occurrence{'s' if count != 1 else ''} in {path}."

    def search(self, pattern: str, regex: bool = False, path: str | None = None, case_sensitive: bool = False) -> str:
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regular expression: {e}")
        out = []
        for p in sorted(self.r.files.keys()):
            if path and p != path and not p.startswith(path.rstrip("/") + "/"):
                continue
            for n, line in enumerate(str(self.r.files[p]).split("\n"), 1):
                if rx.search(line):
                    out.append(f"{p}:{n}: {line[:200]}")
                    if len(out) >= 300:
                        return "\n".join(out + ["… (more matches; narrow the search)"])
        return "\n".join(out) or "No matches."

    def _add_item(self, kind: str, path: str, start: int, end: int, text: str) -> str:
        if not self.r.keys.writable:
            raise PermissionError("Comments and suggestions need an edit link.")
        yt = self.r.files[path]
        s = str(yt)
        iid = f"{secrets.token_hex(4)}-{secrets.token_hex(2)}-4{secrets.token_hex(2)[1:]}-a{secrets.token_hex(2)[1:]}-{secrets.token_hex(6)}"
        a = self._sticky(yt, s, start, start == len(s))
        b = self._sticky(yt, s, end, True) if end > 0 else self._sticky(yt, s, end, False)
        item = Map({
            "kind": kind, "file": path, "text": text[:20000], "by": self.name, "at": int(time.time() * 1000),
            "quote": s[start:end][:20000], "resolved": False,
            "start": a.encode(), "end": b.encode(), "replies": Array(),
        })
        self.r.local_change(lambda: self.review.__setitem__(iid, item))
        self.point_at(path, start, end)
        return iid

    def suggest_edit(self, path: str, old_string: str, new_string: str, comment: str | None = None) -> str:
        i = self.find_unique(path, old_string)
        iid = self._add_item("suggestion", path, i, i + len(old_string), new_string)
        if comment:
            self.reply(iid, comment)
        return (f"Suggested a change in {path} (line {self.line_of(self.text(path), i)}), id {iid}. "
                "It is shown to collaborators, who can accept or reject it; the file is unchanged until then.")

    def add_comment(self, path: str, quote: str, comment: str) -> str:
        i = self.find_unique(path, quote)
        iid = self._add_item("comment", path, i, i + len(quote), comment)
        return f"Comment added on {path} line {self.line_of(self.text(path), i)}, id {iid}."

    def list_comments(self, include_resolved: bool = False) -> str:
        rows = []
        for iid, m in self.review.items():
            if not isinstance(m, Map):
                continue
            if m.get("resolved") and not include_resolved:
                continue
            path, quote = str(m.get("file", "")), str(m.get("quote", ""))
            s = str(self.r.files[path]) if path in self.r.files else ""
            where = f"{path}:{self.line_of(s, s.index(quote))}" if quote and quote in s else f"{path} (text changed)"
            kind = m.get("kind")
            status = ("accepted" if m.get("outcome") == "accepted" else "rejected" if m.get("outcome") == "rejected" else "resolved") if m.get("resolved") else "open"
            body = f"replace “{quote}” with “{m.get('text')}”" if kind == "suggestion" else f"on “{quote[:200]}”: {m.get('text')}"
            replies = m.get("replies")
            rep = [f"    ↳ {x.get('by')}: {x.get('text')}" for x in (replies.to_py() if isinstance(replies, Array) else []) if isinstance(x, dict)]
            rows.append("\n".join([f"[{iid}] {kind} by {m.get('by')} at {where} ({status}): {body}", *rep]))
        return "\n\n".join(rows) or "No open comments or suggestions."

    def reply(self, id: str, text: str) -> str:
        m = self.review.get(id)
        if not isinstance(m, Map):
            raise ValueError(f"No comment with id {id}.")
        replies = m.get("replies")
        if not isinstance(replies, Array):
            raise ValueError("That item can't take replies.")
        self.edit(lambda: replies.append({"by": self.name, "at": int(time.time() * 1000), "text": text[:20000]}))
        return "Replied."

    def resolve(self, id: str) -> str:
        m = self.review.get(id)
        if not isinstance(m, Map):
            raise ValueError(f"No comment with id {id}.")
        self.edit(lambda: m.__setitem__("resolved", True))
        return "Resolved."

    async def compile_log(self) -> str:
        o = self.r.out.get("latest")
        if not isinstance(o, dict) or not o.get("log"):
            return "No compile output has been published yet. Use compile (needs latexmk here) or ask a collaborator to compile."
        log_text = (await self.r.get_blob(o["log"])).decode("utf-8", "replace")
        when = time.strftime("%H:%M", time.localtime(float(o.get("at", 0))))
        return f"Latest compile by {o.get('by')} at {when}: {'OK' if o.get('ok') else 'errors'}\n\n" + summarize_log(log_text)

    async def compile(self) -> str:
        if not shutil.which("latexmk"):
            return "latexmk isn't installed on this machine. " + await self.compile_log()
        main = self.main_file()
        if not main:
            return "No main .tex file found."
        with tempfile.TemporaryDirectory(prefix="texloom-mcp-") as tmp:
            root = Path(tmp)
            for p in self.r.files.keys():
                if safe_rel(p):
                    (root / p).parent.mkdir(parents=True, exist_ok=True)
                    (root / p).write_text(str(self.r.files[p]))
            for p, info in self.r.bin.items():
                if safe_rel(p) and isinstance(info, dict) and info.get("h"):
                    try:
                        data = await asyncio.wait_for(self.r.get_blob(info["h"]), 30)
                    except Exception:  # noqa: BLE001
                        continue
                    (root / p).parent.mkdir(parents=True, exist_ok=True)
                    (root / p).write_bytes(data)
            engine = str(self.r.meta.get("engine", "pdflatex"))
            flag = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}.get(engine, "-pdf")
            extra = ["-bibtex-"] if self.r.meta.get("bib") == "none" else []
            mp = root / main
            env = {**os.environ, "openin_any": "p", "openout_any": "p"}
            proc = await asyncio.create_subprocess_exec(
                "latexmk", "-norc", flag, "-interaction=nonstopmode", "-file-line-error", "-no-shell-escape",
                *extra, f"./{mp.name}", cwd=mp.parent, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 300)
            except asyncio.TimeoutError:
                proc.kill()
                return "Compile timed out after 5 minutes."
            logf = mp.with_suffix(".log")
            text = logf.read_text("utf-8", "replace") if logf.exists() else out.decode("utf-8", "replace")
            ok = proc.returncode == 0 and mp.with_suffix(".pdf").exists()
            pages = re.search(r"Output written on .*?\((\d+) pages?", text)
            return (f"{'Compiled OK' if ok else 'Compile failed'}{f' ({pages.group(1)} pages)' if pages else ''}.\n\n"
                    + summarize_log(text))


def summarize_log(text: str) -> str:
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if re.match(r"^(\./)?[^:\s]+\.(tex|sty|cls|bib):\d+: ", line) or line.startswith("! "):
            ctx = " ".join(x.strip() for x in lines[i + 1:i + 3] if x.strip().startswith("l."))
            out.append(f"ERROR {line.strip()}{'  ' + ctx if ctx else ''}")
        elif re.search(r"(LaTeX|Package \S+|Class \S+) Warning:", line):
            out.append("WARNING " + line.strip())
        elif re.match(r"^(Over|Under)full \\[hv]box", line):
            out.append("BOX " + line.strip())
    dedup = list(dict.fromkeys(out))
    if not dedup:
        return "No errors or warnings."
    errors = [x for x in dedup if x.startswith("ERROR")]
    rest = [x for x in dedup if not x.startswith("ERROR")]
    return "\n".join(errors + rest[: max(0, 60 - len(errors))]) + (f"\n… {len(rest) - 60} more" if len(rest) > 60 else "")


# --- tool schemas ----------------------------------------------------------------------
def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
TOOLS = [
    ("list_files", "List the project's files (text files with line counts, binary files with sizes) and which is the main .tex file.", _schema({}, [])),
    ("read_file", "Read a text file, with line numbers. Optionally only lines start_line..end_line.",
     _schema({"path": S, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"])),
    ("write_file", "Create a file or replace its whole content. Collaborators see the change live. Prefer edit_file for changes to existing files.",
     _schema({"path": S, "content": S}, ["path", "content"])),
    ("edit_file", "Replace old_string with new_string in a file, directly (collaborators see it live). old_string must match exactly and be unique unless replace_all. "
     "When the user wants to review changes first, use suggest_edit instead.",
     _schema({"path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"])),
    ("suggest_edit", "Propose replacing old_string with new_string without changing the file: it appears as a tracked change that collaborators accept or reject. "
     "Optionally explain it in comment.",
     _schema({"path": S, "old_string": S, "new_string": S, "comment": S}, ["path", "old_string", "new_string"])),
    ("add_comment", "Leave a comment on a passage (quote must match the file exactly and be unique).",
     _schema({"path": S, "quote": S, "comment": S}, ["path", "quote", "comment"])),
    ("list_comments", "List comments and suggested changes, with ids, locations, status and replies.",
     _schema({"include_resolved": {"type": "boolean"}}, [])),
    ("reply_comment", "Reply to a comment or suggestion by id.", _schema({"id": S, "text": S}, ["id", "text"])),
    ("resolve_comment", "Mark a comment as resolved by id.", _schema({"id": S}, ["id"])),
    ("search", "Search all text files for a string (or a regular expression); returns file:line: text.",
     _schema({"pattern": S, "regex": {"type": "boolean"}, "path": {"type": "string", "description": "limit to a file or folder"},
              "case_sensitive": {"type": "boolean"}}, ["pattern"])),
    ("compile", "Compile the project with latexmk on this machine (when installed) and return errors and warnings with file:line.", _schema({}, [])),
    ("compile_log", "Errors and warnings from the latest compile anyone published (browser or helper).", _schema({}, [])),
]


class Server:
    def __init__(self, project: Project):
        self.p = project
        self.out = sys.stdout

    def send(self, msg: dict) -> None:
        self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.out.flush()

    async def call(self, name: str, args: dict) -> str:
        p = self.p
        if not p.r.synced.is_set():
            try:
                await asyncio.wait_for(p.r.synced.wait(), 10)
            except asyncio.TimeoutError:
                pass  # offline: work on the local copy; changes sync later
        fn = {
            "list_files": p.list_files, "read_file": p.read_file, "write_file": p.write_file, "edit_file": p.edit_file,
            "suggest_edit": p.suggest_edit, "add_comment": p.add_comment, "list_comments": p.list_comments,
            "reply_comment": p.reply, "resolve_comment": p.resolve, "search": p.search,
            "compile": p.compile, "compile_log": p.compile_log,
        }.get(name)
        if fn is None:
            raise ValueError(f"Unknown tool {name}")
        res = fn(**args)
        return await res if asyncio.iscoroutine(res) else res

    async def handle(self, msg: dict) -> None:
        mid, method = msg.get("id"), msg.get("method")
        if method is None:
            return  # a response or junk; we send no requests
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {
                    "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "texloom", "version": __version__},
                    "instructions": (
                        "This server edits a shared, live LaTeX project in TeXLoom. Collaborators see your edits and cursor "
                        f"as “{self.p.name}”. Read files before editing; when the user wants to review changes, use "
                        "suggest_edit / add_comment rather than edit_file. Use compile (or compile_log) to check for errors."),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                try:
                    text = await self.call(str(name), args if isinstance(args, dict) else {})
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except (ValueError, PermissionError, TypeError, KeyError) as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            elif mid is None:
                return  # notifications (initialized, cancelled, …)
            else:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
                return
        except Exception as e:  # noqa: BLE001
            log.exception("error handling %s", method)
            if mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
            return
        if mid is not None:
            self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while line := await reader.readline():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict):
                    await self.handle(m)


async def serve(keys: Keys, server: str, name: str, code: str | None) -> None:
    state = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "texloom" / "mcp" / keys.project
    replica = Replica(state, keys, server, code)
    project = Project(replica, name)
    conn = asyncio.create_task(replica.run())
    presence = asyncio.create_task(project.presence_loop())

    async def register() -> None:
        await replica.synced.wait()
        cid = str(replica.doc.client_id)
        if keys.writable and replica.clients.get(cid) != name:  # "who wrote this" shows the agent
            replica.local_change(lambda: replica.clients.__setitem__(cid, name))
    reg = asyncio.create_task(register())
    try:
        await Server(project).serve()
    finally:
        await project.announce(gone=True)
        await replica.flush_outbox()
        await asyncio.sleep(0.5)  # let the last updates go out
        replica.save()
        for t in (conn, presence, reg):
            t.cancel()
