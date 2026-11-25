"""The desktop app's dialogs for sharing and joining: a project's links, joining one from a link,
sharing a folder as a new project, and asking for a creation code."""

from __future__ import annotations

import html
import json
import platform
import re
import time
from pathlib import Path
from urllib.parse import quote

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync
from .cli import clone_project, new_project, peek, server_info
from .config import load, load_config, update_config
from .crypto import Keys
from .daemon import FolderProblem
from .i18n import _, ngettext
from .paths import state_dir, write_json
from .ui import AMBER, Dialog, Engine, default_parent, folder_of, note, open_path, red, server_choices, slug, warn


def issued_file(d: Path) -> Path:
    return state_dir(d) / "issued.json"


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
    write_json(issued_file(d), issued, private=True, indent=2)  # these links are keys
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
        keys, server = load(d)
        lay = QtWidgets.QVBoxLayout(self)

        def row(label: str, text: str, about: str = "") -> None:
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
            if about:
                lay.addWidget(note(about))

        row(_("Read-only link"), keys.link(server, write=False), _("Anyone with it can read the project."))
        if keys.seed:
            row(_("Your owner link"), keys.link(server, write=True),
                _("Full control, including making and revoking people’s links: keep it to yourself."))
            lay.addWidget(QtWidgets.QLabel(f"<b>{_('Edit links for others')}</b>"))
            lay.addWidget(note(_("Each person (or group) gets their own link, so you can later revoke one without "
                                 "affecting the others (revoking is in the web app: Open in browser → Share).")))
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
                    warn(self, _("Create link"), _("Couldn’t create it: {err}", err=err))
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
            lay.addWidget(note(_("A web page asked TeXLoom to add this project. Join only if you expected it."), AMBER))
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
        self.already = note("", AMBER)
        self.already.setTextFormat(Qt.PlainText)
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
        lay.addWidget(note(_("Collaborators’ files will be written to this folder. TeXLoom won’t write files that "
                             "LaTeX tools run as programs, but compile it with your own tools only if you trust "
                             "everyone who can edit the project.")))
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
            warn(self, _("Join"), _("{d} isn't empty. Choose a new or empty folder.", d=d))
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


class CodeField(QtWidgets.QLineEdit):
    """A creation code to type in, and `source`, where codes come from (CodeSource), for the dialog
    to show near it."""

    def __init__(self, engine: Engine, server: str = "", code: str = "", optional: bool = True) -> None:
        super().__init__(code)
        self.setPlaceholderText(_("only if the server asks for one") if optional else _("Creation code"))
        self.source = CodeSource(engine, server)


def ask_code(parent, engine: Engine, server: str, title: str, text: str, code: str = "") -> str | None:
    """Ask for a creation code, saying where codes come from. None if cancelled."""
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(480)
    lay = QtWidgets.QVBoxLayout(dlg)
    intro = QtWidgets.QLabel(text)
    intro.setWordWrap(True)
    lay.addWidget(intro)
    box = CodeField(engine, server, code, optional=False)
    lay.addWidget(box.source)
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
            lay.addRow(note(_("A web page asked TeXLoom to make a new project from a folder. Go on only if you "
                              "expected it, and check the server it goes to."), AMBER))
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
        servers = server_choices(server)
        self.server.addItems(servers)
        self.server.lineEdit().setPlaceholderText("https://your.server")
        lay.addRow(_("Server"), self.server)
        self.code = CodeField(engine, servers[0], code or listsync.code_for(servers[0]))  # yours there, or from your project list
        self.given_code = bool(code)
        self.server.currentTextChanged.connect(self.server_changed)
        lay.addRow(_("Creation code"), self.code)
        hint = QtWidgets.QLabel(_("Without a code, a server that asks for one keeps no copy: the project syncs only between "
                                  "computers that are online at the same time. You can add a code later."))
        hint.setWordWrap(True)
        hint.setEnabled(False)  # greyed, as a note
        lay.addRow("", hint)
        lay.addRow("", self.code.source)
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
        self.code.source.show_for(s)

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
        if code:  # a new code is kept, and follows your project list
            listsync.keep_code(server, code)
        if server != self.web_server:
            listsync.remember_server(server)  # the default next time
        self.made = d.resolve()
        self.engine.call(self.engine.daemon.poke)
        self.accept()
