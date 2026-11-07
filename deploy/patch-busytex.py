#!/usr/bin/env python3
"""Fixes in the in-browser TeX engine (texlyre-busytex's release assets), applied after each
download and on every deploy. Each patch is idempotent and is skipped, with a warning, if
the engine's code has changed (then check whether the new release still needs it).

Usage: deploy/patch-busytex.py <tex-assets dir>
"""

import sys
from pathlib import Path

MARK = "texloom patch"

ORIGINAL = """        for (const { path, contents } of files)
            FS.writeFile(this.work_dir + '/' + basename(path), contents);
"""

# v1 (deployed briefly): every tail of each file's path. Replaced by v2.
V1 = """        // texloom patch: bib files in folders (\\addbibresource{bib/own.bib})
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
"""

# v2: biber looks for each data source the .bcf names (bib/own.bib, ../refs.bib, Bib/Own.bib)
# relative to the main file's folder, which the engine flattened away. Write each project
# file at every tail of its path, and each named data source exactly where biber will look,
# matched by path, else ignoring case, else by file name.
V2 = """        // texloom patch: bib files in folders v2 (\\addbibresource{bib/own.bib}, {../refs.bib})
        const parts_of = p => p.split('/').filter(s => s && s !== '.');
        const put = (target, contents) => {
            const abs = [];
            for (const seg of (target.startsWith('/') ? '' : this.work_dir + '/').concat(target).split('/')) {
                if (seg === '..') abs.pop(); else if (seg && seg !== '.') abs.push(seg);
            }
            let dir = '';
            for (const seg of abs.slice(0, -1)) {
                dir += '/' + seg;
                if (!FS.analyzePath(dir).exists) FS.mkdir(dir);
            }
            FS.writeFile('/' + abs.join('/'), contents);
        };
        for (const { path, contents } of files) {
            const parts = parts_of(path).filter(s => s !== '..');
            for (let i = 0; i < parts.length; i++) put(parts.slice(i).join('/'), contents);
        }
        const bcf_text = String((files.find(f => f.path === bcf_path) || {}).contents || '');
        for (const m of bcf_text.matchAll(/<bcf:datasource[^>]*>([^<]+)<\\/bcf:datasource>/g)) {
            const want = m[1].trim().replace(/&amp;/g, '&');
            const tail = parts_of(want).filter(s => s !== '..').join('/');
            const low = tail.toLowerCase(), name = basename(low);
            const ends = (p, t) => p === t || p.endsWith('/' + t);
            const f = files.find(x => ends(x.path, tail)) || files.find(x => ends(x.path.toLowerCase(), low))
                || files.find(x => basename(x.path.toLowerCase()) === name);
            if (f && f.path !== bcf_path) put(want, f.contents);
        }
"""

PATCHES = [
    # (file, name, [texts it replaces], new text)
    ("busytex/busytex_biber.js", "bib files in folders v2", [ORIGINAL, V1], V2),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1])
        return 2
    root = Path(sys.argv[1])
    for rel, name, olds, new in PATCHES:
        f = root / rel
        if not f.is_file():
            print(f"    busytex patch '{name}': {rel} not found, skipped")
            continue
        src = f.read_text(encoding="utf-8")
        if f"{MARK}: {name}" in src:
            continue
        old = next((o for o in olds if o in src), None)
        if old is None:
            print(f"    busytex patch '{name}': the engine's code changed, not applied (check whether it's still needed)")
            continue
        f.write_text(src.replace(old, new, 1), encoding="utf-8")
        print(f"    busytex patch applied: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
