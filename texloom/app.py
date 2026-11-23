"""TeXLoom desktop app: the folders this computer keeps in sync, in a window and a tray icon.

It runs the same sync as `texloom start` on a background thread (a plain asyncio loop);
the Qt window lives on the main thread and only reads the folders' state or hands work
to that loop. Opening a `texloom://join?link=…` URL (from the web app's "Open in the
TeXLoom app") shows the join dialog with the link filled in; nothing is joined without
confirming there.

Install with `uv tool install 'texloom[app]'` and run `texloom app`, or download a build.

The window and main() are here; the rest of the app is in ui.py (what the window and the dialogs
share), dialogs.py, wordcount.py, app_links.py (texloom:// links, one copy running) and mac.py.
"""

from __future__ import annotations

import collections
import functools
import html
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import __version__, autostart, listsync, registry, tags
from .app_links import SCHEME, instance_socket, parse_url, register_url_scheme
from .cli import DEFAULT_CONFIG, code_ok, load, load_config, update_config
from .daemon import CURRENT, FOLDER, FolderTag
from .dialogs import (ConnectToolDialog, CopyFolderDialog, JoinDialog, LinksDialog, ManageTagsDialog, ProgramsDialog,
                      ProjectListDialog, SettingsDialog, ShareFolderDialog, TagsDialog, ask_code)
from .i18n import _, app_pref, ngettext, set_app_pref
from .mac import DockFollowsWindows, menu_opens_where_clicked, nsapp
from .paths import state_dir, use_tex_on_path
from .ui import (GREY, ICON, MAC, RED, TAGS_ROLE, Engine, browser_link, chevron, default_parent, describe, dot, move_refusal_text, usage_text,
                 grey_note, open_path, project_name, red, short_path, tag_hex, theme_icon)
from .wordcount import find_texcount, texcount_report

