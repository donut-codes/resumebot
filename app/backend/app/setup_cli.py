"""`resumebot init`, `connect`, `shortcut`, `open`, `doctor`, `status`: what the user's own Claude runs to set the app
up and to know where onboarding stands.

- `init` makes the data folder, its `.env` (the owner's name and IANA time zone, validated, and the SQLite database's
  address), the database file itself and its secret files.
- `connect` registers the app's doors with the user's Claude Code and checks that each answers.
- `shortcut` puts `resumebot.lnk` on the user's desktop (Windows); it runs `open`, which starts the app when it
  is not answering, the way `start` does, and opens the page in the default browser.
- `signin` runs `claude setup-token` on a pseudo-terminal out of sight and stores its token as the file the five
  agents sign in with; the user only approves in the browser (`--code` hands it the code the sign-in page shows).
- `doctor` says, one line each, what is missing: Claude Code, its sign-in, the agents' token, Node, the settings, the
  app, the doors.
- `status` says where onboarding stands in a few plain lines; it always exits 0, so a session-start hook can run it.

Nothing here needs the app's settings or its database: the commands read the data folder's files and ask the running
app over HTTP, so `resumebot --help` and a first `init` work before anything else is installed.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.windows_zones import WINDOWS_ZONES

#: The same name as `app.settings.DATA_DIR_VARIABLE` (a test holds them together); not imported, because importing the
#: settings reads the environment and these commands run before there is any.
DATA_VARIABLE = "RESUMEBOT_DATA_DIR"
DEFAULT_APP_URL = "http://127.0.0.1:8430"
#: The engine's port is the page's plus this (app/notice.py `OFFSET`; a test holds the two equal, as for stack.py).
ENGINE_OFFSET = 100
#: The files `init` makes in `secrets/` (random, kept when they are already there).
SECRET_FILES = ("agent-token", "wake-secret", "owner-token", "browser-token")
NODE_MAJOR = 22
#: The scout lanes: this many consecutive ports from SCOUT_PORT (stack.py's `LANE_COUNT` and
#: `DEFAULT_SCOUT_PORT`; a test holds them equal).
LANE_COUNT = 5
DEFAULT_SCOUT_PORT = 8651
#: The same name as `app.settings.APP_NAME` (a test holds them together); /health says it, so a check knows it reached
#: this app and not another program on the port.
APP_NAME = "resumebot"
HTTP_SECONDS = 8.0
#: How long `connect` waits for the engine's door to answer (a start in progress): the page is up first.
DOOR_WAIT_SECONDS = 30
CLAUDE_PROBE_SECONDS = 5
SUBPROCESS_SECONDS = 30
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
EXIT_REFUSED = 2  # the command line was wrong: a blank owner, an unknown time zone
EXIT_NOT_BUILT = 3  # the same meaning as in app/main_cli.py: asked for, and not built for this system yet
DATABASE_SETTING = "JOBSEARCH_DATABASE_URL"
DATABASE_FILE = "resumebot.db"
PACKET_SETTING = "RESUMEBOT_PACKET_DIR"
TASK_NAME = "resumebot"
#: TASK_NAME is the logon task of the removed `autostart` verb (only cleaned up now); ONE_OFF_TASK is the task
#: `resumebot start` makes: no trigger, run once now.
ONE_OFF_TASK = "resumebot-start"
#: How long `start` waits for the app to answer (the same as stack.py's own wait), and how often it asks.
START_WAIT_SECONDS = 300
START_POLL_SECONDS = 2.0
#: How long `signin` waits for the user to approve in the browser.
SIGNIN_WAIT_SECONDS = 600
SIGNIN_POLL_SECONDS = 0.25
#: The file in `secrets/` the scout reads for the agents' sign-in (app/scout/scout.mjs `claudeToken`).
TOKEN_FILE = "claude-oauth-token"
#: What `claude setup-token` prints is one long line; anything shorter or with a space in it was cut or padded.
TOKEN_MIN_LENGTH = 40

#: The clock and the wait, replaced by tests (nothing there sleeps).
_clock = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class Door:
    """One MCP door of the app that the user's Claude connects to. The scout's own door (`/mcp/`) is the lanes', not
    the user's, so it is not here."""

    name: str  #: what the user's Claude calls it
    path: str
    token_file: str  #: in `secrets/`, unless `setting` names another
    setting: str
    tool: str  #: a tool its `tools/list` names: how a check knows it reached the right door
    on: str = "page"  #: the program that serves it: "page" (the owner's address) or "engine" (the engine's address)


DOORS = (
    Door("resumebot-owner", "/mcp-owner/", "owner-token", "JOBSEARCH_OWNER_TOKEN_FILE", "act.do"),
    Door("resumebot-browser", "/mcp-browser/", "browser-token", "JOBSEARCH_BROWSER_TOKEN_FILE", "browser.next_item",
         on="engine"),
)


# --- the data folder and what is in it -------------------------------------------------------------------------------


def default_data_folder() -> Path:
    """Where an install keeps its data when nothing names a folder: the user's own, per platform."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "resumebot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "resumebot"


def data_folder(named: str | None = None) -> Path:
    """`--data`, else RESUMEBOT_DATA_DIR, else the default folder."""
    value = (named or os.environ.get(DATA_VARIABLE) or "").strip()
    return Path(value).expanduser().resolve() if value else default_data_folder()


def documents_folder() -> Path:
    """The user's Documents folder. On Windows it is asked of the known-folder API (it may be redirected, for example
    to OneDrive); when that fails, and on every other system, it is `Documents` in the home folder."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class Guid(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                            ("Data4", ctypes.c_ubyte * 8)]

            tail = (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)
            documents = Guid(0xFDD39AD0, 0x238F, 0x46AF, tail)  # FOLDERID_Documents
            get = ctypes.windll.shell32.SHGetKnownFolderPath
            get.restype = ctypes.HRESULT
            get.argtypes = [ctypes.POINTER(Guid), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
            free = ctypes.windll.ole32.CoTaskMemFree
            free.restype = None
            free.argtypes = [ctypes.c_void_p]
            out = ctypes.c_void_p()
            try:
                get(ctypes.byref(documents), 0, None, ctypes.byref(out))  # a nonzero HRESULT raises OSError
                found = ctypes.wstring_at(out.value) if out.value else ""
            finally:
                if out.value:
                    free(out)
            if found:
                return Path(found)
        except Exception:
            pass
    return Path.home() / "Documents"


def packet_setting_line() -> str:
    """`RESUMEBOT_PACKET_DIR=<Documents>/resumebot` for a new settings file, or "" (said on stdout) when that path
    could not be stored in it: a `$`, a line break, or whitespace then `#` (the readers strip that as a comment)."""
    where = str(documents_folder() / "resumebot")
    if re.search(r"[$\r\n]|\s#", where):
        print("packet folder: not set (the Documents path cannot be stored in settings)")
        return ""
    return f"{PACKET_SETTING}={where}\n"


ENV_NOT_UTF8 = ("{path} is not saved as UTF-8 (a Windows editor or PowerShell redirect may have saved it as "
               "UTF-16): open it and save it again as UTF-8")


def env_problem(path: Path) -> str | None:
    """One sentence when the settings file is there and cannot be read as UTF-8 text, else None."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return ENV_NOT_UTF8.format(path=path)
    except OSError:
        return None
    return ENV_NOT_UTF8.format(path=path) if "\x00" in text else None


def read_env(path: Path) -> dict[str, str]:
    """The KEY=value lines of a settings file: blank lines and # comments skipped, one pair of quotes allowed. A file
    that is not UTF-8 reads as empty (`env_problem` says why)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if parts:
            value = parts.group(2)
            quoted = re.match(r"^([\"'])(.*)\1$", value)
            found[parts.group(1)] = quoted.group(2) if quoted else re.sub(r"\s+#.*$", "", value)
    return found


SECRET_MIN_LENGTH = 32  #: `init` writes 64 hex characters; a file shorter than this was cut short


def secret_is_whole(path: Path) -> bool:
    """Whether a secret file is there with a value that is not blank or cut short (the value is never shown)."""
    try:
        return len(path.read_text(encoding="utf-8-sig").strip()) >= SECRET_MIN_LENGTH
    except (OSError, UnicodeDecodeError):
        return False


def has_settings_row(file: Path) -> bool | None:
    """Whether the SQLite file holds a saved Settings version, read without changing it. False for a database with no
    such table or row (a stopped `init` left it); None when the file is not a database that can be read at all (then
    `init` leaves it alone: it is someone's file)."""
    import sqlite3

    try:
        connection = sqlite3.connect(f"{file.resolve().as_uri()}?mode=ro", uri=True, timeout=HTTP_SECONDS)
        try:
            return connection.execute(
                "SELECT count(*) FROM profile_versions WHERE kind = 'settings'").fetchone()[0] > 0
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        return False if "no such table" in str(error) else None
    except sqlite3.Error:
        return None


class Install:
    """One data folder, read as it is now."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.env_file = data / ".env"
        self.env = read_env(self.env_file)
        self.env_problem = env_problem(self.env_file)

    @property
    def is_set_up(self) -> bool:
        return self.env_file.is_file()

    def missing_pieces(self) -> list[str]:
        """What a finished `init` leaves that is not there now, one plain phrase each (empty: complete). The settings
        file with its owner and database, the database file, each secret (not blank, not cut short) and the first
        Settings row. Every piece is looked at; nothing is changed."""
        if not self.is_set_up:
            return [f"the settings file {self.env_file}"]
        if self.env_problem:
            return [self.env_problem]
        missing = [f"{key} in the settings file" for key in ("JOBSEARCH_OWNER", DATABASE_SETTING)
                   if not self.env.get(key)]
        file = database_file(self.env.get(DATABASE_SETTING, ""))
        if file is not None:
            if not file.is_file():
                missing.append(f"the database file {file}")
            else:
                saved = has_settings_row(file)
                if saved is None:
                    missing.append(f"a database in {file} that can be read")
                elif not saved:
                    missing.append("the starter settings in the database")
        missing += [f"the secret {name}" for name in SECRET_FILES if not secret_is_whole(self.data / "secrets" / name)]
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_pieces()

    @property
    def app_url(self) -> str:
        return (self.env.get("RESUMEBOT_APP_URL") or DEFAULT_APP_URL).rstrip("/")

    @property
    def engine_url(self) -> str:
        """The engine's address: the page's port plus `ENGINE_OFFSET`, on this machine. The pool's sessions' doors
        (`/mcp-browser`) are the engine's."""
        return f"http://127.0.0.1:{(urllib.parse.urlsplit(self.app_url).port or 80) + ENGINE_OFFSET}"

    def door_base(self, door: Door) -> str:
        """The address of the program that serves `door`."""
        return self.engine_url if door.on == "engine" else self.app_url

    @property
    def agent_token_path(self) -> Path:
        """The agents' sign-in token (not a door's token: `token_path` is those)."""
        return self.data / "secrets" / TOKEN_FILE

    def token_path(self, door: Door) -> Path:
        return Path(self.env.get(door.setting) or self.data / "secrets" / door.token_file)

    def token(self, door: Door) -> str | None:
        try:
            return self.token_path(door).read_text(encoding="utf-8-sig").strip() or None
        except OSError:
            return None


# --- the owner's time zone --------------------------------------------------------------------------------------------

def is_zone(name: str) -> bool:
    """Whether `name` is an IANA zone this machine knows."""
    if not name or name != name.strip():
        return False
    try:
        ZoneInfo(name)
    except (OSError, ValueError, LookupError):  # ZoneInfoNotFoundError is a LookupError
        return False
    return True


def windows_zone_name() -> str | None:
    """The name Windows gives this machine's zone (what `tzutil /g` prints, such as "UTC"), read from the registry;
    None anywhere else, or when it cannot be read. Windows names its zones its own way: `machine_zone` turns the name
    into the IANA one."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    return str(name).split("\x00", 1)[0].strip() or None


def machine_zone() -> str | None:
    """This machine's IANA zone (TZ, the system's own file, or on Windows the standard name of the zone Windows
    reports, from the CLDR table in `app/windows_zones.py`), or None when it cannot be told for certain: `init` then
    asks for --timezone."""
    guesses = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            guesses.append(link.split("zoneinfo/", 1)[1])
        guesses.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    guesses.append(WINDOWS_ZONES.get(PLATFORM.zone_name() or "", ""))
    return next((guess for guess in guesses if is_zone(guess)), None)


def refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_REFUSED


# --- init ------------------------------------------------------------------------------------------------------------


def database_url(data: Path) -> str:
    """The address of the SQLite file in a data folder: three slashes and a drive letter on Windows, four and a rooted
    path elsewhere (the form `app.settings` and `stack.py` read)."""
    return f"sqlite+aiosqlite:///{(data / DATABASE_FILE).as_posix()}"


def database_file(url: str) -> Path | None:
    """The file a `sqlite+aiosqlite:///<path>` address names; None for any other kind of database."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix):]
    return Path(path[1:] if re.match(r"^/[A-Za-z]:", path) else path)


