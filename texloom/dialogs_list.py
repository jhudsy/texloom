"""The desktop app's dialog for syncing the list of projects between computers."""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from urllib.parse import urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync
from .i18n import _, ngettext
from .ui import AMBER, Disclosure, ask, heading, in_background, known_server, note, red

log = logging.getLogger("texloom")


def NO_RECOVERY() -> str:  # a constant, translated when shown
    return _("There is no way to recover a lost list. The server can't read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren't deleted, but you'll need each project's own link to open it.")


def keep_it_safe() -> QtWidgets.QLabel:
    """The warning that a lost list can't be recovered, with more in its tooltip."""
    label = note(_("Nobody can recover a lost list, not even the server’s owner: keep the sync link safe."), AMBER)
    label.setToolTip(NO_RECOVERY())
    return label


def expected(link: str, nonce: str) -> bool:
    """Whether a list from the browser is the one this app sent someone there for a moment ago: it
    comes back with that request's nonce, from the server it was sent to."""
    if not asked_nonce or not nonce or time.time() - asked_browser_at >= EXPECTED_FOR:
        return False
    try:
        server = listsync.ListKey.from_link(link)[1]
    except ValueError:
        return False
    return hmac.compare_digest(nonce.encode(), asked_nonce.encode()) and listsync._origin(server) == listsync._origin(asked_server)


def ask_yes(parent, title: str, text: str) -> bool:
    """A question whose default answer is No."""
    return ask(parent, title, text, safe=True)


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
        lay.addWidget(note(_("Your username and password stay on this device. Only a value worked out from them reaches the server, so it never learns either one.")))
        if setting:
            lay.addWidget(note(_("A forgotten password can’t be reset, by anyone. You’d need your sync link or a passkey to set a new one. Someone who got hold of the server’s data could try to guess it, so use a long password that you don’t use anywhere else."), AMBER))
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
        if setting:
            suggest = QtWidgets.QPushButton(_("Suggest a passphrase"))
            suggest.setToolTip(_("Fill in a random passphrase that is hard to guess"))
            suggest.setAutoDefault(False)
            suggest.clicked.connect(self.suggest)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(suggest)
            row.addStretch(1)
            lay.addLayout(row)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
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
            problem = listsync.password_strength(pw, user)[1]
            if problem == "short":
                self.msg.setText(red(_("Use at least {n} characters.", n=listsync.MIN_PASSWORD)))
                return
            if problem:
                self.msg.setText(red(_("That password is too easy to guess. Make it longer and less predictable, or use a suggested passphrase.")))
                return
            if pw != self.again.text():
                self.msg.setText(red(_("The two passwords don’t match.")))
                return
        self.accept()

    def suggest(self) -> None:
        phrase = listsync.suggest_passphrase()
        for field in (self.password, self.again):
            field.setText(phrase)
            field.setEchoMode(QtWidgets.QLineEdit.Normal)
        self.msg.setText(_("Keep this passphrase in your password manager, or write it down somewhere safe."))

    @classmethod
    def ask(cls, parent, setting: bool) -> tuple[str, str] | None:
        d = cls(parent, setting)
        return (d.username.text(), d.password.text()) if d.exec() == QtWidgets.QDialog.Accepted else None


asked_browser_at = 0.0  # when this app last sent someone to their browser to unlock the list there
asked_nonce = ""  # with what nonce (it comes back with the list: expected())
asked_server = ""  # and to which server
EXPECTED_FOR = 600  # seconds: a list coming back from the browser within this is the one asked for


