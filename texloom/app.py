"""TeXLoom desktop app: the folders this computer keeps in sync, in a window and a tray icon.

It runs the same sync as `texloom start` on a background thread (a plain asyncio loop);
the Qt window lives on the main thread and only reads the folders' state or hands work
to that loop. Opening a `texloom://join?link=…` URL (from the web app's "Open in the
TeXLoom app") shows the join dialog with the link filled in; nothing is joined without
confirming there.

Install with `uv tool install 'texloom[app]'` and run `texloom app`, or download a build.
"""

from __future__ import annotations

import asyncio
import collections
import getpass
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import __version__, autostart, registry
from .cli import DEFAULT_CONFIG, clone_project, load_config, new_project, peek, run, update_config
from .crypto import Keys
from .daemon import CURRENT, FOLDER, Daemon, FolderTag

log = logging.getLogger("texloom")
ICON = Path(__file__).with_name("assets") / "icon.png"
SOCKET = f"texloom-app-{re.sub(r'[^A-Za-z0-9]', '', getpass.getuser())}"
SCHEME = "texloom"


# ---- the sync engine, on its own thread -------------------------------------------------
class Engine:
    """The asyncio side: the daemon and anything the window asks of the network."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.daemon = Daemon(lambda d: run(d, False))
        self.thread = threading.Thread(target=self._main, name="texloom-sync", daemon=True)

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.daemon.loop())
        self.loop.run_forever()

    def start(self) -> None:
        self.thread.start()

    def call(self, fn, *a) -> None:
        self.loop.call_soon_threadsafe(fn, *a)

    def submit(self, coro, done) -> None:
        """Run a coroutine on the sync thread; `done(result, error)` is called on the Qt thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: bridge.invoke(
            lambda: done(None if f.exception() else f.result(), f.exception())))


class Bridge(QtCore.QObject):
    """Runs callables on the Qt thread (signals cross threads safely)."""
    call = QtCore.Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(lambda fn: fn(), Qt.QueuedConnection)

    def invoke(self, fn) -> None:
        self.call.emit(fn)


bridge: Bridge  # created once the QApplication exists


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


# ---- what a folder is doing ---------------------------------------------------------------
GREEN, AMBER, RED, GREY = "#2e7d32", "#b26a00", "#b3261e", "#75736c"


def describe(d: Path, engine: Engine) -> tuple[str, str, str]:
    """(short status, colour, longer explanation) for a folder."""
    st = engine.daemon.states.get(d)
    if engine.daemon.paused:
        return "Paused", GREY, "Syncing is paused for all folders."
    if st is None:
        return "Starting…", GREY, ""
    if st.problem:
        return "Not syncing", RED, st.problem
    r = st.replica
    if r is None:
        return "Starting…", GREY, ""
    if not r.connected.is_set():
        return "Offline", AMBER, "Can't reach the server. Changes here are kept and sent when it's back."
    if not r.synced.is_set():
        return "Catching up…", AMBER, ""
    blobs = r.dir / "blobs-pending"
    pending = len(r.outbox) + (sum(1 for _ in blobs.iterdir()) if blobs.is_dir() else 0)
    if pending:
        return f"Sending {pending} change{'s' if pending != 1 else ''}…", AMBER, ""
    if not r.keys.writable:
        return "Up to date (read-only)", GREEN, "This link can read but not change the project: edits here aren't shared."
    return "Up to date", GREEN, ""


def project_name(d: Path, engine: Engine) -> str:
    st = engine.daemon.states.get(d)
    try:
        name = str(st.replica.meta.get("name") or "") if st and st.replica else ""
    except Exception:  # noqa: BLE001 - read from another thread; a stale value is fine
        name = ""
    return name or d.name


def dot(colour: str) -> QtGui.QIcon:
    pm = QtGui.QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(colour))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QtGui.QIcon(pm)


def open_path(p: Path | str) -> None:
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(p)))


def slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:60] or "project"


def default_parent() -> Path:
    return Path.home() / "TeXLoom"


def link_from_url(url: str) -> str | None:
    """The project link in a texloom://join?link=… URL."""
    u = urlparse(url)
    if u.scheme != SCHEME:
        return None
    link = (parse_qs(u.query).get("link") or [""])[0]
    return link if link.startswith(("https://", "http://")) and "#" in link else None