def create_database(url: str) -> bool:
    """Make the SQLite file `url` names, as an empty file is made today (every table from the models by
    `db.create_schema`, stamped at the Alembic head, as the copier makes its target). True when it was made; False when
    the address is not a SQLite file or the file is there already, and then nothing is touched."""
    path = database_file(url)
    if path is None or path.exists():
        return False
    import sqlalchemy as sa
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app import db

    config = Config(str(db.BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(db.BACKEND_DIR / "migrations"))
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            db.create_schema(connection)
            MigrationContext.configure(connection).stamp(ScriptDirectory.from_config(config), "head")
    except BaseException:
        engine.dispose()
        path.unlink(missing_ok=True)  # a file cut short is no database: the next run makes it again
        raise
    engine.dispose()
    return True


#: The night's spend limit a NEW install starts with, in dollars at API prices (`Settings.nightly_budget`). A shared
#: build starts light. The setting takes any number from 0 up and has no presets, so this is a judgement, not the
#: smallest allowed value (0 would stop every night): enough for one night's search, little enough that a mistake is
#: cheap. Open question in docs/PRD.md (path 48). Only a database `init` has just made is given it
#: (`seed_starter_settings`); an existing install keeps what it has saved.
STARTER_NIGHTLY_BUDGET = 5.0


def seed_starter_settings(url: str, owner: str) -> None:
    """Save the first version of the owner's Settings in a database `init` has just made: the night's spend limit set
    (`STARTER_NIGHTLY_BUDGET`), the signed-out job board read off (written out, not left to the default), and the job
    systems a new install starts without (`ats.NOT_OFFERED`: the runbooks that say `starts_off`; the owner's "Off and
    hidden", docs/PRD.md path 55) switched off. Nothing else is set. The schema's default is every system on, so an
    install that already exists is given none of this."""
    path = database_file(url)
    if path is None:
        return
    import sqlalchemy as sa

    from app.jobsearch import ats
    from app.jobsearch.actors import BY_APP
    from app.jobsearch.models import ProfileVersion
    from app.jobsearch.profile.schema import Settings
    from app.jobsearch.work import queue

    data = Settings.model_validate(
        {"nightly_budget": STARTER_NIGHTLY_BUDGET, "read_robots_exempt": False,
         "job_systems_off": list(ats.NOT_OFFERED)}).model_dump(mode="json")
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection, queue.exempt(
                "the first Settings row of a database `init` has just made: no app is running on it yet, so no turn "
                "is waiting to hear of it and the first start reads it"):
            connection.execute(sa.insert(ProfileVersion).values(
                id=uuid.uuid4(), owner=owner, kind="settings", version=1, data=data, saved_by=BY_APP,
                why="starter limits for a new install", saved_at=datetime.now(UTC)))
    finally:
        engine.dispose()


def port_problem(port: int, env: dict[str, str]) -> str | None:
    """Why the page cannot take this port, else None: it and the engine's (the page's plus `ENGINE_OFFSET`) must be
    real ports, and neither may fall in the lanes' range (`SCOUT_PORT`, else 8651, and the next `LANE_COUNT` - 1)."""
    engine = port + ENGINE_OFFSET
    if port < 1024 or engine > 65535:
        return (f"port {port} cannot be used: the page needs a port from 1024 up, and the engine takes the page's "
                f"plus {ENGINE_OFFSET}")
    raw = env.get("SCOUT_PORT", "")
    scout = int(raw) if raw.isdigit() and 0 < int(raw) < 65536 - LANE_COUNT else DEFAULT_SCOUT_PORT
    lanes = range(scout, scout + LANE_COUNT)
    for name, used in (("the page", port), ("the engine", engine)):
        if used in lanes:
            return (f"port {port} would put {name} on {used}, which is one of the lanes' ports "
                    f"({lanes[0]} to {lanes[-1]}): choose another")
    return None


def _write_env(path: Path, text: str) -> None:
    """Write the settings file whole or not at all: a temporary file in the same folder, then a rename."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def init(argv: list[str]) -> int:
    """make the data folder, its settings file (owner, time zone) and its secret files; run again, it finishes whatever
    a stopped run left undone and changes nothing that is already there"""
    parser = argparse.ArgumentParser(prog="resumebot init", description=init.__doc__)
    parser.add_argument("--owner", required=True, help="the name the roles are kept under")
    parser.add_argument("--timezone", help="an IANA zone such as America/New_York (default: this machine's)")
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    parser.add_argument("--port", type=int, help="the page's port, when the usual one is taken "
                        f"(the engine takes the page's plus {ENGINE_OFFSET})")
    args = parser.parse_args(argv)

    owner = args.owner.strip()
    if not owner:
        return refuse("the owner's name is blank: give --owner a name")
    if re.search(r"[\x00-\x1f#]|^[\"']|\$\{", owner):
        return refuse("the owner's name cannot hold a line break, a #, a ${, or start with a quote: "
                      "it is stored in .env")
    zone = args.timezone if args.timezone is not None else machine_zone()
    if zone is None:
        windows_name = PLATFORM.zone_name()
        called = f" (Windows calls it {windows_name!r}, which has no standard name here)" if windows_name else ""
        return refuse(f"this machine's time zone could not be told{called}: "
                      "give --timezone its IANA name, in the form Area/City, such as America/New_York")
    if not is_zone(zone):
        return refuse(f"{zone!r} is not an IANA time zone this machine knows (for example America/New_York): "
                      "give --timezone a real one")

    install = Install(data_folder(args.data))
    if re.search(r"\s#|\$\{", str(install.data)):  # the database address in .env would be cut or expanded there
        return refuse(f"the data folder {install.data} has a space then # or a ${{ in its path, which the settings "
                      "file cannot hold: choose another folder with --data")
    if install.env_problem:
        return refuse(install.env_problem)
    if args.port is not None:
        problem = port_problem(args.port, install.env)
        if problem:
            return refuse(problem)
    if install.is_set_up:
        have = (install.env.get("JOBSEARCH_OWNER"), install.env.get("JOBSEARCH_TIMEZONE"))
        if have != (owner, zone):
            print(f"{install.env_file} already names owner {have[0]!r} and zone {have[1]!r}: edit it by hand to change "
                  "them (init never rewrites it)", file=sys.stderr)
            return 1
    data = install.data
    url = install.env.get(DATABASE_SETTING) or database_url(data)
    try:
        data.mkdir(parents=True, exist_ok=True)
        made_database = create_database(url)
    except Exception as error:
        print(f"could not make the database at {url}: {error}", file=sys.stderr)
        return 1
    file = database_file(url)
    if file is not None and has_settings_row(file) is False:  # a stopped run made the file, no settings
        try:
            seed_starter_settings(url, owner)
        except Exception as error:
            print(f"could not save the starter settings in {file}: {type(error).__name__}: {error}: run "
                  "`resumebot init` again", file=sys.stderr)
            return 1
    folder = data / "secrets"
    folder.mkdir(parents=True, exist_ok=True)
    made = replaced = 0
    for name in SECRET_FILES:
        path = folder / name
        if secret_is_whole(path):
            continue
        existed = path.exists()  # blank or cut short: a half-written one is written again
        problem = _store_token(path, secrets.token_hex(32), what=f"the secret {name}")
        if problem:
            print(problem, file=sys.stderr)
            return 1
        replaced += existed
        made += not existed
    try:  # best effort: a folder in the user's own profile is private already
        folder.chmod(0o700)
    except OSError:
        pass
    # The settings file goes last, so a run that stopped before here left no file to be mistaken for a finished one.
    wanted = [(DATABASE_SETTING, url)]
    if args.port is not None:
        wanted.append(("RESUMEBOT_APP_URL", f"http://127.0.0.1:{args.port}"))
    if install.is_set_up:
        text = install.env_file.read_text(encoding="utf-8-sig")
    else:
        text = (f"# resumebot settings, made by `resumebot init`\nJOBSEARCH_OWNER={owner}\nJOBSEARCH_TIMEZONE={zone}\n"
                f"{packet_setting_line()}")
    added = False
    for key, value in wanted:
        if key not in install.env:
            text += ("" if text.endswith("\n") else "\n") + f"{key}={value}\n"
            added = True
    if args.port is not None and install.env.get("RESUMEBOT_APP_URL") and install.app_url != wanted[-1][1]:
        print(f"{install.env_file} already names the page at {install.app_url}: --port {args.port} was not applied "
              "(edit RESUMEBOT_APP_URL by hand to move it)", file=sys.stderr)
    if added or not install.is_set_up:
        try:
            _write_env(install.env_file, text)
        except OSError as error:
            print(f"could not write {install.env_file}: {error}", file=sys.stderr)
            return 1
    print(f"data folder: {data}")
    print(f"settings: {install.env_file} (owner and time zone {zone})")
    print(f"secrets: {made + replaced} made, {len(SECRET_FILES) - made - replaced} kept")
    if replaced:
        print(f"{replaced} secret file(s) were blank or cut short and are new: restart the app so it reads them")
    print(f"database: {'made' if made_database else 'kept (not touched)'}")
    current = os.environ.get(DATA_VARIABLE, "").strip()
    if current and os.path.normcase(current) != os.path.normcase(str(data)):
        print(f"{DATA_VARIABLE} already names {current}, and this install is at {data}: it was left alone "
              "(pass --data to every command for this one)")
    elif not current:
        said = False
        if sys.platform == "win32":
            try:
                PLATFORM.set_user_variable(DATA_VARIABLE, str(data))
                print(f"set {DATA_VARIABLE} to {data} for this user: new windows and a restarted Claude Code "
                      "find this folder without --data")
                said = True
            except OSError:
                pass
        if not said:
            print(f"optional: set {DATA_VARIABLE} to {data} for this user, and the app and these commands "
                  "find this folder without --data")
    return 0


def _set_user_variable(name: str, value: str) -> None:
    """Set an environment variable for this user, as `setx` does (HKCU Environment), and tell running programs so new
    windows see it. Windows only; raises OSError when the registry refuses."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    try:  # best effort: without the broadcast, a new sign-in still sees it
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))
    except Exception:
        pass


