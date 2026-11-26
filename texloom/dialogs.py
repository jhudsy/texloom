"""The desktop app's dialogs: settings, programs a project asks to run, copying a folder. The others are
in dialogs_share.py (sharing and joining), dialogs_list.py (the project list), dialogs_tags.py and
dialogs_tools.py (AI tools and editors)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from PySide6 import QtGui, QtWidgets

from . import autostart, listsync
from .cli import new_project
from .config import load, load_config
from .crypto import Keys
from .folder import copy_project_files
from .i18n import _, app_pref, lang_pref, set_app_pref, set_lang_pref
from .locales import NAMES
from .paths import state_dir
from .dialogs_share import CodeField, NameBox
from .ui import AMBER, GREY, MAC, Dialog, Engine, known_server, note, program_what, project_name, red, server_choices, shade, slug, tint


async def program_lines(engine: Engine, d: Path) -> tuple[list, set[str]]:
    """Lines in a folder's project that ask to run programs (shell escape), and those allowed."""
    from .replica import Replica
    from .scan import allowed, binaries_on_disk, shell_lines
    st = engine.daemon.states.get(d)
    r = st.replica if st and st.replica is not None else Replica(state_dir(d), *load(d))
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
        lay.addWidget(note(_("Anyone with an edit link can change these lines, and TeX macros can hide a command "
                             "from this list, so allow this only for projects whose editors you trust."), AMBER))
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
                it.setForeground(0, QtGui.QColor(shade(AMBER)))
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


