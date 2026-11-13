#!/usr/bin/env python3
"""Checks that positions in project texts can't be mixed up (texloom/textpos.py):

1. Nothing outside textpos.py edits a pycrdt text by position: `del x[a:b]` and `x.insert(i, s)`
   count UTF-8 bytes there, and doing the conversion by hand is how edits landed in the wrong
   place before. Lists are edited the same way, so a line that edits a list says so with
   `# not a text`.
2. mypy finds nothing in the modules that handle positions, so a character index is never passed
   where a byte or UTF-16 offset is expected (textpos's Char, Byte and U16).

  uv run python scripts/check_textpos.py
"""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPED = ["texloom/textpos.py", "texloom/anchors.py", "texloom/merges.py", "texloom/folder.py", "texloom/mcp.py"]


def by_position(tree: ast.AST, lines: list[str]):
    for node in ast.walk(tree):
        if isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Slice):
                    yield node.lineno
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "insert"
              and len(node.args) == 2):
            yield node.lineno


def main() -> None:
    bad = []
    for p in sorted((ROOT / "texloom").glob("*.py")):
        if p.name == "textpos.py":
            continue
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()
        for n in by_position(ast.parse(src), lines):
            if "# not a text" not in lines[n - 1]:
                bad.append(f"{p.relative_to(ROOT)}:{n}: {lines[n - 1].strip()}  (edit texts through textpos; a list: add '# not a text')")
    mypy = subprocess.run([sys.executable, "-m", "mypy", "--ignore-missing-imports", "--follow-imports=silent", *TYPED],
                          cwd=ROOT, capture_output=True, text=True, check=False)
    if mypy.returncode:
        bad += [line for line in mypy.stdout.splitlines() if ": error:" in line] or [mypy.stdout + mypy.stderr]
    if bad:
        print("textpos: FAILED\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"textpos: no text edited by position outside textpos.py; mypy clean on {len(TYPED)} modules")


if __name__ == "__main__":
    main()
