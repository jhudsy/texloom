#!/usr/bin/env python3
"""Checks how comments are shown in editors (the language server, texloom/lsp.py; .texloom/comments.md)
(the security review of 2026-11-24): collaborators write the names and the text, so

- a name can't carry a ✓ (or a lookalike, or one hidden behind invisible characters) that would
  pass for the mark of a verified author;
- a comment's Markdown can't make the editor fetch a picture or follow a link: it's shown as text.

    uv run python scripts/check_comment_names.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from texloom.comments import overview  # noqa: E402
from texloom.lsp import Lsp, who  # noqa: E402

failures: list[str] = []


def check(what: str, cond: object, detail: object = "") -> None:
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"), flush=True)
    if not cond:
        failures.append(what)


def comment(by: str, text: str, verified: bool = False, replies=()) -> SimpleNamespace:
    return SimpleNamespace(id="c1", kind="comment", by=by, verified=verified, at=0, text=text, quote="", file="main.tex",
                           line=1, span=(0, 1), resolved=False, replies=list(replies), mentions=lambda _me: False)


def main() -> int:
    print("Names:")
    for fake in ("Mallory ✓", "Mallory ✔", "Mallory ✅", "Mallory ✓️", "Mallory ​✓", "Mallory ✔‍"):
        check(f"{fake!r} unverified shows no check mark", "✓" not in who(fake, False) and "✔" not in who(fake, False)
              and "✅" not in who(fake, False), who(fake, False))
    check("a verified name gets one", who("Alice", True) == "Alice ✓", who("Alice", True))
    check("a name of only marks isn't empty", who("✓", False) == "?", who("✓", False))

    print("Hovers and comments.md:")
    evil = "see ![x](https://attacker.example/pixel.png) and [click](https://attacker.example) <img src=https://attacker.example/a>"
    c = comment("Mallory ✓", evil, replies=[SimpleNamespace(by="Eve ✔", verified=False, at=0, text=evil)])
    hover = Lsp.thread(SimpleNamespace(), c)  # type: ignore[arg-type]
    md = overview([c], "p")  # type: ignore[list-item]
    for label, text in (("the hover", hover), ("comments.md", md)):
        theirs = [line[line.index("see "):] for line in text.splitlines() if "attacker" in line]
        check(f"{label} has no picture, link or HTML (Markdown's characters are escaped)",
              theirs and not any(re.search(r"(?<!\\)[<>\[\]()!]", line) for line in theirs), theirs)
        check(f"{label} shows no ✓ for an unverified author", "✓" not in text.replace("✓: a personal link", ""), text)
    print(f"\ncomment names: {'FAILED' if failures else 'ok'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
