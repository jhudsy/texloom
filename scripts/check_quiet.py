#!/usr/bin/env python3
"""No error is swallowed without a trace (issue #24).

Web app (web/src): a `catch` with an empty body (comments don't count) and `.catch(() => {})`
or `.catch(() => undefined)` are refused. An error that is safe to ignore goes to `quiet(why, e)`
(web/src/quiet.ts), which keeps it for debugging (localStorage texloom.debug = 1) and says why.
A `catch` that does something instead (returns a fallback, tells the person) is fine.

Desktop app and server (texloom/, server/): a handler for `Exception`, `BaseException` or
anything (a bare `except:`) must log (`log.…`), raise, or use the error it caught (show it,
return it). Handlers for named errors (`except OSError: pass`) are taken as deliberate.

    uv run python scripts/check_quiet.py            # every problem, with its place
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROAD = {"Exception", "BaseException"}
NO_OP_CATCH = re.compile(r"\.catch\(\s*(?:\(\s*\)|\(\s*_\w*\s*\)|_\w*)\s*=>\s*(?:\{\s*\}|undefined|void 0)\s*\)")


def strip_ts(src: str) -> str:
    """The source with comments and the contents of strings, regexes aside, blanked out (same
    length, newlines kept), so braces inside them don't count."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in "'\"`":
            j = i + 1
            depth = 0  # template literal ${…} nesting: braces in there are code, but rare in a catch
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if c == "`" and src.startswith("${", j):
                    depth += 1
                elif c == "`" and depth and src[j] == "}":
                    depth -= 1
                elif src[j] == c and not depth:
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


CATCH = re.compile(r"\bcatch\s*(?:\(\s*[\w$]*\s*(?::\s*\w+\s*)?\))?\s*\{")


def ts_problems(path: Path) -> list[tuple[int, str]]:
    src = path.read_text("utf-8")
    code = strip_ts(src)
    found = []
    for m in CATCH.finditer(code):
        start = m.end()  # just after "{"
        depth, j = 1, start
        while j < len(code) and depth:
            depth += {"{": 1, "}": -1}.get(code[j], 0)
            j += 1
        if not code[start:j - 1].strip():
            found.append((code.count("\n", 0, m.start()) + 1, "empty catch: call quiet(why, e) or handle it"))
    for m in NO_OP_CATCH.finditer(code):
        found.append((code.count("\n", 0, m.start()) + 1, "no-op .catch(): use .catch((e) => quiet(why, e)) or handle it"))
    return found


def broad(t: ast.expr | None) -> bool:
    if t is None:
        return True
    if isinstance(t, ast.Name):
        return t.id in BROAD
    if isinstance(t, ast.Attribute):
        return t.attr in BROAD
    if isinstance(t, ast.Tuple):
        return any(broad(e) for e in t.elts)
    return False


def leaves_a_trace(h: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=h.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id in ("log", "logger", "logging"):
            return True
        if h.name and isinstance(node, ast.Name) and node.id == h.name:
            return True  # the error is passed on: shown, returned, kept
    return False


def py_problems(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text("utf-8"), str(path))
    return [(h.lineno, "broad except that leaves no trace: log it (log.debug(..., exc_info=True)), raise, or use the error")
            for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and broad(h.type) and not leaves_a_trace(h)]


def main() -> int:
    problems = []
    for p in sorted((ROOT / "web" / "src").rglob("*.ts")):
        problems += [(p, line, msg) for line, msg in ts_problems(p)]
    for d in ("texloom", "server"):
        for p in sorted((ROOT / d).rglob("*.py")):
            problems += [(p, line, msg) for line, msg in py_problems(p)]
    for p, line, msg in problems:
        print(f"{p.relative_to(ROOT)}:{line}: {msg}")
    if problems:
        print(f"quiet: {len(problems)} error(s) swallowed without a trace")
        return 1
    print("quiet: no error is swallowed without a trace")
    return 0


if __name__ == "__main__":
    sys.exit(main())
