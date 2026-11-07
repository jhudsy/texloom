# PyInstaller spec for the downloadable TeXLoom app:
#   uv sync --extra app && uv run --with pyinstaller --with pillow pyinstaller --noconfirm packaging/texloom-app.spec
# Gives dist/TeXLoom.app on macOS, dist/TeXLoom/ (with TeXLoom.exe or TeXLoom) elsewhere.
import re
import sys
from pathlib import Path

root = Path(SPECPATH).parent
version = re.search(r'__version__ = "([^"]+)"', (root / "texloom" / "__init__.py").read_text())[1]
icon = str(root / "texloom" / "assets" / "icon.png")

a = Analysis(
    [str(root / "packaging" / "app_main.py")],
    pathex=[str(root)],
    datas=[(str(root / "texloom" / "assets"), "texloom/assets")],
    hiddenimports=["texloom.gitsync", "texloom.mcp", "PySide6.QtNetwork"],
    excludes=["tkinter", "server", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql", "PySide6.QtTest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TeXLoom", console=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name="TeXLoom")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TeXLoom.app",
        icon=icon,
        bundle_identifier="org.texloom.app",
        version=version,
        info_plist={
            "CFBundleShortVersionString": version,
            "NSHighResolutionCapable": True,
            # texloom://join?link=… (the web app's "Open in the TeXLoom app") opens this app
            "CFBundleURLTypes": [{"CFBundleURLName": "TeXLoom project link", "CFBundleURLSchemes": ["texloom"]}],
        },
    )
