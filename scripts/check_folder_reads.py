#!/usr/bin/env python3
"""A file is read only as it was once it stopped changing. Folder sync waits for a changed file to
settle, then reads it; an editor that saves by emptying the file and writing it again can start its
next save in between (on a slow or busy computer). Before 0.4.39 that read was taken as it was, so
the empty file was shared and collaborators briefly got an empty main.tex (seen in CI on macOS).
Now a file that differs from the one that settled is looked at again later.

    uv run python scripts/check_folder_reads.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from texloom.crypto import Keys  # noqa: E402
from texloom.folder import FolderSync  # noqa: E402
from texloom.replica import Replica  # noqa: E402

ok = True


def check(what, cond):
    global ok
    ok &= bool(cond)
    print(("✓ " if cond else "✗ ") + what)


TEXT = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"


async def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".texloom").mkdir()
        (root / "main.tex").write_text(TEXT)
        r = Replica(root / ".texloom", Keys.generate(), "http://127.0.0.1:1")
        fs = FolderSync(root, r)
        later: set[str] = set()
        fs._later = lambda paths: later.update(paths)
        async with r.lock:
            await fs.ingest_all()
        check("the file is in the project", str(r.files["main.tex"]) == TEXT)

        # an edit is saved and settles; then, before it is read, the editor's next save empties it
        (root / "main.tex").write_text(TEXT.replace("Hello.", "Hello again."))
        settle = fs._settle

        async def settle_then_empty(paths):
            out = await settle(paths)
            (root / "main.tex").write_bytes(b"")  # the next save has begun
            return out
        fs._settle = settle_then_empty
        async with r.lock:
            await fs.ingest({"main.tex"})
        fs._settle = settle
        check("the emptied file isn't shared", str(r.files["main.tex"]) == TEXT)
        check("it's looked at again later", "main.tex" in later)

        # the save finishes: that is what gets shared
        (root / "main.tex").write_text(TEXT.replace("Hello.", "Hello at last."))
        later.clear()
        async with r.lock:
            await fs.ingest({"main.tex"})
        check("the finished save is shared", str(r.files["main.tex"]) == TEXT.replace("Hello.", "Hello at last."))
    print("folder reads: ok" if ok else "folder reads: FAILED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