class ProjectListDialog(QtWidgets.QDialog):
    """The encrypted project list: set it up or open it here, then its sync settings."""

    current: ProjectListDialog | None = None  # the one open now: a list coming back from the browser goes there

    def __init__(self, window, link: str = "", from_web: bool = False) -> None:
        """`from_web`: `link` came from a web page (texloom://sync), unasked: shown, with a warning,
        to be used only by choosing Open."""
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle(_("Project list"))
        self.setMinimumWidth(600)
        lay = QtWidgets.QVBoxLayout(self)
        ProjectListDialog.current = self
        self.finished.connect(lambda _r: ProjectListDialog.current is self and setattr(ProjectListDialog, "current", None))
        if from_web and link and link == listsync.saved_link():
            from_web = False  # the list this computer already uses: nothing to decide
        self.web_warn = note(self.web_warning(link) if from_web else "", AMBER)
        self.web_warn.setVisible(from_web)
        lay.addWidget(self.web_warn)
        intro = QtWidgets.QLabel(_("Your project list follows you to your other computers and browsers. It is kept on the "
                                   "server, encrypted, and opened by a sync link that only you have."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.pages = QtWidgets.QStackedWidget()
        lay.addWidget(self.pages, 1)

        # not set up on this computer: open a list you have, or start one
        setup = QtWidgets.QWidget()
        s = QtWidgets.QVBoxLayout(setup)
        s.setContentsMargins(0, 8, 0, 0)
        s.addWidget(heading(_("Already syncing on another device?")))
        s.addWidget(note(_("Open the same list here with:")))
        row = QtWidgets.QHBoxLayout()
        passkey = QtWidgets.QPushButton(_("Passkey…"))
        passkey.setToolTip(_("Opens the server's page in your browser, which sends the list back to this app."))
        passkey.clicked.connect(self.passkey)
        password = QtWidgets.QPushButton(_("Password…"))
        password.setToolTip(_("The username and password you set for the list"))
        password.clicked.connect(self.password)
        row.addWidget(passkey)
        row.addWidget(password)
        self.link = QtWidgets.QLineEdit(link or listsync.saved_link() or "")
        self.link.setEchoMode(QtWidgets.QLineEdit.Password)
        self.link.setPlaceholderText(_("or paste its sync link"))
        use = QtWidgets.QPushButton(_("Open"))
        use.setToolTip(_("Open the list this sync link belongs to"))
        use.clicked.connect(self.use)
        self.link.returnPressed.connect(self.use)
        row.addWidget(self.link, 1)
        row.addWidget(use)
        s.addLayout(row)
        s.addSpacing(12)
        s.addWidget(heading(_("First time? Start syncing")))
        s.addWidget(note(_("Choose how you’ll open the list elsewhere. You also get a sync link, which always works.")))
        row = QtWidgets.QHBoxLayout()
        with_pw = QtWidgets.QPushButton(_("With a password…"))
        with_pw.setToolTip(_("A username and password of your choice. Nobody can reset it if you forget it."))
        with_pw.setDefault(True)
        with_pw.clicked.connect(lambda: self.start("password"))
        with_pk = QtWidgets.QPushButton(_("With a passkey…"))
        with_pk.setToolTip(_("Touch ID, Windows Hello, your phone or a security key, added in your browser. Works on this server only."))
        with_pk.clicked.connect(lambda: self.start("passkey"))
        with_link = QtWidgets.QPushButton(_("With the link only"))
        with_link.setToolTip(_("Paste the sync link into each browser or computer where you want the list."))
        with_link.clicked.connect(lambda: self.start("link"))
        for b in (with_pw, with_pk, with_link):
            row.addWidget(b)
        row.addStretch(1)
        s.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.code = QtWidgets.QLineEdit(listsync.code_for(known_server()))
        self.code.setPlaceholderText(_("Creation code (if the server needs one)"))
        row.addWidget(self.code, 1)
        s.addLayout(row)
        s.addWidget(keep_it_safe())
        s.addStretch(1)
        self.pages.addWidget(setup)

        # in use: the list's sync settings (the projects on it that aren't here are in the main window)
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

        u.addSpacing(6)
        u.addWidget(heading(_("Your sync link")))
        u.addWidget(note(_("It opens this list on any computer or browser. Save it in a password manager.")))
        row = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton(_("Copy sync link"))
        self.copy_btn.setToolTip(_("Copy the link, to paste into another browser or a password manager"))
        self.copy_btn.clicked.connect(self.copy_link)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        u.addLayout(row)
        u.addWidget(keep_it_safe())

        u.addSpacing(6)
        u.addWidget(heading(_("Other ways to open it")))
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
        self.more_btn = Disclosure(_("More options"))
        self.more_btn.opened.connect(self.show_more)
        u.addWidget(self.more_btn)
        self.more = QtWidgets.QWidget()
        m = QtWidgets.QVBoxLayout(self.more)
        m.setContentsMargins(0, 0, 0, 0)
        m.addWidget(heading(_("Copies on other servers")))
        m.addWidget(note(_("Kept on more than one server, the list outlives any one of them, and each device keeps them all up to date. Every copy is encrypted. The sync link works on any of them; passkeys only work on the server where they were added.")))
        self.copies = QtWidgets.QListWidget()
        self.copies.setMaximumHeight(90)
        self.copies.itemChanged.connect(self.copy_changed)
        m.addWidget(self.copies)
        m.addWidget(heading(_("Stop or start over")))
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


    def _server(self) -> str:
        try:
            return listsync.ListKey.from_link(self.link.text())[1]
        except ValueError:
            return known_server()

    def in_background(self, work, then, quiet: bool = False) -> None:
        """Network and password hashing are slow: keep the window responsive. `then(result)` once
        done; an error is shown in the status line instead. `quiet`: leave the status line alone
        (what's under way is only filling in the window), and call `then(None)` on an error."""
        if not quiet:
            self.status.setText(_("Working…"))

        def done(result, error) -> None:
            if error is not None and quiet:
                log.debug("project list: %s", error)
            elif error is not None:
                self.status.setText(listsync.explain(error.code) if isinstance(error, listsync.ListError) else str(error))
                return
            elif not quiet:
                self.status.setText("")
            then(result)

        in_background(work, done)

    def refresh_list(self) -> None:
        code = self.code.text().strip() or None
        self.in_background(lambda: listsync.sync_this_computer(code), self.show_list)

    def show_list(self, found) -> None:
        if found is None:
            self.count.setText(_("This computer has no project list yet."))
            return
        server, doc = found
        if hasattr(self.window_, "list_data"):
            self.window_.list_data = found  # the main window shows the projects on it that aren't here
        self.show_copies(server)
        n = len(doc["projects"])
        self.count.setText("✓ " + (ngettext(n, "Your {n} project is synced, encrypted.", "Your {n} projects are synced, encrypted.", n=n) if n
                                   else _("Your list is synced, encrypted.")) + " " + synced_ago(listsync.last_synced))
        link = listsync.saved_link()
        if not link:
            return  # stopped using the list while it synced
        key = listsync.ListKey.from_link(link)[0]
        self.in_background(lambda: listsync.read_slots(key, server), lambda slots: slots is not None and self.show_ways(slots), quiet=True)

    def show_ways(self, slots: dict) -> None:
        """The passkeys and password that open the list, each with Remove."""
        while self.ways.count():
            item = self.ways.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not slots:
            self.ways.addWidget(note(_("None yet: only the sync link opens it.")))
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
        question = _("Stop this passkey opening the list? You can also delete it from your password manager.") if passkey else _("Remove this password?")
        if not ask(self, _("Project list"), question):
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
        if not ask(self, _("Add a passkey"), _(
                "Your browser will open this list on {server}. There, confirm with Use this list, then choose Add a passkey under "
                "“Other ways to open it”. That browser keeps the list too.", server=urlparse(home).netloc)):
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(listsync.saved_link() or ""))
        self.status.setText(_("Add the passkey in your browser, then come back and choose Sync now."))

    def copy_link(self) -> None:
        QtWidgets.QApplication.clipboard().setText(listsync.saved_link() or "")
        self.copy_btn.setText(_("Copied"))
        QtCore.QTimer.singleShot(2000, lambda: self.copy_btn.setText(_("Copy sync link")))

    def show_more(self, on: bool) -> None:
        self.more.setVisible(on)

    def replace(self) -> None:
        keep = ReplaceDialog.ask(self, self._key_home()[1])
        if keep is None:
            return
        self.in_background(lambda: listsync.replace_list(keep), lambda link: (self.link.setText(link), self.refresh_list(),
                           self.status.setText(_("Done. Copy the new sync link and keep it safe. Add any passkeys and password again.") + " " + TOKENS())))

    def delete(self) -> None:
        if not ask(self, _("Delete this list"), _(
                "Delete this list from the server, for every device? Your projects aren’t deleted, and this computer keeps its folders."), safe=True):
            return
        self.in_background(listsync.delete_list, lambda _r: self.stopped(_("The list is deleted. This computer keeps its folders.")))

    def show_copies(self, home: str) -> None:
        """The servers keeping a copy of the list, and the others this computer knows, to tick."""
        self.home = home
        on, suggested = set(listsync.copy_servers(home)), listsync.suggested_copies(home)
        self.copies.blockSignals(True)
        self.copies.clear()
        for server in [*listsync.copy_servers(home), *suggested, *[x for x in listsync.known_servers() if x not in on and x not in suggested]]:
            trouble = listsync.copy_problems.get(server) if server in on else None
            label = urlparse(server).netloc + (f"  ·  {listsync.explain(trouble)}" if trouble else "") \
                + (f"  ·  {_('added elsewhere: not used here until you tick it')}" if server in suggested else "")
            item = QtWidgets.QListWidgetItem(label)
            if server in suggested:
                item.setToolTip(_("Another device, or someone else who has the sync link, added this server. Tick it only if you know it."))
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

    def start(self, way: str) -> None:
        """A new list, opened elsewhere with a password, a passkey (added in the browser) or the link only."""
        asked = PasswordDialog.ask(self, setting=True) if way == "password" else None
        if way == "password" and not asked:
            return
        server, code = known_server(), self.code.text().strip() or None
        link = listsync.ListKey.generate().link(server)
        self.link.setText(link)
        listsync.save_link(link)
        self.web_warn.setVisible(False)
        self.pages.setCurrentIndex(1)

        def work():
            found = listsync.sync_this_computer(code)
            if asked:
                listsync.set_password(listsync.ListKey.from_link(link)[0], server, *asked)
            return found

        def then(found) -> None:
            self.show_list(found)
            self.status.setText(_("Syncing. Now copy the sync link and keep it safe: it’s your way back in if you forget this password.") if asked
                                else _("Syncing. Now copy the sync link and keep it safe."))
            if way == "passkey":
                self.add_passkey()
        self.in_background(work, then)

    def passkey(self) -> None:
        global asked_browser_at, asked_nonce, asked_server
        asked_browser_at, asked_nonce, asked_server = time.time(), secrets.token_urlsafe(18), self._server()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(f"{asked_server}/#unlock-for-app={asked_nonce}"))
        self.status.setText(_("Unlock the list in your browser; it will come back here."))

    @staticmethod
    def web_warning(link: str) -> str:
        text = _("A web page asked TeXLoom to use this project list. Your folders, your creation codes and your name would be added to it, and whoever made the link could read them all. Use it only if the link is yours.")
        if listsync.saved_link() and listsync.saved_link() != link:
            text += " " + _("This computer uses a different list now: this one would take its place.")
        return text

    def receive(self, link: str, from_web: bool, nonce: str = "") -> None:
        """A list sent here (from the browser) while this window is open: used straight away if it is
        the one this app just asked for (expected), else only once confirmed (No is the default)."""
        if link == listsync.saved_link():
            self.refresh_list()
            return
        if from_web and not expected(link, nonce) and not ask_yes(self, _("Project list"), self.web_warning(link)):
            return
        global asked_browser_at, asked_nonce
        asked_browser_at, asked_nonce = 0.0, ""  # (once)
        self.web_warn.setVisible(False)
        self.adopt(link)
        self.status.setText(_("The list is open here now."))

    def password(self) -> None:
        server = self._server()
        asked = PasswordDialog.ask(self, setting=False)
        if asked:
            self.in_background(lambda: listsync.unlock_with_password(server, *asked), lambda key: self.adopt(key.link(server)))

    def off(self) -> None:
        if not ask(self, _("Project list"), _(
                "Stop using the list on this computer? Its folders stay, and other devices carry on.")):
            return
        listsync.save_link(None)
        self.stopped(_("This computer no longer uses a project list. The list itself stays on the server."))

    def stopped(self, text: str) -> None:
        self.link.clear()
        self.pages.setCurrentIndex(0)
        self.status.setText(text)


