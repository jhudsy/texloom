"""The desktop app's dialogs for tags: a project's tags, and managing them."""

from __future__ import annotations

import functools

from PySide6 import QtWidgets
from PySide6.QtCore import Qt

from . import listsync, tags
from .i18n import _, ngettext
from .ui import Dialog, colour_names, dot, tag_hex


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
