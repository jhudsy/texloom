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
import getpass
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

from . import __version__, autostart, listsync, registry
from .cli import DEFAULT_CONFIG, clone_project, load_config, new_project, peek, run, update_config
from .crypto import Keys
from .daemon import CURRENT, FOLDER, Daemon, FolderProblem, FolderTag
from .i18n import _, lang_pref, ngettext, set_lang_pref
from .locales import NAMES

log = logging.getLogger("texloom")
ICON = Path(__file__).with_name("assets") / "icon.png"
SOCKET = f"texloom-app-{re.sub(r'[^A-Za-z0-9]', '', getpass.getuser())}"
SCHEME = "texloom"


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
class ProgramsDialog(QtWidgets.QDialog):
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


class LinksDialog(QtWidgets.QDialog):
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

class JoinDialog(QtWidgets.QDialog):
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


class ShareFolderDialog(QtWidgets.QDialog):
    """Share an existing folder as a new project (`texloom new`)."""

    def __init__(self, engine: Engine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle(_("Share a folder as a new project"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
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
        self.go = bb.addButton(_("Share"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.clicked.connect(self.do_share)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def choose(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("Folder to share"), str(Path.home()))
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
            self.msg.setText(red(_("Couldn’t share it: {err}", err=err)))
            self.go.setEnabled(True)
            return
        self.engine.call(self.engine.daemon.poke)
        self.accept()


class ProjectListDialog(QtWidgets.QDialog):
    """The encrypted project list: set it up, and join projects from it."""

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
        intro = QtWidgets.QLabel(_(
            "Keep the list of your projects on the server, encrypted, so it follows you to other computers and "
            "browsers. The server can't read it. It is opened by a sync link: keep that link in a password manager, "
            "because it opens every project on the list."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        no_recovery = QtWidgets.QLabel(_("There is no way to recover a lost list. The server can't read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren't deleted, but you'll need each project's own link to open it."))
        no_recovery.setWordWrap(True)
        no_recovery.setStyleSheet(f"color: {AMBER}")
        lay.addWidget(no_recovery)
        row = QtWidgets.QHBoxLayout()
        self.link = QtWidgets.QLineEdit(link or listsync.saved_link() or "")
        self.link.setEchoMode(QtWidgets.QLineEdit.Password)
        self.link.setPlaceholderText(_("Sync link"))
        use = QtWidgets.QPushButton(_("Use this link"))
        use.clicked.connect(self.use)
        copy = QtWidgets.QPushButton(_("Copy"))
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.link.text()))
        row.addWidget(self.link, 1)
        row.addWidget(use)
        row.addWidget(copy)
        lay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.code = QtWidgets.QLineEdit("")
        self.code.setPlaceholderText(_("Creation code (if the server needs one)"))
        new = QtWidgets.QPushButton(_("Start a new list"))
        new.clicked.connect(self.start)
        passkey = QtWidgets.QPushButton(_("Unlock with a passkey…"))
        passkey.setToolTip(_("Opens the server's page in your browser, which sends the list back to this app."))
        passkey.clicked.connect(self.passkey)
        password = QtWidgets.QPushButton(_("Unlock with a password…"))
        password.clicked.connect(self.password)
        off = QtWidgets.QPushButton(_("Stop on this computer"))
        off.clicked.connect(self.off)
        for w in (self.code, new, passkey, password, off):
            row.addWidget(w)
        lay.addLayout(row)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addWidget(QtWidgets.QLabel(_("Projects on the list that aren't on this computer:")))
        self.items = QtWidgets.QListWidget()
        self.items.itemDoubleClicked.connect(lambda _item: self.join())
        lay.addWidget(self.items, 1)
        row = QtWidgets.QHBoxLayout()
        join = QtWidgets.QPushButton(_("Join…"))
        join.clicked.connect(self.join)
        close = QtWidgets.QPushButton(_("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(join)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        if listsync.saved_link() and not from_web:
            self.refresh_list()

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
            self.status.setText(_("This computer has no project list yet."))
            return
        server, doc, away = found
        self.status.setText(ngettext(len(doc["projects"]), "The list has {n} project.", "The list has {n} projects.", n=len(doc["projects"])))
        for _p, entry in away:
            item = QtWidgets.QListWidgetItem(entry.get("nick") or entry.get("n") or _("Untitled project"))
            item.setData(Qt.UserRole, f"{server}/#{entry['f']}")
            self.items.addItem(item)

    def adopt(self, link: str) -> None:
        self.link.setText(link)
        listsync.save_link(link)
        self.refresh_list()

    def need_server(self, server: str) -> str:
        if not server:
            self.status.setText(_("Join or share a project first, so TeXLoom knows which server to use."))
        return server

    def use(self) -> None:
        try:
            listsync.ListKey.from_link(self.link.text())
        except ValueError:
            self.status.setText(_("That isn't a sync link."))
            return
        self.adopt(self.link.text().strip())

    def start(self) -> None:
        if server := self.need_server(known_server()):
            self.adopt(listsync.ListKey.generate().link(server))

    def passkey(self) -> None:
        if server := self.need_server(self._server()):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"{server}/#unlock-for-app"))
            self.status.setText(_("Unlock the list in your browser; it will come back here."))

    def password(self) -> None:
        server = self.need_server(self._server())
        if not server:
            return
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
        self.status.setText(_("This computer no longer uses a project list. The list itself stays on the server."))

    def join(self) -> None:
        item = self.items.currentItem()
        if item:
            self.window_.join(item.data(Qt.UserRole))
            self.refresh_list()


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
    for d in registry.folders():
        try:
            return Keys.from_link(load_config(d)["link"])[1]
        except Exception:  # noqa: BLE001
            continue
    return ""


# ---- the window ----------------------------------------------------------------------------
class Window(QtWidgets.QMainWindow):
    def __init__(self, engine: Engine, activity: Activity) -> None:
        super().__init__()
        self.engine, self.activity = engine, activity
        self.setWindowTitle("TeXLoom")
        self.setWindowIcon(QtGui.QIcon(str(ICON)))
        self.resize(860, 540)

        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.addAction(_("Join project…"), self.join)
        tb.addAction(_("Share a folder…"), self.share_folder)
        tb.addAction(_("Add existing folder…"), self.add_existing)
        tb.addAction(_("Project list…"), lambda: self.project_list())
        tb.addSeparator()
        self.pause_act = tb.addAction(_("Pause syncing"), self.toggle_pause)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.login_box = QtWidgets.QCheckBox(_("Start at login"))
        self.login_box.setChecked(autostart.status() != "off")
        self.login_box.toggled.connect(self.set_autostart)
        tb.addWidget(self.login_box)
        self.lang_box = QtWidgets.QComboBox()
        self.lang_box.setToolTip(_("Language of this app (takes effect when it restarts)"))
        self.lang_box.addItem(_("Language: automatic"), "auto")
        for code, name in NAMES.items():
            self.lang_box.addItem(name, code)
        self.lang_box.setCurrentIndex(max(0, self.lang_box.findData(lang_pref())))
        self.lang_box.currentIndexChanged.connect(self.set_language)
        tb.addWidget(self.lang_box)

        split = QtWidgets.QSplitter()
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(240)
        self.list.currentRowChanged.connect(lambda _i: self.show_detail())
        split.addWidget(self.list)
        split.addWidget(self.build_detail())
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.statusBar().addPermanentWidget(QtWidgets.QLabel(f"texloom {__version__}"))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.list_busy, self.list_at, self.list_folders = False, 0.0, None
        self.list_timer = QtCore.QTimer(self)
        self.list_timer.timeout.connect(self.keep_list_updated)
        self.list_timer.start(60_000)
        self.refresh()

    # -- detail pane
    def build_detail(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.title = QtWidgets.QLabel()
        f = self.title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.title.setFont(f)
        self.path_lbl = QtWidgets.QLabel()
        self.path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        for x in (self.title, self.path_lbl, self.status_lbl):
            lay.addWidget(x)
        btns = QtWidgets.QHBoxLayout()
        self.b_open = QtWidgets.QPushButton(_("Open folder"))
        self.b_open.clicked.connect(lambda: self.current() and open_path(self.current()))
        self.b_web = QtWidgets.QPushButton(_("Open in browser"))
        self.b_web.clicked.connect(self.open_web)
        self.b_ro = QtWidgets.QPushButton(_("Links…"))
        self.b_ro.clicked.connect(lambda: self.current() and LinksDialog(
            self.current(), self, self.engine, project_name(self.current(), self.engine)).exec())
        self.b_leave = QtWidgets.QPushButton(_("Leave…"))
        self.b_leave.clicked.connect(self.leave)
        for b in (self.b_open, self.b_web, self.b_ro):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_leave)
        lay.addLayout(btns)
        self.c_compile = QtWidgets.QCheckBox(_("Compile with my own TeX (latexmk) when files change"))
        self.c_compile.clicked.connect(lambda on: self.set_option("compile", on))
        self.c_git = QtWidgets.QCheckBox(_("Keep this folder’s git repository in step (commit, pull, push)"))
        self.c_git.clicked.connect(lambda on: self.set_option("git", on))
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(self.c_compile)
        crow.addStretch(1)
        self.b_scratch = QtWidgets.QPushButton(_("Compile from scratch"))
        self.b_scratch.setToolTip(_("Delete LaTeX’s working files (.aux, .bbl, …) in .texloom/build and compile again"))
        self.b_scratch.clicked.connect(self.compile_from_scratch)
        self.b_scratch.setEnabled(False)  # until a folder that compiles is selected
        crow.addWidget(self.b_scratch)
        lay.addLayout(crow)
        lay.addWidget(self.c_git)
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
        lay.addLayout(prow)
        lay.addWidget(QtWidgets.QLabel(_("Activity")))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.log.setFont(mono)
        lay.addWidget(self.log, 1)
        self.detail = w
        return w

    def folders(self) -> list[Path]:
        return registry.folders()

    def current(self) -> Path | None:
        it = self.list.currentItem()
        return Path(it.data(Qt.UserRole)) if it else None

    def refresh(self) -> None:
        fs = self.folders()
        keep = self.current()
        if [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())] != [str(d) for d in fs]:
            self.list.blockSignals(True)
            self.list.clear()
            for d in fs:
                it = QtWidgets.QListWidgetItem()
                it.setData(Qt.UserRole, str(d))
                self.list.addItem(it)
            self.list.blockSignals(False)
            idx = next((i for i, d in enumerate(fs) if d == keep), 0 if fs else -1)
            self.list.setCurrentRow(idx)
        problems = 0
        for i, d in enumerate(fs):
            short, colour, _longer = describe(d, self.engine)
            problems += colour == RED
            it = self.list.item(i)
            name = project_name(d, self.engine)
            it.setText(f"{name}\n{short}" + (f" · {d.name}" if d.name != name else ""))
            it.setIcon(dot(colour))
            it.setToolTip(str(d))
        self.detail.setVisible(bool(fs))
        self.statusBar().showMessage(
            _("Paused") if self.engine.daemon.paused else
            ngettext(len(fs), "{n} folder", "{n} folders", n=len(fs))
            + (" · " + ngettext(problems, "{n} not syncing", "{n} not syncing", n=problems) if problems else "") if fs else
            _("No folders yet: Join project… or Share a folder…"))
        self.pause_act.setText(_("Resume syncing") if self.engine.daemon.paused else _("Pause syncing"))
        self.show_detail()

    def show_detail(self) -> None:
        d = self.current()
        if d is None:
            return
        short, colour, longer = describe(d, self.engine)
        self.title.setText(project_name(d, self.engine))
        self.path_lbl.setText(str(d))
        self.status_lbl.setText(f"<span style='color:{colour}'>●</span> <b>{short}</b>" + (f" — {longer}" if longer else ""))
        try:
            cfg = {**DEFAULT_CONFIG, **load_config(d)}
        except (OSError, ValueError):
            cfg = DEFAULT_CONFIG
        self.c_compile.setChecked(bool(cfg.get("compile")))
        self.b_scratch.setEnabled(bool(cfg.get("compile")))
        mode = cfg.get("shell_escape")
        self.p_mode.setCurrentIndex(2 if mode is True else 1 if mode == "restricted" else 0)
        self.p_mode.setEnabled(bool(cfg.get("compile")))
        self.p_review.setEnabled(bool(cfg.get("compile")) and mode is True)
        is_git = (d / ".git").exists()
        self.c_git.setEnabled(is_git)
        self.c_git.setChecked(bool(cfg.get("git")) and is_git)
        self.c_git.setToolTip("" if is_git else _("This folder isn't a git repository (run `git init` in it first)."))
        text = "\n".join(self.activity.lines.get(str(d), []))
        if text != self.log.toPlainText():
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # -- actions
    def join(self, link: str = "", from_web: bool = False) -> None:
        self.show_window()
        JoinDialog(self.engine, self, link, from_web).exec()
        self.refresh()

    def keep_list_updated(self) -> None:
        """Keep the project list in step: when this computer's folders change, and every half hour."""
        if self.list_busy or not listsync.saved_link():
            return
        folders = {str(d) for d in registry.folders()}
        if folders == self.list_folders and time.time() - self.list_at < 1800:
            return

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
                "{d} isn’t a TeXLoom folder. To join a project, use "
                "Join project…; to share this folder as a new project, Share a folder….", d=d))
            return
        if not registry.add(d):
            self.statusBar().showMessage(_("That folder is already on the list."), 4000)
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def share_folder(self) -> None:
        if ShareFolderDialog(self.engine, self).exec():
            self.refresh()
            QtWidgets.QMessageBox.information(self, _("Shared"), _("The folder is now a TeXLoom project. Open it in the "
                                                                "browser and use Share there to invite people."))

    def leave(self) -> None:
        d = self.current()
        if d and QtWidgets.QMessageBox.question(
                self, _("Leave"), _("Stop syncing “{project}”?\n\nThe folder and its files stay "
                                    "where they are ({d}); you can delete them yourself.",
                                    project=project_name(d, self.engine), d=d)) == QtWidgets.QMessageBox.Yes:
            registry.remove(d)
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

    def set_language(self, _i: int) -> None:
        set_lang_pref(self.lang_box.currentData())
        QtWidgets.QMessageBox.information(self, _("Language"), _("The new language is used the next time TeXLoom starts. Quit TeXLoom (from its menu) and open it again."))

    def set_autostart(self, on: bool) -> None:
        try:
            msg = autostart.enable(app=True) if on else autostart.disable()
            self.statusBar().showMessage(msg, 6000)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, _("Start at login"), _("Couldn’t change it: {e}", e=e))

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore()  # keep syncing from the tray
            self.hide()
        else:
            QtWidgets.QApplication.quit()


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

    def event(self, e: QtCore.QEvent) -> bool:  # macOS delivers texloom:// URLs here
        if e.type() == QtCore.QEvent.FileOpen and e.url().scheme() == SCHEME:
            self.on_url(e.url().toString())
            return True
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
        menu.addAction(_("Join project…"), lambda: win.join())
        menu.addSeparator()
        pause = menu.addAction(_("Pause syncing"), win.toggle_pause)
        menu.aboutToShow.connect(lambda: pause.setText(_("Resume syncing") if engine.daemon.paused else _("Pause syncing")))
        menu.addAction(_("Quit TeXLoom"), app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip("TeXLoom")
        tray.activated.connect(lambda reason: win.show_window() if reason == QtWidgets.QSystemTrayIcon.Trigger else None)
        tray.show()

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
