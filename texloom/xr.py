"""Documents that take labels from another document of the project (\\externaldocument, from
the xr, xr-hyper or zref-xr packages; xcite's \\externalcitedocument for citations).

xr reads the other document's .aux, which only exists once that document has been compiled,
so a compile builds those documents first. Keep in sync with web/src/xr.ts;
scripts/xr-vectors.json checks both.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .scan import code_part

INPUT = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]{1,300})\}")
EXTERNAL = re.compile(r"\\(?:z?externaldocument|externalcitedocument)\s*(?:\[[^\]]{0,200}\]\s*)?\{([^}]{1,300})\}")


def _strip_comments(text: str) -> str:
    return "\n".join(code_part(line) for line in text.split("\n"))


def _resolve(dir: str, name: str) -> str | None:
    """A project path joined and tidied ('a/../b' is 'b'); None if it leaves the project."""
    if not name or name.startswith("/"):
        return None
    out: list[str] = []
    for seg in f"{dir}/{name}".split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                return None
            out.pop()
        else:
            out.append(seg)
    return "/".join(out) or None


def _dir(p: str) -> str:
    return p.rpartition("/")[0]


def _is_document(files: Mapping[str, str], p: str) -> bool:
    return p in files and re.search(r"\\documentclass\b", _strip_comments(files[p])) is not None


def _sources(files: Mapping[str, str], doc: str) -> list[str]:
    """`doc` and the files it reads with \\input, \\include or \\subfile, as TeX finds them:
    relative to the directory `doc` is compiled in."""
    seen, todo = [doc], [doc]
    while todo:
        for m in INPUT.finditer(_strip_comments(files.get(todo.pop(), ""))):
            p = _resolve(_dir(doc), m.group(1).strip())
            hit = next((q for q in (p, f"{p}.tex") if p and q in files), None)
            if hit and hit not in seen:
                seen.append(hit)
                todo.append(hit)
    return seen


def external_documents(files: Mapping[str, str], doc: str) -> list[tuple[str, str]]:
    """The documents `doc` takes labels from, as (where its compile reads the .aux, that
    document's .tex). Only documents of the project (with \\documentclass) are listed."""
    out: dict[str, str] = {}
    for src in _sources(files, doc):
        for m in EXTERNAL.finditer(_strip_comments(files.get(src, ""))):
            base = _resolve(_dir(doc), m.group(1).strip())
            if base and f"{base}.tex" != doc and _is_document(files, f"{base}.tex"):
                out[f"{base}.aux"] = f"{base}.tex"
    return list(out.items())


def compile_order(files: Mapping[str, str], main: str) -> list[str]:
    """The documents to compile before `main`, in order, so that each finds the .aux files it
    reads. Documents that take labels from each other (main and a supplement, say) form a
    cycle: it is cut where it closes, so a document already on the way is not compiled again."""
    order: list[str] = []
    seen = {main}

    def visit(doc: str) -> None:
        for _, tex in external_documents(files, doc):
            if tex not in seen:
                seen.add(tex)
                visit(tex)
                order.append(tex)

    visit(main)
    return order
