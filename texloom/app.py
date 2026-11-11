"""TeXLoom desktop app: the folders this computer keeps in sync, in a window and a tray icon.

It runs the same sync as `texloom start` on a background thread (a plain asyncio loop);
the Qt window lives on the main thread and only reads the folders' state or hands work
to that loop. Opening a `texloom://join?link=…` URL (from the web app's "Open in the
TeXLoom app") shows the join dialog with the link filled in; nothing is joined without
confirming there.

Install with `uv tool install 'texloom[app]'` and run `texloom app`, or download a build.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import getpass
import html
import json
import logging
import platform
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import __version__, autostart, listsync, registry, tags
from .cli import DEFAULT_CONFIG, clone_project, load, load_config, new_project, peek, run, update_config
from .crypto import Keys
from .daemon import CURRENT, FOLDER, Daemon, FolderProblem, FolderTag
from .folder import copy_project_files
from .paths import main_file, tex_env
from .i18n import _, app_pref, lang_pref, ngettext, set_app_pref, set_lang_pref
from .locales import NAMES

log = logging.getLogger("texloom")
ICON = Path(__file__).with_name("assets") / "icon.png"
SOCKET = f"texloom-app-{re.sub(r'[^A-Za-z0-9]', '', getpass.getuser())}"
SCHEME = "texloom"
MAC = sys.platform == "darwin"
DEFAULT_SERVER = "https://app.texloom.org"
HELP_URL = "https://github.com/jhudsy/texloom#readme"


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
        super().__init__(parent)
        if MAC and isinstance(parent, QtWidgets.QMainWindow):
            self.setWindowModality(Qt.WindowModal)


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
        fut.add_done_callback(lambda f: bridge.invoke(
            lambda: done(None if f.exception() else f.result(), f.exception())))


class Bridge(QtCore.QObject):
    """Runs callables on the Qt thread (signals cross threads safely)."""
    call = QtCore.Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(lambda fn: fn(), Qt.QueuedConnection)

    def invoke(self, fn) -> None:
        self.call.emit(fn)


bridge: Bridge  # created once the QApplication exists


class Activity(logging.Handler):
    """Keeps recent log lines per folder (and general ones) for the window."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=300))
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        st = CURRENT.get()
        key = str(st.path) if st else ""
        self.lines[key].append(self.format(record))


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
    if r.store_problem:
        why = {
            "needs-code": _("This server needs a creation code to store new projects: add it to the folder's .texloom/config.json as \"create_code\" and restart the app."),
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
    return _("Up to date"), GREEN, ""


def project_name(d: Path, engine: Engine) -> str:
    return engine.names.get(d) or d.name


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


class ProjectRows(QtWidgets.QStyledItemDelegate):
    """A project's row, with its tags drawn as coloured pills on the last line of its text."""

    PADDING = 7

    def with_blank_line(self, option, index) -> QtWidgets.QStyleOptionViewItem:
        """The row's text with its last line, the tags, left blank for the pills."""
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = "\u2028".join([*index.data(Qt.DisplayRole).split("\n")[:-1], " "])  # Qt's own line break in a delegate
        return opt

    def sizeHint(self, option, index) -> QtCore.QSize:
        if not index.data(TAGS_ROLE):
            return super().sizeHint(option, index)
        opt = self.with_blank_line(option, index)  # so that long tags don't widen the list: they're cut off to fit instead
        return opt.widget.style().sizeFromContents(QtWidgets.QStyle.CT_ItemViewItem, opt, QtCore.QSize(), opt.widget) + QtCore.QSize(0, 6)

    def paint(self, painter, option, index) -> None:
        own = index.data(TAGS_ROLE)
        if not own:
            super().paint(painter, option, index)
            return
        opt = self.with_blank_line(option, index)
        style = opt.widget.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        area = style.subElementRect(QtWidgets.QStyle.SE_ItemViewItemText, opt, opt.widget)
        fm, line = opt.fontMetrics, opt.fontMetrics.lineSpacing()
        top = area.center().y() + line // 2
        text = opt.palette.color(QtGui.QPalette.HighlightedText if opt.state & QtWidgets.QStyle.State_Selected else QtGui.QPalette.Text)
        x, right, height = area.left(), min(area.right(), opt.widget.viewport().rect().right() - 4), fm.height() + 2
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        for i, tag in enumerate(own):
            label, width = tag.name, fm.horizontalAdvance(tag.name) + 2 * self.PADDING
            more = f"+{len(own) - i}"
            if x + width > right - (fm.horizontalAdvance(more) + 6 if i < len(own) - 1 else 0):
                if i:
                    painter.setPen(text)
                    painter.drawText(QtCore.QRect(x, top, right - x, height), Qt.AlignLeft | Qt.AlignVCenter, more)
                    break
                width = right - x
                label = fm.elidedText(tag.name, Qt.ElideRight, width - 2 * self.PADDING)
            colour = QtGui.QColor(tag_hex(tag.colour))
            colour.setAlphaF(0.7)
            painter.setPen(colour)
            colour.setAlphaF(0.26)
            painter.setBrush(colour)
            pill = QtCore.QRectF(x + 0.5, top + 0.5, width - 1, height - 1)
            painter.drawRoundedRect(pill, height / 2, height / 2)
            painter.setPen(text)
            painter.drawText(pill, Qt.AlignCenter, label)
            x += width + 4
        painter.restore()


def open_path(p: Path | str) -> None:
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(p)))


def slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:60] or "project"


def default_parent() -> Path:
    return Path.home() / "TeXLoom"


def sync_link_from_url(url: str) -> str | None:
    """The sync link in a texloom://sync?link=… URL (sent by the web page that unlocks a list)."""
    u = urlparse(url)
    if u.scheme != SCHEME or u.netloc != "sync":
        return None
    link = (parse_qs(u.query).get("link") or [""])[0]
    return link if link.startswith(("https://", "http://")) and "#sync=" in link else None


def link_from_url(url: str) -> str | None:
    """The project link in a texloom://join?link=… URL."""
    u = urlparse(url)
    if u.scheme != SCHEME:
        return None
    link = (parse_qs(u.query).get("link") or [""])[0]
    return link if link.startswith(("https://", "http://")) and "#" in link else None


async def program_lines(engine: Engine, d: Path) -> tuple[list, set[str]]:
    """Lines in a folder's project that ask to run programs (shell escape), and those allowed."""
    from .cli import STATE, load
    from .paths import safe_rel
    from .replica import Replica
    from .scan import allowed, scan
    st = engine.daemon.states.get(d)
    r = st.replica if st and st.replica is not None else Replica(d / STATE, *load(d))
    found = [f for f in scan({p: str(t) for p, t in r.files.items() if safe_rel(p)}) if f.kind == "shell"]
    return found, allowed(d)


# ---- dialogs ----------------------------------------------------------------------------
class ProgramsDialog(Dialog):
    """`texloom allow` in a window: see the lines that run programs, allow exactly those."""

    def __init__(self, engine: Engine, d: Path, parent=None) -> None:
        super().__init__(parent)
        self.engine, self.d, self.found = engine, d, []
        self.setWindowTitle(_("Lines that run programs"))
        self.setMinimumSize(640, 360)
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(_("With “Anything the document asks”, these lines run programs on this computer when "
                                   "it compiles. Only lines you allow here run; a new or changed line waits for you "
                                   "(that compile runs without it, and the log says so)."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.view = QtWidgets.QTreeWidget()
        self.view.setHeaderLabels(["", _("Where"), _("What"), _("Line")])
        self.view.setRootIsDecorated(False)
        lay.addWidget(self.view, 1)
        warn = QtWidgets.QLabel(_("Anyone with an edit link can change these lines, and TeX macros can hide a command "
                                  "from this list, so allow this only for projects whose editors you trust."))
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {AMBER}")
        lay.addWidget(warn)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.ok = bb.addButton(_("Allow these lines"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.allow)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        engine.submit(program_lines(engine, d), self.loaded)

    def loaded(self, res, err) -> None:
        if err:
            self.view.addTopLevelItem(QtWidgets.QTreeWidgetItem(["", "", _("Couldn’t read the project: {err}", err=err), ""]))
            return
        self.found, allowed_keys = res
        for f in self.found:
            new = f.key not in allowed_keys
            it = QtWidgets.QTreeWidgetItem([_("new") if new else _("allowed"), f"{f.path}:{f.line}", program_what(f.what), f.text])
            if new:
                it.setForeground(0, QtGui.QColor(AMBER))
            self.view.addTopLevelItem(it)
        if not self.found:
            self.view.addTopLevelItem(QtWidgets.QTreeWidgetItem(["", "", _("No lines in this project ask to run programs."), ""]))
        for i in range(3):
            self.view.resizeColumnToContents(i)
        self.ok.setEnabled(bool(self.found))

    def allow(self) -> None:
        from .scan import allow
        allow(self.d, {f.key for f in self.found})
        self.engine.call(self.engine.daemon.restart, self.d)  # the next compile uses them
        self.accept()


def issued_file(d: Path) -> Path:
    return d / ".texloom" / "issued.json"


def load_issued(d: Path) -> dict:
    """Personal links made from this folder's app, kept so they can be copied again (owner only)."""
    try:
        return json.loads(issued_file(d).read_text())
    except (OSError, ValueError):
        return {}


async def create_personal(engine: Engine, d: Path, name: str) -> str:
    """Owner only: a personal edit link for `name`, recorded in the project like the web app does."""
    st = engine.daemon.states.get(d)
    r = st.replica if st else None
    if r is None or not r.connected.is_set():
        raise FolderProblem(_("this folder isn't connected right now: try again in a moment"))
    pub, grant, personal = r.keys.personal(name)
    at = int(time.time() * 1000)
    me = load_config(d).get("name") or f"TeXLoom app on {platform.node().split('.')[0]}"
    r.local_change(lambda: r.members.__setitem__(pub, {"name": personal.member[2], "at": at, "by": me, "g": grant}))
    link = personal.link(r.server)
    issued = {**load_issued(d), pub: {"name": personal.member[2], "at": at, "link": link}}
    fd = os.open(issued_file(d), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # these links are keys
    with os.fdopen(fd, "w") as f:
        json.dump(issued, f, indent=2)
    return link


def mailto(link: str, project: str, who: str) -> QtCore.QUrl:
    body = _("Hi {who},\n\nYou’re invited to edit “{project}” on TeXLoom. Open this link in your browser:\n\n{link}\n\n"
             "This link is for you only: please don’t forward it. Anyone who has it can read the project and change it as you.",
             who=who, project=project, link=link)
    subject = _("Invitation to edit “{project}” on TeXLoom", project=project)
    return QtCore.QUrl(f"mailto:?subject={quote(subject)}&body={quote(body)}")


class LinksDialog(Dialog):
    """This folder's links: read-only, your own edit link, and (owners) edit links for others."""

    def __init__(self, d: Path, parent=None, engine: Engine | None = None, project: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Links"))
        self.setMinimumWidth(600)
        keys, server = Keys.from_link(load_config(d)["link"])
        lay = QtWidgets.QVBoxLayout(self)

        def row(label: str, text: str, note: str = "") -> None:
            lay.addWidget(QtWidgets.QLabel(f"<b>{label}</b>"))
            r = QtWidgets.QHBoxLayout()
            field = QtWidgets.QLineEdit(text)
            field.setReadOnly(True)
            field.setCursorPosition(0)
            copy = QtWidgets.QPushButton(_("Copy"))
            copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(text), copy.setText(_("Copied"))))
            r.addWidget(field, 1)
            r.addWidget(copy)
            lay.addLayout(r)
            if note:
                n = QtWidgets.QLabel(note)
                n.setWordWrap(True)
                n.setStyleSheet(f"color: {GREY}")
                lay.addWidget(n)

        row(_("Read-only link"), keys.link(server, write=False), _("Anyone with it can read the project."))
        if keys.seed:
            row(_("Your owner link"), keys.link(server, write=True),
                _("Full control, including making and revoking people’s links: keep it to yourself."))
            lay.addWidget(QtWidgets.QLabel(f"<b>{_('Edit links for others')}</b>"))
            hint = QtWidgets.QLabel(_("Each person (or group) gets their own link, so you can later revoke one without "
                                      "affecting the others (revoking is in the web app: Open in browser → Share)."))
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {GREY}")
            lay.addWidget(hint)
            make = QtWidgets.QHBoxLayout()
            who = QtWidgets.QLineEdit()
            who.setPlaceholderText(_("Name, e.g. Alice, or Lab group"))
            create = QtWidgets.QPushButton(_("Create link"))
            make.addWidget(who, 1)
            make.addWidget(create)
            lay.addLayout(make)
            made = QtWidgets.QVBoxLayout()
            lay.addLayout(made)

            def show(name: str, link: str) -> None:
                r = QtWidgets.QHBoxLayout()
                r.addWidget(QtWidgets.QLabel(name), 1)
                c = QtWidgets.QPushButton(_("Copy link"))
                c.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(link), c.setText(_("Copied"))))
                m = QtWidgets.QPushButton(_("Email…"))
                m.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(mailto(link, project or d.name, name)))
                r.addWidget(c)
                r.addWidget(m)
                made.addLayout(r)

            for v in sorted(load_issued(d).values(), key=lambda v: v.get("at", 0)):
                if isinstance(v, dict) and isinstance(v.get("link"), str):
                    show(str(v.get("name", "?")), v["link"])

            def go() -> None:
                if not who.text().strip() or engine is None:
                    return who.setFocus()
                create.setEnabled(False)
                engine.submit(create_personal(engine, d, who.text()), lambda link, err: done(who.text().strip(), link, err))

            def done(name: str, link, err) -> None:
                create.setEnabled(True)
                if err:
                    QtWidgets.QMessageBox.warning(self, _("Create link"), _("Couldn’t create it: {err}", err=err))
                    return
                who.clear()
                show(name[:60], link)

            create.clicked.connect(go)
            who.returnPressed.connect(go)
        elif keys.member:
            row(_("Your personal edit link (as {name})", name=keys.member[2]), keys.link(server, write=True),
                _("Yours only: don’t pass it on. For others, ask the project’s owner for a link."))
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