# ---- dialogs ----------------------------------------------------------------------------
class JoinDialog(QtWidgets.QDialog):
    """Paste a link → see what it is → choose a folder → join."""

    def __init__(self, engine: Engine, parent=None, link: str = "", from_web: bool = False) -> None:
        super().__init__(parent)
        self.engine, self.info = engine, None
        self.setWindowTitle("Join a project")
        self.setMinimumWidth(560)
        lay = QtWidgets.QVBoxLayout(self)
        if from_web:
            warn = QtWidgets.QLabel("A web page asked TeXLoom to add this project. Join only if you expected it.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {AMBER}")
            lay.addWidget(warn)
        lay.addWidget(QtWidgets.QLabel("Project link (from the Share dialog, or an email):"))
        row = QtWidgets.QHBoxLayout()
        self.link = QtWidgets.QLineEdit(link)
        self.link.setPlaceholderText("https://…/#p=…")
        self.check = QtWidgets.QPushButton("Check")
        self.check.clicked.connect(self.do_peek)
        row.addWidget(self.link, 1)
        row.addWidget(self.check)
        lay.addLayout(row)
        self.what = QtWidgets.QLabel("")
        self.what.setWordWrap(True)
        lay.addWidget(self.what)
        lay.addWidget(QtWidgets.QLabel("Folder on this computer:"))
        frow = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit("")
        choose = QtWidgets.QPushButton("Choose…")
        choose.clicked.connect(self.choose)
        frow.addWidget(self.folder, 1)
        frow.addWidget(choose)
        lay.addLayout(frow)
        self.compile = QtWidgets.QCheckBox("Compile with my own TeX (latexmk) when files change")
        lay.addWidget(self.compile)
        note = QtWidgets.QLabel("Collaborators’ files will be written to this folder. TeXLoom won’t write files that "
                                "LaTeX tools run as programs, but compile it with your own tools only if you trust "
                                "everyone who can edit the project.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {GREY}")
        lay.addWidget(note)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.join = self.buttons.addButton("Join", QtWidgets.QDialogButtonBox.AcceptRole)
        self.join.setEnabled(False)
        self.join.clicked.connect(self.do_join)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.link.textChanged.connect(lambda: (self.join.setEnabled(False), self.what.setText("")))
        if link:
            QtCore.QTimer.singleShot(0, self.do_peek)

    def choose(self) -> None:
        start = str(Path(self.folder.text()).parent if self.folder.text() else default_parent())
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Put the project in…", start)
        if d:
            name = Path(self.folder.text()).name if self.folder.text() else slug(self.info["name"] if self.info else "project")
            self.folder.setText(str(Path(d) / name))

    def do_peek(self) -> None:
        link = self.link.text().strip()
        try:
            Keys.from_link(link)
        except Exception as e:  # noqa: BLE001
            self.what.setText(f"<span style='color:{RED}'>That isn't a TeXLoom project link ({e}).</span>")
            return
        self.what.setText("Looking it up…")
        self.check.setEnabled(False)
        self.engine.submit(peek(link), self.peeked)

    def peeked(self, info, err) -> None:
        self.check.setEnabled(True)
        if err:
            self.what.setText(f"<span style='color:{RED}'>{err}</span>")
            return
        self.info = info
        access = (f"edit access as {info['member']}" if info["member"] else "edit access") if info["writable"] else "read-only"
        self.what.setText(f"<b>“{info['name'] or 'Untitled'}”</b> — {access}, {info['files']} file(s), on {info['server']}")
        if not self.folder.text():
            self.folder.setText(str(default_parent() / slug(info["name"] or "project")))
        self.join.setEnabled(True)

    def do_join(self) -> None:
        d = Path(self.folder.text()).expanduser()
        if not d.is_absolute():
            d = default_parent() / d
        if d.exists() and (not d.is_dir() or any(d.iterdir())):
            QtWidgets.QMessageBox.warning(self, "Join", f"{d} isn't empty. Choose a new or empty folder.")
            return
        self.join.setEnabled(False)
        self.what.setText("Downloading the project…")
        link, compile_ = self.link.text().strip(), self.compile.isChecked()

        async def go():
            await clone_project(link, d)
            if compile_:
                update_config(d, compile=True)

        self.engine.submit(go(), lambda _r, err: self.joined(d, err))

    def joined(self, d: Path, err) -> None:
        if err:
            self.what.setText(f"<span style='color:{RED}'>Couldn’t join: {err}</span>")
            self.join.setEnabled(True)
            return
        self.engine.call(self.engine.daemon.poke)
        self.accept()


