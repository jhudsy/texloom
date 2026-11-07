#!/usr/bin/env python3
"""Fixes in the in-browser TeX engine (texlyre-busytex's release assets), applied after each
download and on every deploy. Each patch is idempotent and is skipped, with a warning, if
the engine's code has changed (then check whether the new release still needs it).

Usage: deploy/patch-busytex.py <tex-assets dir>
"""

import sys
from pathlib import Path

MARK = "texloom patch"

PATCHES = [
    (
        "busytex/busytex_biber.js",
        "bib files in folders",
        # biber gets the .bib files flattened into one folder, so \addbibresource{bib/own.bib}
        # fails ("Cannot find bib/own.bib"). Keep each file's folders; and since the .bcf names
        # paths relative to the main file, which may itself sit in a folder, write each file at
        # every tail of its project path (paper/bib/own.bib, bib/own.bib, own.bib).
        """        for (const { path, contents } of files)
            FS.writeFile(this.work_dir + '/' + basename(path), contents);
""",
        """        // texloom patch: bib files in folders (\\addbibresource{bib/own.bib})
        for (const { path, contents } of files) {
            const parts = path.split('/').filter(p => p && p !== '.' && p !== '..');
            for (let i = 0; i < parts.length; i++) {
                let dir = this.work_dir;
                for (const seg of parts.slice(i, -1)) {
                    dir += '/' + seg;
                    if (!FS.analyzePath(dir).exists) FS.mkdir(dir);
                }
                FS.writeFile(this.work_dir + '/' + parts.slice(i).join('/'), contents);
            }
        }
""",
    ),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    root = Path(sys.argv[1])
    for rel, name, old, new in PATCHES:
        f = root / rel
        if not f.is_file():
            print(f"    busytex patch '{name}': {rel} not found, skipped")
            continue
        src = f.read_text(encoding="utf-8")
        if f"{MARK}: {name}" in src:
            continue
        if old not in src:
            print(f"    busytex patch '{name}': the engine's code changed, not applied (check whether it's still needed)")
            continue
        f.write_text(src.replace(old, new, 1), encoding="utf-8")
        print(f"    busytex patch applied: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
