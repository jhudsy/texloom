"""The desktop app's dialog for syncing the list of projects between computers."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync, tags
from .i18n import _, ngettext
from .ui import AMBER, chevron, grey_note, known_server, red


def NO_RECOVERY() -> str:  # a constant, translated when shown
    return _("There is no way to recover a lost list. The server can't read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren't deleted, but you'll need each project's own link to open it.")


def synced_ago(at: float) -> str:
    """When the list was last synced, in words."""
    if not at:
        return ""
    mins = int((time.time() - at) // 60)
    if mins < 1:
        return _("Last synced just now.")
    if mins < 60:
        return ngettext(mins, "Last synced {n} minute ago.", "Last synced {n} minutes ago.", n=mins)
    return _("Last synced at {time}.", time=time.strftime("%H:%M", time.localtime(at)))


class PasswordDialog(QtWidgets.QDialog):
    """A username and password: to unlock a list, or (`setting`) to set one, confirmed and checked
    as the web app does."""

    def __init__(self, parent, setting: bool) -> None:
        super().__init__(parent)
        self.setting = setting
        self.setWindowTitle(_("Set a password") if setting else _("Unlock with a password"))
        self.setMinimumWidth(440)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(grey_note(_("Your username and password stay on this device. Only a value worked out from them reaches the server, so it never learns either one.")))
        if setting:
            warn = QtWidgets.QLabel(_("A forgotten password can’t be reset, by anyone. You’d need your sync link or a passkey to set a new one. Someone who got hold of the server’s data could try to guess it, so use a long password that you don’t use anywhere else."))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {AMBER}")
            lay.addWidget(warn)
        form = QtWidgets.QFormLayout()
        self.username = QtWidgets.QLineEdit()
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.Password)
        self.again = QtWidgets.QLineEdit()
        self.again.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow(_("Username or email"), self.username)
        form.addRow(_("Password"), self.password)
        if setting:
            form.addRow(_("Password again"), self.again)
        lay.addLayout(form)
        self.msg = QtWidgets.QLabel("")
        lay.addWidget(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.check)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def check(self) -> None:
        user, pw = self.username.text(), self.password.text()
        if not user.strip() or not pw:
            return
        if self.setting:
            if len(pw) < listsync.MIN_PASSWORD:
                self.msg.setText(red(_("Use at least {n} characters.", n=listsync.MIN_PASSWORD)))
                return
            if len(set(pw)) < 6:
                self.msg.setText(red(_("That password is too easy to guess.")))
                return
            if pw != self.again.text():
                self.msg.setText(red(_("The two passwords don’t match.")))
                return
        self.accept()

    @classmethod
    def ask(cls, parent, setting: bool) -> tuple[str, str] | None:
        d = cls(parent, setting)
        return (d.username.text(), d.password.text()) if d.exec() == QtWidgets.QDialog.Accepted else None


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
        no_recovery = QtWidgets.QLabel(NO_RECOVERY())
        no_recovery.setWordWrap(True)
        no_recovery.setStyleSheet(f"color: {AMBER}")
        s.addWidget(no_recovery)
        s.addStretch(1)
        self.pages.addWidget(setup)

        # in use: the list's sync settings (and, until they move to the main window, the projects
        # on it that aren't here yet)
        inuse = QtWidgets.QWidget()
        u = QtWidgets.QVBoxLayout(inuse)
        u.setContentsMargins(0, 8, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.count = QtWidgets.QLabel("")
        self.count.setWordWrap(True)
        self.sync_btn = QtWidgets.QPushButton(_("Sync now"))
        self.sync_btn.setToolTip(_("Send and fetch changes to the list now. It also happens by itself."))
        self.sync_btn.clicked.connect(self.refresh_list)
        row.addWidget(self.count, 1)
        row.addWidget(self.sync_btn)
        u.addLayout(row)

        u.addWidget(QtWidgets.QLabel(_("Projects on the list that aren't on this computer:")))
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText(_("Search by name or tag…"))
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _text: self.apply_find())
        u.addWidget(self.find)
        self.items = QtWidgets.QListWidget()
        self.items.setMaximumHeight(130)
        self.items.itemDoubleClicked.connect(lambda _item: self.join())
        u.addWidget(self.items)
        row = QtWidgets.QHBoxLayout()
        join = QtWidgets.QPushButton(_("Join…"))
        join.clicked.connect(self.join)
        row.addWidget(join)
        row.addStretch(1)
        u.addLayout(row)

        u.addSpacing(6)
        u.addWidget(self.heading(_("Your sync link")))
        u.addWidget(grey_note(_("It opens this list on any computer or browser. Save it in a password manager.")))
        row = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton(_("Copy sync link"))
        self.copy_btn.setToolTip(_("Copy the link, to paste into another browser or a password manager"))
        self.copy_btn.clicked.connect(self.copy_link)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        u.addLayout(row)
        keep = QtWidgets.QLabel(_("Nobody can recover a lost list, not even the server’s owner: keep the sync link safe."))
        keep.setWordWrap(True)
        keep.setStyleSheet(f"color: {AMBER}")
        keep.setToolTip(NO_RECOVERY())
        u.addWidget(keep)

        u.addSpacing(6)
        u.addWidget(self.heading(_("Other ways to open it")))
        self.ways = QtWidgets.QVBoxLayout()
        self.ways.setSpacing(2)
        u.addLayout(self.ways)
        row = QtWidgets.QHBoxLayout()
        add_passkey = QtWidgets.QPushButton(_("Add a passkey…"))
        add_passkey.setToolTip(_("Opens the list in your browser, where you can add a passkey: Touch ID, Windows Hello, your phone or a security key"))
        add_passkey.clicked.connect(self.add_passkey)
        set_pw = QtWidgets.QPushButton(_("Set a password…"))
        set_pw.setToolTip(_("Open the list with a username and password of your choice. Nobody can reset it if you forget it."))
        set_pw.clicked.connect(self.set_password)
        row.addWidget(add_passkey)
        row.addWidget(set_pw)
        row.addStretch(1)
        u.addLayout(row)

        u.addSpacing(6)
        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText(_("More options"))
        self.more_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.more_btn.setAutoRaise(True)
        self.more_btn.setStyleSheet("QToolButton { border: none; padding: 2px 0; } QToolButton:checked { background: transparent; }")
        self.more_btn.setCheckable(True)
        self.more_btn.setIcon(chevron(self.more_btn, down=False))
        self.more_btn.toggled.connect(self.show_more)
        u.addWidget(self.more_btn)
        self.more = QtWidgets.QWidget()
        m = QtWidgets.QVBoxLayout(self.more)
        m.setContentsMargins(0, 0, 0, 0)
        m.addWidget(self.heading(_("Copies on other servers")))
        m.addWidget(grey_note(_("Kept on more than one server, the list outlives any one of them, and each device keeps them all up to date. Every copy is encrypted. The sync link works on any of them; passkeys only work on the server where they were added.")))
        self.copies = QtWidgets.QListWidget()
        self.copies.setMaximumHeight(90)
        self.copies.itemChanged.connect(self.copy_changed)
        m.addWidget(self.copies)
        m.addWidget(self.heading(_("Stop or start over")))
        row = QtWidgets.QHBoxLayout()
        off = QtWidgets.QPushButton(_("Stop using it here…"))
        off.setToolTip(_("This computer keeps its folders but stops syncing the list. Other devices carry on."))
        off.clicked.connect(self.off)
        replace = QtWidgets.QPushButton(_("Replace the sync link…"))
        replace.setToolTip(_("If the link has got out: a new link, and the old one stops working everywhere"))
        replace.clicked.connect(self.replace)
        delete = QtWidgets.QPushButton(_("Delete this list…"))
        delete.setToolTip(_("Delete the list from the server for every device. Projects aren’t deleted."))
        delete.clicked.connect(self.delete)
        for b in (off, replace, delete):
            row.addWidget(b)
        row.addStretch(1)
        m.addLayout(row)
        self.more.setVisible(False)
        u.addWidget(self.more)
        u.addStretch(1)
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
        n = len(doc["projects"])
        self.count.setText("✓ " + (ngettext(n, "Your {n} project is synced, encrypted.", "Your {n} projects are synced, encrypted.", n=n) if n
                                   else _("Your list is synced, encrypted.")) + " " + synced_ago(listsync.last_synced))
        key = listsync.ListKey.from_link(listsync.saved_link() or "")[0]
        self.in_background(lambda: listsync.read_slots(key, server), self.show_ways)
        of = tags.reader()
        for project, entry in away:
            names = [t.name for t in of(project)]
            title = entry.get("nick") or entry.get("n") or _("Untitled project")
            elsewhere = entry.get("s") if isinstance(entry.get("s"), str) and entry["s"].rstrip("/") != server.rstrip("/") else None  # on another server
            where = f"  ·  {urlparse(elsewhere).netloc}" if elsewhere else ""
            item = QtWidgets.QListWidgetItem(title + where + (f"  ·  {', '.join(names)}" if names else ""))
            # compat(data 0.4.29): list entries without their server (s); not converted: read as the server that keeps the list
            there = entry.get("s") if isinstance(entry.get("s"), str) and entry["s"].startswith(("https://", "http://")) else server
            item.setData(Qt.UserRole, f"{there.rstrip('/')}/#{entry['f']}")  # on its own server
            self.items.addItem(item)
        self.apply_find()

    def show_ways(self, slots: dict) -> None:
        """The passkeys and password that open the list, each with Remove."""
        while self.ways.count():
            item = self.ways.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not slots:
            self.ways.addWidget(grey_note(_("None yet: only the sync link opens it.")))
            return
        for sid, slot in sorted(slots.items(), key=lambda x: x[1].get("at", 0)):
            row = QtWidgets.QWidget()
            r = QtWidgets.QHBoxLayout(row)
            r.setContentsMargins(0, 0, 0, 0)
            when = time.strftime("%d %b %Y", time.localtime(slot.get("at", 0) / 1000))
            passkey = slot.get("kind") == listsync.PASSKEY
            r.addWidget(QtWidgets.QLabel(_("Passkey, added {when}", when=when) if passkey else _("Password, set {when}", when=when)), 1)
            remove = QtWidgets.QPushButton(_("Remove"))
            remove.setToolTip(_("Stop this passkey opening the list") if passkey else _("Stop this password opening the list"))
            remove.clicked.connect(lambda _c=False, sid=sid, slot=slot: self.remove_way(sid, slot))
            r.addWidget(remove)
            self.ways.addWidget(row)

    def _key_home(self) -> tuple[listsync.ListKey, str]:
        return listsync.ListKey.from_link(listsync.saved_link() or "")

    def remove_way(self, sid: str, slot: dict) -> None:
        passkey = slot.get("kind") == listsync.PASSKEY
        ask = _("Stop this passkey opening the list? You can also delete it from your password manager.") if passkey else _("Remove this password?")
        if QtWidgets.QMessageBox.question(self, _("Project list"), ask) != QtWidgets.QMessageBox.Yes:
            return
        key, home = self._key_home()
        self.in_background(lambda: listsync.remove_slot(key, home, sid, slot), lambda _r: self.refresh_list())

    def set_password(self) -> None:
        asked = PasswordDialog.ask(self, setting=True)
        if not asked:
            return
        key, home = self._key_home()
        self.in_background(lambda: listsync.set_password(key, home, *asked), lambda _r: (self.refresh_list(), self.status.setText(_("Password set."))))

    def add_passkey(self) -> None:
        """A passkey is made by a browser, so the list opens there, where one can be added."""
        _key, home = self._key_home()
        if QtWidgets.QMessageBox.question(self, _("Add a passkey"), _(
                "Your browser will open this list on {server}. There, choose Add a passkey under “Other ways to open it”. "
                "That browser keeps the list too.", server=urlparse(home).netloc)) != QtWidgets.QMessageBox.Yes:
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(listsync.saved_link() or ""))
        self.status.setText(_("Add the passkey in your browser, then come back and choose Sync now."))

    def copy_link(self) -> None:
        QtWidgets.QApplication.clipboard().setText(listsync.saved_link() or "")
        self.copy_btn.setText(_("Copied"))
        QtCore.QTimer.singleShot(2000, lambda: self.copy_btn.setText(_("Copy sync link")))

    def show_more(self, on: bool) -> None:
        self.more.setVisible(on)
        self.more_btn.setIcon(chevron(self.more_btn, down=on))

    def replace(self) -> None:
        if QtWidgets.QMessageBox.question(self, _("Replace the sync link"), _(
                "Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.")) != QtWidgets.QMessageBox.Yes:
            return
        self.in_background(listsync.replace_list, lambda link: (self.link.setText(link), self.refresh_list(),
                           self.status.setText(_("Done. Copy the new sync link and keep it safe. Add any passkeys and password again."))))

    def delete(self) -> None:
        if QtWidgets.QMessageBox.question(self, _("Delete this list"), _(
                "Delete this list from the server, for every device? Your projects aren’t deleted, and this computer keeps its folders.")) != QtWidgets.QMessageBox.Yes:
            return
        self.in_background(listsync.delete_list, lambda _r: self.stopped(_("The list is deleted. This computer keeps its folders.")))

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
        asked = PasswordDialog.ask(self, setting=False)
        if asked:
            self.in_background(lambda: listsync.unlock_with_password(server, *asked), lambda key: self.adopt(key.link(server)))

    def off(self) -> None:
        if QtWidgets.QMessageBox.question(self, _("Project list"), _(
                "Stop using the list on this computer? Its folders stay, and other devices carry on.")) != QtWidgets.QMessageBox.Yes:
            return
        listsync.save_link(None)
        self.stopped(_("This computer no longer uses a project list. The list itself stays on the server."))

    def stopped(self, text: str) -> None:
        self.link.clear()
        self.items.clear()
        self.pages.setCurrentIndex(0)
        self.status.setText(text)

    def join(self) -> None:
        item = self.items.currentItem()
        if item:
            self.window_.join(item.data(Qt.UserRole))
            self.refresh_list()