# --- signin -----------------------------------------------------------------------------------------------------------


def _no_window() -> int:
    """The creation flag that keeps a console program from opening a window of its own, which it does when started
    from a process with no console (the desktop shortcut runs pythonw.exe); 0 away from Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def _only_me_file(path: Path) -> None:
    """Leave this user alone with one file (Windows: inherited access dropped and this user's SID granted full access,
    as stack.py's `only_me` does for a folder; elsewhere mode 0600). Unlike `only_me` it fails closed: it raises.
    `init` only chmods `secrets/` (a no-op on Windows), so this file access list is the only protection: the folder
    itself is not limited."""
    if sys.platform == "win32":
        who = _quiet_run(["whoami", "/user", "/fo", "csv", "/nh"], check=True).stdout.strip()
        sid = who.split(",")[1].strip().strip('"')
        _quiet_run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"], check=True)
    else:
        path.chmod(0o600)


def _whole_token(token: str) -> bool:
    """One unbroken run of at least TOKEN_MIN_LENGTH characters: what `claude setup-token` prints."""
    return len(token) >= TOKEN_MIN_LENGTH and not re.search(r"\s", token)


def _store_token(target: Path, token: str, what: str = "the token") -> str | None:
    """Write the token as the agents' sign-in file, limited to this user before it holds anything. None when stored,
    else one line saying what failed (never the value)."""
    temporary = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)  # a leftover from a killed run may hold a token
    except OSError:
        return f"could not write {target}"
    problem = f"could not store {what} at {target}"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as file:  # truncated: empty until it is limited
            pass
        problem = f"could not limit {target} to this user"
        _only_me_file(temporary)
        problem = f"could not store {what} at {target}"
        with temporary.open("w", encoding="utf-8", newline="") as file:
            file.write(token)
        os.replace(temporary, target)
    except Exception:  # never the exception itself: it could carry the value
        temporary.unlink(missing_ok=True)
        return problem
    return None


#: A token `claude setup-token` makes lasts about a year; `doctor` says so from this age on.
TOKEN_OLD_DAYS = 330
_wall = time.time  #: replaced by tests


def token_is_stored(install: Install) -> bool:
    """The agents' sign-in file holds something shaped like a token: read here, in this process, and never shown or
    sent anywhere (no call is made to check it is still valid). One `sk-ant-` run, whole, not glued to another or cut
    short; anything else counts as not stored, so `signin` signs in anew. A token the first sign-in flow stored
    (`claude setup-token`, pasted at `resumebot signin`) is this same file."""
    try:
        text = install.agent_token_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return text.startswith("sk-ant-") and text.count("sk-ant-") == 1 and _whole_token(text)


def token_age_days(install: Install) -> int | None:
    """How many days ago the agents' sign-in file was written, or None when it cannot be told."""
    try:
        return int((_wall() - install.agent_token_path.stat().st_mtime) // 86400)
    except OSError:
        return None


def signin(argv: list[str]) -> int:
    """sign the agents in: runs `claude setup-token` out of sight, so you only approve it in the browser"""
    parser = argparse.ArgumentParser(prog="resumebot signin", description=signin.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    parser.add_argument("--code", help="the code the sign-in page showed, when no browser tab came back by itself")
    parser.add_argument("--again", action="store_true",
                        help="sign in anew although a token is stored (it is replaced only when the new sign-in works)")
    args = parser.parse_args(argv)
    install = Install(data_folder(args.data))
    if not install.is_set_up:
        print("no settings here yet: run `resumebot init` first", file=sys.stderr)
        return 1
    if args.code is not None:
        return _hand_over_code(install, args.code)
    if not args.again and not token_is_stored(install) and install.agent_token_path.is_file():
        print("the stored token is not a usable one (blank, cut short or glued): signing in anew")
    if not args.again and token_is_stored(install):  # a second run changes nothing; a token near its end is renewed
        print("already signed in: a token is stored, so nothing was changed "  # with --again
              "(`resumebot signin --again` signs in anew and replaces it only if that works)")
        return 0
    claude = PLATFORM.find_claude()
    if claude is None:
        print(CLAUDE_NOT_FOUND, file=sys.stderr)
        return 1
    return _run_signin(install, claude)


# --- the sign-in's own terminal: a pseudo-terminal, because `claude setup-token` prints nothing to a pipe ----------


class _ThreadedPty:
    """A child on a pseudo-terminal, read without blocking: a thread moves what it prints into a queue.
    `read()` gives what has arrived ("" when nothing has); `alive()` stays true until the output is all read."""

    def __init__(self, read_blocking, write_text, stop) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._write, self._stop = write_text, stop
        self._ended = False

        def pump() -> None:
            try:
                while True:
                    chunk = read_blocking()
                    if not chunk:
                        break
                    self._queue.put(chunk)
            except (EOFError, OSError):
                pass
            self._queue.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def read(self) -> str:
        text = ""
        while not self._ended:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            if chunk is None:
                self._ended = True
            else:
                text += chunk
        return text

    def write(self, text: str) -> None:
        try:
            self._write(text)
        except (EOFError, OSError):
            pass

    def alive(self) -> bool:
        return not self._ended or not self._queue.empty()

    def close(self) -> None:
        try:
            self._stop()
        except Exception:
            pass


def _spawn_pty(argv: Sequence[str], env: dict[str, str]):
    """Start a program on a pseudo-terminal wide enough that Ink never wraps a line (tests replace this)."""
    if sys.platform == "win32":
        import winpty  # pyright: ignore[reportMissingImports]  # pywinpty: installed on Windows only

        process = winpty.PtyProcess.spawn(list(argv), env=env, dimensions=(50, 1000))
        return _ThreadedPty(lambda: process.read(65536), process.write, lambda: process.terminate(force=True))
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
    try:
        process = _quiet_popen(list(argv), stdin=slave, stdout=slave, stderr=slave, env=env,
                                   start_new_session=True, close_fds=True)
    finally:
        os.close(slave)

    def read_blocking() -> str:
        return os.read(master, 65536).decode("utf-8", "replace")

    def stop() -> None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)

    return _ThreadedPty(read_blocking, lambda text: os.write(master, text.encode()), stop)


def _has_terminal() -> bool:
    """A real terminal on stdin. On Windows NUL also passes `isatty`, so ask the console itself."""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            mode = ctypes.c_ulong()
            return bool(kernel32.GetConsoleMode(kernel32.GetStdHandle(-10), ctypes.byref(mode)))
        except (OSError, AttributeError):
            return False
    return bool(sys.stdin and sys.stdin.isatty())


def _ask_for_code() -> str:
    """One line from the user's terminal, not shown (tests replace this)."""
    return getpass.getpass("")


#: Cursor moves that Ink writes in place of spaces and line breaks: they become the white space they stand for.
_MOVE_ACROSS = re.compile(r"\x1b\[[0-9;]*[CGHfI]")
_MOVE_DOWN_OR_UP = re.compile(r"\x1b\[[0-9;]*[ABEFd]")
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_TOKEN_RUN = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_ADDRESS = re.compile(r"https://[^\s\x1b\"'<>]+")
CODE_PROMPT = "Paste code here"
#: How long the sign-in waits for the result of a code it was handed, and `signin --code` for the sign-in's answer.
SIGNIN_CODE_WAIT_SECONDS = 60
#: In `secrets/`: what the waiting sign-in says (`waiting`, then the result) and the code `signin --code` hands it.
SIGNIN_STATUS_FILE = "signin-status"
SIGNIN_CODE_FILE = "signin-code"
#: In `secrets/`: the waiting sign-in's process id, so `signin --code` can tell a live one from one that was killed.
SIGNIN_PID_FILE = "signin-pid"
SIGNIN_WAITING = "waiting"


def screen_text(raw: str) -> str:
    """What `claude setup-token` printed, as plain text: cursor moves become the spaces and line breaks they stand for
    (else the words after the token glue onto it), every other escape sequence goes."""
    text = _MOVE_ACROSS.sub(" ", raw)
    text = _MOVE_DOWN_OR_UP.sub("\n", text)
    return _ESCAPES.sub("", text).replace("\r", "\n")


def _delimited(pattern: re.Pattern[str], text: str, ended: bool):
    """Each match of `pattern` that is whole: something follows it, or the output has ended."""
    for found in pattern.finditer(text):
        if found.end() < len(text) or ended:
            yield found.group(0)


def find_token(text: str, *, ended: bool = False) -> str | None:
    """The token on the screen: `sk-ant-` and the run of token characters after it, up to the first other character."""
    for run in _delimited(_TOKEN_RUN, text, ended):
        if _whole_token(run):
            return run
    return None


def find_address(text: str, *, ended: bool = False) -> str | None:
    """The sign-in address `setup-token` prints (the authorize page when there is one, else the first address)."""
    found = list(_delimited(_ADDRESS, text, ended))
    chosen = [one for one in found if "authorize" in one or "oauth" in one] or found
    return chosen[0] if chosen else None


def _secrets(install: Install) -> Path:
    return install.agent_token_path.parent


def _write_status(install: Install, text: str) -> None:
    try:
        folder = _secrets(install)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / SIGNIN_STATUS_FILE).write_text(text, encoding="utf-8")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Whether a process with this id is running (tests replace this)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, as another user's
        return True
    return True


def _signin_pid(install: Install) -> int | None:
    try:
        return int((_secrets(install) / SIGNIN_PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _signin_is_live(install: Install) -> bool:
    """Whether the sign-in that wrote `waiting` is still running: its pid file names a running process."""
    pid = _signin_pid(install)
    return pid is not None and _pid_alive(pid)


def _clear_signin_files(install: Install) -> None:
    for name in (SIGNIN_STATUS_FILE, SIGNIN_CODE_FILE, SIGNIN_PID_FILE):
        try:
            (_secrets(install) / name).unlink(missing_ok=True)
        except OSError:
            pass


def _take_code(install: Install, typed: queue.Queue[str]) -> str | None:
    """A code the user pasted in this terminal, else one `signin --code` left in its file (deleted as it is read)."""
    try:
        return typed.get_nowait().strip() or None
    except queue.Empty:
        pass
    path = _secrets(install) / SIGNIN_CODE_FILE
    try:
        code = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path.unlink(missing_ok=True)
    return code or None


def redact(text: str) -> str:
    """Screen text safe to show: every `sk-ant-` run, every address and every long unbroken run blanked."""
    text = re.sub(r"sk-ant-\S*", "[token]", text)
    text = re.sub(r"https?://\S+", "[address]", text)
    return re.sub(r"\S{40,}", "[long text]", text)


def _ended_help(raw: str) -> str:
    """What `claude setup-token` last showed (redacted, the last few lines) and the usual causes."""
    tail = [line.strip() for line in screen_text(raw).splitlines() if line.strip()][-5:]
    shown = "\n".join(f"  | {redact(line)}" for line in tail) or "  (it showed nothing)"
    return (f"what it last showed:\n{shown}\n"
            "usual causes: the browser sign-in was closed or not finished, Claude Code is not signed in or needs "
            "updating (run `claude`, then /login), or the account is not one that can make a token. You can also run "
            "`claude setup-token` yourself in a terminal and keep its token for `resumebot signin`.")


def _run_signin(install: Install, claude: str) -> int:
    """Run `claude setup-token` on a pseudo-terminal and wait up to ten minutes for its token, which goes to the
    agents' file and nowhere else (never printed). The user approves in the browser tab setup-token opens; if none
    opened, they open the printed address and give back the code its page shows."""
    waiting = ("a sign-in is already waiting: approve it in the browser, or give its code with "
               "`resumebot signin --code` (nothing was changed)")
    if _signin_is_live(install) and _signin_pid(install) != os.getpid():
        print(waiting, file=sys.stderr)
        return 1
    _clear_signin_files(install)  # what an earlier run left (a killed one's `waiting`, a code) is not for this one
    try:
        _secrets(install).mkdir(parents=True, exist_ok=True)
        with (_secrets(install) / SIGNIN_PID_FILE).open("x", encoding="utf-8") as file:  # exclusive: one run wins
            file.write(str(os.getpid()))
    except FileExistsError:
        print(waiting, file=sys.stderr)
        return 1
    except OSError:
        pass
    _write_status(install, SIGNIN_WAITING)
    try:
        child = _spawn_pty([claude, "setup-token"], dict(os.environ))
    except Exception as error:  # only its type: nothing of the child's output
        reason = f": {error}" if isinstance(error, (ImportError, FileNotFoundError)) else ""  # no secret is in these
        message = f"could not start `claude setup-token`: {type(error).__name__}{reason}"
        _write_status(install, message)
        (_secrets(install) / SIGNIN_PID_FILE).unlink(missing_ok=True)
        print(message, file=sys.stderr)
        return 1
    typed: queue.Queue[str] = queue.Queue()
    if _has_terminal():
        def listen() -> None:
            try:
                while True:
                    typed.put(_ask_for_code())
            except (EOFError, KeyboardInterrupt, OSError):
                pass

        threading.Thread(target=listen, daemon=True).start()
    not_accepted = "that code was not accepted: nothing was stored; run `resumebot signin` again"
    raw, told, handed = "", False, None
    end = _clock() + SIGNIN_WAIT_SECONDS
    failure: str | None = None
    try:
        while True:
            chunk = child.read()
            raw = (raw + chunk)[-100_000:]
            alive = child.alive()
            text = screen_text(raw)
            token = find_token(text, ended=not alive)
            if token:
                failure = _store_token(install.agent_token_path, token)
                del token
                break
            if not told:
                address = find_address(text, ended=not alive)
                if address:
                    print(f"approve the sign-in in the browser tab that opened; if none opened, open: {address}",
                          flush=True)
                    told = True
            if CODE_PROMPT in text and handed is None:
                code = _take_code(install, typed)
                if code:
                    child.write(code)
                    child.write("\r")
                    handed = _clock()
                    del code
            if not alive and not chunk:
                failure = not_accepted if handed is not None else (
                    "`claude setup-token` ended without a token: nothing was stored; run `resumebot signin` again")
                break
            if handed is not None and _clock() - handed >= SIGNIN_CODE_WAIT_SECONDS:
                failure = not_accepted
                break
            if _clock() >= end:
                failure = "timed out after 10 minutes: nothing was stored; run `resumebot signin` again"
                break
            if not chunk:
                _sleep(SIGNIN_POLL_SECONDS)
    finally:
        child.close()
        ours = _signin_pid(install) == os.getpid()  # a second run's files are not ours to delete
        if ours:
            (_secrets(install) / SIGNIN_CODE_FILE).unlink(missing_ok=True)
            (_secrets(install) / SIGNIN_PID_FILE).unlink(missing_ok=True)
    if failure:
        if ours:
            _write_status(install, failure)
        print(failure, file=sys.stderr)
        if "ended without a token" in failure:
            print(_ended_help(raw), file=sys.stderr)
        return 1
    if ours:
        _write_status(install, "signed in")
    print("signed in", flush=True)
    return 0


def _hand_over_code(install: Install, code: str) -> int:
    """`signin --code`: give the waiting `signin` the code the sign-in page showed, through a file only this user can
    read, and wait for its answer."""
    code = code.strip()
    if not code:
        print("no code given: nothing was written", file=sys.stderr)
        return 1
    status = _secrets(install) / SIGNIN_STATUS_FILE
    try:
        waiting = status.read_text(encoding="utf-8").strip() == SIGNIN_WAITING
    except OSError:
        waiting = False
    if not waiting:
        print("no sign-in is waiting for a code: run `resumebot signin` first", file=sys.stderr)
        return 1
    if not _signin_is_live(install):  # `waiting` stays in its file when the sign-in was killed
        print("the sign-in that was waiting is no longer running: run `resumebot signin` again", file=sys.stderr)
        return 1
    problem = _store_token(_secrets(install) / SIGNIN_CODE_FILE, code, what="the code")
    del code
    if problem:
        print(problem, file=sys.stderr)
        return 1
    end = _clock() + SIGNIN_CODE_WAIT_SECONDS
    while True:
        try:
            result = status.read_text(encoding="utf-8").strip()
        except OSError:
            result = SIGNIN_WAITING
        if result != SIGNIN_WAITING:
            if result == "signed in":
                print("signed in")
                return 0
            print(result, file=sys.stderr)
            return 1
        if _clock() >= end:
            (_secrets(install) / SIGNIN_CODE_FILE).unlink(missing_ok=True)
            print("no answer to that code yet: run `resumebot signin` again", file=sys.stderr)
            return 1
        _sleep(SIGNIN_POLL_SECONDS)


def _launch(command: Sequence[str], **kwargs) -> None:
    """Start a program that outlives this one (tests replace this: nothing real starts)."""
    _quiet_popen(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 **kwargs)


class Platform:
    """What this module asks of the PC it runs on, in one place: the time zone name from the registry, a user
    environment variable, PowerShell (the scheduled task and the shortcut) and the lookup of Claude Code. Each method
    calls the module's function of the same job, so a test or a rehearsal replaces `PLATFORM` (or one function) and
    nothing real is touched."""

    def zone_name(self) -> str | None:
        return windows_zone_name()

    def set_user_variable(self, name: str, value: str) -> None:
        _set_user_variable(name, value)

    def powershell(self, script: str) -> subprocess.CompletedProcess[str]:
        return _powershell(script)

    def find_claude(self) -> str | None:
        return find_claude()

    def shortcut_problem(self, install: Install) -> str | None:
        return _shortcut_problem(install)


PLATFORM = Platform()


def _call(command: Sequence[str]) -> int:
    """Run a program on this one's own input and output; its exit code (tests replace this)."""
    return subprocess.call(list(command))


# --- the app, over HTTP -----------------------------------------------------------------------------------------------


def _client() -> httpx.Client:
    # trust_env=False: a proxy variable on this PC must not stand between the setup commands and the app on this same PC
    # (stack.py's own checks already bypass proxies the same way).
    return httpx.Client(timeout=HTTP_SECONDS, trust_env=False)


def start_hint(install: Install) -> str:
    """How to start the app by hand: the `resumebot start` command on this data folder."""
    return f'`resumebot start --data "{install.data}"`'


NOTHING_ANSWERS = "nothing answers at"


def _this_version() -> str:
    from app.settings import VERSION  # not at the top: the settings read the environment

    return VERSION


def _identity(client: httpx.Client, install: Install, base: str) -> str | None:
    """None when `base` answers /health as this app, this version, healthy; else one sentence on what is wrong. No
    token is sent: nothing is told a secret before it is known to be this app."""
    try:
        reply = client.get(base + "/health")
    except httpx.HTTPError:
        return f"{NOTHING_ANSWERS} {base}: start the app with {start_hint(install)}"
    try:
        body = reply.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("app") != APP_NAME:
        return (f"something else answers at {base} (it is not {APP_NAME}, status {reply.status_code}): stop that "
                f"program or move this install to another port")
    if reply.status_code != 200 or body.get("status") != "ok" or body.get("db") != "ok":
        return (f"the app at {base} is not healthy yet (status {body.get('status')}, db {body.get('db')}): wait, or "
                "see its log")
    wanted = _this_version()
    if body.get("version") != wanted:
        return (f"an older or different {APP_NAME} ({body.get('version')}, this install is {wanted}) answers at "
                f"{base}: stop it, then start the app with {start_hint(install)}")
    return None


def app_health(install: Install) -> str | None:
    """None when this app answers /health with ok; else one sentence on what is wrong."""
    with _client() as client:
        return _identity(client, install, install.app_url)


def check_door(install: Install, door: Door) -> str | None:
    """None when the door answers a `tools/list` with its token; else one sentence on what is wrong. The program must
    first say it is this app (no token is sent before that)."""
    token = install.token(door)
    if token is None:
        return f"no token at {install.token_path(door)}: run `resumebot init`"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    base = install.door_base(door)
    with _client() as client:
        unknown = _identity(client, install, base)
        if unknown is not None:
            return unknown
        try:
            reply = client.post(base + door.path, json=body,
                                headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            return f"{NOTHING_ANSWERS} {base}: start the app with {start_hint(install)}"
    if reply.status_code == 404:
        return f"{door.path} is not there (404): the running app may be older than this install"
    if reply.status_code == 401:
        return f"{door.path} refused the token (401): this install's token does not match the app's"
    if reply.status_code != 200 or door.tool not in reply.text:
        return f"{door.path} answered {reply.status_code} and not its tools"
    return None


def _json(client: httpx.Client, install: Install, path: str) -> dict | None:
    try:
        reply = client.get(install.app_url + path)
        return reply.json() if reply.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# --- connect ---------------------------------------------------------------------------------------------------------


def _quiet_run(command: Sequence[str], *, timeout: float = SUBPROCESS_SECONDS, check: bool = False,
               cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """The one way this module runs a child and reads its output. No console of its own: under the desktop shortcut
    (pythonw.exe, no console) each call would otherwise open a window on the user's screen. Output is decoded as UTF-8
    with undecodable bytes replaced, never with the PC's code page: a child may print a byte the code page leaves
    undefined, and a strict decode would end the command with a traceback (a test fails on any other `subprocess` call
    in this file, except `_call`, which keeps the user's own console)."""
    return subprocess.run(list(command), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=check, cwd=cwd, creationflags=_no_window())


def _quiet_popen(command: Sequence[str], **kwargs) -> subprocess.Popen:
    """The one way this module starts a program that outlives the call: no console window of its own."""
    kwargs.setdefault("creationflags", _no_window())
    return subprocess.Popen(list(command), **kwargs)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`claude mcp ...` runs in a folder of no project (the temporary folder): run in a project whose `.mcp.json`, or
    whose local config, holds a door of the same name, `mcp get` reports that entry instead of the user's, and connect
    would register again a door that already works (found on the owner's install, 2.5.6). The doors are the user's."""
    if list(command)[1:2] == ["mcp"]:
        return _quiet_run(command, cwd=tempfile.gettempdir())
    return _quiet_run(command)


def _describe(error: BaseException) -> str:
    """What to print about a failed child. Never `str(error)`: a TimeoutExpired or CalledProcessError holds the whole
    command line, and a connect command carries the door's token."""
    text = type(error).__name__
    if isinstance(error, OSError) and error.strerror:
        text += f": {error.strerror}"
    return text


#: Where a Windows install of Claude Code leaves a program a terminal can run, in the order they are looked in after
#: the PATH. From Claude Code's own setup documentation (code.claude.com/docs/en/setup); the WinGet and npm folders are
#: those tools' own defaults:
#: - the native installer: `%USERPROFILE%\.local\bin\claude.exe` ("Uninstall a native installation";
#:   `~/.local/bin/claude` elsewhere). It does not always put the folder on the PATH of every shell.
#: - WinGet (`winget install Anthropic.ClaudeCode`): a link in `%LOCALAPPDATA%\Microsoft\WinGet\Links`, or the program
#:   in the package's own folder under `...\WinGet\Packages` where links are off.
#: - npm (`npm install -g @anthropic-ai/claude-code`): npm's shim `%APPDATA%\npm\claude.cmd`, its default global folder.
#: - the Claude desktop app's own copy, `<root>\<version>\<build>\claude.exe`: it is a whole Claude Code (`claude auth
#:   status` and `claude mcp add` answer from a terminal, and it shares `~/.claude.json` with the app's sessions), but
#:   it is on no PATH and its folder is new with every update, so it is looked for each time and never kept. The
#:   scout's `findClaude` (app/scout/scout.mjs) looks in the same places for the lanes.
CLAUDE_INSTALL_FOLDER = Path(".local") / "bin"
CLAUDE_SETUP_PAGE = "https://code.claude.com/docs/en/setup"
CLAUDE_NOT_FOUND_WHERE = (
    f"not found on PATH, in the native installer's folder (~/{CLAUDE_INSTALL_FOLDER.as_posix()}), in WinGet's or "
    f"npm's, or in the Claude desktop app's: install it from {CLAUDE_SETUP_PAGE}")
CLAUDE_NOT_FOUND = f"Claude Code was {CLAUDE_NOT_FOUND_WHERE}, then run this again"
_VERSION_FOLDER = r"\d+(?:\.\d+){1,3}"


def _names(folder: Path, pattern: str) -> list[Path]:
    """The entries of `folder` whose whole name matches `pattern` (any case); none when it cannot be read."""
    try:
        return sorted(entry for entry in folder.iterdir() if re.fullmatch(pattern, entry.name, re.IGNORECASE))
    except OSError:
        return []


def _desktop_claude(root: Path) -> list[tuple[bool, tuple[int, ...], float, Path]]:
    """The Claude desktop app's copies under one of its folders, `<root>/<version>/<build>/claude.exe`, each with
    whether the `.payload` beside it vouches for its size (a copy still being written is left out), its version and
    its age."""
    copies: list[tuple[bool, tuple[int, ...], float, Path]] = []
    for version in _names(root, _VERSION_FOLDER):
        for build in _names(version, ".+"):
            program = build / "claude.exe"
            try:
                size, modified = program.stat().st_size, build.stat().st_mtime
                if not program.is_file():
                    continue
            except OSError:
                continue
            try:
                promised = json.loads((build / ".payload").read_text(encoding="utf-8")).get("size")
            except (OSError, ValueError, AttributeError):
                promised = None
            if isinstance(promised, int) and promised != size:
                continue
            numbers = tuple(int(n) for n in version.name.split("."))
            copies.append((isinstance(promised, int), numbers, modified, program))
    return copies


def _claude_locations() -> list[Path]:
    """The places a supported install leaves Claude Code, in the order they are tried: the seam a test sets. Built from
    the environment each time, never kept."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if sys.platform != "win32":
        return [home / CLAUDE_INSTALL_FOLDER / "claude"]
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    winget = local / "Microsoft" / "WinGet"
    places = [home / CLAUDE_INSTALL_FOLDER / "claude.exe", winget / "Links" / "claude.exe"]
    places += [package / "claude.exe" for package in _names(winget / "Packages", r"Anthropic\.ClaudeCode_.+")]
    places.append(roaming / "npm" / "claude.cmd")
    # The desktop app is a packaged app: what it keeps under %APPDATA%\Claude a terminal sees in its package's folder;
    # newer versions keep their copies under %LOCALAPPDATA%\Claude-Data instead.
    roots = [roaming / "Claude" / "claude-code"]
    roots += [package / "LocalCache" / "Roaming" / "Claude" / "claude-code"
              for package in _names(local / "Packages", r"Claude_[a-z0-9]+")]
    roots += [data / "claude-code" for data in _names(local / "Claude-Data", r"claude_[a-z0-9]+")]
    copies = [copy for root in roots for copy in _desktop_claude(root)]
    return places + [copy[-1] for copy in sorted(copies, key=lambda copy: copy[:3], reverse=True)]


def _claude_candidates() -> list[str]:
    """Every program that could be Claude Code, in the order they are tried: the one on the PATH, then each place a
    supported install leaves it that holds a file."""
    found = []
    on_path = shutil.which("claude")
    if on_path:
        found.append(on_path)
    found += [str(place) for place in _claude_locations() if place.is_file() and str(place) not in found]
    return found


def _claude_runs(program: str) -> bool:
    """Whether `program --version` answers (a dangling shim on the PATH does not): the seam a test sets."""
    try:
        return _quiet_run([program, "--version"], timeout=CLAUDE_PROBE_SECONDS).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_claude() -> str | None:
    """The one lookup for Claude Code: the first candidate that answers `--version` (the PATH first, then each place a
    supported install leaves it)."""
    return next((program for program in _claude_candidates() if _claude_runs(program)), None)


def _searched_folders() -> str:
    """The folders a missing Claude Code was looked for in, for the sentence that says so."""
    folders: list[str] = []
    for place in _claude_locations():
        if str(place.parent) not in folders:
            folders.append(str(place.parent))
    return f" (looked in: {', '.join(folders)})" if folders else ""


def claude_config_file() -> Path:
    """The file Claude Code keeps its MCP servers in: `.claude.json` in `CLAUDE_CONFIG_DIR` when that is set, else in
    the user's home folder."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(folder).expanduser() if folder else Path.home()) / ".claude.json"


@dataclass(frozen=True)
class Registration:
    """What `claude mcp get <name>` says Claude Code holds for a door. `url` is "" when the output had no URL line."""

    url: str
    token: str | None  #: the bearer token in its Authorization header, where it is shown; never printed
    scope: str | None  #: the scope line (`User config`, `Local config`, `Project config`)


def _registration(claude: str, door: Door) -> Registration | None:
    """What Claude Code holds for `door`, or None when it holds none (`mcp get` of a missing name exits 1). Raises
    OSError and SubprocessError when Claude Code cannot be run. The output holds the bearer header: it is parsed here
    and never printed."""
    got = _run([claude, "mcp", "get", door.name])
    if got.returncode != 0:
        return None
    url = re.search(r"^\s*URL:\s*(\S+)", got.stdout, re.MULTILINE)
    token = re.search(r"^\s*Authorization:\s*Bearer\s+(\S+)", got.stdout, re.MULTILINE | re.IGNORECASE)
    scope = re.search(r"^\s*Scope:\s*(.+?)\s*$", got.stdout, re.MULTILINE)
    return Registration(url.group(1) if url else "", token.group(1) if token else None,
                        scope.group(1) if scope else None)


def _same_address(one: str, other: str) -> bool:
    return one.rstrip("/") == other.rstrip("/")


def _registration_problem(held: Registration | None, url: str, token: str | None, name: str) -> str | None:
    """None when Claude Code holds the door at `url` (and with `token`, where it shows one); else one sentence. Never
    holds a token."""
    if held is None:
        return "your Claude Code does not have it"
    if not held.url:
        return f"could not read what your Claude Code holds (run `claude mcp get {name}` to see it)"
    where = ""
    if held.scope is not None and not held.scope.lower().startswith("user"):
        word = held.scope.split()[0].lower()
        where = (f" in {held.scope}, which wins over the user config: remove it with "
                 f"`claude mcp remove {name} --scope {word}`")
    if not _same_address(held.url, url):
        return f"your Claude Code has it registered at {held.url}, not at this install's {url}{where}"
    if token is not None and held.token is not None and held.token != token:
        return f"your Claude Code holds a token for it that is not this install's{where}"
    return None


def _first_reason(done: subprocess.CompletedProcess[str]) -> str:
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[0] if lines else str(done.returncode)


def _add_command(claude: str, door: Door, url: str, token: str) -> list[str]:
    return [claude, "mcp", "add", "--transport", "http", "--scope", "user", door.name, url,
            "--header", f"Authorization: Bearer {token}"]


def _put_back(claude: str, door: Door, old: Registration | None) -> str:
    """After a failed replace: add the old door again; one sentence on how that went."""
    if old is None or not old.url or old.token is None:
        return "the old door could not be read, so it was not put back: run `resumebot connect` again"
    try:
        again = _run(_add_command(claude, door, old.url, old.token))
    except (OSError, subprocess.SubprocessError) as error:
        return f"the door is now unregistered ({_describe(error)}): run `resumebot connect` again"
    if again.returncode == 0:
        return "the old door was put back"
    return f"the door is now unregistered ({_first_reason(again)}): run `resumebot connect` again"


def _register(claude: str, door: Door, url: str, token: str, old: Registration | None) -> str | None:
    """Add the door; None when it is added, else one sentence. Add first: a name that is already taken is the one
    reason to remove the old door, and a failure after the remove puts the old one back."""
    command = _add_command(claude, door, url, token)
    # The token is never printed, not even a part of it.
    shown = command[:-1] + ["Authorization: Bearer <hidden>"]
    print("running: " + shlex.join(shown))
    try:
        added = _run(command)
    except (OSError, subprocess.SubprocessError) as error:
        return (f"could not run Claude Code: {_describe(error)}; the door may or may not be registered now: check "
                "`claude mcp list`")
    if added.returncode == 0:
        return None
    if "already exists" not in ((added.stderr or "") + (added.stdout or "")).lower():
        return f"Claude Code refused to register it: {_first_reason(added)}"
    try:  # the name is taken by another address or token: replace it
        removed = _run([claude, "mcp", "remove", door.name, "--scope", "user"])
        if removed.returncode != 0:
            return f"Claude Code would not remove the old door: {_first_reason(removed)}; it was left as it is"
        added = _run(command)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run Claude Code: {_describe(error)}; {_put_back(claude, door, old)}"
    if added.returncode != 0:
        return f"Claude Code refused to register it: {_first_reason(added)}; {_put_back(claude, door, old)}"
    return None


def _wait_for_door(install: Install, door: Door) -> str | None:
    """`check_door`, asked again for up to `DOOR_WAIT_SECONDS` while the engine's door has nothing answering yet (the
    engine comes up after the page)."""
    end = _clock() + DOOR_WAIT_SECONDS
    while True:
        wrong = check_door(install, door)
        if wrong is None or door.on != "engine" or NOTHING_ANSWERS not in wrong or _clock() >= end:
            return wrong
        _sleep(START_POLL_SECONDS)


def connect(argv: list[str]) -> int:
    """register the app's doors with your Claude Code (this install's tokens) and check each one answers"""
    parser = argparse.ArgumentParser(prog="resumebot connect", description=connect.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    install = Install(data_folder(parser.parse_args(argv).data))
    if not install.is_set_up:
        print(f"no settings at {install.env_file}: run `resumebot init` first", file=sys.stderr)
        return 1
    claude = PLATFORM.find_claude()
    if claude is None:
        print(CLAUDE_NOT_FOUND + _searched_folders(), file=sys.stderr)
        return 1
    print(f"using Claude Code at {claude} (its configuration: {claude_config_file()})")
    problems = 0
    for door in DOORS:
        token = install.token(door)
        if token is None:
            print(f"{door.name}: no token at {install.token_path(door)}: run `resumebot init`", file=sys.stderr)
            problems += 1
            continue
        url = install.door_base(door) + door.path
        try:
            held = _registration(claude, door)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"{door.name}: could not run Claude Code: {_describe(error)}", file=sys.stderr)
            problems += 1
            continue
        already = (held is not None and held.token is not None
                   and _registration_problem(held, url, token, door.name) is None)
        if not already:
            refused = _register(claude, door, url, token, held)
            if refused is not None:
                print(f"{door.name}: {refused}", file=sys.stderr)
                problems += 1
                continue
            try:  # what Claude Code holds now is read back: the add's own word is not taken for it
                mismatch = _registration_problem(_registration(claude, door), url, token, door.name)
            except (OSError, subprocess.SubprocessError) as error:
                mismatch = f"could not read it back: {_describe(error)}"
            if mismatch is not None:
                print(f"{door.name}: registered, but {mismatch}", file=sys.stderr)
                problems += 1
                continue
        wrong = _wait_for_door(install, door)
        verb = "already registered" if already else "registered"
        if wrong is None:
            print(f"{door.name}: {verb}, and it answers")
        elif NOTHING_ANSWERS in wrong and door.on == "engine":
            print(f"{door.name}: {verb}; the engine is not up yet: {wrong}")
        else:
            print(f"{door.name}: {verb}, and it does NOT answer: {wrong}")
        problems += wrong is not None
    return 1 if problems else 0


# --- the one-off start, the desktop shortcut and `open` --------------------------------------------------------------


def _quoted(text: str) -> str:
    """A PowerShell single-quoted string."""
    # PowerShell also ends a quoted string at the typographic single quotes, so those are doubled like the apostrophe.
    for quote in "'‘’‚‛":
        text = text.replace(quote, quote * 2)
    return "'" + text + "'"


def _stack_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "local" / "stack.py"


def _backend_folder() -> Path:
    """The folder `python -m app.main_cli` must run in (the one that holds the `app` package)."""
    return Path(__file__).resolve().parents[1]


def shortcut_icon() -> Path:
    """The robot icon the desktop shortcut wears: the page's favicon as a Windows icon, shipped beside this module."""
    return Path(__file__).resolve().with_name("resumebot.ico")


def _python(windowless: bool) -> str:
    """This install's Python; with `windowless`, the pythonw.exe beside it when there is one, so no console window
    shows (the programs started that way work with no standard streams)."""
    program = Path(sys.executable)
    if windowless:
        windowed = program.with_name("pythonw.exe")
        if windowed.is_file():
            program = windowed
    return str(program)


def start_command(data: Path, windowless: bool = True) -> tuple[str, str]:
    """The program and its arguments that start the app: this install's Python running `stack.py start` on this data
    folder, the same start the setup skill uses by hand. `windowless` picks pythonw.exe beside that Python when there
    is one (stack.py start needs no console: it hides its own supervisor and works with no standard streams)."""
    return _python(windowless), f'"{_stack_script()}" --data "{data}" start'


def open_command(data: Path) -> tuple[str, str]:
    """The program and arguments the desktop shortcut runs: this install's pythonw.exe running `resumebot open`."""
    return _python(True), f'-m app.main_cli open --data "{data}"'


STARTUP_FILE = "resumebot.cmd"
SHORTCUT_FILE = "resumebot.lnk"
MAC_SHORTCUT_FILE = "resumebot.command"  #: macOS: a shell script Finder runs on a double-click
NEW_SHORTCUT_FILE = "resumebot-new.lnk"  #: the shortcut while it is being written; it must end in .lnk to be saved
OPEN_LOG = "open.log"


def startup_folder() -> Path:
    """The user's Startup folder, resolved when called (tests point it at a temporary folder). The plain path under
    %APPDATA%. Only the cleanup of an old `autostart` file looks here."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _names_data(text: str, data: Path) -> bool:
    """True when `text` (a task's arguments, the Startup file or a shortcut's arguments) holds `--data "<folder>"` and
    the folder is this data folder: compared as real paths, so a different spelling, case or link of it still counts."""
    def same(path: str) -> bool:
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(data))

    return any(same(found) for found in re.findall(r'--data\s+"([^"]+)"', text))


#: Put first in every PowerShell script: Windows PowerShell 5 writes its output in the console's OEM code page, which
#: the decode above would otherwise read as text it is not.
_UTF8_OUTPUT = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _UTF8_OUTPUT + script])


def _first_line(done: subprocess.CompletedProcess[str]) -> str:
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[0] if lines else str(done.returncode)


def _task_arguments() -> str | None:
    """The arguments of the old logon task `resumebot`, or None when there is no such task (or PowerShell cannot
    say)."""
    script = (f"$t = Get-ScheduledTask -TaskName {_quoted(TASK_NAME)} -ErrorAction SilentlyContinue; "
              "if ($t) { 'TASK ' + (($t.Actions | ForEach-Object { $_.Arguments }) -join ' ') }")
    try:
        done = PLATFORM.powershell(script)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in done.stdout.splitlines():
        if line.startswith("TASK"):
            return line[4:].strip()
    return None


def _unregister_task() -> bool:
    """Remove the old logon task; True only when the task is gone afterwards (it is asked again, not assumed)."""
    try:
        PLATFORM.powershell(f"Unregister-ScheduledTask -TaskName {_quoted(TASK_NAME)} -Confirm:$false "
                            "-ErrorAction SilentlyContinue")
    except (OSError, subprocess.SubprocessError):
        return False
    return _task_arguments() is None


def _file_names_data(path: Path, data: Path) -> bool:
    try:
        return path.is_file() and _names_data(path.read_text(encoding="utf-8", errors="replace"), data)
    except OSError:
        return False


def remove_old_autostart(install: Install) -> None:
    """Remove the logon task and the Startup folder file that the removed `resumebot autostart` left, only when they
    belong to this install's data folder (another install's are left as they are). Says one line when it removed
    something and nothing otherwise. Windows only."""
    if sys.platform != "win32":
        return
    removed = []
    existing = _task_arguments()
    if existing is not None and _names_data(existing, install.data):
        if _unregister_task():
            removed.append(f"logon task {TASK_NAME}")
        else:
            print(f"could not remove the old logon task {TASK_NAME}: remove it in Task Scheduler", file=sys.stderr)
    startup_file = startup_folder() / STARTUP_FILE
    if _file_names_data(startup_file, install.data):
        try:
            startup_file.unlink()
            removed.append(f"Startup folder file {startup_file}")
        except OSError as error:
            print(f"could not remove {startup_file}: {error}", file=sys.stderr)
    if removed:
        print(f"removed the old start at sign-in ({' and '.join(removed)}): the desktop shortcut starts the app now")


def _register_task(program: str, arguments: str, name: str = ONE_OFF_TASK) -> str | None:
    """Register a task with no trigger and start it now; None when it worked, else why Windows refused."""
    script = (
        f"$action = New-ScheduledTaskAction -Execute {_quoted(program)} -Argument {_quoted(arguments)}; "
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName {_quoted(name)} -Action $action -Settings $settings -Force | Out-Null")
    try:
        done = PLATFORM.powershell(script)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run PowerShell: {error}"
    if done.returncode != 0:
        return _first_line(done)
    # Start it once now, under Task Scheduler: an app started from inside Claude dies when Claude updates or closes.
    return _start_task(name)


def _start_task(name: str) -> str | None:
    """Start a registered task now; None when it started, else why not."""
    try:
        started = PLATFORM.powershell(f"Start-ScheduledTask -TaskName {_quoted(name)}")
    except (OSError, subprocess.SubprocessError) as error:
        return f"the task is registered but could not be started: {error}"
    if started.returncode != 0:
        return f"the task is registered but Windows would not start it: {_first_line(started)}"
    return None


def mac_desktop() -> Path:
    """The user's Desktop folder on macOS, resolved when called (tests point it at a temporary folder)."""
    return Path.home() / "Desktop"


def mac_shortcut_text(data: Path) -> str:
    """What the macOS desktop file holds: a shell script that runs `resumebot open` with this install's Python on this
    data folder, from the backend folder (the same command the Windows shortcut runs)."""
    return ("#!/bin/sh\n"
            "# Starts resumebot and opens its page. Made by `resumebot shortcut`: run it again to replace this.\n"
            f"cd {shlex.quote(str(_backend_folder()))} || exit 1\n"
            f"exec {shlex.quote(_python(False))} -m app.main_cli open --data {shlex.quote(str(data))}\n")


def _mac_shortcut_words(path: Path) -> list[str] | None:
    """The words of the `exec` line of the macOS desktop file; None when there is no file or no such line."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for text in lines:
        if text.startswith("exec "):
            try:
                return shlex.split(text)[1:]
            except ValueError:
                return None
    return None


def _mac_shortcut(install: Install) -> int:
    """Write the macOS desktop file. The new file is written beside the old one and only then replaces it, so a failed
    write leaves the old one as it was."""
    target = mac_desktop() / MAC_SHORTCUT_FILE
    new = target.with_name(MAC_SHORTCUT_FILE + ".new")
    try:
        new.write_text(mac_shortcut_text(install.data), encoding="utf-8")
        new.chmod(0o755)
        os.replace(new, target)
    except OSError as error:
        with contextlib.suppress(OSError):
            new.unlink()
        print(f"could not make the shortcut: {_describe(error)}", file=sys.stderr)
        return 1
    print(f"shortcut {MAC_SHORTCUT_FILE} is on your desktop ({target}): double-click it to start the app and open "
          "its page")
    return 0


def shortcut(argv: list[str]) -> int:
    """put a resumebot shortcut on your desktop: double-click it to start the app and open its page (Windows, macOS)"""
    parser = argparse.ArgumentParser(prog="resumebot shortcut", description=shortcut.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    args = parser.parse_args(argv)
    if sys.platform not in ("win32", "darwin"):
        print(f"the desktop shortcut is not built for {sys.platform}: only Windows and macOS have it (nothing was "
              "changed)")
        return 0
    install = Install(data_folder(args.data))
    if not install.is_set_up:
        print(f"no settings at {install.env_file}: run `resumebot init` first", file=sys.stderr)
        return 1
    if sys.platform == "darwin":
        return _mac_shortcut(install)
    program, arguments = open_command(install.data)
    # The Desktop is asked for as the known folder, so a OneDrive-redirected one is found; saving over the path
    # replaces this shortcut's earlier file.
    # The new file is saved beside the old one and checked, and only then replaces it: a failed save leaves the old
    # shortcut as it was, and a save that wrote nothing is an error, not a success.
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        f"$path = Join-Path $desktop {_quoted(SHORTCUT_FILE)}; "
        f"$new = Join-Path $desktop {_quoted(NEW_SHORTCUT_FILE)}; "
        "Remove-Item -LiteralPath $new -ErrorAction SilentlyContinue; "
        "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($new); "
        f"$link.TargetPath = {_quoted(program)}; $link.Arguments = {_quoted(arguments)}; "
        f"$link.WorkingDirectory = {_quoted(str(_backend_folder()))}; "
        f"$link.IconLocation = {_quoted(str(shortcut_icon()) + ',0')}; "
        "$link.Description = 'Start resumebot and open its page'; "
        "$link.Save(); "
        "if (-not (Test-Path -LiteralPath $new)) { throw 'the shortcut file was not saved' }; "
        "Move-Item -LiteralPath $new -Destination $path -Force; "
        "if (-not (Test-Path -LiteralPath $path)) { throw 'the shortcut file is not on the desktop' }; "
        "'SHORTCUT ' + $path")
    try:
        done = PLATFORM.powershell(script)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"could not make the shortcut: could not run PowerShell: {error}", file=sys.stderr)
        return 1
    if done.returncode != 0:
        print(f"could not make the shortcut: {_first_line(done)}", file=sys.stderr)
        return 1
    where = next((line[9:].strip() for line in done.stdout.splitlines() if line.startswith("SHORTCUT ")), None)
    remove_old_autostart(install)
    print(f"shortcut {SHORTCUT_FILE} is on your desktop" + (f" ({where})" if where else "")
          + ": double-click it to start the app and open its page")
    return 0


# --- start, open and smoke ---------------------------------------------------------------------------------------


def _stack_words(data: Path, *words: str) -> list[str]:
    return [sys.executable, str(_stack_script()), "--data", str(data), *words]


def _bring_up(install: Install) -> int:
    """Start the app outside this session and wait until it answers (the work of `start`; `open` shares it)."""
    if app_health(install) is None:
        print(f"already running at {install.app_url}")
        return 0
    if sys.platform == "win32":
        program, arguments = start_command(install.data)
        refused = _register_task(program, arguments)
        if refused is not None:  # Task Scheduler refused: a detached process of our own
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                     | _no_window())
            try:
                _launch([program, str(_stack_script()), "--data", str(install.data), "start"], creationflags=flags)
            except OSError as error:
                print(f"could not start the app: Task Scheduler refused ({refused}) and a detached process failed "
                      f"({error})", file=sys.stderr)
                return 1
    else:
        try:
            _launch(_stack_words(install.data, "start"), start_new_session=True)
        except OSError as error:
            print(f"could not start the app: {error}", file=sys.stderr)
            return 1
    end = _clock() + START_WAIT_SECONDS
    while _clock() < end:
        _sleep(START_POLL_SECONDS)
        if app_health(install) is None:
            print(f"running at {install.app_url}")
            return 0
    print(f"the app did not answer at {install.app_url} within {START_WAIT_SECONDS} seconds: see "
          f"{install.data / 'logs' / 'supervisor.log'}", file=sys.stderr)
    return 1


def start(argv: list[str]) -> int:
    """start the app outside this session, so it keeps running, and wait until it answers"""
    parser = argparse.ArgumentParser(prog="resumebot start", description=start.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    install = Install(data_folder(parser.parse_args(argv).data))
    if not install.is_set_up:
        print(f"no settings at {install.env_file}: run `resumebot init` first", file=sys.stderr)
        return 1
    return _bring_up(install)


def _log_open(install: Install, text: str) -> None:
    """Keep the reason `open` failed where the user can find it (it runs with no window, so nothing else shows it)."""
    try:
        folder = install.data / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / OPEN_LOG).open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {text.strip()}\n")
    except OSError:
        pass


def open_page(argv: list[str]) -> int:
    """start the app if it is not running, then open its page in your browser (what the desktop shortcut runs)"""
    parser = argparse.ArgumentParser(prog="resumebot open", description=open_page.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    install = Install(data_folder(parser.parse_args(argv).data))
    if not install.is_set_up:
        message = f"no settings at {install.env_file}: run `resumebot init` first"
        print(message, file=sys.stderr)
        _log_open(install, message)
        return 1
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            code = _bring_up(install)
    except Exception as error:  # a window-less run has no one to show a traceback to: it goes in the log
        _log_open(install, f"open failed: {error!r}")
        print(f"open failed: {error!r}", file=sys.stderr)
        return 1
    if code != 0:
        _log_open(install, said.getvalue() or f"the app did not start (exit {code})")
        print(said.getvalue().strip(), file=sys.stderr)
        return code
    if not webbrowser.open(install.app_url):
        _log_open(install, f"the app is running but no browser opened: open {install.app_url} by hand")
    print(f"opened {install.app_url}")
    return 0


def smoke(argv: list[str]) -> int:
    """run the read-only checks of the running app (stack.py smoke); its exit code is this one's"""
    parser = argparse.ArgumentParser(prog="resumebot smoke", description=smoke.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    parser.add_argument("--lanes", action="store_true", help="check that the five lanes answer too")
    args = parser.parse_args(argv)
    return _call(_stack_words(data_folder(args.data), "smoke", *(["--lanes"] if args.lanes else [])))


# --- doctor ----------------------------------------------------------------------------------------------------------


def doctor(argv: list[str]) -> int:
    """say what is missing, one line each: Claude Code and its sign-in, Node, the settings, the app and its doors"""
    parser = argparse.ArgumentParser(prog="resumebot doctor", description=doctor.__doc__)
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    install = Install(data_folder(parser.parse_args(argv).data))
    missing = 0

    def line(problem: str | None, ok: str, what: str | None = None) -> None:
        nonlocal missing
        if problem is None:
            print(f"ok       {ok}")
        else:
            missing += 1
            print(f"MISSING  {what + ': ' if what else ''}{problem}")

    claude = PLATFORM.find_claude()
    found_but_broken = _claude_candidates() if claude is None else []
    if found_but_broken:
        missing_claude = (f"found at {found_but_broken[0]}, but it would not run (`claude --version` failed): "
                          f"reinstall it from {CLAUDE_SETUP_PAGE}")
    else:
        missing_claude = CLAUDE_NOT_FOUND_WHERE + _searched_folders()
    line(None if claude else missing_claude, f"Claude Code is installed ({claude})", "Claude Code")
    if claude:
        print(f"config   Claude Code's configuration is read from {claude_config_file()}")
        try:
            signed_in, could_not_run = _run([claude, "auth", "status"]).returncode == 0, None
        except (OSError, subprocess.SubprocessError) as error:
            signed_in, could_not_run = False, _describe(error)
        line(None if signed_in else (f"could not run `claude auth status`: {could_not_run}" if could_not_run
                                     else "not signed in: run `claude`, then /login"),
             "Claude Code is signed in", "Claude Code sign-in")
    stored = token_is_stored(install)
    has_file = install.agent_token_path.is_file()
    line(None if stored else ("the stored token is not a usable one (blank, cut short or glued): run `resumebot "
                              "signin`" if has_file else "no token stored: run `resumebot signin`"),
         "a token is stored (valid for about a year; if the agents stop, run `resumebot signin --again`)",
         "agents sign in")
    age = token_age_days(install) if stored else None
    if age is not None and age >= TOKEN_OLD_DAYS:
        print(f"warn     the token is {age} days old and lasts about a year: run `resumebot signin --again` soon")
    line(_node_problem(), "Node is there", "Node")
    line(sqlite_problem(), f"SQLite {sqlite_version()} is clear of the WAL-reset bug", "SQLite")
    if not install.is_set_up:
        line(f"no settings at {install.env_file}: run `resumebot init`", "", "settings")
    elif install.env_problem:
        line(install.env_problem, "", "settings")
    else:
        pieces = install.missing_pieces()
        for piece in pieces:
            line(f"{piece} is missing: run `resumebot init --owner <name>` again, it finishes what is left", "",
                 "install")
        if not pieces:
            line(None, f"settings are at {install.env_file}, and the install is complete")
    down = app_health(install)
    line(down, f"the app answers at {install.app_url}", "app")
    for door in DOORS:
        if down is not None:
            print(f"skipped  {door.name}: not checked while the app is down")
            continue
        problem = check_door(install, door)
        wanted = install.door_base(door) + door.path
        if problem is None and claude:
            try:
                held = _registration(claude, door)
            except (OSError, subprocess.SubprocessError) as error:
                held, problem = None, f"could not ask Claude Code what it holds: {_describe(error)}"
            else:
                problem = _registration_problem(held, wanted, install.token(door), door.name)
                if problem == "your Claude Code does not have it":
                    problem = "the app answers it, but your Claude Code does not have it"
                if problem is not None:
                    problem += ": run `resumebot connect`"
        if problem is None and not claude:
            print(f"ok       {door.name} answers (registration not checked: Claude Code was not found)")
            continue
        line(problem, f"{door.name} answers and is registered", door.name)
    if sys.platform in ("win32", "darwin"):
        file = SHORTCUT_FILE if sys.platform == "win32" else MAC_SHORTCUT_FILE
        line(PLATFORM.shortcut_problem(install), f"the desktop shortcut {file} starts this install",
             "desktop shortcut")
    print(logs_line(install.data / "logs"))
    return 1 if missing else 0


def _shortcut_problem(install: Install) -> str | None:
    """None when the desktop shortcut exists, its program exists and its arguments name this data folder; else one
    sentence. On Windows it is asked of PowerShell, which finds a OneDrive-redirected Desktop; on macOS the desktop
    file's own `exec` line is read."""
    if sys.platform == "darwin":
        words = _mac_shortcut_words(mac_desktop() / MAC_SHORTCUT_FILE)
        if words is None:
            return f"there is none on the desktop: run `resumebot shortcut --data \"{install.data}\"`"
        if not words or not Path(words[0]).is_file():
            return f"it starts {words[0] if words else 'nothing'}, which is not there: run `resumebot shortcut` again"
        named = words[words.index("--data") + 1] if "--data" in words[:-1] else ""
        if not named or os.path.realpath(named) != os.path.realpath(install.data):
            return f"it opens another data folder: run `resumebot shortcut --data \"{install.data}\"`"
        return None
    script = ("$p = Join-Path ([Environment]::GetFolderPath('Desktop')) " + _quoted(SHORTCUT_FILE) + "; "
              "if (Test-Path -LiteralPath $p) { $l = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
              "'LINK ' + $l.TargetPath + [char]9 + $l.Arguments } else { 'NOLINK' }")
    try:
        done = PLATFORM.powershell(script)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not look: {_describe(error)}"
    link = next((text[5:] for text in done.stdout.splitlines() if text.startswith("LINK ")), None)
    if link is None:
        return f"there is none on the desktop: run `resumebot shortcut --data \"{install.data}\"`"
    target, _, arguments = link.partition("\t")
    if not Path(target.strip()).is_file():
        return f"it starts {target.strip()}, which is not there: run `resumebot shortcut` again"
    if not _names_data(arguments, install.data):
        return f"it opens another data folder: run `resumebot shortcut --data \"{install.data}\"`"
    return None


def logs_line(folder: Path) -> str:
    """One plain line: the logs folder and the newest page, engine and supervisor log names (never their content)."""
    names = []
    for stem in ("page", "engine", "supervisor", "open", "start"):
        try:
            pattern = "start.log*" if stem == "start" else f"{stem}*.log*"  # start-app.log is another log
            found = [f for f in folder.glob(pattern) if f.is_file()]
            newest = max(found, key=lambda f: f.stat().st_mtime).name if found else "none yet"
        except OSError:
            newest = "none yet"
        names.append(f"{stem}: {newest}")
    return f"logs     {folder} ({'; '.join(names)})"


def sqlite_version() -> str:
    """The SQLite the running Python carries: the seam a test sets."""
    import sqlite3

    return sqlite3.sqlite_version


def sqlite_is_safe(version: tuple[int, ...]) -> bool:
    """Whether a SQLite version is clear of the WAL-reset bug: fixed in 3.51.3 and backported to 3.50.7 and 3.44.6."""
    return version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)


def sqlite_problem() -> str | None:
    said = sqlite_version()
    try:
        version = tuple(int(part) for part in said.split(".")[:3])
    except ValueError:
        return f"{said} is not a version this check can read: run  uv python install 3.12 --upgrade"
    if sqlite_is_safe(version):
        return None
    return (f"{said} has the WAL-reset bug (3.51.3 or newer is needed): run  uv python install 3.12 --upgrade, delete "
            "the app's .venv, run  stack.py setup  with  $(uv python find --managed-python 3.12)")


def _node_problem() -> str | None:
    node = shutil.which("node")
    if node is None:
        return f"not on PATH: install Node {NODE_MAJOR} or newer"
    try:
        said = _run([node, "--version"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "found, but would not say its version"
    found = re.match(r"v(\d+)", said)
    if not found or int(found.group(1)) < NODE_MAJOR:
        return f"is {said or 'unknown'}: Node {NODE_MAJOR} or newer is needed"
    return None


# --- status ----------------------------------------------------------------------------------------------------------

#: The mission that searches the boards (`app.jobsearch.missions.record.SCOUT`).
SEARCH_MISSION = "scout_boards"


@dataclass(frozen=True)
class Step:
    """What Claude does for a step of onboarding: what it is, how it is done through the owner door (the tools of
    `app/jobsearch/owner_actions/onboarding.py`, and `mission.run` for the first search) and the actions that names, so
    a test can hold the words to the door. `yes` and `no` are the words for done and not done; `offered` is whether the
    line after `next:` may name the step when it is the first one not done (a step the owner may have nothing for is
    asked once by the onboarding skill and never blocks the next one)."""

    todo: str
    how: str
    names: tuple[str, ...]
    yes: str
    no: str
    offered: bool = True

    def said(self) -> str:
        return f"{self.todo} ({self.how})"


#: The steps `status` reports, by name and in order: the line after `next:` names the first one not done.
TOOLS = {
    "resume": Step("import the master resume",
                   "act.do resume.import with the file's whole path; the owner confirms the reading on My Resume",
                   ("resume.import",), "imported", "not imported yet"),
    "tracker": Step("bring in the tracker (optional)",
                    "act.do tracker.import with the .xlsx path, a dry run first", ("tracker.import",),
                    "imported", "not imported (optional: only if they have one)", offered=False),
    "employer list": Step("bring in the employer list (optional)",
                          "act.do employers.import with the .csv path, a dry run first", ("employers.import",),
                          "imported", "not imported (optional: only if they have one)", offered=False),
    "targets": Step("set the target roles and search settings",
                    "act.read profile.read, then act.do profile.save with what the owner says",
                    ("profile.read", "profile.save"), "set", "not set yet"),
    "first search": Step("run the first search", f"act.do mission.run with mission {SEARCH_MISSION}",
                         ("mission.run",), "run", "not run yet"),
    "mail": Step("connect mail (optional)",
                 "act.read mail.state; the owner types the password on the page, never into the chat",
                 ("mail.state",), "connected", "not connected"),
}
ALL_DONE = "onboarding is done"


def step_report(facts: dict[str, bool | None]) -> tuple[list[str], str]:
    """The plain line of each step in `TOOLS` and the step to do next, from whether each is done (None: the source did
    not say). The one place the lines are made: `status` calls it with what the app's pages and database answer, and
    the owner door's `onboarding.state` with what the database holds."""
    lines: list[str] = []
    next_step = ALL_DONE
    for name, step in TOOLS.items():
        done = facts.get(name)
        lines.append(f"{name}: {'unknown (the app did not say)' if done is None else step.yes if done else step.no}")
        if done is False and step.offered and next_step == ALL_DONE:
            next_step = step.said()
    return lines, next_step


def imported_counts(install: Install) -> dict[str, int] | None:
    """How many roles came from a tracker and how many rows the employer list holds, read from the install's SQLite
    file without changing it. None when the database is not a file here or would not answer."""
    import sqlite3

    path = database_file(install.env.get(DATABASE_SETTING, ""))
    if path is None or not path.is_file():
        return None
    owner = install.env.get("JOBSEARCH_OWNER", "")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=HTTP_SECONDS)
        try:
            roles = connection.execute("SELECT count(*) FROM js_opportunities WHERE owner = ? AND origin = 'tracker'",
                                       (owner,)).fetchone()[0]
            rows = connection.execute("SELECT count(*) FROM js_employer_list WHERE owner = ?", (owner,)).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return {"tracker": roles, "employer list": rows}


def status(argv: list[str]) -> int:
    """say where onboarding stands in a few plain lines (--for-claude: the form Claude reads at session start)"""
    parser = argparse.ArgumentParser(prog="resumebot status", description=status.__doc__)
    parser.add_argument("--for-claude", action="store_true",
                        help="changes nothing: prints the plain lines Claude reads at the start of a session")
    parser.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    install = Install(data_folder(parser.parse_args(argv).data))
    from app.settings import VERSION  # not at the top: the settings read the environment

    lines = [f"resumebot {VERSION}"]
    if not install.is_set_up:
        lines += [f"install: not set up (no settings at {install.env_file})",
                  "next: run the resumebot-setup:install-resumebot skill (prerequisites, the app, "
                  "`resumebot init`, start it, `resumebot connect`)"]
        print("\n".join(lines))
        return 0
    pieces = install.missing_pieces()
    if pieces:
        lines += [f"install: started at {install.data} but not finished: missing " + "; ".join(pieces),
                  "next: run `resumebot init` again with the same owner (it finishes what is left and changes nothing "
                  "else)"]
        print("\n".join(lines))
        return 0
    lines.append(f"install: set up at {install.data}")
    if app_health(install) is not None:
        lines += ["app: not running",
                  f"next: start the app ({start_hint(install)}, or double-click resumebot on the desktop)"]
        print("\n".join(lines))
        return 0
    lines.append(f"app: running at {install.app_url}")
    with _client() as client:
        documents = _json(client, install, "/api/v1/documents")
        profile = _json(client, install, "/api/v1/profile")
        missions = _json(client, install, "/api/v1/missions")
        mail = _json(client, install, "/api/v1/mail-login")
    brought = imported_counts(install)
    facts: dict[str, bool | None] = {
        "resume": None if documents is None else documents.get("versions", {}).get("resume", 0) > 0,
        "tracker": None if brought is None else brought["tracker"] > 0,
        "employer list": None if brought is None else brought["employer list"] > 0,
        "targets": None if profile is None else bool(profile.get("is_set")),
        "first search": None if missions is None else any(
            m.get("key") == SEARCH_MISSION and m.get("last_run") for m in missions.get("missions", [])),
        "mail": None if mail is None else bool(mail.get("connected")),
    }
    step_lines, next_step = step_report(facts)
    lines += step_lines
    lines.append(f"next: {next_step}")
    print("\n".join(lines))
    return 0
