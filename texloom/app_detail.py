"""The app window: the chosen project (its details, where it is kept, comments and mentions, word count, copying, compiling). Part of Window (app.py)."""

from __future__ import annotations

import functools
import html
import json
import logging
import shutil
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from . import listsync
from .cli import code_ok
from .comments import OVERVIEW, counts
from .config import DEFAULT_CONFIG, browser_link, load_config, update_config
from .dialogs import CopyFolderDialog, ProgramsDialog
from .dialogs_share import ask_code
from .i18n import _, ngettext
from .paths import state_dir, write_json
from .ui import GREY, ICON, describe, heading, move_refusal_text, note, open_path, project_name, red, short_path, usage_text
from .wordcount import find_texcount, texcount_report

log = logging.getLogger("texloom")


class ProjectDetailPart:
    """The chosen project, in Window."""

    def keep_on_server(self) -> None:
        """A sync-only project (no creation code): with a code, the server keeps a copy again."""
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        r = st.replica if st else None
        if not d or r is None:
            return
        server = r.server
        code = ask_code(self, self.engine, server, _("Keep a copy on the server"),
                        _("The server keeps an encrypted copy of a project only with a creation code. "
                          "With one, your other computers and collaborators get changes even when you're not online "
                          "together, and the project opens anywhere in the browser."), listsync.code_for(server))
        code = (code or "").strip()
        if not code:
            return

        def checked(good, err) -> None:
            if err or not good:
                QtWidgets.QMessageBox.warning(self, _("Keep a copy on the server"),
                                              _("The server didn't accept that code.") if not err else
                                              _("Couldn't reach the server: {err}", err=err))
                return
            update_config(d, create_code=code)
            listsync.keep_code(server, code)

            def switch() -> None:  # on the sync thread: the setting goes to everyone, then sync starts again with the code
                r.set_storage("server")
                self.engine.daemon.restart(d)
            self.engine.call(switch)
            self.statusBar().showMessage(_("Uploading a copy to the server…"), 5000)

        self.engine.submit(code_ok(server, code), checked)

    def move_to_code(self) -> None:
        """The owner moves a stored project to another creation code (issue #32)."""
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        r = st.replica if st else None
        if not d or r is None:
            return
        title = _("Move to another creation code")
        code = ask_code(self, self.engine, r.server, title,
                        _("This project’s storage counts against the creation code it was first stored with. Move it to another code, "
                          "for example when that code has lapsed, or when someone else’s plan should hold the project. "
                          "Links, collaborators and history stay as they are."), listsync.code_for(r.server))
        code = (code or "").strip()
        if not code:
            return

        def done(no, err) -> None:
            if err or no:
                QtWidgets.QMessageBox.warning(self, title, move_refusal_text(no) if no else _("Couldn't reach the server: {err}", err=err))
                return
            update_config(d, create_code=code)  # this copy holds the project's code from now on
            if not listsync.code_for(r.server):
                listsync.remember_code(r.server, code)
            self.statusBar().showMessage(_("The project now counts against the new code."), 5000)
            self.engine.submit(r.ask_usage(), lambda _res, _err: None)
        self.engine.submit(r.move_to_code(code), done)


    def check_comments(self) -> None:
        from .replica import Replica
        for d, st in list(self.engine.daemon.states.items()):
            if isinstance(st.replica, Replica):
                self.engine.submit(counts(st.replica, listsync.name_in(d)), functools.partial(self.got_comments, d))

    def got_comments(self, d: Path, res, err) -> None:
        if err or res is None:
            log.debug("comments not counted for %s: %s", d, err)
            return
        n, mine = res
        self.comment_counts[d] = (n, len(mine))
        if d == self.current():
            self.show_comment_counts(d)
        seen_file = state_dir(d) / "mentions-seen.json"
        try:
            seen = set(json.loads(seen_file.read_text()))
        except (OSError, ValueError):
            seen = None  # the first look: what's there already isn't news (the details show it)
        new = [k for k in mine if seen is not None and k not in seen]
        if set(mine) != (seen or set()):
            try:
                write_json(seen_file, sorted(mine))
            except OSError as e:
                log.debug("mentions seen not saved: %s", e)
        if new and self.tray is not None:
            self.mention_folder = d
            name = project_name(d, self.engine)
            self.tray.showMessage("TeXLoom", ngettext(len(new), "A new comment in {project} mentions you.",
                                                      "{n} new comments in {project} mention you.", n=len(new), project=name)
                                  + " " + _("Click to see it in the browser."), QtGui.QIcon(str(ICON)), 10000)

    def show_comment_counts(self, d: Path) -> None:
        n, mine = self.comment_counts.get(d, (0, 0))
        if not n:
            self.comments_lbl.setText("")
            self.comments_lbl.setVisible(False)
            return
        parts = [html.escape(ngettext(n, "{n} open comment", "{n} open comments", n=n))]
        if mine:
            parts.append("<b>" + html.escape(ngettext(mine, "{n} mentions you", "{n} mention you", n=mine)) + "</b>")
        links = [f"<a href='review'>{html.escape(_('Review in browser'))}</a>"]
        if (d / ".texloom" / OVERVIEW).is_file():
            links.append(f"<a href='overview'>{html.escape(_('Overview file'))}</a>")
        self.comments_lbl.setText(" · ".join(parts) + " — " + " · ".join(links))
        self.comments_lbl.setToolTip(_("The overview file lists the open comments with their file and line, for reading in your own editor; "
                                       "reply in the browser, or with `texloom comments`."))
        self.comments_lbl.setVisible(True)

    def comments_link(self, href: str) -> None:
        d = self.current()
        if d is None:
            return
        if href == "overview":
            open_path(d / ".texloom" / OVERVIEW)
        else:
            self.open_review(d, mentions=bool(self.comment_counts.get(d, (0, 0))[1]))

    def mention_clicked(self) -> None:
        if self.mention_folder is not None:
            self.open_review(self.mention_folder, mentions=True)

    def open_review(self, d: Path, mentions: bool = False) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(browser_link(d, "tab=review" + ("&mentions=1" if mentions else ""))))

    def copy_project(self) -> None:
        d = self.current()
        if not d:
            return
        dlg = CopyFolderDialog(self.engine, d, self)
        if dlg.exec():
            self.find.clear()
            self.clear_filter()
            self.select_folder(dlg.made)

    def word_count(self) -> None:
        d = self.current()
        exe = find_texcount()
        if not d:
            return
        if not exe:
            QtWidgets.QMessageBox.information(self, _("Word count"), _("TeXCount wasn’t found. It comes with TeX Live and most TeX installations."))
            return
        st = self.engine.daemon.states.get(d)
        if st is None or st.replica is None:
            self.statusBar().showMessage(_("This folder isn’t running yet: try again in a moment."), 5000)
            return
        name = project_name(d, self.engine)

        def show(report, err) -> None:
            if err:
                QtWidgets.QMessageBox.warning(self, _("Word count"), _("TeXCount couldn’t count this folder: {err}", err=err))
                return
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(_("Word count of “{name}”", name=name))
            dlg.resize(560, 460)
            lay = QtWidgets.QVBoxLayout(dlg)
            text = QtWidgets.QPlainTextEdit(report)
            text.setReadOnly(True)
            text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            lay.addWidget(text, 1)
            close = QtWidgets.QPushButton(_("Close"))
            close.clicked.connect(dlg.accept)
            lay.addWidget(close, 0, Qt.AlignRight)
            dlg.exec()

        self.engine.submit(texcount_report(st, d, exe), show)

    def select_folder(self, d: Path) -> None:
        for i in range(self.list.count()):
            if Path(self.list.item(i).data(Qt.UserRole)) == d.resolve():
                self.list.setCurrentRow(i)


    def build_detail(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 12, 8)
        self.title = QtWidgets.QLabel()
        self.title.setTextFormat(Qt.PlainText)  # a project's name is chosen by collaborators
        f = self.title.font()
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        self.title.setFont(f)
        self.path_lbl = QtWidgets.QLabel()
        self.path_lbl.linkActivated.connect(lambda _href: self.open_folder())
        self.status_lbl = QtWidgets.QLabel()
        self.status_lbl.setWordWrap(True)
        self.usage_lbl = note("")  # how much the server keeps (ui.usage_text)
        self.usage_asked: dict[Path, float] = {}  # when fresh figures were last asked for, per folder
        self.comments_lbl = QtWidgets.QLabel()  # open comments, and the ones that mention you
        self.comments_lbl.setWordWrap(True)
        self.comments_lbl.linkActivated.connect(self.comments_link)
        for x in (self.title, self.path_lbl, self.status_lbl, self.usage_lbl, self.comments_lbl):
            lay.addWidget(x)
        lay.addSpacing(4)
        btns = QtWidgets.QHBoxLayout()
        self.buttons = []
        for act in (self.a_open, self.a_web, self.a_share):
            b = QtWidgets.QPushButton(act.text())
            b.clicked.connect(act.trigger)
            btns.addWidget(b)
            self.buttons.append(b)
        more = QtWidgets.QPushButton(_("More"))
        more.setMenu(self.more_menu)
        btns.addWidget(more)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addSpacing(10)

        self.options = QtWidgets.QWidget()  # one folder's settings: hidden while several are selected
        o = QtWidgets.QVBoxLayout(self.options)
        o.setContentsMargins(0, 0, 0, 0)
        o.addWidget(heading(_("Compiling")))
        self.c_compile = QtWidgets.QCheckBox(_("Compile with my own TeX (latexmk) when files change"))
        self.c_compile.clicked.connect(lambda on: self.set_option("compile", on))
        o.addWidget(self.c_compile)
        self.no_tex = QtWidgets.QLabel(red(html.escape(_("latexmk wasn’t found on this computer, so it can’t compile here. Install TeX Live (MacTeX on a Mac, TeX Live or MiKTeX on Windows), then open TeXLoom again."))))
        self.no_tex.setWordWrap(True)
        o.addWidget(self.no_tex)
        self.compile_more = QtWidgets.QWidget()  # only while compiling is on
        c = QtWidgets.QVBoxLayout(self.compile_more)
        c.setContentsMargins(24, 0, 0, 0)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel(_("Running programs when compiling:")))
        self.p_mode = QtWidgets.QComboBox()
        self.p_mode.addItem(_("Never (safest)"), False)
        self.p_mode.addItem(_("TeX Live’s safe list only (e.g. epstopdf)"), "restricted")
        self.p_mode.addItem(_("Anything the document asks, once I allow it (e.g. minted)"), True)
        self.p_mode.activated.connect(self.set_programs)
        self.p_review = QtWidgets.QPushButton(_("Review lines…"))
        self.p_review.clicked.connect(lambda: self.current() and ProgramsDialog(self.engine, self.current(), self).exec())
        prow.addWidget(self.p_mode, 1)
        prow.addWidget(self.p_review)
        c.addLayout(prow)
        srow = QtWidgets.QHBoxLayout()
        scratch = QtWidgets.QPushButton(self.a_scratch.text())
        scratch.setToolTip(self.a_scratch.toolTip())
        scratch.clicked.connect(self.a_scratch.trigger)
        srow.addWidget(scratch)
        srow.addStretch(1)
        c.addLayout(srow)
        o.addWidget(self.compile_more)
        o.addSpacing(8)
        o.addWidget(heading(_("Git")))
        self.c_git = QtWidgets.QCheckBox(_("Keep this folder’s git repository in step (commit, pull, push)"))
        self.c_git.clicked.connect(lambda on: self.set_option("git", on))
        o.addWidget(self.c_git)
        self.git_note = note(_("This folder isn’t a git repository. Run “git init” in it first to use this."))
        o.addWidget(self.git_note)
        lay.addWidget(self.options)
        lay.addSpacing(10)

        self.act_btn = QtWidgets.QToolButton()
        self.act_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.act_btn.setAutoRaise(True)
        self.act_btn.setCursor(Qt.PointingHandCursor)
        self.act_btn.setFont(heading("").font())
        self.act_btn.setStyleSheet("QToolButton { border: none; padding: 0; background: transparent; }")
        self.act_btn.clicked.connect(self.a_activity.toggle)
        lay.addWidget(self.act_btn)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay.addWidget(self.log, 10)
        lay.addStretch(1)
        self.detail = w
        self.show_activity(self.a_activity.isChecked())
        return w

    def build_away_detail(self) -> QtWidgets.QWidget:
        """A project on your list that isn't on this computer: what it is, and Join."""
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 12, 8)
        self.away_title = QtWidgets.QLabel()
        self.away_title.setTextFormat(Qt.PlainText)  # chosen by collaborators
        f = self.away_title.font()
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        self.away_title.setFont(f)
        self.away_where = note("")
        self.away_tags = QtWidgets.QLabel()
        self.away_tags.setTextFormat(Qt.PlainText)
        for x in (self.away_title, self.away_where, self.away_tags):
            lay.addWidget(x)
        lay.addWidget(QtWidgets.QLabel(_("This project is on your project list but not on this computer. Join it to keep it in a folder here, or open it in your browser.")))
        lay.itemAt(lay.count() - 1).widget().setWordWrap(True)
        lay.addSpacing(4)
        row = QtWidgets.QHBoxLayout()
        join = QtWidgets.QPushButton(_("Join…"))
        join.setDefault(True)
        join.clicked.connect(self.join_away)
        web = QtWidgets.QPushButton(_("Open in browser"))
        web.clicked.connect(lambda: (p := self.away_current()) and QtGui.QDesktopServices.openUrl(QtCore.QUrl(p[3])))
        row.addWidget(join)
        row.addWidget(web)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def show_detail(self) -> None:
        away = self.away_current()
        self.right.setCurrentIndex(1 if away else 0)
        if away:
            for act in (self.a_open, self.a_web, self.a_share, self.a_copy, self.a_count, self.a_scratch, self.a_mcp, self.a_tags, self.a_leave):
                act.setEnabled(False)
            _project, title, where, _link, own = away
            self.away_title.setText(title)
            self.away_where.setText(_("On {server}", server=where))
            self.away_tags.setText(", ".join(t.name for t in own))
            self.away_tags.setVisible(bool(own))
            return
        picked = self.selected()
        one, several = len(picked) == 1, len(picked) > 1
        for act in (self.a_open, self.a_web, self.a_share, self.a_copy, self.a_count, self.a_scratch, self.a_mcp):
            act.setEnabled(one)
        for act in (self.a_tags, self.a_leave):
            act.setEnabled(bool(picked))
        st = self.engine.daemon.states.get(picked[0]) if one else None
        r = st.replica if st else None
        self.a_store.setVisible(bool(r and r.storage == "relay" and r.keys.seed and r.deleted_at is None))
        self.a_move.setVisible(bool(r and r.storage == "server" and r.keys.seed and r.deleted_at is None
                                    and (r.store_info or {}).get("registered")))
        for b in self.buttons:
            b.setEnabled(one)
        self.options.setVisible(not several)
        if several:
            self.title.setText(ngettext(len(picked), "{n} folder selected", "{n} folders selected", n=len(picked)))
            self.path_lbl.setText("")
            self.status_lbl.setText("")
            self.usage_lbl.setText("")
            self.comments_lbl.setText("")
            self.log.setPlainText("")
            return
        d = self.current()
        if d is None:
            return
        short, colour, longer = describe(d, self.engine)
        self.title.setText(project_name(d, self.engine))
        self.path_lbl.setText(f"<a href='open' style='color:{GREY}; text-decoration:none'>{html.escape(short_path(d))}</a>")
        self.path_lbl.setToolTip(_("Open folder") + f": {d}")
        self.status_lbl.setText(f"<span style='color:{colour}'>●</span> <b>{html.escape(short)}</b>" + (f" — {html.escape(longer)}" if longer else ""))
        st = self.engine.daemon.states.get(d)
        r = st.replica if st else None
        usage = usage_text(r.store_info, r.storage) if r else ""
        self.usage_lbl.setText(usage)
        self.usage_lbl.setVisible(bool(usage))
        self.show_comment_counts(d)
        if r is not None and time.time() - self.usage_asked.get(d, 0) > 60:  # fresh figures, once a minute while shown
            self.usage_asked[d] = time.time()
            self.engine.submit(r.ask_usage(), lambda _res, _err: None)
        try:
            cfg = load_config(d)
        except (OSError, ValueError):
            cfg = DEFAULT_CONFIG
        compiles = bool(cfg.get("compile"))
        self.c_compile.setChecked(compiles)
        self.compile_more.setVisible(compiles)
        self.no_tex.setVisible(compiles and shutil.which("latexmk") is None)
        self.a_scratch.setEnabled(compiles)
        mode = cfg.get("shell_escape")
        self.p_mode.setCurrentIndex(2 if mode is True else 1 if mode == "restricted" else 0)
        self.p_review.setEnabled(mode is True)
        is_git = (d / ".git").exists()
        self.c_git.setEnabled(is_git)
        self.c_git.setChecked(bool(cfg.get("git")) and is_git)
        self.git_note.setVisible(not is_git)
        text = "\n".join(self.activity.lines.get(str(d), []))
        if text != self.log.toPlainText():
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def compile_from_scratch(self) -> None:
        d = self.current()
        st = self.engine.daemon.states.get(d) if d else None
        if st is None or st.fs is None:
            self.statusBar().showMessage(_("This folder isn’t running yet: try again in a moment."), 5000)
            return
        self.engine.call(st.fs.compile_from_scratch)
        self.statusBar().showMessage(_("Compiling {name} from scratch…", name=d.name), 5000)

    def set_option(self, key: str, on: bool) -> None:
        d = self.current()
        if d:
            update_config(d, **{key: on})
            self.engine.call(self.engine.daemon.restart, d)

    def set_programs(self, index: int) -> None:
        d = self.current()
        if not d:
            return
        mode = self.p_mode.itemData(index)
        if mode is True and QtWidgets.QMessageBox.question(
                self, _("Running programs"), _("Compiles may then run programs that the document asks for, on this "
                "computer, once you allow each line (Review lines…).\n\nAnyone with an edit link writes the "
                "document. Turn this on only for projects whose editors you trust.")) != QtWidgets.QMessageBox.Yes:
            self.show_detail()
            return
        self.set_option("shell_escape", mode)
        if mode is True:
            ProgramsDialog(self.engine, d, self).exec()
