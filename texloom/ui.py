"""What the desktop app's windows and dialogs share: the sync engine's thread, a folder's
status in words and colours, and small widgets and helpers."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync
from .cli import run
from .config import linked_folders
from .daemon import Daemon
from .i18n import _, ngettext

log = logging.getLogger("texloom")

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


def over(parent: QtWidgets.QWidget | None) -> QtWidgets.QWidget | None:
    """Where a dialog asked for from `parent` opens: asked for from the main window while another
    dialog is open over it (Join… in the project list), on top of that one, since a sheet would
    open under it and be blocked by it."""
    if isinstance(parent, QtWidgets.QMainWindow) and (top := QtWidgets.QApplication.activeModalWidget()) is not None:
        return top
    return parent


def attach(dlg: QtWidgets.QDialog) -> QtWidgets.QDialog:
    """`dlg` tied to the window it was opened from, blocking only that window: Qt shows it as a
    sheet sliding from the window on macOS and as an ordinary dialog elsewhere."""
    parent = dlg.parentWidget()
    if parent is not None and parent.isVisible():
        dlg.setWindowModality(Qt.WindowModal)
    return dlg


class Dialog(QtWidgets.QDialog):
    """A dialog tied to the window it was opened from (see attach)."""

    def __init__(self, parent=None) -> None:
        super().__init__(over(parent))
        if isinstance(self.parentWidget(), QtWidgets.QMainWindow):
            attach(self)


def message(parent, icon: QtWidgets.QMessageBox.Icon, title: str, text: str,
            buttons=QtWidgets.QMessageBox.Ok, default=QtWidgets.QMessageBox.NoButton) -> QtWidgets.QMessageBox.StandardButton:
    """A message box tied to its window (a sheet on macOS), the button pressed. The title shows
    only where the system puts one on message boxes (not macOS): the text must stand alone."""
    box = QtWidgets.QMessageBox(icon, title, text, buttons, over(parent))
    if default != QtWidgets.QMessageBox.NoButton:
        box.setDefaultButton(default)
    return QtWidgets.QMessageBox.StandardButton(attach(box).exec())


def ask(parent, title: str, text: str, safe: bool = False) -> bool:
    """A yes/no question; `safe`: No is the default answer (Return doesn't say yes)."""
    yes, no = QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No
    return message(parent, QtWidgets.QMessageBox.Question, title, text, yes | no, no if safe else yes) == yes


def choose(parent, icon: QtWidgets.QMessageBox.Icon, title: str, text: str, act: str, cancel: str) -> bool:
    """A question answered with `act` or `cancel` (the default, and Escape): whether `act` was chosen."""
    box = QtWidgets.QMessageBox(icon, title, text, QtWidgets.QMessageBox.NoButton, over(parent))
    yes = box.addButton(act, QtWidgets.QMessageBox.AcceptRole)
    no = box.addButton(cancel, QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    attach(box).exec()
    return box.clickedButton() is yes


def warn(parent, title: str, text: str) -> None:
    message(parent, QtWidgets.QMessageBox.Warning, title, text)


def inform(parent, title: str, text: str) -> None:
    message(parent, QtWidgets.QMessageBox.Information, title, text)


class Disclosure(QtWidgets.QPushButton):
    """A flat button with a chevron that shows or hides part of a window (› closed, ⌄ open): text
    alone on every system (a tool button draws a frame on macOS). It isn't Qt-checkable: a
    checked button draws pressed in."""

    opened = QtCore.Signal(bool)

    def __init__(self, text: str = "", is_open: bool = False, bold: bool = False) -> None:
        super().__init__()
        self.setFlat(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Fixed)  # at the left, as a heading
        self.setText(text)
        if bold:
            f = self.font()
            f.setBold(True)
            self.setFont(f)
        self._open = is_open
        self.clicked.connect(lambda: self.set_open(not self._open))
        self._chevron()

    def is_open(self) -> bool:
        return self._open

    def set_open(self, on: bool) -> None:
        if on != self._open:
            self._open = on
            self._chevron()
            self.opened.emit(on)

    def _chevron(self) -> None:
        self.setIcon(chevron(self, down=self._open))
        h = self.fontMetrics().height()
        self.setIconSize(QtCore.QSize(h, h))

    def changeEvent(self, e: QtCore.QEvent) -> None:
        super().changeEvent(e)
        if e.type() in (QtCore.QEvent.PaletteChange, QtCore.QEvent.ApplicationPaletteChange):
            self._chevron()  # drawn in the text colour: light or dark mode changed


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


_bridge: Bridge | None = None  # in_background's way back to the Qt thread


def in_background(work, done) -> None:
    """Run `work()` on a thread of its own (the network, password hashing: slow), so the window
    stays responsive; then `done(result, error)` on the Qt thread. Called from the Qt thread. Needs
    no window or Engine (the project list's dialog is also used without them)."""
    global _bridge
    if _bridge is None:
        _bridge = Bridge()  # made here, on the Qt thread, so that it calls back there
    bridge = _bridge

    def run() -> None:
        try:
            result, error = work(), None
        except Exception as e:  # noqa: BLE001 - handed to done(), which says what went wrong
            result, error = None, e
        bridge.invoke(lambda: done(result, error))

    threading.Thread(target=run, daemon=True).start()


# ---- what a folder is doing ---------------------------------------------------------------
GREEN, AMBER, RED, GREY = "#2e7d32", "#b26a00", "#b3261e", "#75736c"
ON_DARK = {GREEN: "#81c995", AMBER: "#f6ad6b", RED: "#f28b82"}  # the same, readable on a dark window


def dark() -> bool:
    return QtWidgets.QApplication.palette().color(QtGui.QPalette.Window).lightness() < 128


def shade(colour: str) -> str:
    """`colour` as it shows in the current light or dark mode: GREY is the system's text colour
    faded towards the window's (secondary text), the others lighter on a dark window."""
    if colour == GREY:
        pal = QtWidgets.QApplication.palette()
        text, window = pal.color(QtGui.QPalette.WindowText), pal.color(QtGui.QPalette.Window)
        mix = [round(a * 0.6 + b * 0.4) for a, b in zip(text.getRgb()[:3], window.getRgb()[:3])]
        return QtGui.QColor(*mix).name()
    return ON_DARK.get(colour, colour) if dark() else colour


def tint(w: QtWidgets.QWidget, colour: str) -> QtWidgets.QWidget:
    """`w`'s text in `colour` (shade), kept right when light or dark mode changes (Tints), through
    its palette: a style sheet would have Qt draw the widget itself instead of the system."""
    w.setProperty("texloom_tint", colour)
    pal = w.palette()
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(shade(colour)))
    w.setPalette(pal)
    return w


class Tints(QtCore.QObject):
    """Installed on the application: tints widgets again when light or dark mode changes."""

    def eventFilter(self, obj: QtCore.QObject, e: QtCore.QEvent) -> bool:
        if e.type() == QtCore.QEvent.ApplicationPaletteChange and isinstance(obj, QtWidgets.QWidget) \
                and (colour := obj.property("texloom_tint")):
            tint(obj, colour)
        return False


def note(text: str, colour: str | None = GREY) -> QtWidgets.QLabel:
    """A label that wraps, in `colour` (None: the window's text colour)."""
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    if colour:
        tint(label, colour)
    return label


def heading(text: str, wrap: bool = False) -> QtWidgets.QLabel:
    """A label in bold, over a part of a window or dialog."""
    label = QtWidgets.QLabel(text)
    label.setWordWrap(wrap)
    f = label.font()
    f.setBold(True)
    label.setFont(f)
    return label


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
    return f"<span style='color:{shade(RED)}'>{text}</span>"


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
    turns = st.turns
    if turns is not None and turns.shared and not turns.holding:
        who = turns.holder
        return _("Standing by"), GREEN, (
            _("{name} keeps this folder in step. The folder is shared with that computer through a file-sync service "
              "(Dropbox, iCloud…), which brings its changes here; this computer takes over if it stops.", name=who) if who else
            _("This folder is shared through a file-sync service (Dropbox, iCloud…) with another computer that runs TeXLoom. "
              "One computer at a time keeps it in step; this one is waiting to hear from the others."))
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


def open_path(p: Path | str) -> None:
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(p)))


def slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:60] or "project"


def default_parent() -> Path:
    return Path.home() / "TeXLoom"


def folder_of(project: str) -> Path | None:
    """The folder on this computer that already syncs this project, if any."""
    return next((d for d, keys, _server in linked_folders() if keys.project == project), None)


def known_server() -> str:
    """Where a new project or list goes by default: the server last used for one, else that of this
    computer's projects, else TeXLoom's own."""
    if last := listsync.last_server():
        return last
    return next((server for _d, _keys, server in linked_folders()), DEFAULT_SERVER)


def server_choices(first: str = "") -> list[str]:
    """The servers to offer for a new project: `first` (else the one it goes to by default), then
    the others this computer knows."""
    first = (first or known_server()).rstrip("/")
    return [first, *[s for s in listsync.known_servers() if s != first]]


KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3


def move_refusal_text(no: dict) -> str:
    """Why the server didn't move a project to another creation code (Replica.move_to_code), as the
    web app says it (web/src/movecode.ts)."""
    code = no.get("code")
    if code == "move-bad-code":
        return _("That code isn’t valid on this server, or has expired.")
    if code == "move-no-room":
        return _("That code hasn’t room for this project: it needs {need}, and the code has {free} free.",
                 need=size_text(no.get("need") or 0), free=size_text(no.get("free") or 0))
    if code == "move-too-big":
        return _("This project is larger than a project may be with that code ({max}).", max=size_text(no.get("max") or 0))
    if code == "move-not-stored":
        return _("The server doesn’t keep a copy of this project, so there’s nothing to move.")
    if code == "move-rejected":
        return _("The server refused: only the project’s owner can move it.")
    return _("Too many tries with wrong codes. Quit and reopen TeXLoom to try again.")


