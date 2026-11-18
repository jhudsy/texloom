"""Mac-only touches for the desktop app: the Dock icon follows the windows (Qt has no call for it)."""

from __future__ import annotations

import logging

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt

log = logging.getLogger("texloom")


def nsapp(selector: bytes, arg: int = 0) -> None:
    """Send NSApp (the Mac's application object) one message taking an integer, BOOL or nil, for
    the few things Qt has no call for. Any failure is logged and otherwise ignored."""
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        call = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(send)
        call(get(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication")),
             objc.sel_registerName(selector), arg)
    except Exception:
        log.debug("NSApp %s failed", selector, exc_info=True)


def mac_dock(show: bool) -> None:
    """On a Mac, show or hide TeXLoom in the Dock and ⌘-Tab: while no window is open it runs from
    its menu-bar icon alone, like other background sync apps (the Accessory activation policy)."""
    nsapp(b"setActivationPolicy:", 0 if show else 1)  # Regular / Accessory
    if show:  # coming back from the menu bar: be the active app, so the window and its menus come forward
        nsapp(b"activateIgnoringOtherApps:", 1)
    else:  # and stop being the active app, so opening TeXLoom again (Finder, Spotlight) activates it and shows the window
        nsapp(b"hide:")


class DockFollowsWindows(QtCore.QObject):
    """On a Mac, keep TeXLoom in the Dock and ⌘-Tab only while one of its windows is open."""

    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self.shown = True
        self.timer = QtCore.QTimer(self, singleShot=True, interval=0, timeout=self.check)
        app.installEventFilter(self)
        self.timer.start()  # once running: started with --hidden, nothing has opened

    def eventFilter(self, obj: QtCore.QObject, e: QtCore.QEvent) -> bool:
        if e.type() in (QtCore.QEvent.Show, QtCore.QEvent.Hide) and isinstance(obj, QtWidgets.QWidget) and obj.isWindow():
            # A window opening comes forward at once; one closing may be followed by another opening.
            self.check() if e.type() == QtCore.QEvent.Show else self.timer.start()
        return False

    def check(self) -> None:
        show = any(w.isVisible() and w.windowType() in (Qt.Window, Qt.Dialog, Qt.Sheet)
                   for w in QtWidgets.QApplication.topLevelWidgets())
        if show != self.shown:
            self.shown = show
            mac_dock(show)