log = logging.getLogger("texloom")
HELP_URL = "https://github.com/jhudsy/texloom#readme"
SOURCE_URL = "https://github.com/jhudsy/texloom"
RELEASES_API = "https://api.github.com/repos/jhudsy/texloom/releases/latest"
CHANGELOG_URL = "https://texloom.org/changelog.html"


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_release() -> tuple[str, str]:
    """(version, download URL for this computer) of the newest release on GitHub. Network: call off the Qt thread."""
    import urllib.request

    from . import tls
    req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "TeXLoom"})
    with urllib.request.urlopen(req, timeout=15, context=tls.context()) as r:
        rel = json.load(r)
    asset = ("TeXLoom-macos-arm64.zip" if MAC else "TeXLoom-windows-x64.zip" if sys.platform == "win32" else "TeXLoom-linux-x64.tar.gz")
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a.get("name") == asset), rel.get("html_url") or SOURCE_URL)
    return str(rel.get("tag_name", "")).lstrip("v"), url



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
        self.row_shown: dict[str, tuple] = {}  # folder -> what its row last showed
        self.list_timer = QtCore.QTimer(self)
        self.list_timer.timeout.connect(self.keep_list_updated)
        self.list_timer.timeout.connect(lambda: self.sorting == "changed" and self.scan_dates())
        self.list_timer.start(60_000)
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(lambda: app_pref("check_updates", True) and self.check_updates())
        self.update_timer.start(24 * 3600 * 1000)
        QtCore.QTimer.singleShot(15_000, lambda: app_pref("check_updates", True) and self.check_updates())
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
        self.a_list = a(_("Project list…"), self.project_list)
        self.a_pause = a(_("Pause syncing"), self.toggle_pause, "MediaPlaybackPause")
        self.a_settings = a(_("Settings…"), self.settings_window, "DocumentProperties", K.Preferences, QtGui.QAction.PreferencesRole)
        self.a_close = a(_("Close window"), self.close, keys=K.Close)
        self.a_quit = a(_("Quit TeXLoom"), self.quit_app, "ApplicationExit", K.Quit, QtGui.QAction.QuitRole)
        self.a_open = a(_("Open folder"), self.open_folder, "FolderOpen", K.Open)
        self.a_web = a(_("Open in browser"), self.open_web, "GoNext", "Ctrl+B")
        self.a_share = a(_("Share…"), self.share_links, "MailSend")
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
        self.a_mcp = a(_("Connect an AI tool via MCP…"), self.connect_tool)
        self.a_leave = a(_("Leave…"), self.leave)
        self.a_activity = QtGui.QAction(_("Show activity"), self, checkable=True)
        self.a_activity.setChecked(bool(app_pref("show_activity", False)))
        self.a_activity.toggled.connect(self.show_activity)
        self.a_help = a(_("TeXLoom help"), lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(HELP_URL)), keys=K.HelpContents)
        self.a_about = a(_("About TeXLoom"), self.about, role=QtGui.QAction.AboutRole)
        self.a_updates = a(_("Check for updates…"), lambda: self.check_updates(asked=True), role=QtGui.QAction.ApplicationSpecificRole)
        self.a_update = a(_("Update available"), self.offer_update, "SoftwareUpdateAvailable")
        self.a_update.setVisible(False)
        self.update_info: tuple[str, str] | None = None
        self.add_menu = QtWidgets.QMenu(self)
        for act in (self.a_new, self.a_join, self.a_add):
            self.add_menu.addAction(act)
        self.more_menu = QtWidgets.QMenu(self)
        for act in (self.a_tags, self.a_copy, self.a_store, self.a_move, self.a_count, self.a_mcp, None, self.a_leave):
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
        add.setMenu(self.add_menu)
        add.setToolTip(_("Make a new project from a folder, or join one with a link"))
        tb.addAction(add)
        add_btn = tb.widgetForAction(add)
        add_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        add_btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")  # the menu says what it holds
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
        for act in (self.a_new, self.a_join, self.a_add, None, self.a_list, None, self.a_pause, None,
                    self.a_settings, self.a_close, self.a_quit):
            m.addSeparator() if act is None else m.addAction(act)
        m = mb.addMenu(_("Project"))
        for act in (self.a_open, self.a_web, self.a_share, None, self.a_tags, self.a_copy, self.a_store, self.a_move, self.a_count,
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
        m.addAction(self.a_updates)
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
        lay.setContentsMargins(8, 8, 0, 8)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText(_("Search by name or tag…"))
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _text: self.refresh())
        self.view_btn = QtWidgets.QToolButton()
        self.view_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.view_btn.setStyleSheet("QToolButton { border: none; border-radius: 4px; padding: 0 6px; }"
                                    "QToolButton:hover, QToolButton:pressed { background: palette(midlight); }"
                                    "QToolButton::menu-indicator { image: none; }")
        self.view_menu = QtWidgets.QMenu(self.view_btn)
        self.view_menu.aboutToShow.connect(self.fill_view_menu)
        self.view_btn.setMenu(self.view_menu)
        self.view_btn.setFixedHeight(self.find.sizeHint().height())
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
            for act in (self.a_copy, self.a_store, self.a_move, self.a_count, self.a_mcp):
                if act.isVisible():
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

    def keep_on_server(self) -> None:
        """A sync-only project (no creation code): with a code, the server keeps a copy again."""
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        r = st.replica if st else None
        if not d or r is None:
            return
        server = r.server
        code = ask_code(self, self.engine, server, _("Keep a copy on the server"),
                        _("The server keeps an encrypted copy of a project only with a creation code. "
                          "With one, your other computers and collaborators get changes even when you're not online "
                          "together, and the project opens anywhere in the browser."), listsync.code_for(server))
        code = (code or "").strip()
        if not code:
            return

        def checked(good, err) -> None:
            if err or not good:
                QtWidgets.QMessageBox.warning(self, _("Keep a copy on the server"),
                                              _("The server didn't accept that code.") if not err else
                                              _("Couldn't reach the server: {err}", err=err))
                return
            update_config(d, create_code=code)
            if code != listsync.code_for(server):
                listsync.remember_code(server, code)

            def switch() -> None:  # on the sync thread: the setting goes to everyone, then sync starts again with the code
                r.set_storage("server")
                self.engine.daemon.restart(d)
            self.engine.call(switch)
            self.statusBar().showMessage(_("Uploading a copy to the server…"), 5000)

        self.engine.submit(code_ok(server, code), checked)

    def move_to_code(self) -> None:
        """The owner moves a stored project to another creation code (issue #32)."""
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        r = st.replica if st else None
        if not d or r is None:
            return
        title = _("Move to another creation code")
        code = ask_code(self, self.engine, r.server, title,
                        _("This project’s storage counts against the creation code it was first stored with. Move it to another code, "
                          "for example when that code has lapsed, or when someone else’s plan should hold the project. "
                          "Links, collaborators and history stay as they are."), listsync.code_for(r.server))
        code = (code or "").strip()
        if not code:
            return

        def done(no, err) -> None:
            if err or no:
                QtWidgets.QMessageBox.warning(self, title, move_refusal_text(no) if no else _("Couldn't reach the server: {err}", err=err))
                return
            update_config(d, create_code=code)  # this copy holds the project's code from now on
            if not listsync.code_for(r.server):
                listsync.remember_code(r.server, code)
            self.statusBar().showMessage(_("The project now counts against the new code."), 5000)
            self.engine.submit(r.ask_usage(), lambda _res, _err: None)
        self.engine.submit(r.move_to_code(code), done)

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
        self.title.setTextFormat(Qt.PlainText)  # a project's name is chosen by collaborators
        f = self.title.font()
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        self.title.setFont(f)
        self.path_lbl = QtWidgets.QLabel()
        self.path_lbl.linkActivated.connect(lambda _href: self.open_folder())
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        self.usage_lbl = QtWidgets.QLabel()  # how much the server keeps (ui.usage_text)
        self.usage_lbl.setWordWrap(True)
        self.usage_lbl.setStyleSheet(f"color: {GREY}")
        self.usage_asked: dict[Path, float] = {}  # when fresh figures were last asked for, per folder
        for x in (self.title, self.path_lbl, self.status_lbl, self.usage_lbl):
            lay.addWidget(x)
        lay.addSpacing(4)
        btns = QtWidgets.QHBoxLayout()
        self.buttons = []
        for act in (self.a_open, self.a_web, self.a_share):
            b = QtWidgets.QPushButton(act.text())
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
        self.no_tex = QtWidgets.QLabel(red(html.escape(_("latexmk wasn’t found on this computer, so it can’t compile here. Install TeX Live (MacTeX on a Mac, TeX Live or MiKTeX on Windows), then open TeXLoom again."))))
        self.no_tex.setWordWrap(True)
        o.addWidget(self.no_tex)
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
        self.act_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.act_btn.setAutoRaise(True)
        self.act_btn.setCursor(Qt.PointingHandCursor)
        self.act_btn.setFont(self.section("").font())
        self.act_btn.setStyleSheet("QToolButton { border: none; padding: 0; background: transparent; }")
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
        self.act_btn.setText(_("Activity"))
        self.act_btn.setIcon(chevron(self.act_btn, down=on))
        self.act_btn.setIconSize(QtCore.QSize(*[self.act_btn.fontMetrics().height()] * 2))
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
            shown = (name, short, colour, own)
            if self.row_shown.get(str(d)) == shown:
                continue  # redrawn only where something changed
            self.row_shown[str(d)] = shown
            it.setText(f"{name}\n{short}" + (f" · {d.name}" if d.name != name else "") + (f"\n{', '.join(t.name for t in own)}" if own else ""))
            it.setData(TAGS_ROLE, own)
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
        st = self.engine.daemon.states.get(picked[0]) if one else None
        r = st.replica if st else None
        self.a_store.setVisible(bool(r and r.storage == "relay" and r.keys.seed and r.deleted_at is None))
        self.a_move.setVisible(bool(r and r.storage == "server" and r.keys.seed and r.deleted_at is None
                                    and (r.store_info or {}).get("registered")))
        for b in self.buttons:
            b.setEnabled(one)
        self.options.setVisible(not several)
        if several:
            self.title.setText(ngettext(len(picked), "{n} folder selected", "{n} folders selected", n=len(picked)))
            self.path_lbl.setText("")
            self.status_lbl.setText("")
            self.usage_lbl.setText("")
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
        st = self.engine.daemon.states.get(d)
        r = st.replica if st else None
        usage = usage_text(r.store_info, r.storage) if r else ""
        self.usage_lbl.setText(usage)
        self.usage_lbl.setVisible(bool(usage))
        if r is not None and time.time() - self.usage_asked.get(d, 0) > 60:  # fresh figures, once a minute while shown
            self.usage_asked[d] = time.time()
            self.engine.submit(r.ask_usage(), lambda _res, _err: None)
        try:
            cfg = {**DEFAULT_CONFIG, **load_config(d)}
        except (OSError, ValueError):
            cfg = DEFAULT_CONFIG
        compiles = bool(cfg.get("compile"))
        self.c_compile.setChecked(compiles)
        self.compile_more.setVisible(compiles)
        self.no_tex.setVisible(compiles and shutil.which("latexmk") is None)
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
        from .cli import adopt
        if not (state_dir(d) / "config.json").is_file() and not adopt(d):
            QtWidgets.QMessageBox.warning(self, _("Add folder"), _(
                "{d} isn’t a TeXLoom folder yet. To turn it into a project, use Add project ▸ New project from a folder….", d=d))
            return
        if not registry.add(d):
            self.statusBar().showMessage(_("That folder is already on the list."), 4000)
        self.engine.call(self.engine.daemon.poke)
        self.refresh()

    def share_folder(self, server: str = "", code: str = "", from_web: bool = False) -> None:
        """`from_web`: asked for by the web app (Create project ▸ From a folder), so the new project
        is opened there too, which puts it on that browser's list of projects."""
        self.show_window()
        dlg = ShareFolderDialog(self.engine, self, server, code, from_web)
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
        if box.exec() != QtWidgets.QMessageBox.Yes:
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
        if d := self.current():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(browser_link(d)))

    def toggle_pause(self) -> None:
        dm = self.engine.daemon
        dm.paused = not dm.paused
        self.engine.call(dm.poke)
        self.refresh()

    # -- new versions
    def check_updates(self, asked: bool = False) -> None:
        """Ask GitHub for the newest release (in the background). A newer one shows "Update
        available" in the toolbar and tray; `asked` (Help ▸ Check for updates…) also says when
        this is the newest, or that GitHub couldn't be reached."""
        def work() -> None:
            try:
                found, err = latest_release(), None
            except Exception as e:  # noqa: BLE001 - offline, rate-limited: try again tomorrow
                found, err = None, e
            self.engine.bridge.invoke(lambda: self.updates_found(found, err, asked))
        threading.Thread(target=work, daemon=True).start()

    def updates_found(self, found: tuple[str, str] | None, err, asked: bool) -> None:
        newer = bool(found and found[0] and version_tuple(found[0]) > version_tuple(__version__))
        self.update_info = found if newer else None
        self.a_update.setVisible(newer)
        if newer:
            self.a_update.setText(_("Update to {version}", version=found[0]))
            if self.tray is not None and not asked:
                self.tray.showMessage("TeXLoom", _("TeXLoom {version} is available.", version=found[0]), QtGui.QIcon(str(ICON)), 8000)
        if asked:
            if newer:
                self.offer_update()
            elif err is not None:
                QtWidgets.QMessageBox.warning(self, _("Check for updates"), _("Couldn’t reach GitHub to check: {err}", err=err))
            else:
                QtWidgets.QMessageBox.information(self, _("Check for updates"), _("You have the newest version, {version}.", version=__version__))

    def offer_update(self) -> None:
        if not self.update_info:
            return
        version, url = self.update_info
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(_("Update available"))
        box.setText(_("TeXLoom {version} is available. You have {current}.", version=version, current=__version__))
        frozen = getattr(sys, "frozen", False)
        box.setInformativeText(
            _("Download it, quit TeXLoom (from its menu), and put the new app in place of this one. Your folders and settings stay as they are.")
            if frozen else _("Update it the way you installed it, for example: uv tool upgrade texloom"))
        get = box.addButton(_("Download"), QtWidgets.QMessageBox.AcceptRole) if frozen else None
        news = box.addButton(_("What’s new"), QtWidgets.QMessageBox.HelpRole)
        box.setDefaultButton(box.addButton(_("Later"), QtWidgets.QMessageBox.RejectRole) if not frozen else get)
        if frozen:
            box.addButton(_("Later"), QtWidgets.QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is get:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        elif box.clickedButton() is news:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(CHANGELOG_URL))

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
        if MAC:  # asked for from the menu-bar icon or a second launch: come in front of the app in use
            nsapp(b"activateIgnoringOtherApps:", 1)

    def changeEvent(self, e: QtCore.QEvent) -> None:
        super().changeEvent(e)
        if e.type() == QtCore.QEvent.PaletteChange and hasattr(self, "act_btn"):
            self.show_activity(self.a_activity.isChecked())
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

    app = App([sys.argv[0]])
    app.setApplicationName("TeXLoom")
    app.setWindowIcon(QtGui.QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)

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

    autostart.log_file().parent.mkdir(parents=True, exist_ok=True)
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
            case ("sync", link):
                win.project_list(link, from_web=True)
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
