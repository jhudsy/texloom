"""The database for a check's throwaway server: an SQLite file, or with TEXLOOM_CHECK_DATABASE=postgresql
a throwaway PostgreSQL (scripts/pg.py, which needs PostgreSQL's programs).

    url = testdb.start(tmp / "db")    # …   testdb.stop(tmp / "db")
    with testdb.running() as url: …
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

import pg

KIND = os.environ.get("TEXLOOM_CHECK_DATABASE", "sqlite")
if KIND not in ("sqlite", "postgresql"):
    raise SystemExit(f"TEXLOOM_CHECK_DATABASE={KIND}: sqlite or postgresql")


def start(folder: Path, kind: str = KIND) -> str:
    """A database kept in `folder` (started, for PostgreSQL); returns its address."""
    if kind == "postgresql":
        return pg.start(folder)[0]
    folder.mkdir(parents=True, exist_ok=True)
    return f"sqlite:{folder.resolve() / 'texloom.db'}"


def stop(folder: Path, kind: str = KIND) -> None:
    if kind == "postgresql":
        pg.stop(folder)


@contextlib.contextmanager
def running(folder: Path | None = None, kind: str = KIND):
    """A database (throwaway unless `folder` is given); yields its address."""
    tmp = None
    if folder is None:
        tmp = tempfile.mkdtemp(prefix="texloom-db-")
        folder = Path(tmp) / "db"
    url = start(folder, kind)
    try:
        yield url
    finally:
        stop(folder, kind)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
