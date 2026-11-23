#!/usr/bin/env python3
"""The MCP server answers from a command: the helper's (`texloom mcp`) or the downloadable app's
binary (`TeXLoom mcp`, packaging/app_main.py). App builds run it on the built app, because a
windowed app on Windows may have no standard input and output, which MCP is spoken over.

    uv run python scripts/check_bundled_mcp.py dist/TeXLoom.app/Contents/MacOS/TeXLoom
    uv run python scripts/check_bundled_mcp.py texloom        # the helper
"""
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from texloom.crypto import Keys  # noqa: E402

exe = sys.argv[1] if len(sys.argv) > 1 else "texloom"
exe = shutil.which(exe) or exe
link = Keys.generate().link("http://127.0.0.1:1")  # a project nobody serves: MCP must answer regardless
p = subprocess.Popen([exe, "mcp", "--suggest-only", "--name", "check", link],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
replies: dict = {}


def read() -> None:
    for line in p.stdout:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "id" in m:
            replies[m["id"]] = m


reader = threading.Thread(target=read, daemon=True)
reader.start()
for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "check", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
for _ in range(600):  # until both replies are in, or a minute
    if 1 in replies and 2 in replies or p.poll() is not None:
        break
    reader.join(0.1)
p.stdin.close()
try:
    p.wait(15)
except subprocess.TimeoutExpired:
    p.kill()
tools = [t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])]
ok = "result" in replies.get(1, {}) and "list_files" in tools
print(f"{exe}: " + (f"MCP answers, with {len(tools)} tools" if ok else f"MCP didn't answer: {replies}\n{p.stderr.read()[-3000:]}"))
sys.exit(0 if ok else 1)
