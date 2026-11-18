"""What the desktop app's windows and dialogs share: the sync engine's thread, a folder's
status in words and colours, and small widgets and helpers."""

from __future__ import annotations

import asyncio
import functools
import re
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync, registry
from .cli import load_config, run
from .crypto import Keys
from .daemon import Daemon
from .i18n import _, ngettext

ICON = Path(__file__).with_name("assets") / "icon.png"
MAC = sys.platform == "darwin"
DEFAULT_SERVER = "https://app.texloom.org"


def theme_icon(name: str) -> QtGui.QIcon:
    """A standard icon from the system (SF Symbols on macOS, Fluent on Windows, the icon theme on
    Linux), or none, in which case buttons show their text alone."""
    try:
        return QtGui.QIcon.fromTheme(getattr(QtGui.QIcon.ThemeIcon, name))
    except AttributeError:
        return QtGui.QIcon()


class Dialog(QtWidgets.QDialog):
    """A dialog that, on macOS, slides down from the main window as a sheet."""

    def __init__(self, parent=None) -> None:
        if isinstance(parent, QtWidgets.QMainWindow) and (over := QtWidgets.QApplication.activeModalWidget()) is not None:
            # asked for from another dialog open over the window (Join… in the project list): on top of
            # that one, since a sheet would open under it and be blocked by it
            parent = over
        super().__init__(parent)
        if MAC and isinstance(parent, QtWidgets.QMainWindow):
            self.setWindowModality(Qt.WindowModal)


def chevron(w: QtWidgets.QWidget, down: bool) -> QtGui.QIcon:
    """A disclosure chevron (› or ⌄) in the widget's text colour: the ▸ and ▾ characters are tiny
    in most fonts, and Qt's own arrows differ from system to system."""
    size, ratio = 16, max(2.0, w.devicePixelRatioF())
    pm = QtGui.QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtGui.QPen(w.palette().color(QtGui.QPalette.WindowText), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    pts = [(3, 5.5), (8, 10.5), (13, 5.5)] if down else [(5.5, 3), (10.5, 8), (5.5, 13)]
    p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in pts]))
    p.end()
    return QtGui.QIcon(pm)


def grey_note(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {GREY}")
    return label


# ---- the sync engine, on its own thread -------------------------------------------------
class Engine:
    """The asyncio side: the daemon and anything the window asks of the network."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.daemon = Daemon(lambda d: run(d, False))
        self.thread = threading.Thread(target=self._main, name="texloom-sync", daemon=True)
        self.names: dict[Path, str] = {}  # project names, read on the sync thread (documents aren't shared across threads)
        self.bridge = Bridge()  # made on the Qt thread, once the QApplication exists

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.daemon.loop())
        self.loop.create_task(self._names())
        self.loop.run_forever()

    async def _names(self) -> None:
        while True:
            for d, st in list(self.daemon.states.items()):
                if st.replica is not None:
                    self.names[d] = str(st.replica.meta.get("name") or "")
            await asyncio.sleep(1)

    def start(self) -> None:
        self.thread.start()

    def call(self, fn, *a) -> None:
        self.loop.call_soon_threadsafe(fn, *a)

    def submit(self, coro, done) -> None:
        """Run a coroutine on the sync thread; `done(result, error)` is called on the Qt thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: self.bridge.invoke(
            lambda: done(None if f.exception() else f.result(), f.exception())))


class Bridge(QtCore.QObject):
    """Runs callables on the Qt thread (signals cross threads safely)."""
    call = QtCore.Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(lambda fn: fn(), Qt.QueuedConnection)

    def invoke(self, fn) -> None:
        self.call.emit(fn)


# ---- what a folder is doing ---------------------------------------------------------------
GREEN, AMBER, RED, GREY = "#2e7d32", "#b26a00", "#b3261e", "#75736c"


def program_what(en: str) -> str:
    """A description from texloom/scan.py (kept in English there: it is also data), translated."""
    return {
        'runs a shell command (\\write18)': _('runs a shell command (\\write18)'),
        'runs a shell command (\\ShellEscape)': _('runs a shell command (\\ShellEscape)'),
        'reads the output of a command (\\input|)': _('reads the output of a command (\\input|)'),
        'runs a program from Lua': _('runs a program from Lua'),
        'loads a package that runs programs': _('loads a package that runs programs'),
        'TikZ externalisation runs LaTeX as a command': _('TikZ externalisation runs LaTeX as a command'),
        'tells arara which programs to run': _('tells arara which programs to run'),
        'sets the program your LaTeX editor runs': _('sets the program your LaTeX editor runs'),
    }.get(en, en)


def red(text: str) -> str:
    return f"<span style='color:{RED}'>{text}</span>"


