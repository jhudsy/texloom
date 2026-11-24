#!/usr/bin/env python3
"""The desktop app's windows (issue #25): its main window and dialogs open and close, a dialog
asked for from another dialog opens on top of it, and on a Mac the fixes that rely on
undocumented AppKit and Qt behaviour are still in place.

    uv run --extra app python scripts/check_app.py             # offscreen: any system, no screen needed
    uv run --extra app python scripts/check_app.py --native    # on a Mac, also the menu bar and the Dock

Offscreen (Qt's "offscreen" platform), it opens the main window, then each dialog the way the
window's own menus do (Join, Sync between your computers, New project from a folder, Settings,
Manage tags), checks which window it opened over, and closes it. Then the projects on your list
that aren't on this computer, in the main window, and the project list's own window.

With --native on a Mac (the real menu bar; CI's macOS runners have one), also:
- the menu-bar icon: macOS still has NSStatusBarButton with rightMouseDown:, the app's
  replacement for it is installed (texloom/mac.py, menu_opens_where_clicked), Qt's private
  statusMenu still gives the icon's menu, and NSMenu still pops up at a point. If a macOS or Qt
  update takes one of these away, the menu quietly goes back to opening on the wrong screen;
- the Dock: with a menu-bar icon, TeXLoom leaves the Dock and ⌘-Tab when its last window closes
  and comes back when one opens (the Accessory and Regular activation policies).

Everything runs in a throwaway TEXLOOM_HOME, so the computer's own projects are never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")  # ✓ and ✗ on Windows consoles too
sys.stderr.reconfigure(encoding="utf-8")
NATIVE = "--native" in sys.argv
if NATIVE and sys.platform != "darwin":
    sys.exit("check_app: --native is for macOS")
if not NATIVE:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["TEXLOOM_HOME"] = tempfile.mkdtemp(prefix="texloom-appcheck-")
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from texloom import app as appmod  # noqa: E402
from texloom import mac  # noqa: E402
from texloom.dialogs import SettingsDialog  # noqa: E402
from texloom.dialogs_list import ProjectListDialog  # noqa: E402
from texloom.dialogs_share import JoinDialog, ShareFolderDialog  # noqa: E402
from texloom.dialogs_tags import ManageTagsDialog  # noqa: E402
from texloom.ui import Engine  # noqa: E402

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'✓' if ok else '✗'} {what}")
    if not ok:
        failures.append(what)


def settle(ms: int = 50) -> None:
    end = QtCore.QDeadlineTimer(ms)
    while not end.hasExpired():
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 10)


def when_modal(cls: type, then) -> None:
    """Once a modal dialog of class `cls` is up, call then(dialog); give up after 5 s."""
    tries = [0]

    def look() -> None:
        w = QtWidgets.QApplication.activeModalWidget()
        if isinstance(w, cls):
            then(w)
        elif tries[0] < 100:
            tries[0] += 1
            QtCore.QTimer.singleShot(50, look)
        else:
            check(False, f"{cls.__name__} opened")
            if w is not None:
                w.reject()
    QtCore.QTimer.singleShot(50, look)


def opens_over(open_it, cls: type, over: QtWidgets.QWidget, name: str) -> None:
    """Open a dialog the window's way (`open_it`, which runs it modally), check it's over `over`,
    then close it."""
    seen: list[QtWidgets.QDialog] = []

    def got(d: QtWidgets.QDialog) -> None:
        seen.append(d)
        check(d.isVisible(), f"{name} opens")
        check(d.parentWidget() is over, f"{name} opens over {type(over).__name__}")
        d.reject()
    when_modal(cls, got)
    open_it()
    settle()
    check(bool(seen) and not seen[0].isVisible(), f"{name} closes")


def windows(win: appmod.Window) -> None:
    print("windows:")
    win.show_window()
    settle()
    check(win.isVisible(), "the main window opens")
    check(win.windowTitle() != "", "the main window has a title")

    opens_over(win.join, JoinDialog, win, "Join a project")
    opens_over(win.project_list, ProjectListDialog, win, "Sync between your computers")
    opens_over(win.share_folder, ShareFolderDialog, win, "New project from a folder")
    opens_over(win.manage_tags, ManageTagsDialog, win, "Manage tags")
    if appmod.MAC:  # a window of its own on a Mac, a dialog elsewhere
        win.settings_window()
        settle()
        s = win.settings_dlg
        check(s is not None and s.isVisible(), "Settings opens")
        if s is not None:
            s.close()
            settle()
            check(not s.isVisible(), "Settings closes")
    else:
        opens_over(win.settings_window, SettingsDialog, win, "Settings")

    win.hide()
    settle()
    check(not win.isVisible(), "the main window closes (hides: the app keeps syncing)")


def first_run(win: appmod.Window) -> None:
    """The welcome while there are no folders, a creation code entered in Settings, and the Help menu."""
    from texloom import listsync
    from texloom.ui import DEFAULT_SERVER
    print("first run:")
    win.refresh()
    check(win.pages.currentIndex() == 0, "with no folders, the window shows the welcome")
    check(not win.welcome_list.isHidden(), "the welcome offers to open your project list")
    listsync.save_link(listsync.ListKey.generate().link(DEFAULT_SERVER))
    win.refresh()
    check(win.welcome_list.isHidden() and win.welcome_new.isHidden(), "with a list in use, the welcome doesn't offer it again")
    listsync.save_link(None)
    win.refresh()

    s = SettingsDialog(win)
    s.fill_servers()
    host = DEFAULT_SERVER.split("//")[1]
    check(s.code_server.currentData() == DEFAULT_SERVER, "Settings shows the creation code for the default server")
    check(host in s.code_note.text(), "and says which server it is for")
    s.code.setText("  test-code-123 ")
    s.code.editingFinished.emit()
    check(listsync.code_for(DEFAULT_SERVER) == "test-code-123", "a code entered in Settings is kept for that server")
    s2 = SettingsDialog(win)
    s2.fill_servers()
    check(s2.code.text() == "test-code-123", "and shown the next time Settings opens")
    s.deleteLater()
    s2.deleteLater()

    help_menu = next(a.menu() for a in win.menuBar().actions() if a.menu() and a.text() == "Help")
    names = [a.text() for a in help_menu.actions()]
    check(all(n in names for n in ("TeXLoom help", "What’s new in TeXLoom", "Report a problem…")),
          "the Help menu has help, what's new and reporting a problem")
    check(appmod.HELP_URL.startswith("https://"), "TeXLoom help opens a web page")


def away(win: appmod.Window) -> None:
    """The projects on your list that aren't on this computer, in the main window, to join."""
    from texloom import listsync
    print("projects to join:")
    win.show_window()
    doc = {"v": 1, "gone": {}, "projects": {
        "p1": {"f": "p=p1&k=a", "n": "Thesis", "at": 2, "w": True},
        "p2": {"f": "p=p2&k=b", "n": "Grant proposal", "at": 1, "w": True, "s": "https://other.example"}}}
    sync, listsync.sync_this_computer = listsync.sync_this_computer, lambda code=None: ("https://example.org", doc)
    listsync.save_link(listsync.ListKey.generate().link("https://example.org"))
    win.list_data = ("https://example.org", doc)
    win.refresh()
    settle()
    check(win.pages.currentIndex() == 1, "with projects on the list, the window shows them even with no folders here")
    check(win.away.isVisible() and win.away.count() == 2, "both projects are listed to join")
    check("(2)" in win.away_head.text(), "under a heading that counts them")
    win.away.item(1).setSelected(True)
    settle()
    check(win.right.currentIndex() == 1 and win.away_title.text() == "Grant proposal", "selecting one shows it, with Join")
    check("other.example" in win.away_where.text(), "and which server it's on")
    got: list[str] = []

    def joining(j: QtWidgets.QDialog) -> None:
        got.append(j.link.text())
        check(j.parentWidget() is win, "Join opens over the main window")
        j.reject()
    when_modal(JoinDialog, joining)
    win.join_away()
    settle()
    check(got == ["https://other.example/#p=p2&k=b"], "with the project's link, on its own server")
    win.find.setText("thes")
    win.refresh()
    check(win.away.count() == 1 and win.away.item(0).text().startswith("Thesis"), "the search covers them")
    win.find.setText("")
    win.away_head.setChecked(False)
    win.refresh()
    check(not win.away.isVisible() and win.away_head.isVisible(), "the heading folds them away")
    win.away_head.setChecked(True)
    win.set_show_away(False)
    check(not win.away_head.isVisible() and win.pages.currentIndex() == 0, "and View can hide them")
    win.set_show_away(True)
    listsync.save_link(None)
    listsync.sync_this_computer = sync
    win.keep_list_updated(force=True)
    win.refresh()
    check(not win.away_head.isVisible(), "without a list there's nothing to join")
    win.hide()
    settle()


