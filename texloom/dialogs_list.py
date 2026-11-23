"""The desktop app's dialog for syncing the list of projects between computers."""

from __future__ import annotations

import threading
from urllib.parse import urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync, tags
from .i18n import _, ngettext
from .ui import AMBER, grey_note, known_server, red


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
