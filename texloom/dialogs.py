"""The desktop app's dialogs: joining, sharing, links, the project list, tags, settings…"""

from __future__ import annotations

import asyncio
import functools
import html
import json
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import autostart, listsync, tags
from .cli import (
    clone_project,
    load,
    load_config,
    new_project,
    peek,
    server_info,
    update_config,
)
from .crypto import Keys
from .daemon import FolderProblem
from .folder import copy_project_files
from .i18n import _, app_pref, lang_pref, ngettext, set_app_pref, set_lang_pref
from .locales import NAMES
from .paths import STATE
from .ui import (
    AMBER,
    GREY,
    MAC,
    Dialog,
    Engine,
    colour_names,
    default_parent,
    dot,
    folder_of,
    grey_note,
    known_server,
    open_path,
    program_what,
    project_name,
    red,
    slug,
    tag_hex,
)


async def program_lines(engine: Engine, d: Path) -> tuple[list, set[str]]:
    """Lines in a folder's project that ask to run programs (shell escape), and those allowed."""
    from .replica import Replica
    from .scan import allowed, binaries_on_disk, shell_lines
    st = engine.daemon.states.get(d)
    r = st.replica if st and st.replica is not None else Replica(d / STATE, *load(d))
    return shell_lines(r.files, binaries_on_disk(d, r.bin)), allowed(d)


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
    pub, grant, personal, granted = r.keys.personal(name)
    at = int(time.time() * 1000)
    me = load_config(d).get("name") or f"TeXLoom app on {platform.node().split('.')[0]}"
    r.local_change(lambda: r.members.__setitem__(pub, {"name": granted, "at": at, "by": me, "g": grant}))
    link = personal.link(r.server)
    issued = {**load_issued(d), pub: {"name": granted, "at": at, "link": link}}
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
        lay.addWidget(QtWidgets.QLabel(_("Your name:")))
        self.me = NameBox()
        self.accepted.connect(self.me.save)
        lay.addWidget(self.me)
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
        # the name and member come from collaborators, the server from the link: shown as text, never as markup
        access = ((_("edit access as {member}", member=html.escape(info["member"])) if info["member"] else _("edit access"))
                  if info["writable"] else _("read-only"))
        self.what.setText(ngettext(info["files"], "<b>“{name}”</b> — {access}, {n} file(s), on {server}",
                                   "<b>“{name}”</b> — {access}, {n} file(s), on {server}",
                                   name=html.escape(info["name"] or _("Untitled")), access=access, n=info["files"],
                                   server=html.escape(info["server"])))
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


class NameBox(QtWidgets.QLineEdit):
    """Your name, shown to collaborators next to what you write. Kept for every project, and with
    a project list it follows you to your other computers and browsers."""

    def __init__(self) -> None:
        super().__init__(listsync.my_name()[0])
        self.setPlaceholderText(_("Shown to collaborators"))
        self.setMaxLength(64)

    def save(self) -> bool:
        """Remember it if it changed. True if it did."""
        name = self.text().strip()
        if name == listsync.my_name()[0]:
            return False
        listsync.remember_name(name)
        return True


class CodeSource(QtWidgets.QLabel):
    """Where creation codes come from: whoever runs the server, with its page about them if it
    says it has one. Never a fixed address: each server speaks for itself."""

    def __init__(self, engine: Engine, server: str = "") -> None:
        super().__init__(_("Codes come from whoever runs this server."))
        self.engine, self.server = engine, ""
        self.setWordWrap(True)
        self.setOpenExternalLinks(True)
        self.setTextFormat(Qt.RichText)
        self.show_for(server)

    def show_for(self, server: str) -> None:
        server = server.strip().rstrip("/")
        if server == self.server:
            return
        self.server = server
        self.setText(html.escape(_("Codes come from whoever runs this server.")))
        if re.match(r"https?://", server, re.I):
            self.engine.submit(server_info(server), lambda info, _err, asked=server: self.got(asked, info or {}))

    def got(self, server: str, info: dict) -> None:
        url = info.get("codesUrl")
        if server != self.server or not isinstance(url, str) or not re.match(r"https?://", url, re.I):
            return
        self.setText(f'{html.escape(_("Codes come from whoever runs this server."))} '
                     f'<a href="{html.escape(url, quote=True)}">{html.escape(_("Where to get one"))}</a>')