def TOKENS() -> str:  # a constant, translated when shown
    return _("Whoever had the old link could also read the Zotero, GitHub, GitLab, Gitea and Dropbox tokens on the list: revoke them on those sites and connect again.")


class ReplaceDialog(QtWidgets.QDialog):
    """Confirm replacing the sync link, choosing which other copies keep the new list (none ticked:
    whoever had the old link could have added one). As the web app."""

    def __init__(self, parent, home: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Replace the sync link"))
        self.setMinimumWidth(480)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(note(_("Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.")))
        lay.addWidget(note(TOKENS(), AMBER))
        self.boxes: list[tuple[str, QtWidgets.QCheckBox]] = []
        others = [s for s in listsync.copy_servers(home) if s != home.rstrip("/")]
        if others:
            lay.addWidget(note(_("The new list is kept on {server}. Keep it on these servers too? Tick only servers you added yourself.",
                                 server=urlparse(home).netloc)))
        for server in others:
            box = QtWidgets.QCheckBox(urlparse(server).netloc)
            lay.addWidget(box)
            self.boxes.append((server, box))
        bb = QtWidgets.QDialogButtonBox()
        cancel = bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        go = bb.addButton(_("Replace the sync link"), QtWidgets.QDialogButtonBox.DestructiveRole)
        go.setAutoDefault(False)
        cancel.setDefault(True)
        go.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        lay.addWidget(bb)

    def kept(self) -> list[str]:
        return [s for s, box in self.boxes if box.isChecked()]

    @classmethod
    def ask(cls, parent, home: str) -> list[str] | None:
        d = cls(parent, home)
        return d.kept() if d.exec() == QtWidgets.QDialog.Accepted else None