def list_window() -> None:
    """The project list's window when a list is in use: its sync settings, with the network calls
    stood in for (texloom/listsync.py's are checked against a server by check_web_listways.py)."""
    import time

    from texloom import dialogs_list, listsync
    print("project list:")
    calls: list[tuple] = []
    slots = {"pk": {"kind": "passkey", "at": 1763900000000, "seed": "a"}, "pw": {"kind": "password", "at": 1763990000000, "seed": "b"}}
    doc = {"projects": {"p1": {"f": "x", "n": "Thesis"}, "p2": {"f": "y", "n": "Notes"}}}
    def synced(code=None):
        listsync.last_synced = time.time()
        return "https://example.org", doc
    stand_ins = {
        "sync_this_computer": synced,
        "not_here": lambda doc, server: [],
        "read_slots": lambda key, home: dict(slots),
        "remove_slot": lambda key, home, sid, slot: calls.append(("remove", sid)),
        "set_password": lambda key, home, u, pw: calls.append(("password", u)),
        "delete_list": lambda: calls.append(("delete",)),
    }
    kept = {name: getattr(listsync, name) for name in stand_ins}
    yes, ask = QtWidgets.QMessageBox.question, dialogs_list.PasswordDialog.ask
    try:
        for name, f in stand_ins.items():
            setattr(listsync, name, f)
        QtWidgets.QMessageBox.question = lambda *a, **k: QtWidgets.QMessageBox.Yes
        dialogs_list.PasswordDialog.ask = classmethod(lambda cls, parent, setting: ("ada", "correct horse battery"))
        listsync.save_link(listsync.ListKey.generate().link("https://example.org"))

        def wait(cond, ms: int = 3000) -> bool:
            for _ in range(ms // 20):
                if cond():
                    return True
                settle(20)
            return False
        d = dialogs_list.ProjectListDialog(None)
        d.show()
        check(d.pages.currentIndex() == 1, "with a list, it opens on the list's settings")
        check(wait(lambda: "2 projects" in d.count.text() and "synced" in d.count.text().lower()), "it says how many projects are synced, and when")
        check(wait(lambda: sum(isinstance(d.ways.itemAt(i).widget(), QtWidgets.QWidget) for i in range(d.ways.count())) == 2),
              "it lists the passkey and the password")
        rows = [d.ways.itemAt(i).widget() for i in range(d.ways.count())]
        rows[1].findChild(QtWidgets.QPushButton).click()
        check(wait(lambda: ("remove", "pw") in calls), "Remove takes that way in off the list")
        d.set_password()
        check(wait(lambda: ("password", "ada") in calls), "Set a password sets it")
        d.copy_btn.click()
        check(QtWidgets.QApplication.clipboard().text() == listsync.saved_link(), "Copy sync link copies it")
        check(wait(lambda: d.copy_btn.text() != dialogs_list._("Copied"), 4000), "and its button says so only for a moment")
        check(not d.more.isVisibleTo(d), "More options starts folded")
        d.more_btn.click()
        check(d.more.isVisibleTo(d), "and opens")
        d.delete()
        check(wait(lambda: ("delete",) in calls and d.pages.currentIndex() == 0), "Delete this list deletes it, and the window goes back to setting one up")
        listsync.save_link(listsync.ListKey.generate().link("https://example.org"))
        d2 = dialogs_list.ProjectListDialog(None)
        d2.show()
        d2.off()
        check(listsync.saved_link() is None and d2.pages.currentIndex() == 0, "Stop using it here stops, after asking")
        d.close()
        d2.close()
        settle()

        # setting one up: a new list with a password
        calls.clear()
        d3 = dialogs_list.ProjectListDialog(None)
        d3.show()
        check(d3.pages.currentIndex() == 0, "with no list, it opens on setting one up")
        d3.start("password")
        check(wait(lambda: ("password", "ada") in calls and listsync.saved_link() is not None and d3.pages.currentIndex() == 1),
              "Start syncing With a password… starts a list and sets the password")
        check(wait(lambda: "copy the sync link" in d3.status.text().lower()), "and says to copy the sync link")
        d3.close()
        settle()
        listsync.save_link(None)

        # a list coming back from the browser goes to the window that's open, not a second one
        asked: list[str] = []
        QtWidgets.QMessageBox.question = lambda *a, **k: asked.append(a[2] if len(a) > 2 else "") or QtWidgets.QMessageBox.Yes
        d4 = dialogs_list.ProjectListDialog(None)
        d4.show()
        d4.passkey()  # (opens the browser: offscreen, nothing happens)
        sent = listsync.ListKey.generate().link("https://example.org")
        check(dialogs_list.ProjectListDialog.current is d4, "the open window is the one a list from the browser goes to")
        d4.receive(sent, from_web=True)
        check(listsync.saved_link() == sent and not asked and d4.pages.currentIndex() == 1,
              "a list the app just asked the browser for is used without a warning")
        other = listsync.ListKey.generate().link("https://example.org")
        d4.receive(other, from_web=True)
        check(bool(asked) and listsync.saved_link() == other, "one that arrives unasked is used only once confirmed")
        d4.close()
        settle()
        check(dialogs_list.ProjectListDialog.current is None, "once closed, it's no longer the one")
    finally:
        for name, f in kept.items():
            setattr(listsync, name, f)
        QtWidgets.QMessageBox.question, dialogs_list.PasswordDialog.ask = yes, ask
        listsync.save_link(None)


def objc():
    """The Objective-C runtime, typed for the few calls the Mac checks make."""
    import ctypes
    import ctypes.util
    o = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    o.objc_getClass.restype = o.sel_registerName.restype = ctypes.c_void_p
    o.objc_getClass.argtypes = o.sel_registerName.argtypes = [ctypes.c_char_p]
    o.class_getInstanceMethod.restype = o.method_getImplementation.restype = ctypes.c_void_p
    o.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    o.method_getImplementation.argtypes = [ctypes.c_void_p]
    o.object_getClass.restype = ctypes.c_void_p
    o.object_getClass.argtypes = [ctypes.c_void_p]
    send = ctypes.cast(o.objc_msgSend, ctypes.c_void_p).value
    msg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
    msg_arg = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
    msg_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(send)
    msg_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
    msg_idx = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)(send)
    return o, msg, msg_arg, msg_long, msg_bool, msg_idx


