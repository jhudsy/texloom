"""TeXLoom desktop app: the folders this computer keeps in sync, in a window and a tray icon.

It runs the same sync as `texloom start` on a background thread (a plain asyncio loop);
the Qt window lives on the main thread and only reads the folders' state or hands work
to that loop. Opening a `texloom://join?link=…` URL (from the web app's "Open in the
TeXLoom app") shows the join dialog with the link filled in; nothing is joined without
confirming there.

Install with `uv tool install 'texloom[app]'` and run `texloom app`, or download a build.

The window and main() are here, the window's parts in app_list.py (the list of projects),
app_detail.py (the chosen project) and app_updates.py; the rest of the app is in ui.py (what the
window and the dialogs share), dialogs.py, wordcount.py, app_links.py (texloom:// links, one copy
running) and mac.py.
"""

from __future__ import annotations

import collections
import html
import logging
import os
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import __version__, autostart, listsync, registry, tags
from .app_detail import ProjectDetailPart
from .app_links import SCHEME, instance_socket, parse_url, register_url_scheme
from .app_list import ProjectListPart
from .app_updates import CHANGELOG_URL, SOURCE_URL, UpdatesPart
from .config import adopt, browser_link, config_path
from .daemon import CURRENT, FOLDER, FolderTag
from .dialogs import SettingsDialog
from .dialogs_list import ProjectListDialog, expected
from .dialogs_share import JoinDialog, LinksDialog, ShareFolderDialog
from .dialogs_tools import ConnectToolDialog, EditorDialog
from .i18n import _, app_pref, ngettext, set_app_pref
from .mac import DockFollowsWindows, menu_opens_where_clicked, nsapp
from .paths import use_tex_on_path
from .ui import ICON, MAC, RED, TAGS_ROLE, Engine, Tints, attach, default_parent, describe, dot, in_background, open_path, project_name, shade, theme_icon, warn

log = logging.getLogger("texloom")
COMMENTS_EVERY = 15  # seconds between looks at the comments (counts and mention notices)
HELP_URL = "https://texloom.org/help.html"


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


# ---- the window ----------------------------------------------------------------------------


