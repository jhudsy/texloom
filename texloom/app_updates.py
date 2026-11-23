"""The app window: checking for a newer TeXLoom and offering it. Part of Window (app.py)."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .i18n import _
from .ui import ICON, MAC

log = logging.getLogger("texloom")


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


class UpdatesPart:
    """Updates, in Window."""

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
