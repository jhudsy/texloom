"""The app window: the list of projects (sorting, filtering by tag, selecting, its menu) and the welcome shown when there are none. Part of Window (app.py)."""

from __future__ import annotations

import functools
import html
import logging
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync, tags
from .cli import load
from .dialogs_tags import ManageTagsDialog, TagsDialog
from .i18n import _, ngettext, set_app_pref
from .ui import ICON, TAGS_ROLE, dot, project_name, tag_hex

log = logging.getLogger("texloom")


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


class ProjectListPart:
    """The project list, in Window."""

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
