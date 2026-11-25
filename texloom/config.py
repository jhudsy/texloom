"""A synced folder's settings and project link.

- The project link (it holds the keys), the creation code and your name for the project: in the
  folder, in this computer's part of its `.texloom` (paths.state_dir), readable by you only.
  adopt() finds them there when another computer shares the folder with this one.
- The settings that make this computer run something (compiles, latexmk's arguments, shell
  escape and the lines allowed to use it, the compile confinement, the git bridge) and how
  compiles are labelled: in your own configuration folder (registry.config_dir()), never in the
  synced folder. Anyone sharing that folder through Dropbox, iCloud or the like can write any
  file in it, this computer's `.texloom/computers/<id>/config.json` included, and could
  otherwise make this computer run programs of their choosing.

Those settings are kept per folder *and* project: in a file named after the folder's full path,
which also notes the project's id. Nobody else can point a folder at settings meant for another
one: they don't choose where the folder is on this computer, and a project link changed to
another project's finds no settings (all off). A folder that is moved or renamed starts over
with the default settings (compiles off), as it does when it is added to the folder list again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path

from . import registry
from .crypto import Keys
from .paths import ENGINE_FLAGS, STATE, state_dir, write_json
from .replica import Replica

log = logging.getLogger("texloom")

# Kept in your own configuration folder (settings_path), never in the synced folder.
LOCAL_SETTINGS = {
    "compile": False,          # run latexmk after changes (same as --compile)
    "engine": None,            # override the project's engine on this machine
    "latexmk_args": [],        # extra arguments, e.g. ["-pdfxe"]
    "git": False,              # true, or {"interval": 60, "quiet": 120, "push": true}: see `run --git`
    "shell_escape": False,     # false, "restricted" (TeX Live's safe programs only), true (restricted, plus
                               # the programs of lines approved with `texloom allow`: minted etc.) or
                               # "unrestricted" (approved lines with full shell escape)
    "confine": True,           # compiles may read only the project, TeX's files and fonts (false: anything
                               # you can read, for projects whose collaborators you trust)
    "label": "",               # how your compiles are labelled for others
    "allowed": [],             # lines allowed to run programs with shell escape (`texloom allow`)
}
DEFAULT_CONFIG = {
    **LOCAL_SETTINGS,
    "create_code": "",         # creation code, if the server requires one for new projects (in the folder)
}
# What may be brought over from a folder that may be shared with others: these run nothing a
# compile with the defaults wouldn't, and unconfine nothing.
HARMLESS = ("compile", "engine", "label")


def config_path(d: Path) -> Path:
    """The folder's own config.json: its project link, creation code and name."""
    return state_dir(d) / "config.json"


def _settings_dir() -> Path:
    return registry.config_dir() / "folder-settings"


def settings_path(d: Path) -> Path:
    """Where this computer keeps a folder's settings (LOCAL_SETTINGS): in your configuration folder,
    named after the folder's full path."""
    return _settings_dir() / (hashlib.sha256(str(d.resolve()).encode("utf-8", "surrogatepass")).hexdigest()[:32] + ".json")


def _stored(d: Path) -> dict:
    """What the folder's config.json holds."""
    return json.loads(config_path(d).read_text())


def _project(folder: dict) -> str | None:
    try:
        return Keys.from_link(folder["link"])[0].project
    except (KeyError, TypeError, ValueError):
        return None