def describe(d: Path, engine: Engine) -> tuple[str, str, str]:
    """(short status, colour, longer explanation) for a folder."""
    st = engine.daemon.states.get(d)
    if engine.daemon.paused:
        return _("Paused"), GREY, _("Syncing is paused for all folders.")
    if st is None:
        return _("Starting…"), GREY, ""
    if st.problem:
        return _("Not syncing"), RED, st.problem
    r = st.replica
    if r is None:
        return _("Starting…"), GREY, ""
    if r.deleted_at is not None:
        return _("Deleted by the owner"), GREY, _("The owner deleted this project for everyone. The files in this folder stay, but nothing syncs any more: use Leave… to take it off the list.")
    if r.denied == "update-needed":
        return _("Update needed"), RED, _("This project's server needs TeXLoom {version} or later. Update the app (Help ▸ Check for updates…).", version=r.min_version or "?")
    if r.denied:
        return _("Not syncing"), RED, (_("This server is private, and this project's creation code has expired or been removed. Renewing it restores access.")
                                       if r.denied == "code-expired" else
                                       _("This server is private, and this project needs a creation code: add it to the folder's .texloom/config.json as \"create_code\" and restart the app."))
    if not r.connected.is_set():
        return _("Offline"), AMBER, _("Can't reach the server. Changes here are kept and sent when it's back.")
    if not r.synced.is_set():
        return _("Catching up…"), AMBER, ""
    if r.refused:
        return _("Change refused"), RED, _("The server refused a change from this folder: {reason}", reason=r.refused)
    if r.store_problem and r.storage != "relay":
        why = {
            "needs-code": _("This server keeps projects only with a creation code, and this one has none. Its owner can add one, or make it sync-only."),
            "code-expired": _("This project's creation code has expired: renewing it restores storage."),
            "quota-full": _("The storage allowance of this project's creation code is used up."),
            "project-full": _("This project has reached the server's size limit."),
            "server-full": _("The server is out of space: changes are kept here and retried."),
        }.get(r.store_problem, r.store_problem)
        return _("Not saved on the server"), AMBER, why + " " + _("Collaborators who are online still get your changes.")
    pending = r.pending_count()
    if pending:
        return ngettext(pending, "Sending {n} change…", "Sending {n} changes…", n=pending), AMBER, ""
    if not r.keys.writable:
        return _("Up to date (read-only)"), GREEN, _("This link can read but not change the project: edits here aren't shared.")
    if r.storage == "relay":
        return _("Up to date (sync-only)"), GREEN, _("The server keeps no copy of this project: changes and files reach your other "
                                                    "computers and collaborators while you're online at the same time.")
    return _("Up to date"), GREEN, ""


def project_name(d: Path, engine: Engine) -> str:
    return engine.names.get(d) or d.name


@functools.cache
def dot(colour: str) -> QtGui.QIcon:
    pm = QtGui.QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(colour))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QtGui.QIcon(pm)


# Each tag colour for light and dark windows: the same as .tc-* in web/src/style.css.
TAG_HEX = {
    "red": ("#b3261e", "#f28b82"), "orange": ("#b4530a", "#f6ad6b"), "yellow": ("#8a6a00", "#e6c35c"), "green": ("#2e7d32", "#81c995"),
    "teal": ("#00796b", "#6fd0c0"), "blue": ("#1565c0", "#8ab4f8"), "indigo": ("#3f51b5", "#a5b0f5"), "purple": ("#7b1fa2", "#d7aefb"),
    "pink": ("#c2185b", "#f48fb1"), "grey": ("#5f6b76", "#aab4be"),
}
TAGS_ROLE = Qt.UserRole + 1  # a project row's tags, for ProjectRows to draw


def tag_hex(colour: str) -> str:
    return TAG_HEX[colour][QtWidgets.QApplication.palette().color(QtGui.QPalette.Window).lightness() < 128]


def colour_names() -> dict[str, str]:
    return {"red": _("Red"), "orange": _("Orange"), "yellow": _("Yellow"), "green": _("Green"), "teal": _("Teal"),
            "blue": _("Blue"), "indigo": _("Indigo"), "purple": _("Purple"), "pink": _("Pink"), "grey": _("Grey")}


def browser_link(d: Path) -> str:
    """A folder's project link for opening in the browser, marked as opened by this app, so the web
    app doesn't ask where the files should live: they're in this folder already."""
    cfg = load_config(d)
    page, _hash, keys = cfg["link"].partition("#")
    mark = "from=app" + ("&compile=1" if cfg.get("compile") else "")  # the web app then compiles here by default
    return f"{page}{'&' if '?' in page else '?'}{mark}#{keys}"


def open_path(p: Path | str) -> None:
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(p)))


def slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:60] or "project"


def default_parent() -> Path:
    return Path.home() / "TeXLoom"


def folder_of(project: str) -> Path | None:
    """The folder on this computer that already syncs this project, if any."""
    for d in registry.folders():
        try:
            if Keys.from_link(load_config(d)["link"])[0].project == project:
                return d
        except Exception:  # noqa: BLE001 - a folder that has gone, or isn't set up
            continue
    return None


def known_server() -> str:
    """Where a new project or list goes by default: the server last used for one, else that of this
    computer's projects, else TeXLoom's own."""
    if last := listsync.last_server():
        return last
    for d in registry.folders():
        try:
            return Keys.from_link(load_config(d)["link"])[1]
        except Exception:  # noqa: BLE001
            continue
    return DEFAULT_SERVER


def short_path(d: Path) -> str:
    """A folder as people read it: ~/Papers/thesis, and the middle left out if it's long."""
    try:
        text = str(Path("~") / d.relative_to(Path.home()))
    except ValueError:
        text = str(d)
    return text if len(text) <= 72 else text[:30] + "…" + text[-40:]
