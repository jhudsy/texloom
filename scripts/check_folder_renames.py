#!/usr/bin/env python3
"""A folder renamed or moved on disk takes its files with it. macOS reports a folder's rename as
changes to the folder alone (old and new path), not to each file in it. Before, the files stayed
in the project under their old paths until the next full look at the folder, and in between any
write to disk (another file edited, say) found them missing there and wrote them back: the old
folder came back, and the files were in the project twice (seen on 2026-11-23 renaming
paper_extended/sections to sections). Now a changed path that is a folder, or was one, stands for
every file under it, so the files are taken as moved at once.

    uv run python scripts/check_folder_renames.py
"""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from texloom.crypto import Keys  # noqa: E402
from texloom.folder import FolderSync  # noqa: E402
from texloom.replica import Replica  # noqa: E402

ok = True


def check(what, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"))


MAIN = "\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\end{document}\n"


async def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".texloom").mkdir()
        (root / "main.tex").write_text(MAIN)
        (root / "old" / "deeper").mkdir(parents=True)
        (root / "old" / "intro.tex").write_text("Introduction.\n")
        (root / "old" / "method.tex").write_text("Method.\n")
        (root / "old" / "deeper" / "table.tex").write_text("A table.\n")
        r = Replica(root / ".texloom", Keys.generate(), "http://127.0.0.1:1")
        fs = FolderSync(root, r)
        fs._later = lambda paths: None  # (no settling retries in this check)
        await fs.ingest_all()
        fs.ingest_dirs()
        before = set(r.files.keys())
        check("the files are in the project", before == {"main.tex", "old/intro.tex", "old/method.tex", "old/deeper/table.tex"}, before)

        # the folder is renamed; the watcher hears only of the two folders (as on macOS)
        said: list[str] = []
        hear = logging.Handler()
        hear.emit = lambda rec: said.append(rec.getMessage())
        logging.getLogger("texloom").addHandler(hear)
        logging.getLogger("texloom").setLevel(logging.INFO)
        os.rename(root / "old", root / "sections")
        await fs.changed({"old", "sections"})
        moved = {m for m in said if "→" in m}
        check("each file is taken as moved (so its comments go with it)",
              moved == {"old/intro.tex → sections/intro.tex", "old/method.tex → sections/method.tex",
                        "old/deeper/table.tex → sections/deeper/table.tex"}, sorted(said))
        # then another file is edited: its ingest ends by writing the project out to disk
        (root / "main.tex").write_text(MAIN.replace("\\end", "More.\n\\end"))
        await fs.changed({"main.tex"})

        files = set(r.files.keys())
        want = {"main.tex", "sections/intro.tex", "sections/method.tex", "sections/deeper/table.tex"}
        check("the project has the files at their new paths only", files == want, sorted(files))
        check("the old folder isn't written back", not (root / "old").exists(), sorted(p.name for p in (root / "old").rglob("*")) if (root / "old").exists() else "")
        check("nothing known under the old path", not [p for p in fs.known if p.startswith("old/")], [p for p in fs.known if p.startswith("old/")])
        check("the folders are right", "old" not in r.dirs and "sections" in r.dirs and "sections/deeper" in r.dirs, sorted(r.dirs.keys()))
        check("the contents came along", str(r.files.get("sections/deeper/table.tex")) == "A table.\n", str(r.files.get("sections/deeper/table.tex")))

        # and a folder deleted on disk takes its files out of the project at once
        for f in sorted((root / "sections" / "deeper").iterdir()):
            f.unlink()
        (root / "sections" / "deeper").rmdir()
        await fs.changed({"sections/deeper"})
        check("a deleted folder's files leave the project", "sections/deeper/table.tex" not in r.files, sorted(r.files.keys()))
    print("folder renames: " + ("all checks passed" if ok else "FAILED"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
