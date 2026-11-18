"""Mac-only touches for the desktop app, for things Qt has no call for: the Dock icon follows the
windows, and the menu-bar icon's menu opens on the screen that was clicked."""

from __future__ import annotations

import logging

from PySide6 import QtCore, QtGui, QtWidgets
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


_right_click = None  # the Objective-C method below (kept alive while the app runs)


def menu_opens_where_clicked() -> None:
    """Open the menu-bar icon's menu on the screen whose menu bar was right-clicked.

    With more than one screen, each has a menu bar showing the icon. A left click on any of them
    opens the menu there, but macOS hands a right click to the icon on the active menu bar, which
    opened the menu on that screen instead. So a right click opens the same menu at the pointer,
    under the menu bar it's on. Any failure leaves macOS's own handling in place."""
    global _right_click
    try:
        import ctypes
        import ctypes.util

        class NSPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.class_getInstanceMethod.restype = objc.method_getImplementation.restype = ctypes.c_void_p
        objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        objc.method_getImplementation.argtypes = [ctypes.c_void_p]
        objc.class_replaceMethod.restype = ctypes.c_void_p
        objc.class_replaceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        pop = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, NSPoint, ctypes.c_void_p)(send)
        handler = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        button = objc.objc_getClass(b"NSStatusBarButton")
        sel_right, sel_menu = objc.sel_registerName(b"rightMouseDown:"), objc.sel_registerName(b"statusMenu")
        sel_pop = objc.sel_registerName(b"popUpMenuPositioningItem:atLocation:inView:")
        method = objc.class_getInstanceMethod(button, sel_right) if button else None
        if not method:
            return
        before = handler(objc.method_getImplementation(method))

        def right_click(this, cmd, event) -> None:
            try:
                menu = get(this, sel_menu)
                screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos())
                primary = QtWidgets.QApplication.primaryScreen()
                if menu and screen and primary:
                    # where a left click puts it: just under that screen's menu bar, by the pointer
                    # (macOS counts y up from the bottom of the main screen, Qt down from its top)
                    pop(menu, sel_pop, None, NSPoint(QtGui.QCursor.pos().x() + 6,
                                                     primary.geometry().height() - screen.availableGeometry().top() - 9), None)
                    return
            except Exception:
                log.debug("menu-bar right click", exc_info=True)
            before(this, cmd, event)

        _right_click = handler(right_click)
        objc.class_replaceMethod(button, sel_right, ctypes.cast(_right_click, ctypes.c_void_p), b"v@:@")
    except Exception:
        log.debug("menu-bar right click not changed", exc_info=True)