class CopyFolderDialog(Dialog):
    """Start a new project from a folder's current files."""

    def __init__(self, engine: Engine, src: Path, parent=None) -> None:
        super().__init__(parent)
        self.engine, self.src = engine, src
        self.setWindowTitle(_("Make a copy"))
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)  # macOS keeps fields narrow otherwise
        lay.addRow(note(_("The copy is a new project of your own, with its own links, in a folder of its own. It starts "
                          "with the current files and settings, without the history, comments or collaborators, and "
                          "it counts towards your storage."), None))
        self.name = QtWidgets.QLineEdit(_("Copy of {name}", name=project_name(src, engine)))
        self.name.textEdited.connect(self.suggest_folder)
        lay.addRow(_("Project name"), self.name)
        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit(str(self.free_folder()))
        choose = QtWidgets.QPushButton(_("Choose…"))
        choose.setToolTip(_("Choose where the copy’s folder goes"))
        choose.clicked.connect(self.choose)
        row.addWidget(self.folder, 1)
        row.addWidget(choose)
        lay.addRow(_("Folder"), row)
        self.code = CodeField(engine, code=str(load_config(src)["create_code"] or ""))
        lay.addRow(_("Creation code"), self.code)
        try:
            self.code.source.show_for(load(src)[1])
            lay.addRow("", self.code.source)
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
        lay.addRow("", note(_("Shown to collaborators next to what you write, in every project. With a project list, it follows you to your other computers and browsers.")))
        # the creation code for new projects, one per server (as New project from a folder offers it)
        row = QtWidgets.QHBoxLayout()
        self.code = CodeField(window.engine)
        self.code.editingFinished.connect(self.code_changed)
        self.code_server = QtWidgets.QComboBox()
        self.code_server.setToolTip(_("The server this creation code is for: each server has its own"))
        self.code_server.currentIndexChanged.connect(lambda _i: self.show_code())
        row.addWidget(self.code, 1)
        row.addWidget(self.code_server)
        lay.addRow(_("Creation code"), row)
        self.code_note = note("")
        lay.addRow("", self.code_note)
        tint(self.code.source, GREY)  # a note, as the one above (its link still works)
        lay.addRow("", self.code.source)
        self.login = QtWidgets.QCheckBox(_("Start TeXLoom when I log in, so folders keep syncing"))
        self.login.setChecked(autostart.status() != "off")
        self.login.setToolTip(_("Folders sync only while TeXLoom is running"))
        self.login.toggled.connect(self.set_autostart)
        lay.addRow(_("Startup"), self.login)
        self.ask_quit = QtWidgets.QCheckBox(_("Ask before quitting, since folders then stop syncing"))
        self.ask_quit.setChecked(bool(app_pref("ask_before_quit", True)))
        self.ask_quit.setToolTip(_("Once TeXLoom quits, changes in your folders reach no one until it starts again"))
        self.ask_quit.toggled.connect(lambda on: set_app_pref("ask_before_quit", on))
        lay.addRow("", self.ask_quit)
        self.updates = QtWidgets.QCheckBox(_("Check for new versions once a day"))
        self.updates.setChecked(bool(app_pref("check_updates", True)))
        self.updates.setToolTip(_("Asks GitHub, where TeXLoom is published, for the latest version. Nothing about you or your projects is sent."))
        self.updates.toggled.connect(lambda on: set_app_pref("check_updates", on))
        lay.addRow(_("Updates"), self.updates)
        self.allow_mcp = QtWidgets.QCheckBox(_("Let AI tools connect to my projects (MCP)"))
        self.allow_mcp.setChecked(bool(app_pref("allow_mcp", True)))
        self.allow_mcp.setToolTip(_("Lets an AI tool on this computer, such as Claude Code, read and edit your projects and their comments"))
        self.allow_mcp.toggled.connect(lambda on: set_app_pref("allow_mcp", on))
        lay.addRow(_("AI tools and editors"), self.allow_mcp)
        self.allow_lsp = QtWidgets.QCheckBox(_("Let editors show comments beside the text (language server)"))
        self.allow_lsp.setChecked(bool(app_pref("allow_lsp", True)))
        self.allow_lsp.setToolTip(_("Lets editors such as VS Code show a project’s comments beside its text"))
        self.allow_lsp.toggled.connect(lambda on: set_app_pref("allow_lsp", on))
        lay.addRow("", self.allow_lsp)
        lay.addRow("", note(_("These servers start only when an AI tool or an editor asks, and run only while that program is open. Turned off, they refuse to start on this computer.")))
        self.lang = QtWidgets.QComboBox()
        self.lang.addItem(_("Automatic (the system’s language)"), "auto")
        for code, name in NAMES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(lang_pref())))
        self.lang.setToolTip(_("The language of TeXLoom’s windows and messages"))
        self.lang.activated.connect(self.set_language)
        lay.addRow(_("Language"), self.lang)
        lay.addRow("", note(_("A new language is used the next time TeXLoom starts.")))
        row = QtWidgets.QHBoxLayout()
        self.list_state = QtWidgets.QLabel()
        open_list = QtWidgets.QPushButton(_("Project list…"))
        open_list.setAutoDefault(False)
        open_list.setToolTip(_("Connect, open or change your project list"))
        open_list.clicked.connect(lambda: window.project_list())
        row.addWidget(self.list_state, 1)
        row.addWidget(open_list)
        lay.addRow(_("Project list"), row)
        lay.addRow("", note(_("Keeps the list of your projects in step across your computers and browsers.")))
        self.msg = note("")
        lay.addRow(self.msg)
        if not MAC:
            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            bb.rejected.connect(self.accept)
            lay.addRow(bb)
        else:
            QtGui.QShortcut(QtGui.QKeySequence.Close, self, self.close)

    def showEvent(self, e) -> None:
        self.list_state.setText(_("In use on this computer") if listsync.saved_link() else _("Not set up"))
        self.fill_servers()
        super().showEvent(e)

    def fill_servers(self) -> None:
        """The servers this computer knows, the one a new project goes to by default first; a code
        may have arrived meanwhile from the project list."""
        servers = server_choices()
        keep = self.code_server.currentData() if self.code_server.count() else servers[0]
        self.code_server.blockSignals(True)
        self.code_server.clear()
        for s in servers:
            self.code_server.addItem(urlparse(s).netloc, s)
        self.code_server.setCurrentIndex(max(0, self.code_server.findData(keep)))
        self.code_server.blockSignals(False)
        self.code_server.setVisible(len(servers) > 1)
        self.show_code()

    def show_code(self) -> None:
        server = self.code_server.currentData() or known_server()
        self.code.setText(listsync.code_for(server))
        self.code_note.setText(_("Lets you keep new projects on {server}. Joining a project doesn’t need one. With a project list, "
                                 "it follows you to your other computers and browsers.", server=urlparse(server).netloc))
        self.code.source.show_for(server)

    def code_changed(self) -> None:
        server, code = self.code_server.currentData() or known_server(), self.code.text().strip()
        if not listsync.keep_code(server, code):
            return
        self.msg.setText(_("Creation code saved."))
        if listsync.saved_link():
            self.window_.keep_list_updated(force=True)  # so your other devices get it soon

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