class ShareFolderDialog(QtWidgets.QDialog):
    """Share an existing folder as a new project (`texloom new`)."""

    def __init__(self, engine: Engine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("Share a folder as a new project")
        self.setMinimumWidth(560)
        lay = QtWidgets.QFormLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit()
        choose = QtWidgets.QPushButton("Choose…")
        choose.clicked.connect(self.choose)
        row.addWidget(self.folder, 1)
        row.addWidget(choose)
        lay.addRow("Folder", row)
        self.name = QtWidgets.QLineEdit()
        lay.addRow("Project name", self.name)
        self.server = QtWidgets.QLineEdit(known_server())
        self.server.setPlaceholderText("https://your.server")
        lay.addRow("Server", self.server)
        self.code = QtWidgets.QLineEdit()
        self.code.setPlaceholderText("only if the server asks for one")
        lay.addRow("Creation code", self.code)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        lay.addRow(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.go = bb.addButton("Share", QtWidgets.QDialogButtonBox.AcceptRole)
        self.go.clicked.connect(self.do_share)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def choose(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder to share", str(Path.home()))
        if d:
            self.folder.setText(d)
            if not self.name.text():
                self.name.setText(Path(d).name)

    def do_share(self) -> None:
        d, server = Path(self.folder.text()).expanduser(), self.server.text().strip().rstrip("/")
        if not d.is_dir():
            self.msg.setText(f"<span style='color:{RED}'>Choose an existing folder.</span>")
            return
        if not server.startswith(("https://", "http://")):
            self.msg.setText(f"<span style='color:{RED}'>The server address starts with https://</span>")
            return
        self.go.setEnabled(False)
        self.msg.setText("Uploading…")
        self.engine.submit(new_project(d, server, self.name.text().strip(), self.code.text().strip()),
                           lambda keys, err: self.done_(err))

    def done_(self, err) -> None:
        if err:
            self.msg.setText(f"<span style='color:{RED}'>Couldn’t share it: {err}</span>")
            self.go.setEnabled(True)
            return
        self.engine.call(self.engine.daemon.poke)
        self.accept()


def known_server() -> str:
    for d in registry.folders():
        try:
            return Keys.from_link(load_config(d)["link"])[1]
        except Exception:  # noqa: BLE001
            continue
    return ""


# ---- the window ----------------------------------------------------------------------------
class Window(QtWidgets.QMainWindow):
    def __init__(self, engine: Engine, activity: Activity) -> None:
        super().__init__()
        self.engine, self.activity = engine, activity
        self.setWindowTitle("TeXLoom")
        self.setWindowIcon(QtGui.QIcon(str(ICON)))
        self.resize(860, 540)

        tb = self.addToolBar("main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.addAction("Join project…", self.join)
        tb.addAction("Share a folder…", self.share_folder)
        tb.addSeparator()
        self.pause_act = tb.addAction("Pause syncing", self.toggle_pause)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.login_box = QtWidgets.QCheckBox("Start at login")
        self.login_box.setChecked(autostart.status() != "off")
        self.login_box.toggled.connect(self.set_autostart)
        tb.addWidget(self.login_box)

        split = QtWidgets.QSplitter()
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(240)
        self.list.currentRowChanged.connect(lambda _i: self.show_detail())
        split.addWidget(self.list)
        split.addWidget(self.build_detail())
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.statusBar().addPermanentWidget(QtWidgets.QLabel(f"texloom {__version__}"))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # -- detail pane
    def build_detail(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.title = QtWidgets.QLabel()
        f = self.title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.title.setFont(f)
        self.path_lbl = QtWidgets.QLabel()
        self.path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        for x in (self.title, self.path_lbl, self.status_lbl):
            lay.addWidget(x)
        btns = QtWidgets.QHBoxLayout()
        self.b_open = QtWidgets.QPushButton("Open folder")
        self.b_open.clicked.connect(lambda: self.current() and open_path(self.current()))
        self.b_web = QtWidgets.QPushButton("Open in browser")
        self.b_web.clicked.connect(self.open_web)
        self.b_ro = QtWidgets.QPushButton("Copy read-only link")
        self.b_ro.clicked.connect(self.copy_readonly)
        self.b_leave = QtWidgets.QPushButton("Leave…")
        self.b_leave.clicked.connect(self.leave)
        for b in (self.b_open, self.b_web, self.b_ro):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_leave)
        lay.addLayout(btns)
        self.c_compile = QtWidgets.QCheckBox("Compile with my own TeX (latexmk) when files change")
        self.c_compile.clicked.connect(lambda on: self.set_option("compile", on))
        self.c_git = QtWidgets.QCheckBox("Keep this folder’s git repository in step (commit, pull, push)")
        self.c_git.clicked.connect(lambda on: self.set_option("git", on))
        lay.addWidget(self.c_compile)
        lay.addWidget(self.c_git)
        lay.addWidget(QtWidgets.QLabel("Activity"))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.log.setFont(mono)
        lay.addWidget(self.log, 1)
        self.detail = w
        return w

    def folders(self) -> list[Path]:
        return registry.folders()

    def current(self) -> Path | None:
        it = self.list.currentItem()
        return Path(it.data(Qt.UserRole)) if it else None

    def refresh(self) -> None:
        fs = self.folders()
        keep = self.current()
        if [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())] != [str(d) for d in fs]:
            self.list.blockSignals(True)
            self.list.clear()
            for d in fs:
                it = QtWidgets.QListWidgetItem()
                it.setData(Qt.UserRole, str(d))
                self.list.addItem(it)
            self.list.blockSignals(False)
            idx = next((i for i, d in enumerate(fs) if d == keep), 0 if fs else -1)
            self.list.setCurrentRow(idx)
        problems = 0
        for i, d in enumerate(fs):
            short, colour, _ = describe(d, self.engine)
            problems += colour == RED
            it = self.list.item(i)
            name = project_name(d, self.engine)
            it.setText(f"{name}\n{short}" + (f" · {d.name}" if d.name != name else ""))
            it.setIcon(dot(colour))
            it.setToolTip(str(d))
        self.detail.setVisible(bool(fs))
        self.statusBar().showMessage(
            "Paused" if self.engine.daemon.paused else
            f"{len(fs)} folder{'s' if len(fs) != 1 else ''}" + (f" · {problems} not syncing" if problems else "") if fs else
            "No folders yet: Join project… or Share a folder…")
        self.pause_act.setText("Resume syncing" if self.engine.daemon.paused else "Pause syncing")
        self.show_detail()

    def show_detail(self) -> None:
        d = self.current()
        if d is None:
            return
        short, colour, longer = describe(d, self.engine)
        self.title.setText(project_name(d, self.engine))
        self.path_lbl.setText(str(d))
        self.status_lbl.setText(f"<span style='color:{colour}'>●</span> <b>{short}</b>" + (f" — {longer}" if longer else ""))
        try:
            cfg = {**DEFAULT_CONFIG, **load_config(d)}
        except (OSError, ValueError):
            cfg = DEFAULT_CONFIG
        self.c_compile.setChecked(bool(cfg.get("compile")))
        is_git = (d / ".git").exists()
        self.c_git.setEnabled(is_git)
        self.c_git.setChecked(bool(cfg.get("git")) and is_git)
        self.c_git.setToolTip("" if is_git else "This folder isn't a git repository (run `git init` in it first).")
        text = "\n".join(self.activity.lines.get(str(d), []))
        if text != self.log.toPlainText():
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # -- actions
    def join(self, link: str = "", from_web: bool = False) -> None:
        self.show_window()
        JoinDialog(self.engine, self, link, from_web).exec()
        self.refresh()

    def share_folder(self) -> None:
        if ShareFolderDialog(self.engine, self).exec():
            self.refresh()
            QtWidgets.QMessageBox.information(self, "Shared", "The folder is now a TeXLoom project. Open it in the "
                                              "browser and use Share there to invite people.")

    def leave(self) -> None:
        d = self.current()
        if d and QtWidgets.QMessageBox.question(
                self, "Leave", f"Stop syncing “{project_name(d, self.engine)}”?\n\nThe folder and its files stay "
                f"where they are ({d}); you can delete them yourself.") == QtWidgets.QMessageBox.Yes:
            registry.remove(d)
            self.engine.call(self.engine.daemon.poke)
            self.refresh()

    def set_option(self, key: str, on: bool) -> None:
        d = self.current()
        if d:
            update_config(d, **{key: on})
            self.engine.call(self.engine.daemon.restart, d)

    def open_web(self) -> None:
        d = self.current()
        if d:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(load_config(d)["link"]))

    def copy_readonly(self) -> None:
        d = self.current()
        if d:
            keys, server = Keys.from_link(load_config(d)["link"])
            QtWidgets.QApplication.clipboard().setText(keys.link(server, write=False))
            self.statusBar().showMessage("Read-only link copied", 3000)

    def toggle_pause(self) -> None:
        dm = self.engine.daemon
        dm.paused = not dm.paused
        self.engine.call(dm.poke)
        self.refresh()

    def set_autostart(self, on: bool) -> None:
        try:
            msg = autostart.enable(app=True) if on else autostart.disable()
            self.statusBar().showMessage(msg, 6000)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Start at login", f"Couldn’t change it: {e}")

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e: QtGui.QCloseEvent) -> None:
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore()  # keep syncing from the tray
            self.hide()
        else:
            QtWidgets.QApplication.quit()


# ---- links from the browser, and only one copy running --------------------------------------
def register_url_scheme() -> None:
    """Make texloom:// links open this app (Windows and Linux; macOS uses the app bundle)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        exe = shutil.which("texloom-app") or shutil.which("texloom")
        if not exe:
            return
        cmd = [exe] if exe.endswith(("texloom-app", "texloom-app.exe")) else [exe, "app"]
    try:
        if sys.platform == "win32":
            import winreg
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{SCHEME}")
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:TeXLoom project link")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            c = winreg.CreateKey(k, r"shell\open\command")
            winreg.SetValueEx(c, None, 0, winreg.REG_SZ, " ".join(f'"{x}"' for x in cmd) + ' "%1"')
        elif sys.platform.startswith("linux"):
            apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / "texloom-app.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=TeXLoom\nComment=Keep TeXLoom projects in sync with folders\n"
                f"Exec={' '.join(cmd)} %u\nIcon={ICON}\nTerminal=false\nCategories=Office;\n"
                f"MimeType=x-scheme-handler/{SCHEME};\n")
            subprocess.run(["xdg-mime", "default", "texloom-app.desktop", f"x-scheme-handler/{SCHEME}"],
                           capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - optional nicety
        log.info("couldn't register texloom:// links: %s", e)


class App(QtWidgets.QApplication):
    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.on_url = lambda url: None

    def event(self, e: QtCore.QEvent) -> bool:  # macOS delivers texloom:// URLs here
        if e.type() == QtCore.QEvent.FileOpen and e.url().scheme() == SCHEME:
            self.on_url(e.url().toString())
            return True
        return super().event(e)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    hidden = "--hidden" in argv
    urls = [a for a in argv if a.startswith(f"{SCHEME}:")]

    app = App([sys.argv[0]])
    app.setApplicationName("TeXLoom")
    app.setWindowIcon(QtGui.QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)

    # One copy per user: a second launch (e.g. from a texloom:// link) hands over and exits.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if sock.waitForConnected(300):
        sock.write(("\n".join(urls) or "show").encode())
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return
    QLocalServer.removeServer(SOCKET)
    server = QLocalServer()
    server.listen(SOCKET)

    global bridge
    bridge = Bridge()
    autostart.log_file().parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(folder)s%(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(autostart.log_file(), encoding="utf-8")])
    activity = Activity()
    logging.getLogger().addHandler(activity)
    for h in logging.getLogger().handlers:
        h.addFilter(FolderTag())
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    FOLDER.set("")

    engine = Engine()
    win = Window(engine, activity)

    def handle(url: str) -> None:
        link = link_from_url(url)
        if link:
            win.join(link, from_web=True)
        else:
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
        menu.addAction("Open TeXLoom", win.show_window)
        menu.addAction("Join project…", lambda: win.join())
        menu.addSeparator()
        pause = menu.addAction("Pause syncing", win.toggle_pause)
        menu.aboutToShow.connect(lambda: pause.setText("Resume syncing" if engine.daemon.paused else "Pause syncing"))
        menu.addAction("Quit TeXLoom", app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip("TeXLoom")
        tray.activated.connect(lambda reason: win.show_window() if reason == QtWidgets.QSystemTrayIcon.Trigger else None)
        tray.show()

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
