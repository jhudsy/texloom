#!/usr/bin/env python3
"""Regressions in the desktop's folder sync (texloom/folder.py), each a bug once seen: a deleted
file brought back, a half-saved file shared, a renamed folder's files left behind. One project
folder each, no server.

    uv run python scripts/check_folder_regressions.py
"""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import check, summary  # noqa: E402

from pycrdt import Doc, Map, Text  # noqa: E402

from texloom.crypto import Keys  # noqa: E402
from texloom.folder import FolderSync  # noqa: E402
from texloom.replica import Replica  # noqa: E402


# ---- deletions ----------------------------------------------------------------------------------

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


async def deletions() -> None:
    """A file deleted or renamed in a synced folder, and a change from elsewhere arriving before the
    sync has taken that in (a file that disappears is given a moment, SETTLE_DOUBT): writing out
    that change must not bring the deleted file back (before 0.4.33 it did, with every file deleted
    in that moment). A rename with an edit made elsewhere to the same file ends with the edit in the
    renamed file. A read-only copy restores a deleted file, as it can't share the deletion.
    """
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


# ---- reads --------------------------------------------------------------------------------------

TEXT = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"


async def reads() -> None:
    """A file is read only as it was once it stopped changing. Folder sync waits for a changed file to
    settle, then reads it; an editor that saves by emptying the file and writing it again can start its
    next save in between (on a slow or busy computer). Before 0.4.39 that read was taken as it was, so
    the empty file was shared and collaborators briefly got an empty main.tex (seen in CI on macOS).
    Now a file that differs from the one that settled is looked at again later.
    """
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


# ---- renames ------------------------------------------------------------------------------------

MAIN = "\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\end{document}\n"


async def renames() -> None:
    """A folder renamed or moved on disk takes its files with it. macOS reports a folder's rename as
    changes to the folder alone (old and new path), not to each file in it. Before, the files stayed
    in the project under their old paths until the next full look at the folder, and in between any
    write to disk (another file edited, say) found them missing there and wrote them back: the old
    folder came back, and the files were in the project twice (seen on 2026-11-23 renaming
    paper_extended/sections to sections). Now a changed path that is a folder, or was one, stands for
    every file under it, so the files are taken as moved at once.
    """
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


async def settings() -> None:
    """A folder's settings (config.json, which holds the project link) are written beside it, then
    renamed over it: never through a link planted at a temporary name, as another computer sharing
    the folder through Dropbox could (the security review of 2026-11-24)."""
    import json
    import os
    import stat

    from texloom.paths import write_json
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        victim = d / "victim.txt"
        victim.write_text("keep me")
        if os.name != "nt":
            os.symlink(victim, d / "config.json.tmp")
        write_json(d / "config.json", {"link": "secret"}, private=True, indent=2)
        check("written in full", json.loads((d / "config.json").read_text()) == {"link": "secret"})
        check("not through a link at the old temporary name", victim.read_text() == "keep me", victim.read_text())
        if os.name != "nt":
            check("readable by you only", stat.S_IMODE((d / "config.json").stat().st_mode) == 0o600)
        check("no temporary file left behind", not [x for x in d.iterdir() if x.name.endswith("tmp") and not x.is_symlink()])

        # a project file the sync writes: links planted at the temporary name it used to use, and
        # at the file itself, are replaced, never written through (the security review of 2026-11-24)
        from texloom.paths import replace_file
        if os.name != "nt":
            os.symlink(victim, d / ".main.tex.texloom-tmp")
            os.symlink(victim, d / "main.tex")
            st = replace_file(d / "main.tex", b"\\documentclass{article}\n")
            check("a project file is written in place of a link, not through it",
                  victim.read_text() == "keep me" and not (d / "main.tex").is_symlink()
                  and (d / "main.tex").read_bytes() == b"\\documentclass{article}\n", victim.read_text())
            check("with the usual permissions, and the date and size of what was written",
                  stat.S_IMODE((d / "main.tex").stat().st_mode) & 0o044 and st.st_size == (d / "main.tex").stat().st_size, (oct(stat.S_IMODE((d / "main.tex").stat().st_mode)), st.st_size))


# ---- what a crash mid-write left behind ---------------------------------------------------------

async def leftovers() -> None:
    """The temporary file replace_file writes before it renames (.<name>.<random>.texloom-tmp) is left
    behind by a crash or a power cut. Before 0.4.63 the start-up scan took it for a project file and
    shared it, half-written, with everyone; it was never deleted either. Now it is never the project's,
    and one untouched for an hour is deleted at the start (a recent one may be a write in progress)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ".texloom").mkdir()
        (root / "sub").mkdir()
        (root / "main.tex").write_text(TEXT)
        old, fresh, deep = root / ".main.tex.q1w2e3r4.texloom-tmp", root / ".chapter.tex.t5y6u7i8.texloom-tmp", root / "sub" / ".fig.png.o9p0a1s2.texloom-tmp"
        for f in (old, fresh, deep):
            f.write_bytes(b"half of a file")
        for f in (old, deep):
            os.utime(f, (os.path.getmtime(f) - 7200,) * 2)
        link = root / ".linked.x1y2z3.texloom-tmp"
        link.symlink_to(root / "main.tex")
        os.utime(link, (os.path.getmtime(old),) * 2, follow_symlinks=False)
        r = Replica(root / ".texloom", Keys.generate(), "http://127.0.0.1:1")
        fs = FolderSync(root, r)
        async with r.lock:
            await fs.ingest_all()
        check("the project has its file and none of the temporary ones", sorted(r.files.keys()) == ["main.tex"], sorted(r.files.keys()))
        check("leftovers untouched for an hour are deleted, at any depth", not old.exists() and not deep.exists())
        check("a recent one (a write that may be in progress) stays", fresh.exists())
        check("a link is never followed or deleted, and the file it points to stays", link.is_symlink() and (root / "main.tex").exists())


async def main() -> int:
    for name, part in (("A file deleted or renamed while a change arrives", deletions),
                       ("A file read while it's being saved", reads),
                       ("A folder renamed or deleted", renames),
                       ("Files written in one step (settings, project files)", settings),
                       ("What a crash mid-write left behind", leftovers)):
        print(f"{name}:", flush=True)
        await part()
    return summary("folder regressions")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