class Window(ProjectListPart, ProjectDetailPart, UpdatesPart, QtWidgets.QMainWindow):
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
        self.right = QtWidgets.QStackedWidget()  # a folder's details, or a project on the list that isn't here
        self.right.addWidget(self.build_detail())
        self.right.addWidget(self.build_away_detail())
        split.addWidget(self.right)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 640])
        self.pages.addWidget(split)
        self.setCentralWidget(self.pages)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.list_busy, self.list_at, self.list_folders, self.tags_dirty = False, 0.0, None, False
        self.row_shown: dict[str, tuple] = {}  # folder -> what its row last showed
        self.had_folders = False  # (the last refresh had folders to show)
        self.list_timer = QtCore.QTimer(self)
        self.list_timer.timeout.connect(self.keep_list_updated)
        self.list_timer.timeout.connect(lambda: self.sorting == "changed" and self.scan_dates())
        self.list_timer.start(60_000)
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(lambda: app_pref("check_updates", True) and self.check_updates())
        self.update_timer.start(24 * 3600 * 1000)
        QtCore.QTimer.singleShot(15_000, lambda: app_pref("check_updates", True) and self.check_updates())
        # the Review tab's comments, for people who work here rather than in the browser: counts in the
        # details, and a notice when a new one mentions you (the app runs on; the web app can't do that)
        self.comment_counts: dict[Path, tuple[int, int]] = {}
        self.tray_click = None  # what clicking the last notice does (a mention: the Review tab)
        self.stop_asked: set[Path] = set()  # folders asked about a stopped compile (check_stopped)
        self.comments_timer = QtCore.QTimer(self)
        self.comments_timer.timeout.connect(self.check_comments)
        self.comments_timer.start(COMMENTS_EVERY * 1000)
        QtCore.QTimer.singleShot(5_000, self.check_comments)
        QtCore.QTimer.singleShot(2_000, self.keep_list_updated)  # the projects on the list that aren't here
        self.refresh()
        self.list.setFocus()  # not the search field, which would open with a focus ring and a blinking cursor

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
        self.a_new.setToolTip(_("Turn a folder on this computer into a new project, to share with others"))
        self.a_from_git = a(_("New project from a git repository…"), lambda: self.share_folder(from_git=True), "FolderNew")
        self.a_from_git.setToolTip(_("Clone a repository, or one folder of it, and keep the project and the repository in step"))
        self.a_join.setToolTip(_("Join a project someone shared with you, from its link"))
        self.a_list = a(_("Project list…"), self.project_list)
        self.a_pause = a(_("Pause syncing"), self.toggle_pause, "MediaPlaybackPause")
        self.a_settings = a(_("Settings…"), self.settings_window, "DocumentProperties", K.Preferences, QtGui.QAction.PreferencesRole)
        self.a_close = a(_("Close window"), self.close, keys=K.Close)
        self.a_quit = a(_("Quit TeXLoom"), self.quit_app, "ApplicationExit", K.Quit, QtGui.QAction.QuitRole)
        self.a_open = a(_("Open folder"), self.open_folder, "FolderOpen", K.Open)
        self.a_web = a(_("Open in browser"), self.open_web, "GoNext", "Ctrl+B")
        self.a_share = a(_("Share…"), self.share_links, "MailSend")
        self.a_open.setToolTip(_("Open the project’s folder on this computer"))
        self.a_web.setToolTip(_("Open the project in your browser, to edit and compile it there"))
        self.a_share.setToolTip(_("Make links that invite others to this project"))
        self.a_tags = a(_("Tags…"), self.edit_tags)
        self.a_copy = a(_("Make a copy…"), self.copy_project)
        self.a_store = a(_("Keep a copy on the server…"), self.keep_on_server)
        self.a_store.setToolTip(_("This project is sync-only: with a creation code, the server keeps an encrypted copy"))
        self.a_move = a(_("Move to another code…"), self.move_to_code)
        self.a_move.setToolTip(_("Its storage counts against the code it was first stored with."))
        self.a_count = a(_("Word count…"), self.word_count)
        self.a_count.setToolTip(_("Count the words with TeXCount, from your own TeX installation"))
        self.a_scratch = a(_("Compile from scratch"), self.compile_from_scratch)
        self.a_scratch.setToolTip(_("Delete LaTeX’s working files (.aux, .bbl, …) in .texloom/build and compile again"))
        self.a_mcp = a(_("Use an AI agent on your computer…"), self.connect_tool)
        self.a_lsp = a(_("Show comments in your editor…"), lambda: EditorDialog(self).exec())
        self.a_leave = a(_("Leave…"), self.leave)
        self.a_activity = QtGui.QAction(_("Show activity"), self, checkable=True)
        self.a_activity.setChecked(bool(app_pref("show_activity", False)))
        self.a_activity.toggled.connect(self.show_activity)
        self.a_help = a(_("TeXLoom help"), lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(HELP_URL)), keys=K.HelpContents)
        self.a_news = a(_("What’s new in TeXLoom"), lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(CHANGELOG_URL)))
        self.a_report = a(_("Report a problem…"), lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"{SOURCE_URL}/issues/new")))
        self.a_about = a(_("About TeXLoom"), self.about, role=QtGui.QAction.AboutRole)
        self.a_updates = a(_("Check for updates…"), lambda: self.check_updates(asked=True), role=QtGui.QAction.ApplicationSpecificRole)
        self.a_update = a(_("Update available"), self.offer_update, "SoftwareUpdateAvailable")
        self.a_update.setVisible(False)
        self.update_info: tuple[str, str] | None = None
        self.add_menu = QtWidgets.QMenu(self)
        for act in (self.a_new, self.a_from_git, self.a_join, self.a_add):
            self.add_menu.addAction(act)
        self.more_menu = QtWidgets.QMenu(self)
        for act in (self.a_tags, self.a_copy, self.a_store, self.a_move, self.a_count, self.a_mcp, self.a_lsp, None, self.a_leave):
            self.more_menu.addSeparator() if act is None else self.more_menu.addAction(act)

    def build_toolbar(self) -> None:
        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)  # no menu offering to hide it
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if MAC else Qt.ToolButtonTextBesideIcon)
        if MAC:  # the size of the symbols in other Mac apps' toolbars
            tb.setIconSize(QtCore.QSize(20, 20))
        add = QtGui.QAction(theme_icon("ListAdd"), _("Add project"), self)
        add.setToolTip(_("Make a new project from a folder, or join one with a link"))
        tb.addAction(add)
        add_btn = tb.widgetForAction(add)
        # its menu opens on a click, below it, without the small arrow Qt draws on a button given a menu
        add.triggered.connect(lambda: self.add_menu.exec(add_btn.mapToGlobal(QtCore.QPoint(0, add_btn.height()))))
        tb.addAction(self.a_pause)
        tb.addAction(self.a_update)  # only while a newer version is out
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        if not MAC:  # on macOS, Settings is in the TeXLoom menu (⌘,)
            tb.addAction(self.a_settings)
        self.setUnifiedTitleAndToolBarOnMac(True)

    def build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu(_("File"))
        for act in (self.a_new, self.a_from_git, self.a_join, self.a_add, None, self.a_list, None, self.a_pause, None,
                    self.a_settings, self.a_close, self.a_quit):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu(_("Project"))
        for act in (self.a_open, self.a_web, self.a_share, None, self.a_tags, self.a_copy, self.a_store, self.a_move, self.a_count,
                    self.a_scratch, self.a_mcp, self.a_lsp, None, self.a_leave):
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
        m = mb.addMenu(_("Help"))  # (on a Mac, About and Check for updates move to the TeXLoom menu)
        for act in (self.a_help, self.a_news, self.a_report, None, self.a_updates, self.a_about):
            m.addSeparator() if act is None else m.addAction(act)

    def about(self) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(_("About TeXLoom"))
        box.setIconPixmap(QtGui.QIcon(str(ICON)).pixmap(64, 64))
        box.setTextFormat(Qt.RichText)
        box.setText(f"<b>TeXLoom {__version__}</b>")
        box.setInformativeText(
            html.escape(_("Keeps folders on this computer in step with TeXLoom projects.")) + "<br><br>"
            + html.escape(_("TeXLoom is free software (AGPL-3.0).")) + "<br>"
            + f"<a href='{SOURCE_URL}'>{html.escape(_('Source code'))}</a> · <a href='{HELP_URL}'>{html.escape(_('TeXLoom help'))}</a>")
        licences = box.addButton(_("Licences…"), QtWidgets.QMessageBox.ActionRole)
        box.setDefaultButton(box.addButton(QtWidgets.QMessageBox.Close))
        for label in box.findChildren(QtWidgets.QLabel):
            label.setOpenExternalLinks(True)
        box.exec()
        if box.clickedButton() is licences:
            self.show_licences()

    def show_licences(self) -> None:
        """The licences of TeXLoom and everything it bundles (assets/third-party.txt, made by scripts/licences.py)."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(_("Licences"))
        dlg.resize(680, 560)
        lay = QtWidgets.QVBoxLayout(dlg)
        text = QtWidgets.QPlainTextEdit(readOnly=True)
        text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        try:
            text.setPlainText(ICON.with_name("third-party.txt").read_text(encoding="utf-8"))
        except OSError:
            text.setPlainText(_("TeXLoom is free software (AGPL-3.0).") + f"\n{SOURCE_URL}")
        lay.addWidget(text, 1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def settings_window(self) -> None:
        if MAC:  # a window of its own, like other Mac apps' settings
            if self.settings_dlg is None:
                self.settings_dlg = SettingsDialog(self)
            self.settings_dlg.show()
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
        else:
            SettingsDialog(self).exec()

    def connect_tool(self) -> None:
        ConnectToolDialog(self.selected()[0], self).exec()

    # -- detail pane
    def show_activity(self, on: bool) -> None:
        set_app_pref("show_activity", on)
        self.act_btn.set_open(on)
        self.act_btn.setToolTip(_("Hide activity") if on else _("Show activity"))
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
            self.row_shown.clear()
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
            own = tuple(of(self.project_of(d) or ""))
            shown = (name, short, shade(colour), own)
            if self.row_shown.get(str(d)) == shown:
                continue  # redrawn only where something changed
            self.row_shown[str(d)] = shown
            it.setText(f"{name}\n{short}" + (f" · {d.name}" if d.name != name else "") + (f"\n{', '.join(t.name for t in own)}" if own else ""))
            it.setData(TAGS_ROLE, own)
            it.setIcon(dot(shade(colour)))
            it.setToolTip(str(d))
        self.refresh_away()
        away = self.has_away
        self.away.blockSignals(True)
        if not fs and self.away.count() and not self.away.selectedItems():  # nothing here: show the first to join
            self.away.setCurrentRow(0)
        elif fs and not self.had_folders:  # the first folder here (joined, or added): show it
            self.away.clearSelection()
        self.away.blockSignals(False)
        self.had_folders = bool(fs)
        self.pages.setCurrentIndex(1 if every or self.has_away else 0)
        has_list = bool(listsync.saved_link())  # (the welcome: with a list, opening it is done)
        self.welcome_list.setVisible(not has_list)
        self.welcome_new.setVisible(not has_list)
        self.list.setVisible(bool(every))  # (only projects to join: they have the whole side)
        self.fit_away()
        self.right.setVisible(bool(fs) or away)
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
        self.check_stopped()

    # -- actions
    def join(self, link: str = "", from_web: bool = False) -> None:
        self.show_window()
        JoinDialog(self.engine, self, link, from_web).exec()
        self.refresh()

    def keep_list_updated(self, force: bool = False) -> None:
        """Keep the project list in step: when this computer's folders or tags change, when the window
        comes to the front, and every half hour."""
        if not listsync.saved_link():
            self.list_data = None
            return
        if self.list_busy:
            return
        folders = {str(d) for d in registry.folders()}
        if folders == self.list_folders and not force and not self.tags_dirty and time.time() - self.list_at < 1800:
            return
        self.tags_dirty = False

        def done(found, error) -> None:
            if error is None:
                self.list_data = found
            else:  # offline, say: the next try will do
                log.debug("project list: %s", error)
                if isinstance(error, listsync.ListError) and error.code == "list-retired":
                    self.list_data = None
            self.list_folders, self.list_at, self.list_busy = folders, time.time(), False

        self.list_busy = True
        in_background(listsync.sync_this_computer, done)

    def project_list(self, link: str = "", from_web: bool = False, nonce: str = "") -> None:
        """The project list's window; with `link` from a web page (texloom://sync), which is used only
        once confirmed unless it is the list this app sent someone to their browser for (`nonce`)."""
        self.show_window()
        if (open_now := ProjectListDialog.current) is not None:  # one is open: the list goes there, not to a second
            if link:
                open_now.receive(link, from_web, nonce)
            open_now.raise_()
            open_now.activateWindow()
            return
        asked = bool(link) and from_web and expected(link, nonce)
        d = ProjectListDialog(self, link, from_web and not asked)
        if asked:  # the list this app sent someone to their browser for
            d.receive(link, from_web, nonce)
        d.exec()
        self.list_data = None if not listsync.saved_link() else self.list_data
        self.keep_list_updated(force=True)
        self.refresh()

    def add_existing(self) -> None:
        """A folder already set up with texloom (e.g. left earlier, or cloned on the command line)."""
        d = QtWidgets.QFileDialog.getExistingDirectory(self, _("A texloom folder"), str(default_parent()))
        if not d:
            return
        d = Path(d)
        if not config_path(d).is_file() and not adopt(d):
            warn(self, _("Add folder"), _(
                "{d} isn’t a TeXLoom folder yet. To turn it into a project, use Add project ▸ New project from a folder….", d=d))
            return
        if not registry.add(d):
            self.statusBar().showMessage(_("That folder is already on the list."), 4000)
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def share_folder(self, server: str = "", code: str = "", from_web: bool = False, from_git: bool = False) -> None:
        """`from_web`: asked for by the web app (Create project ▸ From a folder), so the new project
        is opened there too, which puts it on that browser's list of projects. `from_git`: New project
        from a git repository (cloned first)."""
        self.show_window()
        dlg = ShareFolderDialog(self.engine, self, server, code, from_web, from_git)
        if not dlg.exec():
            return
        self.find.clear()
        self.clear_filter()
        self.refresh()
        self.select_folder(dlg.made)
        if from_web:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(browser_link(dlg.made)))
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
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, _("Leave"), ask,
                                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, self)
        box.setTextFormat(Qt.PlainText)  # project names are chosen by collaborators
        if attach(box).exec() != QtWidgets.QMessageBox.Yes:
            return
        for d in fs:
            registry.remove(d)
        if not listsync.saved_link():  # with a list, the project stays on it and keeps its tags
            tags.keep_only({p for p in map(self.project_of, self.folders()) if p})
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def open_web(self) -> None:
        if d := self.current():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(browser_link(d)))

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
        if MAC:  # asked for from the menu-bar icon or a second launch: come in front of the app in use
            nsapp(b"activateIgnoringOtherApps:", 1)

    def changeEvent(self, e: QtCore.QEvent) -> None:
        super().changeEvent(e)
        if e.type() == QtCore.QEvent.PaletteChange and hasattr(self, "row_shown"):
            self.row_shown.clear()  # the status dots' colours, for light or dark mode
            self.refresh()
        if e.type() == QtCore.QEvent.ActivationChange and self.isActiveWindow() and time.time() - self.list_at > 60:
            self.keep_list_updated(force=True)

    def quit_app(self) -> None:
        """Quit TeXLoom from its menu, ⌘Q / Ctrl+Q or the tray: folders stop syncing, which people
        may not expect of an app that lives in the menu bar, so ask first (unless told not to).
        Logging out or shutting down quits without asking."""
        if not app_pref("ask_before_quit", True):
            QtWidgets.QApplication.quit()
            return
        box = QtWidgets.QMessageBox(self if self.isVisible() else None)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(_("Quit TeXLoom?"))
        starts = autostart.status() != "off"
        box.setInformativeText(
            _("Your folders stop syncing until TeXLoom runs again.") + " "
            + (_("It starts again when you next log in.") if starts else
               _("To keep them in sync, let it run in the background: closing the window does that too.")))
        keep = box.addButton(_("Keep running in the background"), QtWidgets.QMessageBox.RejectRole)
        stop = box.addButton(_("Quit"), QtWidgets.QMessageBox.AcceptRole)
        box.setDefaultButton(keep)
        again = QtWidgets.QCheckBox(_("Don’t ask again"))
        box.setCheckBox(again)
        box.exec()
        if box.clickedButton() is stop:
            if again.isChecked():
                set_app_pref("ask_before_quit", False)
            QtWidgets.QApplication.quit()
        elif self.tray is not None or MAC:
            self.hide()
        else:
            self.showMinimized()

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
            attach(box).exec()
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
    use_tex_on_path()  # opened from the Finder, Dock or Start menu: TeX isn't on PATH otherwise
    argv = list(sys.argv[1:] if argv is None else argv)
    hidden = "--hidden" in argv
    urls = [a for a in argv if a.startswith(f"{SCHEME}:")]

    # the name in the menu bar, and in "About TeXLoom": set before the application object, whose menus are made from it
    QtCore.QCoreApplication.setApplicationName("TeXLoom")
    if MAC and not getattr(sys, "frozen", False):  # run from source: not "python"
        from .mac import name_this_process
        name_this_process("TeXLoom")
    app = App([sys.argv[0]])
    app.setWindowIcon(QtGui.QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)
    app.installEventFilter(Tints(app))  # grey and amber text follows light and dark mode

    # One copy per user: a second launch (e.g. from a texloom:// link) hands over and exits.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    try:
        name, why = instance_socket(), ""
    except OSError as e:  # carry on without: links opened while the app runs then start another copy
        name, why = "", str(e)
    sock = QLocalSocket()
    sock.connectToServer(name)
    if name and sock.waitForConnected(300):
        if sys.platform == "win32":  # Windows lets only the app just started take the foreground: pass that on
            try:
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            except Exception:  # noqa: BLE001 - the window then comes up behind, nothing worse
                log.debug("AllowSetForegroundWindow failed", exc_info=True)
        sock.write(("\n".join(urls) or "show").encode())
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    listening = bool(name) and server.listen(name)

    autostart.private_log()  # (readable by you only: it names your folders)
    # TEXLOOM_DEBUG=1: the log file also keeps debug lines, among them errors deliberately ignored
    logging.basicConfig(level=logging.DEBUG if os.environ.get("TEXLOOM_DEBUG") == "1" else logging.INFO,
                        format="%(asctime)s %(folder)s%(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(autostart.log_file(), encoding="utf-8")])
    activity = Activity()
    activity.setLevel(logging.INFO)  # the window's activity never shows debug lines
    logging.getLogger().addHandler(activity)
    for h in logging.getLogger().handlers:
        h.addFilter(FolderTag())
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    FOLDER.set("")
    if not listening:  # this copy works on; links opened while it runs start another copy instead
        log.warning("can't take links from other launches of the app (%s): %s", name, why or server.errorString())

    engine = Engine()
    win = Window(engine, activity)

    def handle(url: str) -> None:
        match parse_url(url):
            case ("new", server, code):
                win.share_folder(server, code, from_web=True)
            case ("sync", link, nonce):
                win.project_list(link, from_web=True, nonce=nonce)
            case ("join", link):
                win.join(link, from_web=True)
            case _:
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
        menu.addAction(win.a_update)  # only while a newer version is out
        menu.addAction(_("Quit TeXLoom"), win.quit_app)
        tray.setContextMenu(menu)
        tray.setToolTip("TeXLoom")
        # A click opens the window, except on a Mac: there it opens the menu (which has Open TeXLoom),
        # as in other menu-bar apps. Bringing the window forward as well closed that menu at once
        # when the click was on another screen's menu bar.
        tray.activated.connect(lambda reason: win.show_window() if reason == QtWidgets.QSystemTrayIcon.Trigger and not MAC else None)
        tray.messageClicked.connect(win.notice_clicked)
        tray.show()
        win.tray = tray
        if MAC:
            menu_opens_where_clicked()

    if MAC:  # opening TeXLoom again (Dock, Finder, Spotlight) brings the window back, as in other Mac apps
        started = time.time()
        app.applicationStateChanged.connect(lambda state: state == Qt.ApplicationActive and not win.isVisible()
                                            and time.time() - started > 3 and win.show_window())
        if tray is not None:  # in the background the menu-bar icon is enough: no Dock icon or place in ⌘-Tab
            DockFollowsWindows(app)

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
