#!/usr/bin/env python3
"""Write texloom/assets/third-party.txt: the licences of everything the desktop app bundles
(Python packages that `texloom[app]` needs, and theirs, as installed here), for its About box.

    uv sync --extra app && uv run python scripts/licences.py

The app build (.github/workflows/app.yml) runs it first, so the list matches what is bundled.
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "texloom" / "assets" / "third-party.txt"
LINE = "=" * 78

HEADER = """TeXLoom desktop app: licences

TeXLoom is free software under the GNU Affero General Public License, version 3 or later
(AGPL-3.0-or-later). Its complete source is at https://github.com/jhudsy/texloom.

It includes the software listed below, each under its own licence.

{line}
Python {python}, with the libraries it includes (OpenSSL: Apache-2.0; libffi, SQLite, zlib and
others under their own permissive licences)
Python Software Foundation License: https://docs.python.org/3/license.html

{line}
Qt and PySide6 (Qt for Python)
GNU Lesser General Public License, version 3 (LGPL-3.0). The app loads Qt's libraries
unchanged from its own folder, so they can be replaced with other builds of the same version.
Their source: https://download.qt.io/official_releases/ and https://code.qt.io/.
"""


def name_of(requirement: str) -> str:
    return re.split(r"[\s;\[<>=!~(]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def wanted(requirement: str, extras: set[str]) -> bool:
    """A requirement this install needs: no marker, or an `extra == …` marker for an extra we use
    (platform markers count as needed, to stay on the safe side)."""
    marker = requirement.split(";", 1)[1] if ";" in requirement else ""
    found = re.findall(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
    return not found or bool(set(found) & extras)


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    short = (meta.get("License") or "").strip().splitlines()
    return ", ".join(classifiers) or (short[0] if short and len(short[0]) < 80 else "see its licence text")


def licence_text(dist: metadata.Distribution) -> str:
    texts = []
    for f in dist.files or []:
        if re.search(r"(^|/)(licen[cs]e|copying|notice)[^/]*$", str(f), re.I) and "/" in str(f) and ".dist-info/" in str(f):
            try:
                texts.append(Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                pass
    return "\n\n".join(texts) or (dist.metadata.get("License") or "").strip() or "(no licence text in the package)"


def main() -> int:
    seen: dict[str, metadata.Distribution] = {}
    todo = [(r, {"app"}) for r in metadata.requires("texloom") or [] if wanted(r, {"app"})]
    while todo:
        req, extras = todo.pop()
        name = name_of(req)
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # for another platform
        seen[name] = dist
        want = set(re.findall(r"\[([^\]]+)\]", req.split(";")[0])[0].split(",")) if "[" in req.split(";")[0] else set()
        todo += [(r, want) for r in dist.requires or [] if wanted(r, want)]
    parts = [HEADER.format(line=LINE, python=".".join(map(str, sys.version_info[:3])))]
    for name in sorted(seen):
        d = seen[name]
        home = d.metadata.get("Home-page") or next((u.split(",", 1)[1].strip() for u in d.metadata.get_all("Project-URL") or []
                                                    if u.lower().startswith(("homepage", "source", "repository"))), "")
        parts += [LINE, f"{d.metadata['Name']} {d.version}", f"Licence: {licence_of(d)}", home, "", licence_text(d), ""]
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(seen)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