def ask_code(parent, engine: Engine, server: str, title: str, text: str, code: str = "") -> str | None:
    """Ask for a creation code, saying where codes come from. None if cancelled."""
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(480)
    lay = QtWidgets.QVBoxLayout(dlg)
    intro = QtWidgets.QLabel(text)
    intro.setWordWrap(True)
    lay.addWidget(intro)
    lay.addWidget(CodeSource(engine, server))
    box = QtWidgets.QLineEdit(code)
    box.setPlaceholderText(_("Creation code"))
    lay.addWidget(box)
    bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    box.setFocus()
    return box.text() if dlg.exec() == QtWidgets.QDialog.Accepted else None


class ShareFolderDialog(Dialog):
    """Share an existing folder as a new project (`texloom new`)."""

    def __init__(self, engine: Engine, parent=None, server: str = "", code: str = "", from_web: bool = False) -> None:
        """`server` and `code` come from the web app (texloom://new), to fill in."""
        super().__init__(parent)
        self.engine = engine
        # a server named by a web page is used for this project only: it doesn't become the default
        self.web_server = server.rstrip("/") if from_web else ""
        self.setWindowTitle(_("New project from a folder"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)  # macOS keeps fields narrow otherwise
        if from_web:
            warn = QtWidgets.QLabel(_("A web page asked TeXLoom to make a new project from a folder. Go on only if you "
                                      "expected it, and check the server it goes to."))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {AMBER}")
            lay.addRow(warn)
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
        # the servers this computer knows (last used first), and any other typed in
        self.server = QtWidgets.QComboBox()
        self.server.setEditable(True)
        self.server.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        start = (server or known_server()).rstrip("/")
        for s in [start, *[x for x in listsync.known_servers() if x != start]]:
            self.server.addItem(s)
        self.server.lineEdit().setPlaceholderText("https://your.server")
        lay.addRow(_("Server"), self.server)
        self.code = QtWidgets.QLineEdit(code or listsync.code_for(start))  # yours there, or from your project list
        self.code.setPlaceholderText(_("only if the server asks for one"))
        self.given_code = bool(code)
        self.server.currentTextChanged.connect(self.server_changed)
        lay.addRow(_("Creation code"), self.code)
        hint = QtWidgets.QLabel(_("Without a code, a server that asks for one keeps no copy: the project syncs only between "
                                  "computers that are online at the same time. You can add a code later."))
        hint.setWordWrap(True)
        hint.setProperty("class", "hint")
        hint.setEnabled(False)  # greyed, as a note
        lay.addRow("", hint)
        self.code_source = CodeSource(engine, start)
        lay.addRow("", self.code_source)
        self.me = NameBox()
        self.accepted.connect(self.me.save)
        lay.addRow(_("Your name"), self.me)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addRow(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.go = bb.addButton(_("Create project"), QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.clicked.connect(self.do_share)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def server_changed(self, text: str) -> None:
        """Another server chosen: its code (kept here or on your list), and where codes come from."""
        s = text.strip().rstrip("/")
        if not self.given_code:
            self.code.setText(listsync.code_for(s))
        self.code_source.show_for(s)

    def choose(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("Folder for the new project"), str(Path.home()))
        if d:
            self.folder.setText(d)
            if not self.name.text():
                self.name.setText(Path(d).name)

    def do_share(self) -> None:
        d, server = Path(self.folder.text()).expanduser(), self.server.currentText().strip().rstrip("/")
        if not d.is_dir():
            self.msg.setText(red(_("Choose an existing folder.")))
            return
        if not server.startswith(("https://", "http://")):
            self.msg.setText(red(_("The server address starts with https://")))
            return
        self.go.setEnabled(False)
        self.msg.setText(_("Uploading…"))
        code = self.code.text().strip()
        self.engine.submit(new_project(d, server, self.name.text().strip(), code),
                           lambda keys, err: self.done_(err, d, server, code))

    def done_(self, err, d: Path, server: str, code: str) -> None:
        if err:
            self.msg.setText(red(_("Couldn’t create the project: {err}", err=err)))
            self.go.setEnabled(True)
            return
        if code and code != listsync.code_for(server):  # a new code: keep it, and it follows your project list
            listsync.remember_code(server, code)
        if server != self.web_server:
            listsync.remember_server(server)  # the default next time
        self.made = d.resolve()
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
        if from_web and link and link == listsync.saved_link():
            from_web = False  # the list this computer already uses: nothing to decide
        if from_web:
            text = _("A web page asked TeXLoom to use this project list. Your folders would be added to it, and anyone who has this link could open them. Use it only if the link is yours.")
            if listsync.saved_link() and listsync.saved_link() != link:
                text += " " + _("This computer uses a different list now: this one would take its place.")
            warn = QtWidgets.QLabel(text)
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
        self.code = QtWidgets.QLineEdit(listsync.code_for(known_server()))
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
        u.addWidget(self.heading(_("Copies of this list")))
        u.addWidget(grey_note(_("Kept on more than one server, the list outlives any one of them, and each device keeps them all up to date. Every copy is encrypted. The sync link works on any of them; passkeys only work on the server where they were added.")))
        self.copies = QtWidgets.QListWidget()
        self.copies.setMaximumHeight(110)
        self.copies.itemChanged.connect(self.copy_changed)
        u.addWidget(self.copies)
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
        self.show_copies(server)
        self.count.setText(ngettext(len(doc["projects"]), "The list has {n} project.", "The list has {n} projects.", n=len(doc["projects"])))
        of = tags.reader()
        for project, entry in away:
            names = [t.name for t in of(project)]
            title = entry.get("nick") or entry.get("n") or _("Untitled project")
            elsewhere = entry.get("s") if isinstance(entry.get("s"), str) and entry["s"].rstrip("/") != server.rstrip("/") else None  # on another server
            where = f"  ·  {urlparse(elsewhere).netloc}" if elsewhere else ""
            item = QtWidgets.QListWidgetItem(title + where + (f"  ·  {', '.join(names)}" if names else ""))
            there = entry.get("s") if isinstance(entry.get("s"), str) and entry["s"].startswith(("https://", "http://")) else server
            item.setData(Qt.UserRole, f"{there.rstrip('/')}/#{entry['f']}")  # on its own server
            self.items.addItem(item)
        self.apply_find()

    def show_copies(self, home: str) -> None:
        """The servers keeping a copy of the list, and the others this computer knows, to tick."""
        self.home = home
        on = set(listsync.copy_servers(home))
        self.copies.blockSignals(True)
        self.copies.clear()
        for server in [*listsync.copy_servers(home), *[x for x in listsync.known_servers() if x not in on]]:
            trouble = listsync.copy_problems.get(server) if server in on else None
            label = urlparse(server).netloc + (f"  ·  {listsync.explain(trouble)}" if trouble else "")
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, server)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if server in on else Qt.Unchecked)
            self.copies.addItem(item)
        self.copies.blockSignals(False)

    def copy_changed(self, item) -> None:
        server, keep = item.data(Qt.UserRole), item.checkState() == Qt.Checked
        if not keep and listsync.copy_servers(self.home) == [server]:
            self.status.setText(red(_("The list needs at least one copy.")))
            self.show_copies(self.home)
            return
        listsync.set_copy(self.home, server, keep)
        self.refresh_list()  # the next sync writes it there (or stops)

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
        try:
            lay.addRow("", CodeSource(engine, load(src)[1]))
        except (OSError, ValueError, KeyError):  # a folder that has gone: the copy fails later, saying why
            pass
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
        self.me = NameBox()
        self.me.editingFinished.connect(self.name_changed)
        lay.addRow(_("Your name"), self.me)
        lay.addRow("", grey_note(_("Shown to collaborators next to what you write, in every project. With a project list, it follows you to your other computers and browsers.")))
        self.login = QtWidgets.QCheckBox(_("Start TeXLoom when I log in, so folders keep syncing"))
        self.login.setChecked(autostart.status() != "off")
        self.login.toggled.connect(self.set_autostart)
        lay.addRow(_("Startup"), self.login)
        self.ask_quit = QtWidgets.QCheckBox(_("Ask before quitting, since folders then stop syncing"))
        self.ask_quit.setChecked(bool(app_pref("ask_before_quit", True)))
        self.ask_quit.toggled.connect(lambda on: set_app_pref("ask_before_quit", on))
        lay.addRow("", self.ask_quit)
        self.updates = QtWidgets.QCheckBox(_("Check for new versions once a day"))
        self.updates.setChecked(bool(app_pref("check_updates", True)))
        self.updates.toggled.connect(lambda on: set_app_pref("check_updates", on))
        lay.addRow(_("Updates"), self.updates)
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

    def name_changed(self) -> None:
        if self.me.save() and listsync.saved_link():
            self.window_.keep_list_updated(force=True)  # so your other devices get it soon

    def set_language(self, _i: int) -> None:
        set_lang_pref(self.lang.currentData())
        self.msg.setText(_("The new language is used the next time TeXLoom starts. Quit TeXLoom (from its menu) and open it again."))

    def set_autostart(self, on: bool) -> None:
        try:
            self.msg.setText(autostart.enable(app=True) if on else autostart.disable())
        except Exception as e:  # noqa: BLE001
            self.msg.setText(red(_("Couldn’t change it: {e}", e=e)))
