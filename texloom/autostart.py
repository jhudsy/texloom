"""Start `texloom start` at login: a launchd agent (macOS), a systemd user service (Linux),
or a hidden script in the Startup folder (Windows). Output goes to a log file."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .registry import config_dir

LABEL = "org.texloom.helper"


def executable() -> list[str]:
    """How to run texloom from a login item (absolute paths: login items have a bare PATH)."""
    exe = shutil.which("texloom")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "texloom.cli"]


def log_file() -> Path:
    return config_dir() / "texloom.log"


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_unit() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "texloom.service"


def _win_script() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "TeXLoom.vbs"


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plist(cmd: list[str], log: Path) -> str:
    args = "".join(f"\n    <string>{_xml(a)}</string>" for a in [*cmd, "start"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{_xml(str(log))}</string>
  <key>StandardErrorPath</key><string>{_xml(str(log))}</string>
</dict>
</plist>
"""


def unit(cmd: list[str]) -> str:
    q = " ".join(f'"{a}"' if " " in a else a for a in [*cmd, "start"])
    return f"""[Unit]
Description=TeXLoom: keep project folders in sync
After=network-online.target

[Service]
ExecStart={q}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def vbs(cmd: list[str], log: Path) -> str:
    # cmd /c with output redirected; window style 0 = hidden
    inner = " ".join(f'""{a}""' for a in [*cmd, "start"])
    return f'CreateObject("WScript.Shell").Run "cmd /c {inner} >> ""{log}"" 2>&1", 0, False\n'


def enable(dry_run: bool = False) -> str:
    cmd, log = executable(), log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        p = _mac_plist()
        if not dry_run:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(plist(cmd, log))
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)], check=True, capture_output=True)
        return f"Starts at login (launchd agent {p}); running now. Log: {log}"
    if sys.platform == "win32":
        p = _win_script()
        if not dry_run:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(vbs(cmd, log))
            subprocess.Popen(["wscript", str(p)])
        return f"Starts at login ({p}); running now. Log: {log}"
    p = _linux_unit()
    if not dry_run:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(unit(cmd))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "texloom.service"], check=True)
    return f"Starts at login (systemd user service {p}); running now. Log: journalctl --user -u texloom"


def disable() -> str:
    if sys.platform == "darwin":
        p = _mac_plist()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)
        p.unlink(missing_ok=True)
        return "Won't start at login any more (and stopped)."
    if sys.platform == "win32":
        _win_script().unlink(missing_ok=True)
        return "Won't start at login any more. A copy running now stops at logout (or end it in Task Manager)."
    subprocess.run(["systemctl", "--user", "disable", "--now", "texloom.service"], capture_output=True)
    _linux_unit().unlink(missing_ok=True)
    return "Won't start at login any more (and stopped)."


def status() -> str:
    p = {"darwin": _mac_plist(), "win32": _win_script()}.get(sys.platform, _linux_unit())
    return f"on ({p})" if p.exists() else "off"
