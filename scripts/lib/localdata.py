"""The helpers the scripts in this folder share for working on the local database and its data folder: a plain Stop for
what ends a run, the data folder and its .env, the local database's address, running a program, a file only this user may
read, and finding PostgreSQL's programs. Stdlib only. backup_local.py is the user; nothing here prints a row, a secret or
a database address.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ENV_URL_KEYS = ("JOBSEARCH_DATABASE_URL", "DATABASE_URL")
#: Databases that are never worked on, whatever the .env says.
PROTECTED_DATABASES = {"postgres", "template0", "template1"}
LOOPBACK = ("localhost", "127.", "::1")


class Stop(Exception):
    """A reason, in a plain sentence, to end the run."""


def say(text: str = "") -> None:
    print(text, flush=True)


# --- the data folder, .env and the local database's address ---------------------------------------------------------


def default_data_dir() -> Path:
    """The data folder: RESUMEBOT_DATA_DIR, else the user's own folder per platform (the one `resumebot init` makes and
    stack.py defaults to; scripts/ cannot import them, so scripts/test_localdata.py holds this equal to stack.py's). A
    script's --data-dir says another."""
    named = os.environ.get("RESUMEBOT_DATA_DIR", "").strip()
    if named:
        return Path(named).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "resumebot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "resumebot"


ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    """The KEY=value lines of a .env file: comments and blank lines skipped, `export ` and one pair of quotes allowed."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        found = ENV_LINE.match(raw)
        if not found:
            continue
        value = found.group(2)
        quoted = re.fullmatch(r"([\"'])(.*)\1", value)
        out[found.group(1)] = quoted.group(2) if quoted else re.sub(r"\s+#.*$", "", value)
    return out


@dataclass(frozen=True)
class Target:
    """The local database: how to reach it, as the environment of a psql or pg_restore (the credentials are never on a
    command line) and what to call it in a message (never with the user or the password)."""

    env: dict[str, str]
    host: str
    port: str
    database: str

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"


def target_from_url(url: str) -> Target:
    """The target a database address names (`postgresql://`, with or without a `+driver`, as the app's settings hold it)."""
    parts = urlsplit(re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql://", url.strip()))
    if parts.scheme != "postgresql" or not parts.hostname:
        raise Stop("the database address in .env is not a postgresql:// address with a host")
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise Stop("the database address in .env names no database")
    host, port = parts.hostname, str(parts.port or 5432)
    env = {"PGHOST": host, "PGPORT": port, "PGDATABASE": database, "PGCONNECT_TIMEOUT": "10", "PGCLIENTENCODING": "UTF8"}
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    return Target(env=env, host=host, port=port, database=database)


def load_target(data_dir: Path) -> Target:
    """The local database named by the data folder's .env, which must be on this machine and not a system database."""
    env_file = data_dir / ".env"
    try:
        values = parse_env(env_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise Stop(f"there is no .env in the data folder ({data_dir}), so the local database's address is not known") from None
    url = next((values[key] for key in ENV_URL_KEYS if values.get(key)), "")
    if not url:
        raise Stop(f"{env_file} has no {ENV_URL_KEYS[0]}")
    target = target_from_url(url)
    if not target.host.lower().startswith(LOOPBACK):
        raise Stop(f"the database in .env is at {target.host}, not on this machine: these scripts only ever work on a local database")
    if target.database.lower() in PROTECTED_DATABASES:
        raise Stop(f"the database in .env is called {target.database}: these scripts never work on a system database")
    return target


# --- running things ------------------------------------------------------------------------------------------------


def first_line(data: bytes | str, limit: int = 300) -> str:
    """The first non-empty line of a program's complaint, for a message. Never more: later lines can carry row data."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return "(no message)"


def run(cmd: list[str], *, input: bytes | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = None if env is None else {**os.environ, **env}
    try:
        return subprocess.run(cmd, input=input, capture_output=True, env=full_env)
    except FileNotFoundError:
        raise Stop(f"{Path(cmd[0]).name} was not found") from None


# --- files that only this user may read ----------------------------------------------------------------------------


def current_user_sid() -> str:
    done = run(["whoami", "/user", "/fo", "csv", "/nh"])
    found = re.search(r"S-1-[0-9-]+", done.stdout.decode("utf-8", "replace")) if done.returncode == 0 else None
    if not found:
        raise Stop("could not tell which Windows user this is, so a file cannot be made readable by that user alone")
    return found.group(0)


def restrict_to_user(path: Path, *, directory: bool = False) -> None:
    """Make `path` readable and writable by this user alone: an ACL with no inherited entries on Windows, mode 600 (700 for
    a folder) elsewhere."""
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    grant = f"*{current_user_sid()}:" + ("(OI)(CI)F" if directory else "F")
    done = run(["icacls", str(path), "/inheritance:r", "/grant:r", grant])
    if done.returncode != 0:
        raise Stop(f"could not restrict {path.name} to this user: {first_line(done.stdout or done.stderr)}")


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    restrict_to_user(path, directory=True)


def find_pg_tool(name: str, pg_bin: str | None, data_dir: Path) -> str | None:
    """A PostgreSQL program: in --pg-bin, in RESUMEBOT_PG_BIN, on the PATH, in the installer's folder, or in a `bin` folder
    under the data folder (where a runtime kept inside it would be)."""
    exe = name + (".exe" if os.name == "nt" else "")
    for folder in (pg_bin, os.environ.get("RESUMEBOT_PG_BIN")):
        if folder and (Path(folder) / exe).is_file():
            return str(Path(folder) / exe)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    folders: list[Path] = []
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        folders += sorted(program_files.glob("*/bin"), reverse=True) if program_files.is_dir() else []
    for depth in ("*/bin", "*/*/bin", "*/*/*/bin"):
        folders += sorted(data_dir.glob(depth), reverse=True) if data_dir.is_dir() else []
    return next((str(f / exe) for f in folders if (f / exe).is_file()), None)


# --- SQLite: the database file, and whether the app is using it ------------------------------------------------------

SQLITE_PREFIX = "sqlite+aiosqlite:"
DEFAULT_APP_URL = "http://127.0.0.1:8430"


def database_url(data_dir: Path) -> str:
    """The database address the data folder's .env names (empty when it names none; a missing .env is a Stop)."""
    try:
        values = parse_env((data_dir / ".env").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise Stop(f"there is no .env in the data folder ({data_dir}), so the local database's address is not known") from None
    return next((values[key] for key in ENV_URL_KEYS if values.get(key)), "")


def is_sqlite_url(url: str) -> bool:
    return url.strip().startswith(SQLITE_PREFIX)


def sqlite_file(url: str, data_dir: Path) -> Path:
    """The file a `sqlite+aiosqlite:///<path>` address names (a relative path is under the data folder)."""
    rest = url.strip()[len(SQLITE_PREFIX):]
    if not rest.startswith("///"):
        raise Stop("the database address in .env is not a sqlite+aiosqlite:///<path> address")
    raw = unquote(rest[3:].split("?", 1)[0])
    if not raw or raw == ":memory:":
        raise Stop("the database address in .env names no file")
    path = Path(raw)
    return path if path.is_absolute() else data_dir / path


def app_url(data_dir: Path) -> str:
    """Where the app listens: RESUMEBOT_APP_URL of the .env (as stack.py reads it), else the default."""
    try:
        values = parse_env((data_dir / ".env").read_text(encoding="utf-8-sig"))
    except OSError:
        values = {}
    value = (os.environ.get("RESUMEBOT_APP_URL") or values.get("RESUMEBOT_APP_URL") or DEFAULT_APP_URL).rstrip("/")
    parts = urlsplit(value)  # the same test stack.py applies: the app is on this machine, so nothing else is probed
    if parts.scheme not in ("http", "https") or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise Stop(f"RESUMEBOT_APP_URL must be an address on this machine (127.0.0.1 or localhost), not {value}")
    return value


def app_answers(url: str) -> bool:
    """True when something answers /health at `url` (stack.py's own test of a running app: 200 or 503)."""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5):
            return True
    except urllib.error.HTTPError as error:
        return error.code in (200, 503)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def check_sqlite_file(path: Path) -> str | None:
    """None when `path` is a sound SQLite database (quick_check and foreign_key_check both clean), else the problem."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return f"cannot open it: {error}"
    try:
        found = [row[0] for row in conn.execute("PRAGMA quick_check")]
        if found != ["ok"]:
            return f"quick_check: {found[0]}"
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            return f"foreign_key_check: {len(broken)} row(s) point at a missing parent"
    except sqlite3.Error as error:
        return f"cannot read it: {error}"
    finally:
        conn.close()
    return None


def megabytes(count: int) -> str:
    return f"{count / (1 << 20):.1f} MB"
