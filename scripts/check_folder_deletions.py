#!/usr/bin/env python3
"""A file deleted or renamed in a synced folder, and a change from elsewhere arriving before the
sync has taken that in (a file that disappears is given a moment, SETTLE_DOUBT): writing out
that change must not bring the deleted file back (before 0.4.33 it did, with every file deleted
in that moment). A rename with an edit made elsewhere to the same file ends with the edit in the
renamed file. A read-only copy restores a deleted file, as it can't share the deletion.

    uv run python scripts/check_folder_deletions.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pycrdt import Doc, Map, Text
from texloom.crypto import Keys
from texloom.replica import Replica
from texloom.folder import FolderSync

ok = True
def check(what, cond):
    global ok
    ok &= bool(cond)
    if not cond:
        print("✗ " + what)

def remote_edit(r: Replica, path: str, text: str) -> bytes:
    other = Doc()
    other.apply_update(r.doc.get_update())
    files = other.get("files", type=Map)
    if path in files:
        t = files[path]
        t.insert(len(t), text)
    else:
        files[path] = Text(text)
    return other.get_update(r.doc.get_state())

async def setup(root: Path, keys: Keys):
    (root / ".texloom").mkdir(parents=True)
    for name, text in {"main.tex": "\\input{chapter}\n", "chapter.tex": "Chapter text.\n", "refs.bib": "@misc{a}\n"}.items():
        (root / name).write_text(text)
    r = Replica(root / ".texloom", keys, "http://127.0.0.1:1")
    fs = FolderSync(root, r)
    later: set[str] = set()
    fs._later = lambda paths: later.update(paths)
    async with r.lock:
        await fs.ingest_all()
        await fs.write_out()
    return r, fs, later

async def receive(r, fs, update):
    async with r.lock:
        with r.doc.transaction(origin="remote"):
            r.doc.apply_update(update)
        await fs.on_remote()

async def main():
    with tempfile.TemporaryDirectory() as d:
        keys = Keys.generate()
        root = Path(d) / "A"
        r, fs, later = await setup(root, keys)
        check("set up: three files in the project", set(r.files.keys()) == {"main.tex", "chapter.tex", "refs.bib"})
        # delete refs.bib, rename chapter.tex; before the sync looks, an edit to main.tex and one to chapter.tex arrive
        (root / "refs.bib").unlink()
        (root / "chapter.tex").rename(root / "chapter1.tex")
        await receive(r, fs, remote_edit(r, "main.tex", "% from elsewhere\n"))
        await receive(r, fs, remote_edit(r, "chapter.tex", "Edited elsewhere.\n"))
        check("the change from elsewhere is written", "% from elsewhere" in (root / "main.tex").read_text())
        check("refs.bib, deleted here, isn't written back", not (root / "refs.bib").exists())
        check("chapter.tex, renamed here, isn't written back", not (root / "chapter.tex").exists())
        check("both are looked at again", {"refs.bib", "chapter.tex"} <= later)
        async with r.lock:  # the sync looks again (with the new name, as the folder watcher reports it)
            await fs.ingest(later | {"chapter1.tex"})
            await fs.write_out()
        check("the deletion reaches the project", "refs.bib" not in r.files)
        check("the rename reaches the project", "chapter1.tex" in r.files and "chapter.tex" not in r.files)
        check("the edit made elsewhere is in the renamed file, on disk too",
              "Edited elsewhere." in str(r.files["chapter1.tex"]) and "Edited elsewhere." in (root / "chapter1.tex").read_text())
        check("nothing was brought back", sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["chapter1.tex", "main.tex"])

        # a read-only copy: a file deleted there comes back
        ro_keys, _ = Keys.from_link(keys.link("http://127.0.0.1:1", write=False))
        root2 = Path(d) / "B"
        (root2 / ".texloom").mkdir(parents=True)
        r2 = Replica(root2 / ".texloom", ro_keys, "http://127.0.0.1:1")
        with r2.doc.transaction(origin="remote"):
            r2.doc.apply_update(r.doc.get_update())
        fs2 = FolderSync(root2, r2)
        fs2._later = lambda paths: None
        async with r2.lock:
            await fs2.write_out()
        (root2 / "main.tex").unlink()
        await receive(r2, fs2, remote_edit(r2, "chapter1.tex", "More.\n"))
        check("read-only: a deleted file is restored", (root2 / "main.tex").exists())
    print("folder deletions: all checks passed" if ok else "folder deletions: FAILED")
    sys.exit(0 if ok else 1)

asyncio.run(main())
