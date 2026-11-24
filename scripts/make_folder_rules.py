#!/usr/bin/env python3
"""Writes scripts/folder-rules.json: the rules the two folder syncs (texloom/folderrules.py, with folder.py, and
web/src/foldersync.ts) must apply alike, as inputs and what they must give. Checked by
scripts/check_folder_rules.py and web/scripts/check-folder-rules.mjs.

- synced: whether a file at a path in the folder is one of the project's files;
- text: whether a file is kept as text (editable, merged by lines) or byte for byte;
- merge: a file saved in the folder while the project changed too (three-way, by lines);
- settle: how long a changed file must stay unchanged before it's read;
- crlf / onDisk: Windows line endings, noticed on reading and kept on writing;
- moves: files renamed or moved in the folder.

The expected values are written here from what each rule says, not taken from either app.

    uv run python scripts/make_folder_rules.py
    uv run python scripts/make_folder_rules.py --check   # whether folder-rules.json is what this writes (CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

synced = [
    # the project's own files
    ("main.tex", "main.tex", True), ("chapters/intro.tex", "main.tex", True), ("fig/plot.png", "main.tex", True),
    ("refs.bib", "main.tex", True), ("figure.pdf", "main.tex", True), ("data/table.csv", "main.tex", True),
    (".gitignore", "main.tex", True),  # a dot file is a project file; a dot folder isn't (below)
    ("Makefile", "main.tex", True),
    # the compiled PDF of the main document is output; another PDF is a figure
    ("main.pdf", "main.tex", False), ("thesis.pdf", "thesis.tex", False), ("main.pdf", "thesis.tex", True),
    ("sub/main.pdf", "main.tex", True),
    # LaTeX's working files
    ("main.aux", "main.tex", False), ("main.log", "main.tex", False), ("main.synctex.gz", "main.tex", False),
    ("main.fdb_latexmk", "main.tex", False), ("main.fls", "main.tex", False), ("main.bbl", "main.tex", False),
    ("main.blg", "main.tex", False), ("main.toc", "main.tex", False), ("main.out", "main.tex", False),
    ("main.run.xml", "main.tex", False), ("main.bcf", "main.tex", False), ("main.xdv", "main.tex", False),
    ("ch/one.aux", "main.tex", False), ("MAIN.LOG", "main.tex", False),
    # editors' droppings
    ("main.tex~", "main.tex", False), (".#main.tex", "main.tex", False), (".DS_Store", "main.tex", False),
    ("fig/.DS_Store", "main.tex", False), ("Thumbs.db", "main.tex", False), (".main.tex.swp", "main.tex", False),
    ("x.tmp", "main.tex", False),
    # file-sync services' copies of a file changed on two computers at once
    ("main (Ann's conflicted copy 2026-11-20).tex", "main.tex", False),
    ("ch/intro (Bob's conflicted copy).tex", "main.tex", False),
    ("main.sync-conflict-20261120-101500-ABCDEFG.tex", "main.tex", False),
    ("conflicted.tex", "main.tex", True),
    # tools' folders, and anything in a dot folder
    (".git/config", "main.tex", False), (".texloom/state.json", "main.tex", False), (".vscode/settings.json", "main.tex", False),
    (".idea/workspace.xml", "main.tex", False), ("node_modules/x/index.js", "main.tex", False),
    ("tools/__pycache__/a.pyc", "main.tex", False), (".github/workflows/build.yml", "main.tex", False),
    ("sub/.cache/x.tex", "main.tex", False),
    # paths a project refuses
    ("latexmkrc", "main.tex", False), ("sub/.latexmkrc", "main.tex", False), ("texmf.cnf", "main.tex", False), ("biber.conf", "main.tex", False),
    ("trailing.", "main.tex", False), ("PROGRA~1/x.tex", "main.tex", False), ("a:b.tex", "main.tex", False),
]

MB4 = 4 * 1024 * 1024
text = [
    ("main.tex", b"\\documentclass{article}\n".hex(), True),
    ("notes.md", "caf\u00e9 \U0001F600\n".encode().hex(), True),
    ("noext", b"plain\n".hex(), True),
    ("data.csv", b"a,b\n1,2\n".hex(), True),
    ("fig.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>".hex(), True),
    ("empty.tex", "", True),
    ("latin1.tex", "caf\u00e9\n".encode("latin-1").hex(), False),  # not UTF-8: kept byte for byte
    ("nul.tex", b"a\x00b".hex(), False),
    ("plot.pdf", b"%PDF-1.4\n%plain ascii\n".hex(), False),  # never text, whatever it holds
    ("photo.JPG", b"ascii".hex(), False),
    ("font.woff2", b"ascii".hex(), False),
    ("bin", bytes([0xFF, 0xFE, 0x41]).hex(), False),
]
big = [("big.tex", MB4, True), ("toobig.tex", MB4 + 1, False)]  # a file of this many "a"s

merge = [
    ("only the file changed: it wins", "a\nb\nc\n", "a\nb\nc\n", "a\nB\nc\n", "a\nB\nc\n"),
    ("only the project changed: kept", "a\nb\nc\n", "a\nB\nc\n", "a\nb\nc\n", "a\nB\nc\n"),
    ("different lines changed: both kept", "a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n", "A\nb\nC\n"),
    ("the same change on both sides: once", "a\nb\nc\n", "a\nX\nc\n", "a\nX\nc\n", "a\nX\nc\n"),
    ("the same line changed differently: the file wins", "a\nb\nc\n", "a\nT\nc\n", "a\nO\nc\n", "a\nO\nc\n"),
    ("a line deleted in the file, a line added in the project: both", "a\nb\nc\n", "a\nb\nc\nd\n", "a\nc\n", "a\nc\nd\n"),
    ("a line changed in the file that the project deleted: the file wins", "a\nb\nc\n", "a\nc\n", "a\nB\nc\n", "a\nB\nc\n"),
    ("no newline at the end", "x\ny", "X\ny", "x\nY", "X\nY"),
    ("lines with accents and emoji", "é\n😀\nz\n", "É\n😀\nz\n", "é\n😀\nZ\n", "É\n😀\nZ\n"),
    ("everything deleted in the file", "a\nb\n", "a\nb\n", "", ""),
    # both kept, the project's first (the rule says both are kept; this order is what both apps do)
    ("added at the same place on both sides, differently", "a\n", "a\nT\n", "a\nO\n", "a\nT\nO\n"),
]

settle = [(0, 0, 0.2), (10, 1, 0.4), (10, 2, 0.8), (10, 3, 1.6), (10, 4, 2.0), (10, 9, 2.0),
          (999_999, 0, 0.2), (1_000_000, 0, 1.0), (5_000_000, 1, 1.0), (5_000_000, 3, 1.6)]

crlf = [(b"a\r\nb\r\n", True), (b"a\nb\r\n", False), (b"abc", False), (b"\r\n", True), (b"\n", False), (b"", False),
        (b"a\rb\n", False)]

on_disk = [("a\nb\n", True, b"a\r\nb\r\n"), ("a\nb\n", False, b"a\nb\n"), ("a\nb", True, b"a\r\nb"), ("", True, b""),
           ("caf\u00e9\n", True, "caf\u00e9\r\n".encode())]

H1, H2, H3 = "1" * 32, "2" * 32, "3" * 32
moves = [
    ("a file renamed", {"a.tex": H1}, {"b.tex": H1}, {"b.tex": "a.tex"}),
    ("moved into a folder", {"a.tex": H1}, {"ch/a.tex": H1}, {"ch/a.tex": "a.tex"}),
    ("different content isn't a move", {"a.tex": H1}, {"b.tex": H2}, {}),
    ("two files renamed", {"a.tex": H1, "b.tex": H2}, {"c.tex": H2, "d.tex": H1}, {"c.tex": "b.tex", "d.tex": "a.tex"}),
    ("a folder renamed: each file keeps its name", {"x/a.tex": H1, "x/b.tex": H1}, {"y/b.tex": H1, "y/a.tex": H1},
     {"y/a.tex": "x/a.tex", "y/b.tex": "x/b.tex"}),
    ("the same content twice, one gone: the first new one by path takes it", {"a.tex": H1}, {"c.tex": H1, "b.tex": H1}, {"b.tex": "a.tex"}),
    ("the same name preferred", {"x/a.tex": H1, "y/b.tex": H1}, {"z/b.tex": H1}, {"z/b.tex": "y/b.tex"}),
    ("otherwise the first gone by path", {"y/q.tex": H3, "x/p.tex": H3}, {"z/r.tex": H3}, {"z/r.tex": "x/p.tex"}),
    ("nothing new", {"a.tex": H1}, {}, {}),
]

out = {
    "about": "Made by scripts/make_folder_rules.py; checked by scripts/check_folder_rules.py and web/scripts/check-folder-rules.mjs.",
    "synced": [{"path": p, "main": m, "want": w} for p, m, w in synced],
    "text": [{"path": p, "hex": h, "want": w} for p, h, w in text] + [{"path": p, "size": n, "want": w} for p, n, w in big],
    "merge": [{"what": w, "base": b, "theirs": t, "ours": o, "want": x} for w, b, t, o, x in merge],
    "settle": [{"size": s, "times": n, "want": w} for s, n, w in settle],
    "crlf": [{"hex": d.hex(), "want": w} for d, w in crlf],
    "onDisk": [{"text": t, "windows": w, "want": d.hex()} for t, w, d in on_disk],
    "moves": [{"what": w, "gone": g, "new": n, "want": x} for w, g, n, x in moves],
}
made = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
target = ROOT / "scripts/folder-rules.json"
if "--check" in sys.argv[1:]:
    if target.read_text("utf-8") != made:
        sys.exit("scripts/folder-rules.json isn't what scripts/make_folder_rules.py writes: run it, and commit the result")
    print("folder-rules.json: up to date")
else:
    target.write_text(made, "utf-8")
    print(sum(len(v) for k, v in out.items() if isinstance(v, list)), "rules")
