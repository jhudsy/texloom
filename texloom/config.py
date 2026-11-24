"""A synced folder's own settings and project link: its config.json, in this computer's part of
the folder's `.texloom` (paths.state_dir), kept readable by you only (the link holds the keys)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from . import registry
from .crypto import Keys
from .paths import STATE, state_dir, write_json
from .replica import Replica

log = logging.getLogger("texloom")

DEFAULT_CONFIG = {
    # Local, per-folder settings (never shared with collaborators).
    "compile": False,          # run latexmk after changes (same as --compile)
    "engine": None,            # override the project's engine on this machine
    "latexmk_args": [],        # extra arguments, e.g. ["-pdfxe"]
    "git": False,              # true, or {"interval": 60, "quiet": 120, "push": true}: see `run --git`
    "shell_escape": False,     # false, "restricted" (TeX Live's safe programs only) or true (minted etc.;
                               # lines that run programs must be approved with `texloom allow`)
    "confine": True,           # compiles may read only the project, TeX's files and fonts (false: anything
                               # you can read, for projects whose collaborators you trust)
    "label": "",               # how your compiles are labelled for others
    "create_code": "",         # creation code, if the server requires one for new projects
}


def config_path(d: Path) -> Path:
    return state_dir(d) / "config.json"


def _stored(d: Path) -> dict:
    """The settings as the file has them (only those ever set)."""
    return json.loads(config_path(d).read_text())


def load_config(d: Path) -> dict:
    """A folder's settings, the defaults filled in for any it doesn't set."""
    return {**DEFAULT_CONFIG, **_stored(d)}


def load(d: Path) -> tuple[Keys, str]:
    """A folder's project keys and server, from its link."""
    return Keys.from_link(_stored(d)["link"])


def save(d: Path, link: str, create_code: str = "") -> None:
    state_dir(d).mkdir(parents=True, exist_ok=True)
    os.chmod(d / STATE, 0o700)
    write_json(config_path(d), {"link": link, **DEFAULT_CONFIG, "create_code": create_code}, private=True, indent=2)  # holds the project keys
    (d / STATE / ".gitignore").write_text("*\n")


def update_config(d: Path, **changes) -> None:
    """Change settings in a folder's .texloom/config.json (kept readable by you only)."""
    write_json(config_path(d), {**_stored(d), **changes}, private=True, indent=2)


def adopt(d: Path) -> bool:
    """A folder another computer syncs, shared with this one through Dropbox or the like (so its
    `.texloom` has that computer's records, not this one's): set it up here with the same project
    link. It starts out known to be shared (sharedfolder.py), so this computer stands by until it has
    the project and has heard from the others. False if no other computer's records are there."""
    for other in sorted((d / STATE / "computers").glob("*/config.json")):
        try:
            cfg = json.loads(other.read_text())
            Keys.from_link(cfg["link"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        save(d, cfg["link"], str(cfg.get("create_code") or ""))
        write_json(state_dir(d) / "shared.json", {"at": time.time()})
        return True
    return False


def browser_link(d: Path, extra: str = "") -> str:
    """A folder's project link for opening in the browser, marked as opened by this app, so the web
    app doesn't ask where the files should live: they're in this folder already. `extra`: more of the
    query, such as "tab=review" (web/src/main.ts reads it)."""
    cfg = load_config(d)
    page, _hash, keys = cfg["link"].partition("#")
    mark = "from=app" + ("&compile=1" if cfg.get("compile") else "") + (f"&{extra}" if extra else "")  # the web app then compiles here by default
    return f"{page}{'&' if '?' in page else '?'}{mark}#{keys}"


def linked_folders() -> Iterator[tuple[Path, Keys, str]]:
    """(folder, keys, server) for each folder on this computer's list whose project link can be
    read; one that can't (gone, or not set up) is left out."""
    for d in registry.folders():
        try:
            keys, server = load(d)
        except Exception:  # noqa: BLE001 - a folder that has gone, or isn't set up
            log.debug("folder %s skipped", d, exc_info=True)
            continue
        yield d, keys, server


def side_replica(kind: str, keys: Keys, server: str, code: str | None = None) -> Replica:
    """A copy of the project of its own, for a command that works beside the app or `texloom run`
    (`texloom mcp`, `lsp`, `comments reply`): kept in the cache folder, by `kind` and project."""
    state = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "texloom" / kind / keys.project
    return Replica(state, keys, server, code)