class JoinDialog(Dialog):
    """Paste a link → see what it is → choose a folder → join."""

    def __init__(self, engine: Engine, parent=None, link: str = "", from_web: bool = False) -> None:
        super().__init__(parent)
        self.engine, self.info = engine, None
        self.setWindowTitle(_("Join a project"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QVBoxLayout(self)
        if from_web:
            warn = QtWidgets.QLabel(_("A web page asked TeXLoom to add this project. Join only if you expected it."))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {AMBER}")
            lay.addWidget(warn)
        lay.addWidget(QtWidgets.QLabel(_("Project link (from the Share dialog, or an email):")))
        row = QtWidgets.QHBoxLayout()
        self.link = QtWidgets.QLineEdit(link)
        self.link.setPlaceholderText("https://…/#p=…")
        self.check = QtWidgets.QPushButton(_("Check"))
        self.check.clicked.connect(self.do_peek)
        row.addWidget(self.link, 1)
        row.addWidget(self.check)
        lay.addLayout(row)
        self.what = QtWidgets.QLabel("")
        self.what.setWordWrap(True)
        lay.addWidget(self.what)
        self.already = QtWidgets.QLabel("")
        self.already.setWordWrap(True)
        self.already.setTextFormat(Qt.PlainText)
        self.already.setStyleSheet(f"color: {AMBER}")
        self.already.hide()
        lay.addWidget(self.already)
        self.existing: Path | None = None
        lay.addWidget(QtWidgets.QLabel(_("Folder on this computer:")))
        frow = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit("")
        choose = QtWidgets.QPushButton(_("Choose…"))
        choose.clicked.connect(self.choose)
        frow.addWidget(self.folder, 1)
        frow.addWidget(choose)
        lay.addLayout(frow)
        self.compile = QtWidgets.QCheckBox(_("Compile with my own TeX (latexmk) when files change"))
        lay.addWidget(self.compile)
        note = QtWidgets.QLabel(_("Collaborators’ files will be written to this folder. TeXLoom won’t write files that "
                                  "LaTeX tools run as programs, but compile it with your own tools only if you trust "
                                  "everyone who can edit the project."))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {GREY}")
        lay.addWidget(note)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.join = self.buttons.addButton(_("Join"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.join.setEnabled(False)
        self.join.clicked.connect(self.do_join)
        self.show_existing = self.buttons.addButton(_("Show folder"), QtWidgets.QDialogButtonBox.ActionRole)
        self.show_existing.hide()
        self.show_existing.clicked.connect(self.reveal_existing)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.link.textChanged.connect(lambda: (self.join.setEnabled(False), self.what.setText(""), self.mark_existing(None)))
        if link:
            QtCore.QTimer.singleShot(0, self.do_peek)

    def choose(self) -> None:
        start = str(Path(self.folder.text()).parent if self.folder.text() else default_parent())
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("Put the project in…"), start)
        if d:
            name = Path(self.folder.text()).name if self.folder.text() else slug(self.info["name"] if self.info else "project")
            self.folder.setText(str(Path(d) / name))

    def mark_existing(self, d: Path | None) -> None:
        """Say if this project already has a folder here. A second copy stays possible, but isn't the default."""
        self.existing = d
        self.already.setText(_("Already syncing in {d}.", d=d) if d else "")
        self.already.setVisible(d is not None)
        self.show_existing.setVisible(d is not None)
        self.join.setText(_("Add another copy") if d else _("Join"))
        self.join.setDefault(d is None)
        self.show_existing.setDefault(d is not None)

    def reveal_existing(self) -> None:
        open_path(self.existing)
        self.accept()

    def do_peek(self) -> None:
        link = self.link.text().strip()
        try:
            Keys.from_link(link)
        except Exception as e:  # noqa: BLE001
            self.what.setText(red(_("That isn't a TeXLoom project link ({e}).", e=e)))
            return
        self.what.setText(_("Looking it up…"))
        self.check.setEnabled(False)
        self.engine.submit(peek(link), self.peeked)

    def peeked(self, info, err) -> None:
        self.check.setEnabled(True)
        if err:
            self.what.setText(red(str(err)))
            return
        self.info = info
        access = ((_("edit access as {member}", member=info["member"]) if info["member"] else _("edit access"))
                  if info["writable"] else _("read-only"))
        self.what.setText(ngettext(info["files"], "<b>“{name}”</b> — {access}, {n} file(s), on {server}",
                                   "<b>“{name}”</b> — {access}, {n} file(s), on {server}",
                                   name=info["name"] or _("Untitled"), access=access, n=info["files"], server=info["server"]))
        if not self.folder.text():
            self.folder.setText(str(default_parent() / slug(info["name"] or "project")))
        self.join.setEnabled(True)
        self.mark_existing(folder_of(Keys.from_link(self.link.text().strip())[0].project))

    def do_join(self) -> None:
        d = Path(self.folder.text()).expanduser()
        if not d.is_absolute():
            d = default_parent() / d
        if d.exists() and (not d.is_dir() or any(d.iterdir())):
            QtWidgets.QMessageBox.warning(self, _("Join"), _("{d} isn't empty. Choose a new or empty folder.", d=d))
            return
        self.join.setEnabled(False)
        self.what.setText(_("Downloading the project…"))
        link, compile_ = self.link.text().strip(), self.compile.isChecked()

        async def go():
            await clone_project(link, d)
            if compile_:
                update_config(d, compile=True)

        self.engine.submit(go(), lambda _r, err: self.joined(d, err))

    def joined(self, d: Path, err) -> None:
        if err:
            self.what.setText(red(_("Couldn’t join: {err}", err=err)))
            self.join.setEnabled(True)
            return
        self.engine.call(self.engine.daemon.poke)
        self.accept()


class ShareFolderDialog(Dialog):
    """Share an existing folder as a new project (`texloom new`)."""

    def __init__(self, engine: Engine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle(_("New project from a folder"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)  # macOS keeps fields narrow otherwise
        intro = QtWidgets.QLabel(_("The folder’s files are uploaded, encrypted, and the folder stays in step with the "
                                   "project from then on. You can invite people once it’s made."))
        intro.setWordWrap(True)
        lay.addRow(intro)
        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit()
        choose = QtWidgets.QPushButton(_("Choose…"))
        choose.clicked.connect(self.choose)
        row.addWidget(self.folder, 1)
        row.addWidget(choose)
        lay.addRow(_("Folder"), row)
        self.name = QtWidgets.QLineEdit()
        lay.addRow(_("Project name"), self.name)
        self.server = QtWidgets.QLineEdit(known_server())
        self.server.setPlaceholderText("https://your.server")
        lay.addRow(_("Server"), self.server)
        self.code = QtWidgets.QLineEdit()
        self.code.setPlaceholderText(_("only if the server asks for one"))
        lay.addRow(_("Creation code"), self.code)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addRow(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.go = bb.addButton(_("Create project"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.clicked.connect(self.do_share)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def choose(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("Folder for the new project"), str(Path.home()))
        if d:
            self.folder.setText(d)
            if not self.name.text():
                self.name.setText(Path(d).name)

    def do_share(self) -> None:
        d, server = Path(self.folder.text()).expanduser(), self.server.text().strip().rstrip("/")
        if not d.is_dir():
            self.msg.setText(red(_("Choose an existing folder.")))
            return
        if not server.startswith(("https://", "http://")):
            self.msg.setText(red(_("The server address starts with https://")))
            return
        self.go.setEnabled(False)
        self.msg.setText(_("Uploading…"))
        self.engine.submit(new_project(d, server, self.name.text().strip(), self.code.text().strip()),
                           lambda keys, err: self.done_(err))

    def done_(self, err) -> None:
        if err:
            self.msg.setText(red(_("Couldn’t create the project: {err}", err=err)))
            self.go.setEnabled(True)
            return
        self.engine.call(self.engine.daemon.poke)
        self.accept()


class ProjectListDialog(QtWidgets.QDialog):
    """The encrypted project list: set it up or unlock it, then join projects from it."""

    work_done = QtCore.Signal(object, object, object)  # what to do next, its result, or the error

    def __init__(self, window, link: str = "", from_web: bool = False) -> None:
        super().__init__(window)
        self.work_done.connect(self.after_work)
        self.window_ = window
        self.setWindowTitle(_("Project list"))
        self.setMinimumWidth(600)
        lay = QtWidgets.QVBoxLayout(self)
        if from_web:
            warn = QtWidgets.QLabel(_("A web page asked TeXLoom to use this project list. Your folders would be added to it, and anyone who has this link could open them. Use it only if the link is yours."))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {AMBER}")
            lay.addWidget(warn)
        intro = QtWidgets.QLabel(_("Your project list follows you to your other computers and browsers. It is kept on the "
                                   "server, encrypted, and opened by a sync link that only you have."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.pages = QtWidgets.QStackedWidget()
        lay.addWidget(self.pages, 1)

        # not set up on this computer: unlock a list you have, or start one
        setup = QtWidgets.QWidget()
        s = QtWidgets.QVBoxLayout(setup)
        s.setContentsMargins(0, 8, 0, 0)
        s.addWidget(self.heading(_("Already have a list?")))
        row = QtWidgets.QHBoxLayout()
        passkey = QtWidgets.QPushButton(_("Unlock with a passkey…"))
        passkey.setToolTip(_("Opens the server's page in your browser, which sends the list back to this app."))
        passkey.clicked.connect(self.passkey)
        password = QtWidgets.QPushButton(_("Unlock with a password…"))
        password.clicked.connect(self.password)
        row.addWidget(passkey)
        row.addWidget(password)
        row.addStretch(1)
        s.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.link = QtWidgets.QLineEdit(link or listsync.saved_link() or "")
        self.link.setEchoMode(QtWidgets.QLineEdit.Password)
        self.link.setPlaceholderText(_("or paste its sync link"))
        use = QtWidgets.QPushButton(_("Use this link"))
        use.clicked.connect(self.use)
        self.link.returnPressed.connect(self.use)
        row.addWidget(self.link, 1)
        row.addWidget(use)
        s.addLayout(row)
        s.addSpacing(12)
        s.addWidget(self.heading(_("New here?")))
        row = QtWidgets.QHBoxLayout()
        self.code = QtWidgets.QLineEdit("")
        self.code.setPlaceholderText(_("Creation code (if the server needs one)"))
        new = QtWidgets.QPushButton(_("Start a new list"))
        new.clicked.connect(self.start)
        row.addWidget(self.code, 1)
        row.addWidget(new)
        s.addLayout(row)
        no_recovery = QtWidgets.QLabel(_("There is no way to recover a lost list. The server can't read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren't deleted, but you'll need each project's own link to open it."))
        no_recovery.setWordWrap(True)
        no_recovery.setStyleSheet(f"color: {AMBER}")
        s.addWidget(no_recovery)
        s.addStretch(1)
        self.pages.addWidget(setup)

        # in use: the projects on it that aren't here yet
        inuse = QtWidgets.QWidget()
        u = QtWidgets.QVBoxLayout(inuse)
        u.setContentsMargins(0, 8, 0, 0)
        self.count = QtWidgets.QLabel("")
        u.addWidget(self.count)
        u.addWidget(QtWidgets.QLabel(_("Projects on the list that aren't on this computer:")))
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText(_("Search by name or tag…"))
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _text: self.apply_find())
        u.addWidget(self.find)
        self.items = QtWidgets.QListWidget()
        self.items.itemDoubleClicked.connect(lambda _item: self.join())
        u.addWidget(self.items, 1)
        row = QtWidgets.QHBoxLayout()
        join = QtWidgets.QPushButton(_("Join…"))
        join.clicked.connect(self.join)
        copy = QtWidgets.QPushButton(_("Copy sync link"))
        copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(listsync.saved_link() or ""), copy.setText(_("Copied"))))
        off = QtWidgets.QPushButton(_("Stop using it here"))
        off.clicked.connect(self.off)
        row.addWidget(join)
        row.addStretch(1)
        row.addWidget(copy)
        row.addWidget(off)
        u.addLayout(row)
        u.addWidget(grey_note(_("Keep the sync link in a password manager: it opens every project on the list, and a lost list can’t be recovered.")))
        self.pages.addWidget(inuse)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)
        in_use = bool(listsync.saved_link()) and not from_web
        self.pages.setCurrentIndex(1 if in_use else 0)
        if in_use:
            self.refresh_list()

    @staticmethod
    def heading(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        f = label.font()
        f.setBold(True)
        label.setFont(f)
        return label

    def _server(self) -> str:
        try:
            return listsync.ListKey.from_link(self.link.text())[1]
        except ValueError:
            return known_server()

    def in_background(self, work, then) -> None:
        """Network and password hashing are slow: keep the window responsive."""
        self.status.setText(_("Working…"))

        def run() -> None:
            try:
                result, error = work(), None
            except Exception as e:  # noqa: BLE001 - shown to the user
                result, error = None, e
            try:
                self.work_done.emit(then, result, error)
            except RuntimeError:  # the dialog was closed meanwhile
                pass

        threading.Thread(target=run, daemon=True).start()

    def after_work(self, then, result, error) -> None:
        if error is None:
            self.status.setText("")
            then(result)
        else:
            self.status.setText(listsync.explain(error.code) if isinstance(error, listsync.ListError) else str(error))

    def refresh_list(self) -> None:
        self.items.clear()
        code = self.code.text().strip() or None

        def work():
            found = listsync.sync_this_computer(code)
            return found and (*found, listsync.not_here(found[1], found[0]))

        self.in_background(work, self.show_list)

    def show_list(self, found) -> None:
        if found is None:
            self.count.setText(_("This computer has no project list yet."))
            return
        server, doc, away = found
        self.count.setText(ngettext(len(doc["projects"]), "The list has {n} project.", "The list has {n} projects.", n=len(doc["projects"])))
        of = tags.reader()
        for project, entry in away:
            names = [t.name for t in of(project)]
            title = entry.get("nick") or entry.get("n") or _("Untitled project")
            item = QtWidgets.QListWidgetItem(title + (f"  ·  {', '.join(names)}" if names else ""))
            item.setData(Qt.UserRole, f"{server}/#{entry['f']}")
            self.items.addItem(item)
        self.apply_find()

    def apply_find(self) -> None:
        words = tags.fold_search(self.find.text()).split()
        for i in range(self.items.count()):
            item = self.items.item(i)
            item.setHidden(not all(w in tags.fold_search(item.text()) for w in words))

    def adopt(self, link: str) -> None:
        self.link.setText(link)
        listsync.save_link(link)
        self.pages.setCurrentIndex(1)
        self.refresh_list()

    def use(self) -> None:
        try:
            listsync.ListKey.from_link(self.link.text())
        except ValueError:
            self.status.setText(_("That isn't a sync link."))
            return
        self.adopt(self.link.text().strip())

    def start(self) -> None:
        self.adopt(listsync.ListKey.generate().link(known_server()))

    def passkey(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"{self._server()}/#unlock-for-app"))
        self.status.setText(_("Unlock the list in your browser; it will come back here."))

    def password(self) -> None:
        server = self._server()
        username, ok = QtWidgets.QInputDialog.getText(self, _("Project list"), _("Username or email:"))
        if not ok or not username.strip():
            return
        secret, ok = QtWidgets.QInputDialog.getText(self, _("Project list"), _("Password:"), QtWidgets.QLineEdit.Password)
        if ok and secret:
            self.in_background(lambda: listsync.unlock_with_password(server, username, secret), lambda key: self.adopt(key.link(server)))

    def off(self) -> None:
        listsync.save_link(None)
        self.link.clear()
        self.items.clear()
        self.pages.setCurrentIndex(0)
        self.status.setText(_("This computer no longer uses a project list. The list itself stays on the server."))

    def join(self) -> None:
        item = self.items.currentItem()
        if item:
            self.window_.join(item.data(Qt.UserRole))
            self.refresh_list()


class ConnectToolDialog(QtWidgets.QDialog):
    """How to connect an MCP tool that's already on this computer, such as Claude Code: it runs
    under the person's own account with that tool. The same guide as web/src/assistant/connect.ts."""

    INSTALL = "uv tool install git+https://github.com/jhudsy/texloom"

    def __init__(self, folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.folder = folder.resolve()
        self.keys, self.server = load(folder)
        self.access = "suggest" if self.keys.writable else "read"
        self.setWindowTitle(_("Connect an AI tool via MCP"))
        self.setMinimumWidth(620)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.note(_("Tools that support MCP, such as Claude Code, Claude Desktop or Cursor, can work on this project like a collaborator. They run on your computer under your own account with that tool, so no API key is needed here.")))
        lay.addWidget(self.heading(_("Install the TeXLoom command-line helper once")))
        self.install = self.block(lay, 1, lambda: self.INSTALL)
        how = self.note(f'<a href="https://docs.astral.sh/uv/getting-started/installation/">{_("How to install uv")}</a>')
        how.setOpenExternalLinks(True)
        lay.addWidget(how)
        if self.keys.writable:
            lay.addWidget(self.heading(_("Choose what the tool may do")))
            for access, label in (("suggest", _("Suggest changes for people to accept")), ("edit", _("Edit files directly")), ("read", _("Read the project only"))):
                radio = QtWidgets.QRadioButton(label)
                radio.setProperty("access", access)
                radio.setChecked(access == self.access)
                radio.toggled.connect(functools.partial(self.choose, access))
                lay.addWidget(radio)
        lay.addWidget(self.heading(_("Add it to your tool")))
        lay.addWidget(self.note(_("Claude Code: run this in a terminal")))
        self.claude = self.block(lay, 3, lambda: "claude mcp add texloom -- texloom " + " ".join(self.quoted(a) for a in self.args("Claude Code")))
        lay.addWidget(self.note(_("Other tools: add this to their MCP settings")))
        self.other = self.block(lay, 9, lambda: json.dumps({"mcpServers": {"texloom": {"command": "texloom", "args": self.args("AI agent")}}}, indent=2, ensure_ascii=False))
        lay.addWidget(self.note(_("If your tool can’t find the texloom command, give its full path instead.")))
        lay.addWidget(self.note(_("Suggesting and editing use this folder’s path. Reading uses a read-only link, which your tool saves in its settings, so anyone who can read those settings can read the project.")))
        lay.addWidget(self.note(_("The tool reads text other people wrote in this project, which could include instructions aimed at it. On shared projects, suggesting is the safer choice.")))
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        close = QtWidgets.QPushButton(_("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.show_commands()

    @staticmethod
    def note(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        return label

    def heading(self, text: str) -> QtWidgets.QLabel:
        label = self.note(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def block(self, lay: QtWidgets.QBoxLayout, lines: int, text) -> tuple:
        box = QtWidgets.QPlainTextEdit(readOnly=True)
        box.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        box.setWordWrapMode(QtGui.QTextOption.WrapAnywhere)
        box.setFixedHeight(box.fontMetrics().lineSpacing() * lines + 14)
        copy = QtWidgets.QPushButton(_("Copy"))
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(text()), copy.setText(_("Copied"))))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(box, 1)
        row.addWidget(copy, 0, Qt.AlignTop)
        lay.addLayout(row)
        return box, copy, text

    def args(self, name: str) -> list[str]:
        target = self.keys.link(self.server, False) if self.access == "read" else str(self.folder)
        return ["mcp", *(["--suggest-only"] if self.access == "suggest" else []), "--name", name, target]

    @staticmethod
    def quoted(arg: str) -> str:
        """For pasting into a terminal. Single quotes, so nothing inside is expanded ($(...), `...`,
        $VAR): a folder's name can come from a project name a collaborator chose."""
        if re.fullmatch(r"[\w./:\\-]+", arg):
            return arg
        if sys.platform == "win32":  # PowerShell: '' is a quote inside single quotes
            return "'" + arg.replace("'", "''") + "'"
        import shlex
        return shlex.quote(arg)

    def choose(self, access: str, on: bool) -> None:
        if on:
            self.access = access
            self.show_commands()

    def show_commands(self) -> None:
        for box, copy, text in (self.install, self.claude, self.other):
            box.setPlainText(text())
            copy.setText(_("Copy"))


class CopyFolderDialog(Dialog):
    """Start a new project from a folder's current files."""

    def __init__(self, engine: Engine, src: Path, parent=None) -> None:
        super().__init__(parent)
        self.engine, self.src = engine, src
        self.setWindowTitle(_("Make a copy"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)  # macOS keeps fields narrow otherwise
        note = QtWidgets.QLabel(_("The copy is a new project of your own, with its own links, in a folder of its own. It starts "
                                  "with the current files and settings, without the history, comments or collaborators, and "
                                  "it counts towards your storage."))
        note.setWordWrap(True)
        lay.addRow(note)
        self.name = QtWidgets.QLineEdit(_("Copy of {name}", name=project_name(src, engine)))
        self.name.textEdited.connect(self.suggest_folder)
        lay.addRow(_("Project name"), self.name)
        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit(str(self.free_folder()))
        choose = QtWidgets.QPushButton(_("Choose…"))
        choose.clicked.connect(self.choose)
        row.addWidget(self.folder, 1)
        row.addWidget(choose)
        lay.addRow(_("Folder"), row)
        self.code = QtWidgets.QLineEdit(str(load_config(src).get("create_code") or ""))
        self.code.setPlaceholderText(_("only if the server asks for one"))
        lay.addRow(_("Creation code"), self.code)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addRow(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.go = bb.addButton(_("Make a copy"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.clicked.connect(self.make)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def free_folder(self) -> Path:
        """Next to the original, under a name that is free and valid on every system."""
        base = slug(self.name.text())  # as for joined projects: letters, digits, spaces and dashes only
        for n in range(1, 1000):
            d = self.src.parent / (base if n == 1 else f"{base} {n}")
            if not d.exists():
                return d
        return self.src.parent / base

    def suggest_folder(self) -> None:
        if not self.folder.isModified():
            self.folder.setText(str(self.free_folder()))

    def choose(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("Folder"), str(self.src.parent))
        if d:
            self.folder.setText(d)
            self.folder.setModified(True)

    def make(self) -> None:
        dst = Path(self.folder.text()).expanduser()
        if not dst.is_absolute() or dst.resolve().is_relative_to(self.src.resolve()):
            self.msg.setText(red(_("Choose a folder outside the project being copied.")))
            return
        if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
            self.msg.setText(red(_("Choose a new or empty folder.")))
            return
        self.go.setEnabled(False)
        self.msg.setText(_("Copying…"))
        self.engine.submit(copy_folder(self.engine, self.src, dst, self.name.text().strip(), self.code.text().strip()),
                           lambda _keys, err: self.finished_(dst, err))

    def finished_(self, dst: Path, err) -> None:
        if err:
            self.msg.setText(red(_("Couldn’t copy the project: {err}", err=err)))
            self.go.setEnabled(True)
            return
        self.made = dst
        self.engine.call(self.engine.daemon.poke)
        self.accept()


COPIED_SETTINGS = ("main", "bib", "engine", "lang")  # as in web/src/copyproject.ts


async def copy_folder(engine: Engine, src: Path, dst: Path, name: str, code: str) -> Keys:
    """Runs on the sync thread, the only place a folder's shared settings can be read."""
    st = engine.daemon.states.get(src)
    meta = st.replica.meta if st is not None and st.replica is not None else {}
    settings = {k: str(meta.get(k)) for k in COPIED_SETTINGS if meta.get(k)}
    await asyncio.to_thread(copy_project_files, src, dst, settings.get("main", "main.tex"))
    return await new_project(dst, load(src)[1], name, code, settings)


def find_texcount() -> str | None:
    """TeXCount from this computer's TeX. Apps opened from the Finder don't get TeX on their PATH."""
    return shutil.which("texcount") or shutil.which("texcount", path="/Library/TeX/texbin:/usr/texbin:/usr/local/bin:/opt/homebrew/bin")


def texcount_language(text: str) -> list[str]:
    """TeXCount counts one script at a time, so the text chooses which (web/src/texcount.ts too)."""
    code = {ord(c) for c in text if ord(c) > 0x2FFF}
    if any(0x3040 <= c <= 0x30FF for c in code):
        return ["-japanese"]
    if any(0x3400 <= c <= 0x9FFF or 0x20000 <= c <= 0x2FFFF for c in code):
        return ["-chinese"]
    if any(0xAC00 <= c <= 0xD7A3 for c in code):
        return ["-korean-words"]
    return []


INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{([^{}\n]*)\}")


def with_includes(files, main: str, depth: int = 0, seen: frozenset = frozenset()) -> str:
    """The main file with its \\input and \\include files written in, taken only from the project's
    own files (resolved from the main file's folder, as LaTeX does). TeXCount then counts one
    file and opens nothing itself: its own -inc option opens whatever name a document gives it,
    and Perl runs a name ending in "|" as a command."""
    text = str(files[main])
    if depth > 20:
        return text
    base = os.path.dirname(main)

    def expand(m: re.Match) -> str:
        line_start = text.rfind("\n", 0, m.start()) + 1
        if re.search(r"(?<!\\)%", text[line_start:m.start()]):
            return m.group(0)  # commented out
        name = m.group(1).strip()
        target = os.path.normpath(os.path.join(base, name)).replace(os.sep, "/")
        for cand in (target, target + ".tex"):
            if cand in files and cand not in seen and cand.endswith(".tex") and not cand.startswith("../"):
                return "\n" + with_includes(files, cand, depth + 1, seen | {main}) + "\n"
        return m.group(0)

    return INCLUDE_RE.sub(expand, text)


async def texcount_report(st, d: Path, exe: str) -> str:
    """Runs on the sync thread, the only place a folder's project can be read. TeXCount reads one
    temporary file, the project's main file with its inputs written in (see with_includes)."""
    import tempfile
    files, meta = st.replica.files, st.replica.meta
    main = main_file(files, meta)
    if not main:
        raise FolderProblem(_("No .tex file with \\documentclass found."))
    whole = with_includes(files, main)

    def run_it() -> str:
        with tempfile.TemporaryDirectory(prefix="texloom-count-") as tmp:
            (Path(tmp) / "main.tex").write_text(whole, encoding="utf-8")
            args = [exe, "-utf8", *texcount_language(whole), "./main.tex"]
            proc = subprocess.Popen(args, cwd=tmp, env=tex_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", start_new_session=True)
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:  # stop it, and anything it started
                try:
                    os.killpg(proc.pid, 9) if hasattr(os, "killpg") else proc.kill()
                except OSError:
                    pass
                proc.communicate()
                raise FolderProblem(_("TeXCount took too long.")) from None
            if proc.returncode:
                raise FolderProblem(err.strip()[-500:] or str(proc.returncode))
            return out

    return await asyncio.to_thread(run_it)


class TagsDialog(Dialog):
    """Assign or unassign tags on one project or several, and create new ones."""

    def __init__(self, projects: list[str], title: str, parent=None) -> None:
        super().__init__(parent)
        self.projects = projects
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        lay = QtWidgets.QVBoxLayout(self)
        note = _("Only you see your tags. They aren’t shared with collaborators.")
        if len(projects) > 1:
            note += " " + _("A filled box means only some of these projects have that tag.")
        if listsync.tags_problem() == "needs-code":
            note += " " + _("Your tags stay on this computer until you enter a creation code in Project list….")
        label = QtWidgets.QLabel(note)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText(_("Find or create a tag…"))
        self.input.setMaxLength(tags.MAX_NAME)
        self.input.textChanged.connect(lambda _text: self.render())
        self.input.returnPressed.connect(self.commit)
        lay.addWidget(self.input)
        self.create = QtWidgets.QPushButton()
        self.create.setAutoDefault(False)
        self.create.clicked.connect(self.commit)
        lay.addWidget(self.create)
        self.items = QtWidgets.QListWidget()
        self.items.itemChanged.connect(self.toggled)
        lay.addWidget(self.items, 1)
        row = QtWidgets.QHBoxLayout()
        manage = QtWidgets.QPushButton(_("Manage tags…"))
        manage.setAutoDefault(False)
        manage.clicked.connect(self.manage)
        close = QtWidgets.QPushButton(_("Close"))
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        row.addWidget(manage)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.render()

    def typed(self) -> str:
        return tags.clean_name(self.input.text())

    def render(self) -> None:
        want, of = listsync.fold_tag(self.typed()), tags.reader()
        self.items.blockSignals(True)
        self.items.clear()
        for tag in tags.all_tags():
            if want not in listsync.fold_tag(tag.name):
                continue
            n = sum(1 for p in self.projects if any(x.id == tag.id for x in of(p)))
            it = QtWidgets.QListWidgetItem(dot(tag_hex(tag.colour)), tag.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setData(Qt.UserRole, tag.id)
            it.setCheckState(Qt.Checked if n == len(self.projects) else Qt.PartiallyChecked if n else Qt.Unchecked)
            self.items.addItem(it)
        self.items.blockSignals(False)
        self.create.setVisible(bool(self.typed()) and tags.find(self.typed()) is None)
        self.create.setText(_("Create “{name}”", name=self.typed()))

    def commit(self) -> None:
        if self.typed():
            tag = tags.find(self.typed()) or tags.create(self.typed())
            if tag:
                tags.assign(self.projects, tag.id, True)
            self.input.clear()
            self.render()

    def toggled(self, it: QtWidgets.QListWidgetItem) -> None:
        tags.assign(self.projects, it.data(Qt.UserRole), it.checkState() == Qt.Checked)

    def manage(self) -> None:
        ManageTagsDialog(self).exec()
        self.render()


class ManageTagsDialog(Dialog):
    """Add, rename and delete tags."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Manage tags"))
        self.setMinimumWidth(420)
        lay = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(_("A change applies to every project that has the tag."))
        note.setWordWrap(True)
        lay.addWidget(note)
        self.items = QtWidgets.QListWidget()
        self.items.itemDoubleClicked.connect(lambda _it: self.rename())
        lay.addWidget(self.items, 1)
        row = QtWidgets.QHBoxLayout()
        self.add_in = QtWidgets.QLineEdit()
        self.add_in.setPlaceholderText(_("New tag"))
        self.add_in.setMaxLength(tags.MAX_NAME)
        self.add_in.returnPressed.connect(self.add)
        add = QtWidgets.QPushButton(_("Add"))
        add.setAutoDefault(False)
        add.clicked.connect(self.add)
        row.addWidget(self.add_in, 1)
        row.addWidget(add)
        lay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        for text, act in ((_("Rename…"), self.rename), (_("Delete"), self.delete)):
            b = QtWidgets.QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(act)
            row.addWidget(b)
        self.colour_btn = QtWidgets.QPushButton(_("Colour"))
        self.colour_btn.setAutoDefault(False)
        self.colour_menu = QtWidgets.QMenu(self.colour_btn)
        self.colour_menu.aboutToShow.connect(self.fill_colours)
        self.colour_btn.setMenu(self.colour_menu)
        self.items.currentItemChanged.connect(lambda now, _before: self.colour_btn.setEnabled(now is not None))
        row.addWidget(self.colour_btn)
        row.addStretch(1)
        close = QtWidgets.QPushButton(_("Close"))
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.render()

    def render(self) -> None:
        was = self.picked()
        self.items.clear()
        self.colour_btn.setEnabled(False)
        for tag in tags.all_tags():
            n = tags.uses(tag.id)
            it = QtWidgets.QListWidgetItem(dot(tag_hex(tag.colour)), f"{tag.name}  ·  " + ngettext(n, "{n} project", "{n} projects", n=n))
            it.setData(Qt.UserRole, tag)
            self.items.addItem(it)
            if was and tag.id == was.id:
                self.items.setCurrentItem(it)

    def fill_colours(self) -> None:
        self.colour_menu.clear()
        tag = self.picked()
        for colour, name in colour_names().items() if tag else ():
            act = self.colour_menu.addAction(dot(tag_hex(colour)), name)
            act.setCheckable(True)
            act.setChecked(colour == tag.colour)
            act.triggered.connect(functools.partial(self.set_colour, colour))

    def set_colour(self, colour: str, _checked: bool = False) -> None:
        tag = self.picked()
        if tag:
            tags.set_colour(tag.id, colour)
            self.render()

    def picked(self) -> tags.Tag | None:
        it = self.items.currentItem()
        return it.data(Qt.UserRole) if it else None

    def add(self) -> None:
        if tags.create(self.add_in.text()):
            self.add_in.clear()
            self.render()

    def rename(self) -> None:
        tag = self.picked()
        if not tag:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, _("Manage tags"), _("New name for “{name}”:", name=tag.name), text=tag.name)
        clean = tags.clean_name(name)
        if not ok or not clean or clean == tag.name:
            return
        other = tags.find(clean)
        if other and other.id != tag.id and QtWidgets.QMessageBox.question(
                self, _("Manage tags"), _("A tag called “{name}” already exists. Merge the two?", name=other.name)) != QtWidgets.QMessageBox.Yes:
            return
        tags.rename(tag.id, clean)
        self.render()

    def delete(self) -> None:
        tag = self.picked()
        if not tag:
            return
        n = tags.uses(tag.id)
        if n and QtWidgets.QMessageBox.question(self, _("Manage tags"), ngettext(
                n, "Delete the tag “{name}”? It will be taken off {n} project. The project itself isn’t changed.",
                "Delete the tag “{name}”? It will be taken off {n} projects. The projects themselves aren’t changed.",
                n=n, name=tag.name)) != QtWidgets.QMessageBox.Yes:
            return
        tags.remove(tag.id)
        self.render()


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
    """The server of this computer's projects, else TeXLoom's own."""
    for d in registry.folders():
        try:
            return Keys.from_link(load_config(d)["link"])[1]
        except Exception:  # noqa: BLE001
            continue
    return DEFAULT_SERVER


class SettingsDialog(QtWidgets.QDialog):
    """Settings for the whole app. On macOS a window of its own (TeXLoom ▸ Settings…, ⌘,) that
    applies changes at once; elsewhere a dialog with a Close button."""

    def __init__(self, window) -> None:
        super().__init__(window if not MAC else None)
        self.window_ = window
        self.setWindowTitle(_("Settings"))
        self.setMinimumWidth(520)
        lay = QtWidgets.QFormLayout(self)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)  # macOS keeps fields narrow otherwise
        lay.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        self.login = QtWidgets.QCheckBox(_("Start TeXLoom when I log in, so folders keep syncing"))
        self.login.setChecked(autostart.status() != "off")
        self.login.toggled.connect(self.set_autostart)
        lay.addRow(_("Startup"), self.login)
        self.lang = QtWidgets.QComboBox()
        self.lang.addItem(_("Automatic (the system’s language)"), "auto")
        for code, name in NAMES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(lang_pref())))
        self.lang.activated.connect(self.set_language)
        lay.addRow(_("Language"), self.lang)
        lay.addRow("", grey_note(_("A new language is used the next time TeXLoom starts.")))
        row = QtWidgets.QHBoxLayout()
        self.list_state = QtWidgets.QLabel()
        open_list = QtWidgets.QPushButton(_("Project list…"))
        open_list.setAutoDefault(False)
        open_list.clicked.connect(lambda: window.project_list())
        row.addWidget(self.list_state, 1)
        row.addWidget(open_list)
        lay.addRow(_("Project list"), row)
        lay.addRow("", grey_note(_("Keeps the list of your projects in step across your computers and browsers.")))
        self.msg = grey_note("")
        lay.addRow(self.msg)
        if not MAC:
            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            bb.rejected.connect(self.accept)
            lay.addRow(bb)
        else:
            QtGui.QShortcut(QtGui.QKeySequence.Close, self, self.close)

    def showEvent(self, e) -> None:
        self.list_state.setText(_("In use on this computer") if listsync.saved_link() else _("Not set up"))
        super().showEvent(e)

    def set_language(self, _i: int) -> None:
        set_lang_pref(self.lang.currentData())
        self.msg.setText(_("The new language is used the next time TeXLoom starts. Quit TeXLoom (from its menu) and open it again."))

    def set_autostart(self, on: bool) -> None:
        try:
            self.msg.setText(autostart.enable(app=True) if on else autostart.disable())
        except Exception as e:  # noqa: BLE001
            self.msg.setText(red(_("Couldn’t change it: {e}", e=e)))


def short_path(d: Path) -> str:
    """A folder as people read it: ~/Papers/thesis, and the middle left out if it's long."""
    try:
        text = str(Path("~") / d.relative_to(Path.home()))
    except ValueError:
        text = str(d)
    return text if len(text) <= 72 else text[:30] + "…" + text[-40:]


# ---- the window ----------------------------------------------------------------------------


class Window(QtWidgets.QMainWindow):
    def __init__(self, engine: Engine, activity: Activity) -> None:
        super().__init__()
        self.engine, self.activity = engine, activity
        self.tray: QtWidgets.QSystemTrayIcon | None = None
        self.settings_dlg: SettingsDialog | None = None
        self.setWindowTitle("TeXLoom")
        self.setWindowIcon(QtGui.QIcon(str(ICON)))
        self.resize(900, 580)
        self.sort_key = app_pref("folder_sort", "oldest")
        self.build_actions()
        self.build_toolbar()
        self.build_menus()

        self.pages = QtWidgets.QStackedWidget()
        self.pages.addWidget(self.build_welcome())
        split = QtWidgets.QSplitter()
        split.setChildrenCollapsible(False)
        split.addWidget(self.build_list())
        split.addWidget(self.build_detail())
        split.setStretchFactor(1, 1)
        split.setSizes([260, 640])
        self.pages.addWidget(split)
        self.setCentralWidget(self.pages)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.list_busy, self.list_at, self.list_folders, self.tags_dirty = False, 0.0, None, False
        self.list_timer = QtCore.QTimer(self)
        self.list_timer.timeout.connect(self.keep_list_updated)
        self.list_timer.timeout.connect(lambda: self.sorting == "changed" and self.scan_dates())
        self.list_timer.start(60_000)
        self.refresh()

    # -- actions, toolbar and menus
    def action(self, text: str, slot, icon: str = "", keys=None, role=QtGui.QAction.NoRole) -> QtGui.QAction:
        act = QtGui.QAction(text, self)
        if icon:
            act.setIcon(theme_icon(icon))
        if keys is not None:
            act.setShortcut(QtGui.QKeySequence(keys))
        act.setMenuRole(role)  # NoRole: macOS mustn't move "Settings"-like items by guessing from their text
        act.triggered.connect(lambda _checked=False: slot())
        return act

    def build_actions(self) -> None:
        a, K = self.action, QtGui.QKeySequence
        self.a_new = a(_("New project from a folder…"), self.share_folder, "FolderNew", K.New)
        self.a_join = a(_("Join a project with a link…"), self.join, "InsertLink", "Ctrl+J")
        self.a_add = a(_("Add a TeXLoom folder from this computer…"), self.add_existing, "FolderOpen")
        self.a_list = a(_("Project list…"), self.project_list)
        self.a_pause = a(_("Pause syncing"), self.toggle_pause, "MediaPlaybackPause")
        self.a_settings = a(_("Settings…"), self.settings_window, "DocumentProperties", K.Preferences, QtGui.QAction.PreferencesRole)
        self.a_close = a(_("Close window"), self.close, keys=K.Close)
        self.a_quit = a(_("Quit TeXLoom"), QtWidgets.QApplication.quit, "ApplicationExit", K.Quit, QtGui.QAction.QuitRole)
        self.a_open = a(_("Open folder"), self.open_folder, "FolderOpen", K.Open)
        self.a_web = a(_("Open in browser"), self.open_web, "GoNext", "Ctrl+B")
        self.a_share = a(_("Share…"), self.share_links, "MailSend")
        self.a_tags = a(_("Tags…"), self.edit_tags)
        self.a_copy = a(_("Make a copy…"), self.copy_project)
        self.a_count = a(_("Word count…"), self.word_count)
        self.a_count.setToolTip(_("Count the words with TeXCount, from your own TeX installation"))
        self.a_scratch = a(_("Compile from scratch"), self.compile_from_scratch)
        self.a_scratch.setToolTip(_("Delete LaTeX’s working files (.aux, .bbl, …) in .texloom/build and compile again"))
        self.a_mcp = a(_("Connect an AI tool via MCP…"), self.connect_tool)
        self.a_leave = a(_("Leave…"), self.leave)
        self.a_activity = QtGui.QAction(_("Show activity"), self, checkable=True)
        self.a_activity.setChecked(bool(app_pref("show_activity", False)))
        self.a_activity.toggled.connect(self.show_activity)
        self.a_help = a(_("TeXLoom help"), lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(HELP_URL)), keys=K.HelpContents)
        self.a_about = a(_("About TeXLoom"), self.about, role=QtGui.QAction.AboutRole)
        self.add_menu = QtWidgets.QMenu(self)
        for act in (self.a_new, self.a_join, self.a_add):
            self.add_menu.addAction(act)
        self.more_menu = QtWidgets.QMenu(self)
        for act in (self.a_tags, self.a_copy, self.a_count, self.a_mcp, None, self.a_leave):
            self.more_menu.addSeparator() if act is None else self.more_menu.addAction(act)

    def build_toolbar(self) -> None:
        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)  # no menu offering to hide it
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if MAC else Qt.ToolButtonTextBesideIcon)
        add = QtGui.QAction(theme_icon("ListAdd"), _("Add project"), self)
        add.setMenu(self.add_menu)
        add.setToolTip(_("Make a new project from a folder, or join one with a link"))
        tb.addAction(add)
        tb.widgetForAction(add).setPopupMode(QtWidgets.QToolButton.InstantPopup)
        tb.addAction(self.a_pause)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        if not MAC:  # on macOS, Settings is in the TeXLoom menu (⌘,)
            tb.addAction(self.a_settings)
        self.setUnifiedTitleAndToolBarOnMac(True)

    def build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu(_("File"))
        for act in (self.a_new, self.a_join, self.a_add, None, self.a_list, None, self.a_pause, None,
                    self.a_settings, self.a_close, self.a_quit):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu(_("Project"))
        for act in (self.a_open, self.a_web, self.a_share, None, self.a_tags, self.a_copy, self.a_count,
                    self.a_scratch, self.a_mcp, None, self.a_leave):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu(_("View"))
        self.sort_menu = m.addMenu(_("Sort by"))
        self.fill_sort_menu(self.sort_menu)
        self.view_filter = m.addMenu(_("Filter by tag"))
        self.view_filter.aboutToShow.connect(lambda: self.fill_filter_menu(self.view_filter))
        m.addSeparator()
        m.addAction(self.a_activity)
        if MAC:
            m = mb.addMenu(_("Window"))
            m.addAction(self.action(_("Minimise"), self.showMinimized, keys="Ctrl+M"))
            m.addAction(self.action("TeXLoom", self.show_window))
        m = mb.addMenu(_("Help"))
        m.addAction(self.a_help)
        m.addAction(self.a_about)

    def fill_sort_menu(self, m: QtWidgets.QMenu) -> None:
        group = QtGui.QActionGroup(m)
        labels = {"oldest": _("Oldest first"), "newest": _("Newest first"), "changed": _("Last changed"),
                  "az": _("Name (A to Z)"), "za": _("Name (Z to A)")}
        for key, label in labels.items():
            act = m.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == self.sort_key)
            group.addAction(act)
            act.triggered.connect(lambda _c=False, k=key: self.set_sorting(k))

    def about(self) -> None:
        QtWidgets.QMessageBox.about(self, _("About TeXLoom"), f"<b>TeXLoom {__version__}</b><br><br>"
                                    + html.escape(_("Keeps folders on this computer in step with TeXLoom projects."))
                                    + f"<br><br><a href='{HELP_URL}'>{html.escape(_('TeXLoom help'))}</a>")

    def settings_window(self) -> None:
        if MAC:  # a window of its own, like other Mac apps' settings
            if self.settings_dlg is None:
                self.settings_dlg = SettingsDialog(self)
            self.settings_dlg.show()
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
        else:
            SettingsDialog(self).exec()

    # -- the welcome page, while there are no folders
    def build_welcome(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addStretch(1)
        icon = QtWidgets.QLabel()
        icon.setPixmap(QtGui.QIcon(str(ICON)).pixmap(80, 80))
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)
        title = QtWidgets.QLabel(_("Welcome to TeXLoom"))
        f = title.font()
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        text = QtWidgets.QLabel(_("TeXLoom keeps a folder on this computer in step with a project that you and your "
                                  "collaborators edit in the browser, so you can also work on it with your own tools."))
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignCenter)
        text.setFixedWidth(500)
        text.setMinimumHeight(text.heightForWidth(500))  # a wrapped label doesn't otherwise get its full height here
        lay.addWidget(text, 0, Qt.AlignHCenter)
        lay.addSpacing(16)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        for act, default in ((self.a_new, True), (self.a_join, False)):
            b = QtWidgets.QPushButton(act.icon(), act.text())
            b.setDefault(default)
            b.setMinimumHeight(36)
            b.clicked.connect(act.trigger)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        more = QtWidgets.QLabel(f"<a href='add'>{html.escape(_('Add a TeXLoom folder from this computer…'))}</a>"
                                f" · <a href='list'>{html.escape(_('Open your project list…'))}</a>")
        more.setAlignment(Qt.AlignCenter)
        more.linkActivated.connect(lambda what: self.add_existing() if what == "add" else self.project_list())
        lay.addSpacing(8)
        lay.addWidget(more)
        lay.addStretch(2)
        return w

    # -- the list of folders
    def build_list(self) -> QtWidgets.QWidget:
        self.tag_filter: set[str] = set()
        self.project_ids: dict[Path, str | None] = {}
        self.dates: dict[Path, tuple[int, int]] = {}
        self.scanning = False
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText(_("Search by name or tag…"))
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _text: self.refresh())
        self.view_btn = QtWidgets.QToolButton()
        self.view_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.view_btn.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        self.view_menu = QtWidgets.QMenu(self.view_btn)
        self.view_menu.aboutToShow.connect(self.fill_view_menu)
        self.view_btn.setMenu(self.view_menu)
        row.addWidget(self.find, 1)
        row.addWidget(self.view_btn)
        lay.addLayout(row)
        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(ProjectRows(self.list))
        self.list.setMinimumWidth(220)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self.show_detail)
        self.list.itemDoubleClicked.connect(lambda _it: self.open_folder())
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_menu)
        lay.addWidget(self.list, 1)
        self.show_filter()
        return w

    @property
    def sorting(self) -> str:
        return self.sort_key

    def set_sorting(self, key: str) -> None:
        self.sort_key = key
        set_app_pref("folder_sort", key)
        self.sort_menu.clear()
        self.fill_sort_menu(self.sort_menu)
        self.scan_dates()
        self.refresh()

    def show_filter(self) -> None:
        names = [t.name for t in tags.all_tags() if t.id in self.tag_filter]
        self.view_btn.setText(f"⇅ {len(names)}" if names else "⇅")
        self.view_btn.setToolTip(_("Sort and filter") + (": " + ", ".join(names) if names else ""))

    def fill_view_menu(self) -> None:
        m = self.view_menu
        m.clear()
        m.addSection(_("Sort by"))
        self.fill_sort_menu(m)
        m.addSection(_("Filter by tag"))
        self.fill_filter_menu(m, clear=False)

    def fill_filter_menu(self, m: QtWidgets.QMenu, clear: bool = True) -> None:
        if clear:
            m.clear()
        every = tags.all_tags()
        for tag in every:
            act = m.addAction(dot(tag_hex(tag.colour)), tag.name.replace("&", "&&"))
            act.setCheckable(True)
            act.setChecked(tag.id in self.tag_filter)
            act.toggled.connect(lambda on, tid=tag.id: self.set_filter(tid, on))
        if not every:
            m.addAction(_("No tags yet")).setEnabled(False)
        m.addSeparator()
        if self.tag_filter:
            m.addAction(_("Clear filter"), self.clear_filter)
        m.addAction(_("Manage tags…"), self.manage_tags)

    def set_filter(self, tid: str, on: bool) -> None:
        (self.tag_filter.add if on else self.tag_filter.discard)(tid)
        self.show_filter()
        self.refresh()

    def clear_filter(self) -> None:
        self.tag_filter.clear()
        self.show_filter()
        self.refresh()

    def project_of(self, d: Path) -> str | None:
        if d not in self.project_ids:
            try:
                self.project_ids[d] = load(d)[0].project
            except (OSError, ValueError, KeyError):
                return None
        return self.project_ids[d]

    def shown_folders(self) -> list[Path]:
        """The folders that match the search and filter, in the chosen order."""
        of = tags.reader()
        every_id = {t.id for t in tags.all_tags()}
        if self.tag_filter - every_id:
            self.tag_filter &= every_id
            self.show_filter()
        words = tags.fold_search(self.find.text()).split()
        out = []
        for d in self.folders():
            p = self.project_of(d)
            own = of(p) if p else []
            if self.tag_filter - {t.id for t in own}:
                continue
            text = tags.fold_search(" ".join([project_name(d, self.engine), d.name, *(t.name for t in own)]))
            if all(w in text for w in words):
                out.append(d)
        return self.in_order(out)

    def in_order(self, fs: list[Path]) -> list[Path]:
        how = self.sorting
        if how in ("az", "za"):
            names = QtCore.QCollator()
            names.setNumericMode(True)
            names.setCaseSensitivity(Qt.CaseInsensitive)
            key = functools.cmp_to_key(lambda a, b: names.compare(project_name(a, self.engine), project_name(b, self.engine)))
            return sorted(fs, key=key, reverse=how == "za")
        when = 1 if how == "changed" else 0
        return sorted(fs, key=lambda d: self.dates.get(d, (0, 0))[when], reverse=how != "oldest")

    def scan_dates(self) -> None:
        """Reading every file's date is slow, so it happens beside the window."""
        if self.scanning:
            return
        self.scanning = True
        fs = self.folders()

        def work() -> None:
            found = {}
            try:
                for d in fs:
                    try:
                        found[d] = listsync.folder_dates(d)
                    except OSError:  # a folder that has gone
                        found[d] = (0, 0)
                self.dates = found
            finally:
                self.scanning = False

        threading.Thread(target=work, daemon=True).start()

    def selected(self) -> list[Path]:
        """What an action applies to: the selected folders, else the current one."""
        fs = [Path(it.data(Qt.UserRole)) for it in self.list.selectedItems()]
        return fs or ([self.current()] if self.current() else [])

    def list_menu(self, pos: QtCore.QPoint) -> None:
        it = self.list.itemAt(pos)
        if not it:
            return
        if not it.isSelected():
            self.list.setCurrentItem(it)
        m = QtWidgets.QMenu(self)
        one = len(self.selected()) == 1
        if one:
            for act in (self.a_open, self.a_web, self.a_share):
                m.addAction(act)
            m.addSeparator()
        m.addAction(self.a_tags)
        if one:
            for act in (self.a_copy, self.a_count, self.a_mcp):
                m.addAction(act)
        m.addSeparator()
        m.addAction(self.a_leave)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def edit_tags(self) -> None:
        fs = self.selected()
        projects = list(dict.fromkeys(p for p in map(self.project_of, fs) if p))
        if not projects:
            return
        title = _("Tags for “{name}”", name=project_name(fs[0], self.engine)) if len(fs) == 1 else ngettext(
            len(fs), "Tags for {n} folder", "Tags for {n} folders", n=len(fs))
        TagsDialog(projects, title, self).exec()
        self.tags_changed()

    def manage_tags(self) -> None:
        ManageTagsDialog(self).exec()
        self.tags_changed()

    def tags_changed(self) -> None:
        self.tags_dirty = True
        self.show_filter()
        self.refresh()
        QtCore.QTimer.singleShot(2000, self.keep_list_updated)

    def connect_tool(self) -> None:
        ConnectToolDialog(self.selected()[0], self).exec()

    def copy_project(self) -> None:
        d = self.current()
        if not d:
            return
        dlg = CopyFolderDialog(self.engine, d, self)
        if dlg.exec():
            self.find.clear()
            self.clear_filter()
            self.select_folder(dlg.made)

    def word_count(self) -> None:
        d = self.current()
        exe = find_texcount()
        if not d:
            return
        if not exe:
            QtWidgets.QMessageBox.information(self, _("Word count"), _("TeXCount wasn’t found. It comes with TeX Live and most TeX installations."))
            return
        st = self.engine.daemon.states.get(d)
        if st is None or st.replica is None:
            self.statusBar().showMessage(_("This folder isn’t running yet: try again in a moment."), 5000)
            return
        name = project_name(d, self.engine)

        def show(report, err) -> None:
            if err:
                QtWidgets.QMessageBox.warning(self, _("Word count"), _("TeXCount couldn’t count this folder: {err}", err=err))
                return
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(_("Word count of “{name}”", name=name))
            dlg.resize(560, 460)
            lay = QtWidgets.QVBoxLayout(dlg)
            text = QtWidgets.QPlainTextEdit(report)
            text.setReadOnly(True)
            text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            lay.addWidget(text, 1)
            close = QtWidgets.QPushButton(_("Close"))
            close.clicked.connect(dlg.accept)
            lay.addWidget(close, 0, Qt.AlignRight)
            dlg.exec()

        self.engine.submit(texcount_report(st, d, exe), show)

    def select_folder(self, d: Path) -> None:
        for i in range(self.list.count()):
            if Path(self.list.item(i).data(Qt.UserRole)) == d.resolve():
                self.list.setCurrentRow(i)

    # -- detail pane
    @staticmethod
    def section(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        f = label.font()
        f.setBold(True)
        label.setFont(f)
        return label

    def build_detail(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 12, 8)
        self.title = QtWidgets.QLabel()
        f = self.title.font()
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        self.title.setFont(f)
        self.path_lbl = QtWidgets.QLabel()
        self.path_lbl.linkActivated.connect(lambda _href: self.open_folder())
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        for x in (self.title, self.path_lbl, self.status_lbl):
            lay.addWidget(x)
        lay.addSpacing(4)
        btns = QtWidgets.QHBoxLayout()
        self.buttons = []
        for act in (self.a_open, self.a_web, self.a_share):
            b = QtWidgets.QPushButton(act.icon(), act.text())
            b.clicked.connect(act.trigger)
            btns.addWidget(b)
            self.buttons.append(b)
        more = QtWidgets.QPushButton(_("More"))
        more.setMenu(self.more_menu)
        btns.addWidget(more)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addSpacing(10)

        self.options = QtWidgets.QWidget()  # one folder's settings: hidden while several are selected
        o = QtWidgets.QVBoxLayout(self.options)
        o.setContentsMargins(0, 0, 0, 0)
        o.addWidget(self.section(_("Compiling")))
        self.c_compile = QtWidgets.QCheckBox(_("Compile with my own TeX (latexmk) when files change"))
        self.c_compile.clicked.connect(lambda on: self.set_option("compile", on))
        o.addWidget(self.c_compile)
        self.compile_more = QtWidgets.QWidget()  # only while compiling is on
        c = QtWidgets.QVBoxLayout(self.compile_more)
        c.setContentsMargins(24, 0, 0, 0)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel(_("Running programs when compiling:")))
        self.p_mode = QtWidgets.QComboBox()
        self.p_mode.addItem(_("Never (safest)"), False)
        self.p_mode.addItem(_("TeX Live’s safe list only (e.g. epstopdf)"), "restricted")
        self.p_mode.addItem(_("Anything the document asks, once I allow it (e.g. minted)"), True)
        self.p_mode.activated.connect(self.set_programs)
        self.p_review = QtWidgets.QPushButton(_("Review lines…"))
        self.p_review.clicked.connect(lambda: self.current() and ProgramsDialog(self.engine, self.current(), self).exec())
        prow.addWidget(self.p_mode, 1)
        prow.addWidget(self.p_review)
        c.addLayout(prow)
        srow = QtWidgets.QHBoxLayout()
        scratch = QtWidgets.QPushButton(self.a_scratch.text())
        scratch.setToolTip(self.a_scratch.toolTip())
        scratch.clicked.connect(self.a_scratch.trigger)
        srow.addWidget(scratch)
        srow.addStretch(1)
        c.addLayout(srow)
        o.addWidget(self.compile_more)
        o.addSpacing(8)
        o.addWidget(self.section(_("Git")))
        self.c_git = QtWidgets.QCheckBox(_("Keep this folder’s git repository in step (commit, pull, push)"))
        self.c_git.clicked.connect(lambda on: self.set_option("git", on))
        o.addWidget(self.c_git)
        self.git_note = grey_note(_("This folder isn’t a git repository. Run “git init” in it first to use this."))
        o.addWidget(self.git_note)
        lay.addWidget(self.options)
        lay.addSpacing(10)

        self.act_btn = QtWidgets.QToolButton()
        self.act_btn.setStyleSheet("QToolButton { border: none; font-weight: bold; padding: 0; }")
        self.act_btn.clicked.connect(self.a_activity.toggle)
        lay.addWidget(self.act_btn)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay.addWidget(self.log, 10)
        lay.addStretch(1)
        self.detail = w
        self.show_activity(self.a_activity.isChecked())
        return w

    def show_activity(self, on: bool) -> None:
        set_app_pref("show_activity", on)
        self.act_btn.setText(("▾ " if on else "▸ ") + _("Activity"))
        self.log.setVisible(on)

    def folders(self) -> list[Path]:
        return registry.folders()

    def current(self) -> Path | None:
        it = self.list.currentItem()
        return Path(it.data(Qt.UserRole)) if it else None

    def refresh(self) -> None:
        every = self.folders()
        fs = self.shown_folders()
        if not self.scanning and any(d not in self.dates for d in every):
            self.scan_dates()
        keep = self.current()
        picked = {str(p) for p in self.selected()}
        if [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())] != [str(d) for d in fs]:
            self.list.blockSignals(True)
            self.list.clear()
            for d in fs:
                it = QtWidgets.QListWidgetItem()
                it.setData(Qt.UserRole, str(d))
                self.list.addItem(it)
            idx = next((i for i, d in enumerate(fs) if d == keep), 0 if fs else -1)
            still = [i for i, d in enumerate(fs) if str(d) in picked]
            if len(still) > 1:  # keep a selection of several across a change of order
                for i in still:
                    self.list.item(i).setSelected(True)
                self.list.setCurrentRow(idx, QtCore.QItemSelectionModel.NoUpdate)
            else:
                self.list.setCurrentRow(idx)
            self.list.blockSignals(False)
        problems = sum(describe(d, self.engine)[1] == RED for d in every)
        of = tags.reader()
        for i, d in enumerate(fs):
            short, colour, _longer = describe(d, self.engine)
            it = self.list.item(i)
            name = project_name(d, self.engine)
            own = of(self.project_of(d) or "")
            it.setText(f"{name}\n{short}" + (f" · {d.name}" if d.name != name else "") + (f"\n{', '.join(t.name for t in own)}" if own else ""))
            it.setData(TAGS_ROLE, tuple(own))
            it.setIcon(dot(colour))
            it.setToolTip(str(d))
        self.pages.setCurrentIndex(1 if every else 0)
        self.detail.setVisible(bool(fs))
        self.statusBar().setVisible(bool(every))
        if every:
            self.statusBar().showMessage(
                _("Paused") if self.engine.daemon.paused else
                ngettext(len(every), "{n} folder", "{n} folders", n=len(every))
                + (" · " + ngettext(len(fs), "{n} shown", "{n} shown", n=len(fs)) if len(fs) != len(every) else "")
                + (" · " + ngettext(problems, "{n} not syncing", "{n} not syncing", n=problems) if problems else ""))
        paused = self.engine.daemon.paused
        self.a_pause.setText(_("Resume syncing") if paused else _("Pause syncing"))
        self.a_pause.setIcon(theme_icon("MediaPlaybackStart" if paused else "MediaPlaybackPause"))
        self.show_detail()

    def show_detail(self) -> None:
        picked = self.selected()
        one, several = len(picked) == 1, len(picked) > 1
        for act in (self.a_open, self.a_web, self.a_share, self.a_copy, self.a_count, self.a_scratch, self.a_mcp):
            act.setEnabled(one)
        for act in (self.a_tags, self.a_leave):
            act.setEnabled(bool(picked))
        for b in self.buttons:
            b.setEnabled(one)
        self.options.setVisible(not several)
        if several:
            self.title.setText(ngettext(len(picked), "{n} folder selected", "{n} folders selected", n=len(picked)))
            self.path_lbl.setText("")
            self.status_lbl.setText("")
            self.log.setPlainText("")
            return
        d = self.current()
        if d is None:
            return
        short, colour, longer = describe(d, self.engine)
        self.title.setText(project_name(d, self.engine))
        self.path_lbl.setText(f"<a href='open' style='color:{GREY}; text-decoration:none'>{html.escape(short_path(d))}</a>")
        self.path_lbl.setToolTip(_("Open folder") + f": {d}")
        self.status_lbl.setText(f"<span style='color:{colour}'>●</span> <b>{html.escape(short)}</b>" + (f" — {html.escape(longer)}" if longer else ""))
        try:
            cfg = {**DEFAULT_CONFIG, **load_config(d)}
        except (OSError, ValueError):
            cfg = DEFAULT_CONFIG
        compiles = bool(cfg.get("compile"))
        self.c_compile.setChecked(compiles)
        self.compile_more.setVisible(compiles)
        self.a_scratch.setEnabled(compiles)
        mode = cfg.get("shell_escape")
        self.p_mode.setCurrentIndex(2 if mode is True else 1 if mode == "restricted" else 0)
        self.p_review.setEnabled(mode is True)
        is_git = (d / ".git").exists()
        self.c_git.setEnabled(is_git)
        self.c_git.setChecked(bool(cfg.get("git")) and is_git)
        self.git_note.setVisible(not is_git)
        text = "\n".join(self.activity.lines.get(str(d), []))
        if text != self.log.toPlainText():
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # -- actions
    def join(self, link: str = "", from_web: bool = False) -> None:
        self.show_window()
        JoinDialog(self.engine, self, link, from_web).exec()
        self.refresh()

    def keep_list_updated(self, force: bool = False) -> None:
        """Keep the project list in step: when this computer's folders or tags change, when the window
        comes to the front, and every half hour."""
        if self.list_busy or not listsync.saved_link():
            return
        folders = {str(d) for d in registry.folders()}
        if folders == self.list_folders and not force and not self.tags_dirty and time.time() - self.list_at < 1800:
            return
        self.tags_dirty = False

        def work() -> None:
            try:
                listsync.sync_this_computer()
            except Exception as e:  # noqa: BLE001 - offline, say: the next try will do
                log.debug("project list: %s", e)
            finally:
                self.list_folders, self.list_at, self.list_busy = folders, time.time(), False

        self.list_busy = True
        threading.Thread(target=work, daemon=True).start()

    def project_list(self, link: str = "", from_web: bool = False) -> None:
        self.show_window()
        ProjectListDialog(self, link, from_web).exec()
        self.refresh()

    def add_existing(self) -> None:
        """A folder already set up with texloom (e.g. left earlier, or cloned on the command line)."""
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("A texloom folder"), str(default_parent()))
        if not d:
            return
        d = Path(d)
        if not (d / ".texloom" / "config.json").is_file():
            QtWidgets.QMessageBox.warning(self, _("Add folder"), _(
                "{d} isn’t a TeXLoom folder yet. To turn it into a project, use Add project ▸ New project from a folder….", d=d))
            return
        if not registry.add(d):
            self.statusBar().showMessage(_("That folder is already on the list."), 4000)
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def share_folder(self) -> None:
        if ShareFolderDialog(self.engine, self).exec():
            self.refresh()
            self.statusBar().showMessage(_("The folder is now a TeXLoom project. To invite people, use Share…."), 10000)

    def open_folder(self) -> None:
        if d := self.current():
            open_path(d)

    def share_links(self) -> None:
        if d := self.current():
            LinksDialog(d, self, self.engine, project_name(d, self.engine)).exec()

    def leave(self) -> None:
        fs = self.selected()
        if not fs:
            return
        ask = _("Stop syncing “{project}”?\n\nThe folder and its files stay where they are ({d}); you can delete them yourself.",
                project=project_name(fs[0], self.engine), d=fs[0]) if len(fs) == 1 else ngettext(
            len(fs), "Stop syncing {n} folder?\n\n{names}\n\nThe folder and its files stay where they are; you can delete it yourself.",
            "Stop syncing {n} folders?\n\n{names}\n\nThe folders and their files stay where they are; you can delete them yourself.",
            n=len(fs), names="\n".join(project_name(d, self.engine) for d in fs))
        if QtWidgets.QMessageBox.question(self, _("Leave"), ask) != QtWidgets.QMessageBox.Yes:
            return
        for d in fs:
            registry.remove(d)
        if not listsync.saved_link():  # with a list, the project stays on it and keeps its tags
            tags.keep_only({p for p in map(self.project_of, self.folders()) if p})
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def compile_from_scratch(self) -> None:
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        if st is None or st.fs is None:
            self.statusBar().showMessage(_("This folder isn’t running yet: try again in a moment."), 5000)
            return
        self.engine.call(st.fs.compile_from_scratch)
        self.statusBar().showMessage(_("Compiling {name} from scratch…", name=d.name), 5000)

    def set_option(self, key: str, on: bool) -> None:
        d = self.current()
        if d:
            update_config(d, **{key: on})
            self.engine.call(self.engine.daemon.restart, d)

    def set_programs(self, index: int) -> None:
        d = self.current()
        if not d:
            return
        mode = self.p_mode.itemData(index)
        if mode is True and QtWidgets.QMessageBox.question(
                self, _("Running programs"), _("Compiles may then run programs that the document asks for, on this "
                "computer, once you allow each line (Review lines…).\n\nAnyone with an edit link writes the "
                "document. Turn this on only for projects whose editors you trust.")) != QtWidgets.QMessageBox.Yes:
            self.show_detail()
            return
        self.set_option("shell_escape", mode)
        if mode is True:
            ProgramsDialog(self.engine, d, self).exec()

    def open_web(self) -> None:
        d = self.current()
        if d:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(load_config(d)["link"]))

    def toggle_pause(self) -> None:
        dm = self.engine.daemon
        dm.paused = not dm.paused
        self.engine.call(dm.poke)
        self.refresh()

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def changeEvent(self, e: QtCore.QEvent) -> None:
        super().changeEvent(e)
        if e.type() == QtCore.QEvent.ActivationChange and self.isActiveWindow() and time.time() - self.list_at > 60:
            self.keep_list_updated(force=True)

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        if getattr(QtWidgets.QApplication.instance(), "quitting", False):
            e.accept()
            return
        if self.tray is not None or MAC:  # keep syncing from the tray / menu bar; on a Mac the Dock icon reopens it
            e.ignore()
            self.hide()
            if self.tray is not None and not MAC and not app_pref("told_tray", False):  # on Windows and Linux, people expect closing to quit
                set_app_pref("told_tray", True)
                self.tray.showMessage("TeXLoom", _("TeXLoom keeps syncing in the background. To quit, use its icon in the notification area."),
                                      QtGui.QIcon(str(ICON)), 10000)
            return
        # No notification area (e.g. GNOME without an extension): closing would stop syncing.
        choice = app_pref("on_close", "")
        if not choice:
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setText(_("Keep syncing?"))
            box.setInformativeText(_("This desktop has no notification area for TeXLoom to wait in, so closing the window "
                                     "stops syncing. Minimise it instead to keep your folders in step."))
            keep = box.addButton(_("Minimise"), QtWidgets.QMessageBox.AcceptRole)
            leave = box.addButton(_("Quit"), QtWidgets.QMessageBox.DestructiveRole)
            box.addButton(QtWidgets.QMessageBox.Cancel)
            box.setDefaultButton(keep)
            again = QtWidgets.QCheckBox(_("Don’t ask again"))
            box.setCheckBox(again)
            box.exec()
            choice = "minimise" if box.clickedButton() is keep else "quit" if box.clickedButton() is leave else ""
            if choice and again.isChecked():
                set_app_pref("on_close", choice)
        if choice == "quit":
            e.accept()
            QtWidgets.QApplication.quit()
        else:
            e.ignore()
            if choice == "minimise":
                self.showMinimized()


# ---- links from the browser, and only one copy running --------------------------------------
def register_url_scheme() -> None:
    """Make texloom:// links open this app (Windows and Linux; macOS uses the app bundle)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        exe = shutil.which("texloom-app") or shutil.which("texloom")
        if not exe:
            return
        cmd = [exe] if exe.endswith(("texloom-app", "texloom-app.exe")) else [exe, "app"]
    try:
        if sys.platform == "win32":
            import winreg
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}")
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:TeXLoom project link")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            c = winreg.CreateKey(k, r"shell\open\command")
            winreg.SetValueEx(c, None, 0, winreg.REG_SZ, " ".join(f'"{x}"' for x in cmd) + ' "%1"')
        elif sys.platform.startswith("linux"):
            apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / "texloom-app.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=TeXLoom\nComment=Keep TeXLoom projects in sync with folders\n"
                f"Exec={' '.join(cmd)} %u\nIcon={ICON}\nTerminal=false\nCategories=Office;\n"
                f"MimeType=x-scheme-handler/{SCHEME};\n")
            subprocess.run(["xdg-mime", "default", "texloom-app.desktop", f"x-scheme-handler/{SCHEME}"],
                           capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - optional nicety
        log.info("couldn't register texloom:// links: %s", e)


class App(QtWidgets.QApplication):
    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.on_url = lambda url: None
        self.quitting = False

    def event(self, e: QtCore.QEvent) -> bool:
        if e.type() == QtCore.QEvent.FileOpen and e.url().scheme() == SCHEME:  # macOS delivers texloom:// URLs here
            self.on_url(e.url().toString())
            return True
        if e.type() == QtCore.QEvent.Quit:  # Quit (menu, tray, ⌘Q, Dock, logging out) first asks windows to close: let them
            self.quitting = True
        return super().event(e)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    hidden = "--hidden" in argv
    urls = [a for a in argv if a.startswith(f"{SCHEME}:")]

    app = App([sys.argv[0]])
    app.setApplicationName("TeXLoom")
    app.setWindowIcon(QtGui.QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)

    # One copy per user: a second launch (e.g. from a texloom:// link) hands over and exits.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if sock.waitForConnected(300):
        sock.write(("\n".join(urls) or "show").encode())
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return
    QLocalServer.removeServer(SOCKET)
    server = QLocalServer()
    server.listen(SOCKET)

    global bridge
    bridge = Bridge()
    autostart.log_file().parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(folder)s%(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(autostart.log_file(), encoding="utf-8")])
    activity = Activity()
    logging.getLogger().addHandler(activity)
    for h in logging.getLogger().handlers:
        h.addFilter(FolderTag())
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    FOLDER.set("")

    engine = Engine()
    win = Window(engine, activity)

    def handle(url: str) -> None:
        sync_link = sync_link_from_url(url)
        link = None if sync_link else link_from_url(url)
        if sync_link:
            win.project_list(sync_link, from_web=True)
        elif link:
            win.join(link, from_web=True)
        else:
            win.show_window()

    app.on_url = handle

    def incoming() -> None:
        c = server.nextPendingConnection()
        c.waitForReadyRead(1000)
        msg = bytes(c.readAll()).decode(errors="replace").strip()
        for line in msg.splitlines() or ["show"]:
            handle(line) if line.startswith(f"{SCHEME}:") else win.show_window()

    server.newConnection.connect(incoming)

    tray = None
    if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(str(ICON)))
        menu = QtWidgets.QMenu()
        menu.addAction(_("Open TeXLoom"), win.show_window)
        menu.addAction(_("New project from a folder…"), lambda: (win.show_window(), win.share_folder()))
        menu.addAction(_("Join a project with a link…"), lambda: win.join())
        menu.addSeparator()
        pause = menu.addAction(_("Pause syncing"), win.toggle_pause)
        menu.aboutToShow.connect(lambda: pause.setText(_("Resume syncing") if engine.daemon.paused else _("Pause syncing")))
        menu.addAction(_("Quit TeXLoom"), app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip("TeXLoom")
        tray.activated.connect(lambda reason: win.show_window() if reason == QtWidgets.QSystemTrayIcon.Trigger else None)
        tray.show()
        win.tray = tray

    if MAC:  # clicking the Dock icon brings the window back, as in other Mac apps
        started = time.time()
        app.applicationStateChanged.connect(lambda state: state == Qt.ApplicationActive and not win.isVisible()
                                            and time.time() - started > 3 and win.show_window())

    register_url_scheme()
    engine.start()
    if not hidden or tray is None:
        win.show_window()
    for u in urls:
        QtCore.QTimer.singleShot(0, lambda u=u: handle(u))
    log.info("TeXLoom app %s started", __version__)
    app.exec()


if __name__ == "__main__":
    main()