def _settings(d: Path, project: str | None) -> dict:
    """This computer's settings for the folder, if they were kept for this place and project."""
    if project is None:
        return {}
    try:
        data = json.loads(settings_path(d).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("folder") != str(d.resolve()) or data.get("project") != project:
        return {}
    return {k: v for k, v in data.items() if k in LOCAL_SETTINGS}


def load_config(d: Path) -> dict:
    """A folder's link and settings, the defaults filled in for any not set. Settings written in the
    folder's own config.json (before 0.4.56, or by someone sharing the folder) are ignored."""
    folder = _stored(d)
    _migrate_once(d, folder)
    return {**DEFAULT_CONFIG, **{k: v for k, v in folder.items() if k not in LOCAL_SETTINGS},
            **_settings(d, _project(folder))}


def load(d: Path) -> tuple[Keys, str]:
    """A folder's project keys and server, from its link."""
    return Keys.from_link(_stored(d)["link"])


def save(d: Path, link: str, create_code: str = "") -> None:
    state_dir(d).mkdir(parents=True, exist_ok=True)
    os.chmod(d / STATE, 0o700)
    write_json(config_path(d), {"link": link, "create_code": create_code}, private=True, indent=2)  # holds the project keys
    (d / STATE / ".gitignore").write_text("*\n")


def _write_settings(d: Path, project: str, settings: dict) -> None:
    _settings_dir().mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(_settings_dir(), 0o700)
    write_json(settings_path(d), {"folder": str(d.resolve()), "project": project, **settings}, private=True, indent=2)


def update_config(d: Path, **changes) -> None:
    """Change a folder's settings: those in LOCAL_SETTINGS in your configuration folder, the rest
    (create_code, name) in the folder's .texloom/config.json (readable by you only)."""
    local = {k: v for k, v in changes.items() if k in LOCAL_SETTINGS}
    rest = {k: v for k, v in changes.items() if k not in LOCAL_SETTINGS}
    folder = _stored(d)
    _migrate_once(d, folder)
    if rest:
        folder = {k: v for k, v in {**_stored(d), **rest}.items() if k not in LOCAL_SETTINGS}
        write_json(config_path(d), folder, private=True, indent=2)
    if local:
        project = _project(folder)
        if project is None:
            raise ValueError(f"{d} has no project link")
        _write_settings(d, project, {**_settings(d, project), **local})


# --- settings kept in the folder before 0.4.56 ---------------------------------
_MOVED = "moved-from-folders"  # in _settings_dir(): settings were brought over, once for all
_moved: set[Path] = set()  # (per configuration folder: the checks change it)

# Folders that file-sync services keep in step (others may write in them): names starting so,
SYNCED_PREFIXES = ("dropbox", "onedrive", "google drive", "googledrive", "icloud", "pcloud", "box sync", "nextcloud",
                   "owncloud", "seadrive", "seafile", "syncthing", "resilio")
# or named so,
SYNCED_NAMES = {"mobile documents", "cloudstorage", "box", "mega", "sync", "sync.com"}
# and files these services put in the folders they keep (or above them)
SYNC_MARKS = (".dropbox", ".dropbox.cache", ".stfolder", ".sync", ".owncloudsync.log", ".nextcloudsync.log")


def looks_shared(d: Path) -> str | None:
    """Why a folder may be shared with others through a file-sync service (who could then have
    written its settings), or None if nothing suggests it."""
    top = d / STATE / "computers"
    try:
        if any(p.is_dir() and p.name != registry.computer_id() for p in top.iterdir()):
            return "another computer syncs it too"
    except OSError:
        pass
    if (state_dir(d) / "shared.json").exists():
        return "it was found shared through a file-sync service"
    r = d.resolve()
    for p in (r, *r.parents):
        if any((p / m).exists() for m in SYNC_MARKS):
            return "a file-sync service keeps it in step"
        name = p.name.lower()
        if p != r and (name.startswith(SYNCED_PREFIXES) or name in SYNCED_NAMES):
            return "it is in a file-sync service's folder"
    if os.name != "nt":
        try:
            if stat.S_IMODE(config_path(d).stat().st_mode) != 0o600:  # not as TeXLoom writes it
                return "its config.json was written by something else"
        except OSError:
            pass
    return None


def _migrate_once(d: Path, folder: dict) -> None:
    """The first time this version runs: bring the settings of each folder on the list (and `d`'s)
    from the folder to your configuration folder. From a folder that may be shared, only those that
    run nothing (HARMLESS). Once for all: settings written in a folder later are ignored."""
    marker = _settings_dir() / _MOVED
    if marker in _moved:
        return
    _moved.add(marker)
    if marker.exists():
        return
    here = d.resolve()
    for f in dict.fromkeys([*registry.folders(), here]):
        try:
            _migrate(f, folder if f == here else _stored(f))
        except Exception:  # noqa: BLE001 - a folder gone, or not set up: nothing to bring
            log.debug("settings of %s not brought over", f, exc_info=True)
    try:
        _settings_dir().mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time()}\n")
    except OSError as e:
        log.warning("couldn't note that folder settings were moved: %s", e)


def _migrate(d: Path, folder: dict) -> None:
    project = _project(folder)
    if project is None or settings_path(d).exists():
        return
    old = {k: v for k, v in folder.items() if k in LOCAL_SETTINGS and v != LOCAL_SETTINGS[k]}
    approvals = state_dir(d) / "allowed.json"
    try:
        old["allowed"] = sorted(k for k in json.loads(approvals.read_text()) if isinstance(k, str))
    except (OSError, ValueError, TypeError):
        pass
    keep = old
    if old and (why := looks_shared(d)):
        keep = {k: v for k, v in old.items() if k in HARMLESS}
        if "compile" in keep:
            keep["compile"] = keep["compile"] is True
        if keep.get("engine") not in ENGINE_FLAGS:
            keep.pop("engine", None)
        if not isinstance(keep.get("label"), str):
            keep.pop("label", None)
        if dropped := sorted(set(old) - set(keep)):
            log.warning("%s: settings that let compiles run programs or read more (%s) are now kept in TeXLoom's own "
                        "settings, not in the folder. They were not brought over, as this folder may be shared with "
                        "others (%s): set them again in the app, or with `texloom settings`.", d, ", ".join(dropped), why)
    _write_settings(d, project, keep)
    # the folder keeps its link, creation code and name
    write_json(config_path(d), {k: v for k, v in folder.items() if k not in LOCAL_SETTINGS}, private=True, indent=2)
    approvals.unlink(missing_ok=True)


def adopt(d: Path) -> bool:
    """A folder another computer syncs, shared with this one through Dropbox or the like (so its
    `.texloom` has that computer's records, not this one's): set it up here with the same project
    link. It starts out known to be shared (sharedfolder.py), so this computer stands by until it has
    the project and has heard from the others; its settings start as the defaults (nothing runs).
    False if no other computer's records are there."""
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