def menu_bar(win: appmod.Window) -> None:
    print("menu bar (macOS):")
    import ctypes
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        check(False, "a menu bar is available")
        return
    tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(str(ROOT / "texloom" / "assets" / "icon.png")))
    menu = QtWidgets.QMenu()
    menu.addAction("Open TeXLoom", win.show_window)
    tray.setContextMenu(menu)
    tray.show()
    settle(300)

    o, msg, msg_arg, msg_long, msg_bool, msg_idx = objc()
    sel = o.sel_registerName
    button_cls = o.objc_getClass(b"NSStatusBarButton")
    check(bool(button_cls), "macOS has NSStatusBarButton")
    check(bool(button_cls and o.class_getInstanceMethod(button_cls, sel(b"rightMouseDown:"))),
          "NSStatusBarButton handles rightMouseDown:")
    check(bool(o.class_getInstanceMethod(o.objc_getClass(b"NSMenu"), sel(b"popUpMenuPositioningItem:atLocation:inView:"))),
          "NSMenu pops up at a point (popUpMenuPositioningItem:atLocation:inView:)")

    mac.menu_opens_where_clicked()
    ours = ctypes.cast(mac._right_click, ctypes.c_void_p).value if mac._right_click else None
    imp = o.method_getImplementation(o.class_getInstanceMethod(button_cls, sel(b"rightMouseDown:"))) if button_cls else None
    check(bool(ours) and imp == ours, "the app's right click is installed on NSStatusBarButton")

    # the icon's button: somewhere in one of the app's status-bar windows (one per screen; the
    # other screens show copies of it)
    nsapp = msg(o.objc_getClass(b"NSApplication"), sel(b"sharedApplication"))
    wins = msg(nsapp, sel(b"windows"))
    buttons = []

    def find(view, depth: int = 0) -> None:
        if not view or depth > 8:
            return
        if msg_bool(view, sel(b"isKindOfClass:"), button_cls):
            buttons.append(view)
        subs = msg(view, sel(b"subviews"))
        for k in range(msg_long(subs, sel(b"count"))):
            find(msg_idx(subs, sel(b"objectAtIndex:"), k), depth + 1)
    for i in range(msg_long(wins, sel(b"count"))):
        find(msg(msg_idx(wins, sel(b"objectAtIndex:"), i), sel(b"contentView")))
    check(bool(buttons), "the menu-bar icon's button is found")
    if buttons:
        b = buttons[0]
        responds = msg_bool(b, sel(b"respondsToSelector:"), sel(b"statusMenu"))
        check(responds, "Qt's statusMenu is there for the icon's button")
        if responds:
            check(bool(msg(b, sel(b"statusMenu"))), "statusMenu gives the icon's menu")

    # the Dock: Regular (0) with a window open, Accessory (1) with none
    policy = lambda: msg_long(nsapp, sel(b"activationPolicy"))  # noqa: E731
    mac.DockFollowsWindows(QtWidgets.QApplication.instance())
    win.show_window()
    settle(300)
    check(policy() == 0, "in the Dock while the window is open")
    win.hide()
    settle(300)
    check(policy() == 1, "out of the Dock and ⌘-Tab once the last window closes")
    win.show_window()
    settle(300)
    check(policy() == 0, "back in the Dock when the window opens again")
    win.hide()
    tray.hide()
    settle()


def quiet_qt(kind, context, message: str) -> None:
    """Qt's own messages, without the offscreen platform's notes on what it can't do."""
    if not message.startswith("This plugin does not support"):
        print(f"qt: {message}", file=sys.stderr)


def main() -> int:
    QtCore.qInstallMessageHandler(quiet_qt)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    engine = Engine()  # not started: nothing here syncs or goes online
    win = appmod.Window(engine, appmod.Activity())
    windows(win)
    first_run(win)
    away(win)
    list_window()
    if NATIVE:
        menu_bar(win)
    if failures:
        print(f"app: FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("app: all checks passed" + ("" if NATIVE else " (offscreen; --native on a Mac for the menu bar and the Dock)"))
    return 0


if __name__ == "__main__":
    code = main()
    QtCore.qInstallMessageHandler(None)  # Qt's last words on exit (Windows) mustn't call back into Python
    sys.stdout.flush()
    sys.stderr.flush()
    # Straight out: Python's teardown deletes Qt's objects in an order PySide doesn't expect, which
    # can crash once the checks are done (and turn a pass into a failure). On Windows even
    # os._exit runs each library's shutdown, where Qt's fails: end the process outright there.
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.TerminateProcess(k.GetCurrentProcess(), code)
    os._exit(code)