def size_text(n: float) -> str:
    """A size for people: KB, MB or GB, with the language's decimal mark and unit names."""
    from .i18n import lang

    def num(x: float, digits: int) -> str:
        t = f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{x:.0f}"
        return t.replace(".", ",") if lang() in ("fr", "de", "es", "pt") else t
    if n >= GB:
        return _("{n} GB", n=num(n / GB, 1))
    if n >= MB:
        return _("{n} MB", n=num(n / MB, 0 if n >= 100 * MB else 1))
    return _("{n} KB", n=num(max(n, KB if n > 0 else 0) / KB, 0))


def usage_text(info: dict | None, storage: str) -> str:
    """How much the server keeps of a project, and of its creation code's allowance when this
    computer holds the code (the web app's usage.ts says the same)."""
    if not info or storage == "relay" or not info.get("registered"):
        return ""
    used, cap = info.get("used") or 0, info.get("projectMax")
    lines = [_("This project takes {used} on the server (up to {max} per project).", used=size_text(used), max=size_text(cap))
             if cap else _("This project takes {used} on the server.", used=size_text(used))]
    total, mine = info.get("allowance"), info.get("ownerUsed")
    if total is not None and mine is not None:
        lines.append(_("Your creation code: {used} of {total} used, {left} left (all its projects and project lists).",
                       used=size_text(mine), total=size_text(total), left=size_text(max(0, total - mine))))
    return "\n".join(lines)


def short_path(d: Path) -> str:
    """A folder as people read it: ~/Papers/thesis, and the middle left out if it's long."""
    try:
        text = str(Path("~") / d.relative_to(Path.home()))
    except ValueError:
        text = str(d)
    return text if len(text) <= 72 else text[:30] + "…" + text[-40:]
