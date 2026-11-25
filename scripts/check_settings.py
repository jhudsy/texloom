#!/usr/bin/env python3
"""Checks that the settings which make this computer run something are kept in the user's own
configuration folder, never in the synced folder (the security review of 2026-11-24): anyone
sharing a folder through Dropbox, iCloud or the like can write any file in it, this computer's
`.texloom/computers/<id>/config.json` included.

- latexmk's arguments, shell escape, the confinement, git, compile and the allowed lines written
  into the folder's config.json (or allowed.json) are ignored;
- settings are kept per folder and project: a link changed to another project's finds none;
- the first run brings a folder's old settings over: all of them from a folder nothing suggests
  is shared, only compile, engine and label from one that may be (another computer's records
  in it, found shared, inside a file-sync service's folder, or a config.json not written by
  TeXLoom); once for all, so settings planted later are never brought over.

    uv run python scripts/check_settings.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(what: str, cond: object, detail: object = "") -> None:
    print(("✓ " if cond else "✗ ") + what + ("" if cond else f": {detail}"), flush=True)
    if not cond:
        failures.append(what)


EVIL = {"compile": True, "engine": "xelatex", "latexmk_args": ["-e", "system('touch /tmp/pwned')"],
        "shell_escape": True, "confine": False, "git": True, "label": "mine"}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="texloom-settings-"))
    os.environ["TEXLOOM_HOME"] = str(tmp / "home")
    from texloom import config, registry
    from texloom.crypto import Keys
    from texloom.paths import STATE, state_dir, write_json
    from texloom.scan import allow, allowed

    def folder(name: str, settings: dict, approvals: list[str] | None = None) -> Path:
        d = tmp / name
        d.mkdir(parents=True)
        config.save(d, Keys.generate().link("https://texloom.invalid"))
        write_json(config.config_path(d), {**json.loads(config.config_path(d).read_text()), **settings}, private=True)
        if approvals is not None:
            (state_dir(d) / "allowed.json").write_text(json.dumps(approvals))
        registry.add(d)
        return d

    # folders as an earlier version left them
    mine = folder("mine", EVIL, ["k1", "k2"])
    shared = folder("shared", EVIL, ["k1"])
    (shared / STATE / "computers" / "0123456789abcdef").mkdir()  # another computer syncs it
    in_dropbox = folder("Dropbox/paper", EVIL)
    touched = folder("touched", EVIL)
    os.chmod(config.config_path(touched), 0o644)  # written by a file-sync service, not by TeXLoom

    print("Settings brought over from the folder, the first time:")
    cfg = config.load_config(mine)
    check("all of them, from a folder only you write to", all(cfg[k] == v for k, v in EVIL.items()) and set(allowed(mine)) == {"k1", "k2"},
          {k: cfg[k] for k in EVIL})
    for d, why in ((shared, "another computer's records in it"), (in_dropbox, "in a Dropbox folder"),
                   (touched, "its config.json not written by TeXLoom")):
        cfg = config.load_config(d)
        check(f"only compile, engine and label from a folder that may be shared ({why})",
              cfg["compile"] is True and cfg["engine"] == "xelatex" and cfg["label"] == "mine" and cfg["latexmk_args"] == []
              and cfg["shell_escape"] is False and cfg["confine"] is True and cfg["git"] is False and not allowed(d),
              {k: cfg[k] for k in EVIL})
    left = json.loads(config.config_path(mine).read_text())
    check("the folder keeps only its link (and creation code)", set(left) <= {"link", "create_code", "name"}, sorted(left))
    check("its approvals file is gone", not (state_dir(mine) / "allowed.json").exists())
    sp = getattr(config, "settings_path", None)
    check("settings are kept outside the folder", sp is not None and mine.resolve() not in sp(mine).resolve().parents)

    print("Settings written into a folder later:")
    late = folder("late", {})
    config.load_config(late)
    for d in (mine, late):
        write_json(config.config_path(d), {**json.loads(config.config_path(d).read_text()), **EVIL,
                                           "latexmk_args": ["-e", "planted"]}, private=True)
        (state_dir(d) / "allowed.json").write_text(json.dumps(["planted"]))
        cfg = config.load_config(d)
        check(f"are ignored ({d.name})", cfg["latexmk_args"] != ["-e", "planted"] and "planted" not in allowed(d)
              and (d != late or (cfg["shell_escape"] is False and cfg["confine"] is True and cfg["git"] is False and cfg["compile"] is False)),
              {k: cfg[k] for k in EVIL})

    print("Changing settings:")
    config.update_config(late, compile=True, shell_escape="restricted", create_code="CODE")
    allow(late, {"a", "b"})
    cfg = config.load_config(late)
    stored = json.loads(config.config_path(late).read_text())
    check("they are read back", cfg["compile"] is True and cfg["shell_escape"] == "restricted" and allowed(late) == {"a", "b"}
          and cfg["create_code"] == "CODE", cfg)
    check("only the creation code goes to the folder", stored.get("create_code") == "CODE" and "compile" not in stored
          and "shell_escape" not in stored, stored)
    other = Keys.generate().link("https://texloom.invalid")
    write_json(config.config_path(late), {**stored, "link": other}, private=True)
    cfg = config.load_config(late)
    check("a link changed to another project's finds none of them", cfg["compile"] is False and cfg["shell_escape"] is False
          and not allowed(late), cfg)

    if os.name != "nt":
        import stat
        print("Files in the settings folder:")
        mode = stat.S_IMODE((registry.config_dir() / "folders.json").stat().st_mode)
        check("the folder list is readable by you only", mode == 0o600, oct(mode))
        from texloom import autostart
        private = getattr(autostart, "private_log", None)
        if private is not None:
            private()
        log = autostart.log_file()
        mode = stat.S_IMODE(log.stat().st_mode) if log.exists() else None
        check("so is the app's log", mode == 0o600, oct(mode) if mode is not None else "not made")
        mode = stat.S_IMODE(config.settings_path(mine).stat().st_mode) if sp else None
        check("and the folders' settings", mode == 0o600, mode)

    print(f"\nfolder settings: {'FAILED' if failures else 'ok'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
