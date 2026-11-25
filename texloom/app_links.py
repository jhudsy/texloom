"""texloom:// links from the browser, and the one copy of the app that receives them: a
second launch hands its link to the running app and exits."""

from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import registry
from .ui import ICON

log = logging.getLogger("texloom")


SOCKET = f"texloom-app-{re.sub(r'[^A-Za-z0-9]', '', getpass.getuser())}"
SCHEME = "texloom"


def parse_url(url: str) -> tuple[str, ...] | None:
    """What a texloom:// URL from the web app asks for: ("join", project link) from
    texloom://join?link=…, ("sync", sync link, nonce) from texloom://sync?link=…&n=… (the web
    page that unlocks a list: `n` is the nonce this app sent it, "" if none), ("new", server or "", creation code) from texloom://new?server=…&code=…
    (Create project ▸ From a folder on this computer, which only fills in the New project
    dialog: the person still chooses the folder, and the keys are made here). None for
    anything else."""
    u = urlparse(url)
    if u.scheme != SCHEME:
        return None
    q = parse_qs(u.query)
    if u.netloc == "new":
        server = (q.get("server") or [""])[0].rstrip("/")
        code = (q.get("code") or [""])[0].strip()[:64]
        return "new", server if re.fullmatch(r"https?://[^/\s?#]+", server) else "", code
    link = (q.get("link") or [""])[0]
    if not link.startswith(("https://", "http://")):
        return None
    if u.netloc == "sync" and "#sync=" in link:
        return "sync", link, (q.get("n") or [""])[0][:64]
    if u.netloc == "join" and "#" in link:
        return "join", link
    return None


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


def instance_socket() -> str:
    """Where the running app listens for what a second launch hands over (texloom:// links, which
    hold project keys). It must be somewhere no one else on the computer can listen first: a
    socket in a folder only this user can open; on Windows, a pipe whose name only this user knows
    (another user could otherwise create one of the same name)."""
    if sys.platform == "win32":
        f = registry.config_dir() / "instance"
        try:
            token = f.read_text().strip()
        except OSError:
            token = ""
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            import secrets
            token = secrets.token_hex(16)
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(token)
            except OSError as e:
                log.warning("couldn't keep the app's pipe name: %s", e)
        return f"{SOCKET}-{token}"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    d = Path(runtime) / "texloom" if runtime else registry.config_dir() / "run"
    if len(str(d / "app.sock").encode()) > 100:  # longer than a socket's path may be (104 bytes on a Mac)
        import hashlib
        import tempfile
        mine = hashlib.sha256(str(registry.config_dir()).encode()).hexdigest()[:12]  # one per TEXLOOM_HOME
        d = Path(tempfile.gettempdir()) / f"texloom-{os.getuid()}-{mine}"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(d)
    if st.st_uid != os.getuid() or not os.path.isdir(d) or os.path.islink(d):
        raise OSError(f"{d} belongs to someone else")  # made first by another user: don't listen there
    os.chmod(d, 0o700)
    return str(d / "app.sock")
