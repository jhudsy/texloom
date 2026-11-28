"""The server's settings: a TOML file, environment variables and the command line.

Every option of texloom-server can be set in the file, under its name without the dashes
(`min_free = "2G"`, `codes_file = ["/etc/texloom/codes.txt"]`); the S3 store's settings go in an
[s3] table (`[s3] endpoint = …`, read as `s3_endpoint`). The file is --config, else
TEXLOOM_CONFIG, else ./texloom.toml, else /etc/texloom/texloom.toml, if they exist.
texloom.example.toml lists every setting.

An environment variable TEXLOOM_<NAME> (TEXLOOM_MIN_FREE, TEXLOOM_S3_SECRET…) overrides the file,
and the command line overrides both. Lists in the environment are separated by ':' (';' on
Windows). TEXLOOM_CODES_FILES adds codes files to the others rather than replacing them.

Secrets (the database password, the S3 secret key, the admin token) belong in the file, readable
only by the server's user, or in the environment: on the command line they'd show in `ps`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

DEFAULT_FILES = (Path("texloom.toml"), Path("/etc/texloom/texloom.toml"))
# One-off commands, and options that only make sense typed: not read from the file or environment.
COMMANDS = {"config", "add_code", "renew_code", "remove_code", "purge_code", "list_codes", "import_codes_file", "export_codes", "allowance", "until", "months",
            "texlive_init", "files_for_backup", "files_restore", "copy_database_from", "backup", "backup_every",
            "backup_no_files", "help"}
ADDED = {"codes_file": "TEXLOOM_CODES_FILES"}  # environment lists added to, rather than replacing, the rest


class ConfigError(Exception):
    pass


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in d.items():
        key = f"{prefix}{k}".replace("-", "_")
        if isinstance(v, dict):
            out.update(_flatten(v, key + "_"))
        else:
            out[key] = v
    return out


def _convert(action: argparse.Action, value: object, where: str) -> object:
    """A value from the file (any TOML type) or the environment (text) as the option wants it."""
    if isinstance(action, argparse._StoreTrueAction):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        raise ConfigError(f"{where}: expected true or false")
    if isinstance(action, argparse._AppendAction):
        items = value if isinstance(value, list) else [x for x in str(value).split(os.pathsep) if x.strip()]
        return [action.type(str(x)) if action.type else x for x in items]
    if action.type is not None:
        try:
            return action.type(str(value))
        except (TypeError, ValueError, argparse.ArgumentTypeError) as e:
            raise ConfigError(f"{where}: {e}") from None
    return value


def _default_file() -> Path | None:
    """The first of DEFAULT_FILES that exists. One that can't even be looked for (a folder this user
    may not read, such as /etc/texloom for another service's user) is an error, not a missing file:
    carrying on without the server's settings could be wrong in quiet ways."""
    for f in DEFAULT_FILES:
        try:
            if f.exists():
                return f
        except OSError as e:
            raise ConfigError(f"can't look for the settings file {f.resolve()}: {e.strerror}. Run this as a user "
                              "that may read it, or name a settings file with --config (an empty one, such as "
                              "--config /dev/null, if none is needed)") from None
    return None


def parse(parser: argparse.ArgumentParser, argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line, with defaults from the settings file and the environment."""
    argv = sys.argv[1:] if argv is None else argv
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    known, _ = pre.parse_known_args(argv)
    path = known.config or (Path(os.environ["TEXLOOM_CONFIG"]) if os.environ.get("TEXLOOM_CONFIG") else None)
    actions = {a.dest: a for a in parser._actions if a.dest not in COMMANDS}
    values: dict[str, object] = {}
    try:
        if path is None:
            path = _default_file()
        if path is not None:
            try:
                raw = tomllib.loads(path.read_text())
            except OSError as e:
                raise ConfigError(f"can't read {path}: {e.strerror}") from None
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{path}: {e}") from None
            for key, v in _flatten(raw).items():
                if key not in actions:
                    raise ConfigError(f"{path}: unknown setting {key!r}")
                values[key] = _convert(actions[key], v, f"{path}: {key}")
        for dest, action in actions.items():
            name = f"TEXLOOM_{dest.upper()}"
            if dest in ADDED or name not in os.environ:
                continue
            values[dest] = _convert(action, os.environ[name], name)
        for dest, name in ADDED.items():
            if os.environ.get(name, "").strip():
                values[dest] = [*(values.get(dest) or []), *_convert(actions[dest], os.environ[name], name)]  # type: ignore[misc]
    except ConfigError as e:
        parser.error(str(e))
    parser.set_defaults(**values)
    a = parser.parse_args(argv)
    a.config = path
    return a
