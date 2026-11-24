"""The desktop app's dialogs for connecting AI tools (MCP) and editors (the language server)."""

from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import Path

from PySide6 import QtGui, QtWidgets
from PySide6.QtCore import Qt

from .cli import load
from .i18n import _


class ConnectToolDialog(QtWidgets.QDialog):
    """How to connect an MCP tool that's already on this computer, such as Claude Code: it runs
    under the person's own account with that tool. The same guide as web/src/assistant/connect.ts."""

    INSTALL = "uv tool install git+https://github.com/jhudsy/texloom"

    def __init__(self, folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.folder = folder.resolve()
        self.keys, self.server = load(folder)
        self.access = "suggest" if self.keys.writable else "read"
        # the downloadable app runs the MCP server itself (packaging/app_main.py): nothing more to install
        self.command = sys.executable if getattr(sys, "frozen", False) else "texloom"
        self.setWindowTitle(_("Use an AI agent on your computer"))
        self.setMinimumWidth(620)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.note(_("Tools that support MCP, such as Claude Code, Claude Desktop or Cursor, can work on this project like a collaborator. They run on your computer under your own account with that tool, so no API key is needed here.")))
        self.install = None
        if self.command == "texloom":
            lay.addWidget(self.heading(_("Install the TeXLoom command-line helper once")))
            self.install = self.block(lay, 1, lambda: self.INSTALL)
            how = self.note(f'<a href="https://docs.astral.sh/uv/getting-started/installation/">{_("How to install uv")}</a>')
            how.setOpenExternalLinks(True)
            lay.addWidget(how)
        else:
            lay.addWidget(self.note(_("Your tool runs the TeXLoom app for this, so there’s nothing else to install. If you move the app, connect your tool again.")))
        if self.keys.writable:
            lay.addWidget(self.heading(_("Choose what the tool may do")))
            for access, label in (("suggest", _("Suggest changes for people to accept")), ("edit", _("Edit files directly")), ("read", _("Read the project only"))):
                radio = QtWidgets.QRadioButton(label)
                radio.setProperty("access", access)
                radio.setChecked(access == self.access)
                radio.toggled.connect(functools.partial(self.choose, access))
                lay.addWidget(radio)
        lay.addWidget(self.heading(_("Add it to your tool")))
        lay.addWidget(self.note(_("Claude Code: run this in a terminal")))
        self.claude = self.block(lay, 3, lambda: "claude mcp add texloom -- " + " ".join(self.quoted(a) for a in (self.command, *self.args("Claude Code"))))
        lay.addWidget(self.note(_("Other tools: add this to their MCP settings")))
        self.other = self.block(lay, 9, lambda: json.dumps({"mcpServers": {"texloom": {"command": self.command, "args": self.args("AI agent")}}}, indent=2, ensure_ascii=False))
        if self.install:
            lay.addWidget(self.note(_("If your tool can’t find the texloom command, give its full path instead.")))
        lay.addWidget(self.note(_("Suggesting and editing use this folder’s path. Reading uses a read-only link, which your tool saves in its settings, so anyone who can read those settings can read the project.")))
        lay.addWidget(self.note(_("The tool reads text other people wrote in this project, which could include instructions aimed at it. On shared projects, suggesting is the safer choice.")))
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        close = QtWidgets.QPushButton(_("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.show_commands()

    @staticmethod
    def note(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        return label

    def heading(self, text: str) -> QtWidgets.QLabel:
        label = self.note(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def block(self, lay: QtWidgets.QBoxLayout, lines: int, text) -> tuple:
        box = QtWidgets.QPlainTextEdit(readOnly=True)
        box.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        box.setWordWrapMode(QtGui.QTextOption.WrapAnywhere)
        box.setFixedHeight(box.fontMetrics().lineSpacing() * lines + 14)
        copy = QtWidgets.QPushButton(_("Copy"))
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(text()), copy.setText(_("Copied"))))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(box, 1)
        row.addWidget(copy, 0, Qt.AlignTop)
        lay.addLayout(row)
        return box, copy, text

    def args(self, name: str) -> list[str]:
        target = self.keys.link(self.server, False) if self.access == "read" else str(self.folder)
        return ["mcp", *(["--suggest-only"] if self.access == "suggest" else []), "--name", name, target]

    @staticmethod
    def quoted(arg: str) -> str:
        """For pasting into a terminal. Single quotes, so nothing inside is expanded ($(...), `...`,
        $VAR): a folder's name can come from a project name a collaborator chose."""
        if re.fullmatch(r"[\w./:\\-]+", arg):
            return arg
        if sys.platform == "win32":  # PowerShell: '' is a quote inside single quotes
            return "'" + arg.replace("'", "''") + "'"
        import shlex
        return shlex.quote(arg)

    def choose(self, access: str, on: bool) -> None:
        if on:
            self.access = access
            self.show_commands()

    def show_commands(self) -> None:
        for box, copy, text in filter(None, (self.install, self.claude, self.other)):
            box.setPlainText(text())
            copy.setText(_("Copy"))


class EditorDialog(ConnectToolDialog):
    """How to show a project's comments in the person's own editor, through `texloom lsp` (texloom/lsp.py).
    Not tied to a folder: the editor starts the server in its workspace folder."""

    def __init__(self, parent=None) -> None:
        QtWidgets.QDialog.__init__(self, parent)
        self.command = sys.executable if getattr(sys, "frozen", False) else "texloom"
        self.setWindowTitle(_("Show comments in your editor"))
        self.setMinimumWidth(620)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.note(_("Your editor can show a synced folder’s comments beside the text they are about, through TeXLoom’s language server. The editor starts it when it needs it, in the folder you open.")))
        lay.addWidget(self.heading(_("Choose your editor")))
        self.notes = {
            "VS Code": _("Install the TeXLoom comments extension from the Extensions view (search for TeXLoom; in VSCodium and similar editors it comes from Open VSX). If it doesn’t find TeXLoom by itself, add this to your settings:"),
            "Neovim": _("Neovim 0.11 or later: add this to your init.lua"),
            "Emacs": _("Emacs with lsp-mode (eglot runs one server per buffer, so use lsp-mode if you also use texlab): add this to your init file"),
            "Helix": _("Helix: add this to your languages.toml"),
            "Zed, Sublime Text, Kate…": _("Zed, Sublime Text (LSP package), Kate and others: add a custom language server for LaTeX files, with this command"),
        }
        self.pick = QtWidgets.QComboBox()
        self.pick.addItems(list(self.notes))
        lay.addWidget(self.pick)
        self.what = self.note("")
        lay.addWidget(self.what)
        self.snippet = self.block(lay, 9, self.text)
        if self.command == "texloom":
            lay.addWidget(self.note(_("If your editor can’t find the texloom command, give its full path instead.")))
        lay.addWidget(self.note(_("Comments that mention you stand out, and hovering shows the whole thread. Replying and starting a comment need an editor that can ask for text, as the VS Code extension does; elsewhere, reply with texloom comments reply, or use the web app. You can turn this off in Settings.")))
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        close = QtWidgets.QPushButton(_("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.pick.currentIndexChanged.connect(self.show_snippet)
        self.show_snippet()

    def text(self) -> str:
        q = json.dumps(self.command, ensure_ascii=False)  # (a double-quoted string: the same in JSON, Lua, Lisp and TOML)
        match self.pick.currentText():
            case "VS Code":
                return json.dumps({"texloom.command": self.command}, indent=2, ensure_ascii=False)
            case "Neovim":
                return ("vim.lsp.config('texloom', {\n  cmd = { %s, 'lsp' },\n  filetypes = { 'tex', 'plaintex' },\n  root_markers = { '.texloom' },\n})\nvim.lsp.enable('texloom')" % q)
            case "Emacs":
                return ("(require 'lsp-mode)\n(lsp-register-client\n (make-lsp-client :new-connection (lsp-stdio-connection '(%s \"lsp\"))\n                  :major-modes '(latex-mode LaTeX-mode)\n                  :server-id 'texloom :add-on? t))" % q)
            case "Helix":
                return ("[language-server.texloom]\ncommand = %s\nargs = [\"lsp\"]\n\n[[language]]\nname = \"latex\"\nlanguage-servers = [\"texlab\", \"texloom\"]" % q)
            case _:
                return " ".join(self.quoted(a) for a in (self.command, "lsp"))

    def show_snippet(self) -> None:
        self.what.setText(self.notes[self.pick.currentText()])
        box, copy, text = self.snippet
        box.setPlainText(text())
        copy.setText(_("Copy"))
