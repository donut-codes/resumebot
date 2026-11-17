#!/usr/bin/env python3
r"""resumebot on this computer, as one stack: the local Postgres (none when JOBSEARCH_DATABASE_URL is a SQLite URL),
the page and the engine (the page's port plus 100), two programs on one database, on 127.0.0.1,
and the five lanes
(app/scout/lanes-local.mjs). Python's
standard library only, and it needs no container, no service and no administrator rights.

<python> below is the app environment's own Python once `setup` has made it (app/backend/.venv/Scripts/python.exe), and
before that, for the first `setup`, uv's own Python 3.12: the path `uv python find --managed-python 3.12` prints. Never
`py`, the Windows launcher: it starts the system Python, whose SQLite can be one with the WAL-reset bug, and `setup`
makes the app's environment from the Python that runs it (the safe versions: `sqlite_is_safe` in
app/backend/app/setup_cli.py).

    <python> app/scripts/local/stack.py setup [--dev]   once, and after a pull: the data folder and its secrets, the
                                                         app's Python environment (from the hash-locked
                                                         requirements.lock; --dev adds the test tools), the
                                                         `resumebot` command in it, the page (npm) and the junction
                                                         its build needs
    <python> app/scripts/local/stack.py start [--hold]  Postgres (if used), the app, the lanes, in the background;
                                                         returns when /health answers. --hold: the schedule and the
                                                         lanes stay off (for a rehearsal on a copy of the data: nothing
                                                         runs by itself)
    <python> app/scripts/local/stack.py stop            the lanes, the app, then Postgres (if used)
    <python> app/scripts/local/stack.py status          what is running, and what /health says
    <python> app/scripts/local/stack.py run [--hold]    like start, in this window, until Ctrl+C
    <python> app/scripts/local/stack.py smoke [--lanes] the checks of a running app (read-only); exit status 0 when
                                                         all pass

`start` runs the page (app.jobsearch.page, the owner's address) and the engine (app.jobsearch.engine, the page's port
plus 100) as two programs on the one database. The lanes start once the engine's /health answers and are sent to the
engine's address (RESUMEBOT_APP_URL for them; RESUMEBOT_PAGE_URL is the page, which the browser window's first tab
shows); `stop` ends the lanes, then the engine, then the page. Their logs are page.log and engine.log; their pid files
are page.pid and engine.pid. `--hold` turns off the scheduled searches and the lanes; the engine still answers the
page's asks.

The data folder is RESUMEBOT_DATA_DIR (default: the user's own folder, the one `resumebot init` makes). It holds:

    .env          the settings of the app (JOBSEARCH_*: the database address, the owner, the time zone, the secret
                  files) and of the lanes (SCOUT_*, RESUMEBOT_*: lanes-local.mjs); RESUMEBOT_APP_URL says where the
                  app listens (default http://127.0.0.1:8430; the host must be this machine). Values are read raw:
                  write a Windows path unquoted or with slashes (C:/data/x), and keep a "$" out of a value that
                  is not single-quoted. RESUMEBOT_PACKET_DIR names a folder where each finished packet's resume and
                  cover letter (PDF and Word) are saved, one folder per role; init names Documents/resumebot by default
                  on a new install; unset (an older install), nothing is saved.
    secrets\      agent-token, wake-secret, owner-token (made by setup; the app and the lanes read the same files;
                  the owner token is the action door's, `/mcp-owner/`, which only the owner's own Claude Code
                  session holds: `stack.py owner-header` hands it to the project's .mcp.json),
                  claude-oauth-token (the lanes' sign-in, from `resumebot signin`), browser-token (the browser door's)
    pg\           Postgres (app/scripts/local/pg.py)
    logs\         start.log (every reason a command of this launcher stopped, with the time: a start that fails under
                  pythonw prints nothing anywhere else), page.log, engine.log, supervisor.log, lanes.log,
                  lanes-stdout.log (what the lanes launcher
                  prints, crashes included) and the lanes' runs*.log: each over 10 MB is set aside (3 kept), at a
                  start and, about once a minute, while it runs; db-snapshot-*.txt, what the database was doing
                  when the supervisor restarted a program that stopped answering /health (10 kept)
    run\          pid files, the lanes' MCP configs, the stop request
    work\, claude\   the lanes' own working and Claude folders

The full backend suite, on this computer against the local Postgres (a separate job from the install; it makes its own
databases beside `resumebot` and drops them with the run, so the app's database is not touched):

    <python> app/scripts/local/stack.py setup --dev
    cd app/backend
    set DATABASE_URL=<JOBSEARCH_DATABASE_URL of the data folder's .env, with the database name resumebot_test>
    .venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -n 4 --durations=15
    (the process engine's own tests: cd ..\packages\process-engine, then the same python -m pytest -q)

No installer and no start at sign-in: the instance is started by hand or by the desktop shortcut. Windows
is what it is made for; the rest of it runs elsewhere too, for the tests.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import runpy
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
APP = HERE.parents[1]
BACKEND = APP / "backend"
FRONTEND = APP / "frontend"
SCOUT = APP / "scout"
ENGINE_SRC = APP / "packages" / "process-engine" / "src"
PG_SCRIPT = HERE / "pg.py"
#: The built page the app serves (`npm run build:package`), and where the launcher's own build is made first.
PAGE_DIR = BACKEND / "app" / "page"
PAGE_INDEX = PAGE_DIR / "index.html"
PAGE_BUILD = FRONTEND / "node_modules" / ".page-build"
WINDOWS = sys.platform == "win32"

DATA_VARIABLE = "RESUMEBOT_DATA_DIR"
#: The secret files `setup` makes in `secrets/` (`resumebot init` makes these and one more; both keep what is there).
SECRET_FILES = ("agent-token", "wake-secret", "owner-token", "browser-token")
DEFAULT_APP_URL = "http://127.0.0.1:8430"
DEFAULT_SCOUT_PORT = 8651
#: The port the stack's own Postgres takes unless RESUMEBOT_PG_PORT says otherwise (pg.py `DEFAULT_PORT`; a test holds
#: the two equal).
DEFAULT_PG_PORT = 5433
LANE_COUNT = 5
#: The engine's port is the page's plus this (app/backend/app/notice.py `OFFSET`; a test holds the two equal).
ENGINE_OFFSET = 100
#: How long `smoke` waits for the page and for the engine to answer /health: seconds. The engine's first start on a
#: large database takes minutes (the migration check, the fix-ups, the lookups) and answers nothing until it is done.
SMOKE_PAGE_WAIT = 60
SMOKE_ENGINE_WAIT = 600
#: How long `smoke` waits for the live stream's next event (`hello`, then `resync`): seconds.
SMOKE_LIVE_WAIT = 10
LOG_LIMIT = 10 * 1024 * 1024
LOG_KEEP = 3
#: While it runs the supervisor looks at the logs this often (`roll`), and a process whose output is one of them looks
#: this often for a log that was set aside under it (`follow`): seconds.
ROLL_EVERY, FOLLOW_EVERY = 60.0, 5.0

#: Windows flags for a child: its own process group (so Ctrl+C aimed at the console does not reach it, and Ctrl+Break
#: can be sent to it alone), and, for the supervisor, a console of its own that no window shows (so it outlives the
#: terminal that started it and can still send its children Ctrl+Break).
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def no_window() -> int:
    """The creation flag that keeps a console program from opening a window of its own, which it does when started
    from a process with no console (the shortcut's pythonw.exe, and the task it starts); 0 away from Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if WINDOWS else 0


def quiet_run(command: list[str], *, timeout: float = 60, env: dict[str, str] | None = None,
              cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    """The one way this launcher runs a short program it reads the answer of: no window (a console program started from
    pythonw.exe opens one), no input, a time limit, and its output read as UTF-8 with what is not UTF-8 replaced, so no
    code page of this PC can raise here. Raises OSError, `subprocess.SubprocessError` (a timeout) and, with `check`,
    CalledProcessError: callers name what they do about each. The programs that must show their own output (`run_step`)
    and the ones that stay running (the supervisor, the children) are not run through it; a test lists them."""
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=no_window(), env=env, cwd=cwd, check=check)


class Stop(Exception):
    """One plain sentence to print before stopping; `main` exits with `code`."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def say(text: str) -> None:
    print(f"==> {text}", flush=True)


def wall_clock() -> dt.datetime:
    """The time now with its offset from UTC (a test sets this)."""
    return dt.datetime.now().astimezone()


def supervisor_say(text: str) -> None:
    """One line of the supervisor's own log: the time with its offset, the program and its pid, then the words (the
    format of the app's own lines, app/log_setup.py). `say` stays plain, for setup and status at a console."""
    print(f"{wall_clock().isoformat(timespec='seconds')} supervisor {os.getpid()} {text}", flush=True)


# --- the data folder, its .env, the settings -----------------------------------------------------------------------


def default_data_folder() -> Path:
    """Where an install keeps its data when nothing names a folder: the user's own, per platform. The same folder
    `resumebot init` makes (app/backend/app/setup_cli.py `default_data_folder`; a test holds the two together)."""
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "resumebot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "resumebot"


def data_folder() -> Path:
    named = os.environ.get(DATA_VARIABLE, "").strip()
    return Path(named).expanduser() if named else default_data_folder()


def parse_env(text: str) -> dict[str, str]:
    """The KEY=value lines of a .env file: blank lines and # comments skipped, `export ` and one pair of quotes allowed.
    The same reading as lanes-local.mjs; nothing is interpolated or unescaped."""
    out: dict[str, str] = {}
    for raw in text.lstrip("\ufeff").splitlines():
        found = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
        if not found:
            continue
        value = found.group(2)
        quoted = re.match(r"^([\"'])(.*)\1$", value)
        out[found.group(1)] = quoted.group(2) if quoted else re.sub(r"\s+#.*$", "", value)
    return out


def read_env(path: Path) -> dict[str, str]:
    """The .env file, read as UTF-8 (a byte order mark allowed, as `resumebot init` reads it). A file saved in another
    encoding (Windows PowerShell's `>` writes UTF-16) is one plain sentence, not a traceback."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        text = "\x00"
    if "\x00" in text:
        raise Stop(f"{path} is not saved as UTF-8: open it and save it again as UTF-8 (Windows PowerShell's > writes "
                   "UTF-16)", 2)
    return parse_env(text)


class Config:
    """Where everything is, and the few settings the launcher itself reads: the data folder's .env, under the
    environment."""

    def __init__(self, data: Path | None = None, environ: dict[str, str] | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.data = data or data_folder()
        self.env_file = self.data / ".env"
        self.file = read_env(self.env_file)
        self.secrets = self.data / "secrets"
        self.logs = self.data / "logs"
        self.run = self.data / "run"

    def get(self, name: str, default: str = "") -> str:
        return self.environ.get(name) or self.file.get(name) or default

    def has(self, name: str) -> bool:
        return bool(self.environ.get(name) or self.file.get(name))

    @property
    def uses_postgres(self) -> bool:
        """True only when JOBSEARCH_DATABASE_URL is a Postgres URL (an existing Postgres instance). A
        SQLite URL, or none yet (a new install before `resumebot init`), starts, stops and asks no Postgres."""
        return self.get("JOBSEARCH_DATABASE_URL").startswith("postgres")

    @property
    def db_file(self) -> Path | None:
        """The SQLite database file JOBSEARCH_DATABASE_URL names (sqlite+aiosqlite:///<path>), None for Postgres."""
        if not self.get("JOBSEARCH_DATABASE_URL").startswith("sqlite"):
            return None
        path = self.get("JOBSEARCH_DATABASE_URL").split("://", 1)[-1].split("?", 1)[0]
        return Path(path.removeprefix("/"))  # sqlite+aiosqlite:///C:/x/db -> C:/x/db; ////abs/db -> /abs/db

    @property
    def app_url(self) -> str:
        """The app's address, which must be this machine's: it is where the lanes send their token."""
        value = self.get("RESUMEBOT_APP_URL", DEFAULT_APP_URL)
        url = urllib.parse.urlsplit(value)
        if url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise Stop(f"RESUMEBOT_APP_URL must be an address on this machine (127.0.0.1 or localhost), not {value}", 2)
        return f"{url.scheme}://{url.netloc}"

    @property
    def port(self) -> int:
        return urllib.parse.urlsplit(self.app_url).port or 80

    @property
    def engine_port(self) -> int:
        """The engine's port: the page's plus ENGINE_OFFSET."""
        return self.port + ENGINE_OFFSET

    @property
    def engine_url(self) -> str:
        return f"http://127.0.0.1:{self.engine_port}"

    @property
    def scout_port(self) -> int:
        raw = self.get("SCOUT_PORT")
        return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 - LANE_COUNT else DEFAULT_SCOUT_PORT

    @property
    def venv(self) -> Path:
        return Path(self.get("RESUMEBOT_VENV") or BACKEND / ".venv")

    @property
    def python(self) -> Path:
        return self.venv / ("Scripts/python.exe" if WINDOWS else "bin/python")

    def app_environment(self, hold: bool) -> dict[str, str]:
        """What the app is started with: this environment, the data folder, UTF-8 and unbuffered output; the lanes' wake
        address and their count unless the settings name them; the schedule off when held. Anything of Python's own that
        the shell started this with is dropped."""
        env = {k: v for k, v in self.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env.update({DATA_VARIABLE: str(self.data), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
        defaults = {"JOBSEARCH_WAKE_URL": f"http://127.0.0.1:{self.scout_port}/wake", "JOBSEARCH_ANALYSTS": "2"}
        for name, value in defaults.items():
            if not self.has(name):
                env[name] = value
        if hold:
            env["JOBSEARCH_RUN_SCHEDULE"] = "false"
        return env

    def lanes_environment(self) -> dict[str, str]:
        """What the lanes are started with. They are the engine's: `RESUMEBOT_APP_URL` is the engine's address
        (their API and the doors `/mcp` and `/mcp-browser` are the engine's; lanes-local.mjs lets the environment beat
        .env).
        `RESUMEBOT_PAGE_URL` is the owner's page, which the browser window's first tab shows."""
        env = dict(self.environ)
        env[DATA_VARIABLE] = str(self.data)
        env["RESUMEBOT_APP_URL"] = self.engine_url
        env["RESUMEBOT_PAGE_URL"] = self.app_url
        if not self.has("SCOUT_JOBWRAP_PYTHON"):
            # The sessions' job wrapper (jobwrap.py, standard library only) runs under the app's own Python: a PC
            # without the `py` launcher has no other.
            env["SCOUT_JOBWRAP_PYTHON"] = str(self.python)
        node = find_program("node")  # a Node installed a minute ago is not on the PATH this was started with
        return with_path_first(env, [str(Path(node).parent)]) if node else env


# --- processes -------------------------------------------------------------------------------------------------------


def _filetime(pid: int) -> int | None:
    """When a process was created, as a Windows FILETIME integer; None when there is no such process (Windows only)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, other = (ctypes.c_ulonglong(), ctypes.c_ulonglong())
        if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(other),
                                        ctypes.byref(other), ctypes.byref(other)):
            return None
        code = ctypes.c_ulong()
        still_active = 259
        if kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)) and code.value != still_active:
            return None
        return created.value
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_mark(pid: int) -> int | None:
    """What tells this process from a later one that is given its number: its creation time (Windows); on other
    systems the number alone is all there is, and a live process gives 0."""
    if WINDOWS:
        return _filetime(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return 0


def kill_tree(pid: int) -> None:
    """End a process and everything it started, at once."""
    if WINDOWS:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            quiet_run([str(taskkill), "/PID", str(pid), "/T", "/F"], timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass  # what is left is found again by its pid file
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class PidFile:
    """`run/<name>.pid`: the process number and its creation time, so a number that Windows has given to some other
    process since (after a reboot, say) is never taken for ours and never ended."""

    def __init__(self, folder: Path, name: str) -> None:
        self.path = folder / f"{name}.pid"

    def write(self, pid: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": pid, "mark": process_mark(pid)}), encoding="utf-8")

    def live(self) -> int | None:
        """The process number when the process this file names is still running, else None."""
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
            pid, mark = int(held["pid"]), held["mark"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        now = process_mark(pid)
        return pid if now is not None and now == mark else None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def acquire(self) -> int | None:
        """Take the file for this process by creating it, which only one process can do (a lock). None: it is ours now.
        A number: another live process holds it. A file that names a process no longer running is taken over."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                made = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                held = self.live()
                if held is not None and held != os.getpid():
                    return held
                self.clear()  # a dead process's, or this process's own from before: start again
                continue
            with os.fdopen(made, "w", encoding="utf-8") as out:
                out.write(json.dumps({"pid": os.getpid(), "mark": process_mark(os.getpid())}))
            return None
        raise Stop(f"could not take {self.path}: another process keeps making it", 2)


def rotate(path: Path, limit: int = LOG_LIMIT, keep: int = LOG_KEEP) -> None:
    """A log over the limit becomes path.1, the one before it .2, and so on; the oldest of `keep` goes. The log itself
    moves first, to path.0 for a moment: when it cannot be moved (OSError; on Windows, something holds it open and
    forbids that) the older ones stay as they are, and a pass that was cut short there is finished by the next."""
    aside = path.with_name(f"{path.name}.0")
    if not aside.exists():
        try:
            if path.stat().st_size <= limit:
                return
        except OSError:
            return
        path.replace(aside)
    for number in range(keep, 0, -1):
        older = path.with_name(f"{path.name}.{number}")
        newer = path.with_name(f"{path.name}.{number - 1}")
        if number == keep:
            older.unlink(missing_ok=True)
        if newer.exists():
            newer.replace(older)


def open_log(path: Path) -> int:
    """A descriptor that writes at the end of the log, which is made when it is not there. On Windows the file is
    opened so that it can be renamed while it is open, which Python's own `open` forbids there: a log set aside while
    the stack runs (`roll`) is renamed under the process that writes it."""
    if not WINDOWS:
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    # FILE_APPEND_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE (every write goes to the end, whoever else writes the
    # file); shared for reading, writing and deleting (a rename is a delete to Windows); OPEN_ALWAYS; a normal file.
    handle = kernel32.CreateFileW(str(path), 0x0004 | 0x0080 | 0x00100000, 0x1 | 0x2 | 0x4, None, 4, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        return msvcrt.open_osfhandle(handle, 0)
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def roll(logs: Path, limit: int = LOG_LIMIT, keep: int = LOG_KEEP) -> None:
    """One look at the logs while the stack runs: each `*.log` over the limit is set aside as at a start (`rotate`).
    The lanes open, write and close their logs for each line, so their next line starts the fresh file; the app and
    the supervisor hold theirs open, write on in the file set aside and start a fresh one at their next look
    (`follow`). A log that cannot be moved now is left whole for the next look."""
    for log in logs.glob("*.log"):
        try:
            rotate(log, limit, keep)
        except OSError:
            continue


def writing(path: Path, fds: tuple[int, ...] = (1, 2)) -> tuple[int, ...]:
    """Which of these descriptors (this process's output and errors) write to the file that is at `path` now."""
    found = []
    for fd in fds:
        try:
            if os.path.samestat(os.fstat(fd), os.stat(path)):
                found.append(fd)
        except OSError:
            continue
    return tuple(found)


def follow(path: Path, fds: tuple[int, ...]) -> bool:
    """For the process that holds the log at `path` open as `fds`: when the file was set aside under it (it is path.1
    now, and what this process writes still lands there, so no line is lost), a fresh log is started and the
    descriptors write there from now on. True when one was started."""
    there = writing(path, fds)
    moved = [fd for fd in fds if fd not in there]
    if not moved:
        return False
    fresh = open_log(path)
    try:
        for fd in moved:
            os.dup2(fresh, fd)
    finally:
        os.close(fresh)
    return True


def keep_following(path: Path, fds: tuple[int, ...], wait: Callable[[float], bool] | None = None) -> None:
    """`follow`, every `FOLLOW_EVERY` seconds, until `wait` says the process is ending (by itself it never does: this
    runs on a thread of its own, so a slow disk holds up no one's writes). A look that fails is made again."""
    wait = wait or threading.Event().wait
    while not wait(FOLLOW_EVERY):
        try:
            follow(path, fds)
        except OSError:
            continue


class Child:
    """One process the supervisor keeps: started in a group of its own, its output in a log file, asked to finish with
    Ctrl+Break (Windows) or SIGTERM, and ended with its whole tree when it does not."""

    def __init__(
        self, name: str, command: list[str], cwd: Path, env: dict[str, str], log: Path | None, pids: Path,
    ) -> None:
        self.name, self.command, self.cwd, self.env, self.log = name, command, cwd, env, log
        self.pidfile = PidFile(pids, name)
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.delay = 5.0

    def start(self) -> None:
        sink = subprocess.DEVNULL
        if self.log is not None:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            rotate(self.log)
            sink = open_log(self.log)
        errors = subprocess.STDOUT if self.log is not None else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                self.command, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=sink, stderr=errors,
                creationflags=NEW_GROUP, start_new_session=not WINDOWS)
        finally:
            if self.log is not None:
                os.close(sink)
        self.started = time.monotonic()
        self.pidfile.write(self.proc.pid)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def finish(self, grace: float) -> bool:
        """Ask it to finish, wait `grace` seconds, then end its tree. True when it finished by itself."""
        if self.proc is None or not self.alive():
            self.pidfile.clear()
            return True
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGTERM)  # type: ignore[attr-defined]
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
            clean = True
        except subprocess.TimeoutExpired:
            kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass  # the rest of the shutdown goes on: the pid file below says nothing is left to wait for
            clean = False
        self.pidfile.clear()
        return clean


# --- health --------------------------------------------------------------------------------------------------------


def fetch(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None,
          timeout: float = 20) -> tuple[int, dict[str, str], bytes]:
    """One request, no redirect followed: (status, headers in lower case, body). A connection that fails is status 0."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    # No proxy: the address is this machine's, whatever the system's proxy settings say.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=timeout) as reply:
            return reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}, b""


def health(url: str) -> dict | None:
    status, _, body = fetch(f"{url}/health", timeout=5)
    if status not in (200, 503):
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def wait_for_health(url: str, seconds: float, alive=lambda: True) -> dict | None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and alive():
        found = health(url)
        if found and found.get("status") == "ok":
            return found
        time.sleep(1)
    return None


class PortUse(NamedTuple):
    """One port the install's programs take: what it is for, and the setting that moves it."""

    port: int
    udp: bool
    what: str
    setting: str


def install_ports(config: Config, lanes: bool = True) -> list[PortUse]:
    """Every port this install's programs take, as `start` uses them: the page's (TCP, and UDP for its notices), the
    engine's (the same two), the lanes' (not under `start --hold`, which starts none), and Postgres's only when this
    stack runs its own."""
    page_move = "RESUMEBOT_APP_URL in the data folder's .env (the engine takes that port plus 100)"
    uses = [
        PortUse(config.port, False, "the page's", page_move),
        PortUse(config.port, True, "the page's notices", page_move),
        PortUse(config.engine_port, False, "the engine's", page_move),
        PortUse(config.engine_port, True, "the engine's notices", page_move),
    ]
    if lanes:
        uses += [PortUse(config.scout_port + index, False, f"lane {index + 1}'s",
                         "SCOUT_PORT in the data folder's .env") for index in range(LANE_COUNT)]
    if config.uses_postgres:
        raw = config.get("RESUMEBOT_PG_PORT")
        uses.append(PortUse(int(raw) if raw.isdigit() else DEFAULT_PG_PORT, False, "Postgres's",
                            "RESUMEBOT_PG_PORT in the data folder's .env"))
    return uses


def port_is_free(port: int, udp: bool) -> bool:
    """Free: nothing accepts a connection on 127.0.0.1 there (TCP) and a test bind of it succeeds."""
    if not udp:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)  # a listener on this machine answers at once; a refusal can take a second on Windows
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as probe:
        if not udp and not WINDOWS:
            # Off Windows a port whose listener has just closed stays reserved for a while (its last connections are
            # still winding down), and a plain bind of it fails though nothing holds it: a start right after a stop
            # was refused. With this the bind is judged as the server's own will be (it sets the same), and a port
            # something still listens on is still refused. Windows needs none of it, and there it would let the bind
            # through beside a live listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


#: The foreign address `netstat` shows for a socket that is listening (TCP) in any language.
ANY_FOREIGN = ("0.0.0.0:0", "[::]:0", "*:*")


def holder(port: int, udp: bool) -> str | None:
    """The program holding a port as 'name (pid N)', where Windows can tell; None otherwise. Best effort: it never
    fails."""
    if not WINDOWS:
        return None
    try:
        shown = quiet_run(["netstat", "-ano", "-p", "UDP" if udp else "TCP"], timeout=20).stdout
        pid = None
        for line in shown.splitlines():
            parts = line.split()
            # A listener is the line whose foreign address is the empty one; the word for its state (LISTENING) is
            # translated on a PC whose language is not English.
            if len(parts) >= 4 and parts[1].endswith(f":{port}") and parts[-1].isdigit() and (
                    udp or parts[2] in ANY_FOREIGN):
                pid = parts[-1]
                break
        if pid is None or pid == "0":
            return None
        listed = quiet_run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=20).stdout.strip()
        name = listed.split(",")[0].strip('"') if listed.startswith('"') else ""
        return f"{name} (pid {pid})" if name else f"pid {pid}"
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def taken_ports(config: Config, lanes: bool = True) -> list[str]:
    """One plain sentence for each port of the install that is taken (or past 65535): which port, what it is for, what
    holds it, and the setting that moves it. None: every port is free."""
    found = []
    uses = install_ports(config, lanes)
    with ThreadPoolExecutor(len(uses)) as pool:  # a refused connection can take a second each on Windows
        free = list(pool.map(lambda use: use.port > 65535 or port_is_free(use.port, use.udp), uses))
    for use, is_free in zip(uses, free):
        kind = "UDP port" if use.udp else "port"
        if use.port > 65535:
            found.append(f"{kind} {use.port} ({use.what}) is past 65535: set {use.setting} to a lower port")
        elif not is_free:
            by = holder(use.port, use.udp)
            found.append(f"{kind} {use.port} ({use.what}) is taken by {by or 'another program'}: "
                         f"set {use.setting} to move it")
    return found


def tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return "(no log)"


# --- Postgres (app/scripts/local/pg.py) ----------------------------------------------------------------------------


def pg_environment(config: Config) -> dict[str, str]:
    env = dict(config.environ)
    env[DATA_VARIABLE] = str(config.data)
    if config.has("RESUMEBOT_PG_PORT"):  # pg.py reads the environment only; the data folder's .env names the port too
        env["RESUMEBOT_PG_PORT"] = config.get("RESUMEBOT_PG_PORT")
    return env


def postgres(config: Config, action: str) -> int:
    """pg.py start | stop | status, with this data folder; it prints its own sentences. With a SQLite URL there is
    no Postgres: nothing runs and the answer is 0."""
    if not config.uses_postgres:
        return 0
    if not PG_SCRIPT.exists():
        raise Stop(f"{PG_SCRIPT} is not there: the local Postgres comes with it")
    try:
        done = quiet_run([sys.executable, "-I", str(PG_SCRIPT), action], env=pg_environment(config), timeout=300)
    except (OSError, subprocess.SubprocessError) as problem:
        say(f"pg.py {action} could not run: {type(problem).__name__}")
        return 1
    for text in (done.stdout, done.stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", flush=True)
    return done.returncode


# --- the watch on the app: a restart when it stops answering, with a snapshot first ---------------------------------

#: Once the app is up, /health is asked this often, and after this many failed answers in a row (about a minute and a
#: half without one) the app is restarted. /health answers on a small pool of its own (app/db.py), so an app that is
#: only busy still answers: this is the last line, for an app that truly stopped, never the fix for a busy one.
WATCH_EVERY, WATCH_FAILS = 15.0, 6
#: A just-started app is not counted against until it has answered once, or until this long after its start.
WATCH_START_GRACE = 300.0
#: How many database snapshots are kept in the logs folder (the oldest goes).
SNAPSHOTS_KEPT = 10


class Watch:
    """Counts the app's failed /health answers in a row, from when it is up (`armed`)."""

    def __init__(self, every: float = WATCH_EVERY, fails: int = WATCH_FAILS, grace: float = WATCH_START_GRACE) -> None:
        self.every, self.fails, self.grace = every, fails, grace
        self.restart(0.0)

    def restart(self, now: float) -> None:
        """The app was (re)started at `now`: nothing is counted until it answers, or the grace has passed."""
        self.started, self.next_at, self.missed, self.armed = now, now + self.every, 0, False

    def due(self, now: float) -> bool:
        return now >= self.next_at

    def saw(self, healthy: bool, now: float) -> bool:
        """Note one answer (`healthy` or not) at `now`. True when the app should be restarted."""
        self.next_at = now + self.every
        if healthy:
            self.missed, self.armed = 0, True
            return False
        if not self.armed and now - self.started < self.grace:
            return False
        self.armed = True
        self.missed += 1
        return self.missed >= self.fails


def sqlite_sizes(config: Config) -> str:
    """The SQLite database file's size and its -wal file's (a wal that keeps growing is a writer that never lets go)."""
    main = config.db_file
    if main is None:
        return ""
    lines = []
    for file in (main, main.with_name(main.name + "-wal")):
        try:
            lines.append(f"{file}: {file.stat().st_size} bytes")
        except OSError:
            lines.append(f"{file}: not there")
    return "\n".join(lines) + "\n"


def db_snapshot(config: Config, why: str, log: Path | None = None) -> Path | None:
    """Write what the database is doing (Postgres: pg.py snapshot, every session, what it waits on and who blocks it,
    every lock; SQLite: the sizes of the database file and of its -wal file) and the end of `log` (default engine.log)
    to logs/db-snapshot-<UTC time>.txt, so the cause of a stop can be read afterwards with no one watching.
    Keeps the newest `SNAPSHOTS_KEPT`. Returns the file, or None when nothing could be written."""
    config.logs.mkdir(parents=True, exist_ok=True)
    path = config.logs / f"db-snapshot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.txt"
    if not config.uses_postgres:
        said = sqlite_sizes(config)
    else:
        try:
            found = quiet_run([sys.executable, "-I", str(PG_SCRIPT), "snapshot"], env=pg_environment(config),
                              timeout=90)
            failure = f"\n(pg.py snapshot: exit {found.returncode})\n{found.stderr}" if found.returncode else ""
            said = found.stdout + failure
        except (OSError, subprocess.SubprocessError) as problem:
            said = f"(pg.py snapshot could not run: {problem})\n"
    try:
        shown = log or config.logs / "engine.log"
        path.write_text(f"{why}\n\n== the database ==\n{said}\n== the end of {shown.name} ==\n"
                        f"{tail(shown, 60)}\n", encoding="utf-8")
    except OSError:
        return None
    for old in sorted(config.logs.glob("db-snapshot-*.txt"))[:-SNAPSHOTS_KEPT]:
        old.unlink(missing_ok=True)
    return path


def watch_child(config: Config, child: Child, watch: Watch, url: str, now: float,
                ask: Callable[[str], dict | None] = health) -> bool:
    """One look at a program (the app, the page or the engine) at `url`, when one is due: after `WATCH_FAILS` failed
    /health answers in a row its database snapshot is written (`db_snapshot`, with the end of the child's own log) and
    it is ended, so the supervisor starts it again. True when it was ended."""
    if not child.alive() or not watch.due(now):
        return False
    found = ask(url)
    if not watch.saw(bool(found and found.get("status") == "ok"), now):
        return False
    why = (f"the {child.name} did not answer /health {watch.missed} times in a row ({watch.every:.0f}s apart; "
           f"last answer: {found}); restarting it")
    kept = db_snapshot(config, why, child.log)
    supervisor_say(why + (f"; what the database was doing is in {kept}" if kept else ""))
    child.ended_why = "health check failed"  # type: ignore[attr-defined]
    child.finish(10)
    return True


#: A page or engine that ends this soon after it started has the end of its own log copied under its restart line (never
#: a lane's log: it can hold a session's text).
QUICK_END = 10.0
QUICK_END_LOGS = ("page", "engine")


class Keeper:
    """One child the supervisor keeps, looked after on its own turn: when it may start again, where its /health is asked
    and its `Watch` (a program that answers), and the address that must answer before it is first started (`wait_url`:
    the lanes wait for the engine). A child that is down waits for its own turn only; no other child waits for it."""

    def __init__(self, child: Child, url: str | None = None, watch: Watch | None = None,
                 wait_url: str | None = None) -> None:
        self.child, self.url, self.watch, self.wait_url = child, url, watch, wait_url
        self.restart_at: float | None = None
        self.noted: tuple[int, int | None, str, str] | None = None  # old pid, exit code, why, its log's end


def start_child(child: Child, keeper: Keeper, now: float) -> bool:
    """Start a program. A start that fails (no such file, access denied, an antivirus holding the program) is said, the
    program gets its own wait (doubling, at most five minutes) and the supervisor goes on with the others. True when
    it started."""
    try:
        child.start()
    except OSError as problem:
        keeper.restart_at = now + child.delay
        supervisor_say(f"the {child.name} could not be started ({problem.strerror or type(problem).__name__}); "
                       f"trying again in {child.delay:.0f}s")
        child.delay = min(child.delay * 2, 300.0)
        return False
    return True


def tend(config: Config, keepers: list[Keeper], now: float, ask: Callable[[str], dict | None] = health) -> None:
    """One look at every child, each on its own turn, at the monotonic time `now`:
    - one that is up has its wait set to five seconds again once it has stayed up a minute, and a program with a
      `Watch` is asked about (`watch_child`: a snapshot of the database, then it is ended);
    - one that was never started is started when it has no `wait_url`, or when `ask(wait_url)` says ok;
    - one that ended is noted once and started again after its own wait, which doubles each time (at most five
      minutes) and, once started, restarts its `Watch`.
    Nothing here sleeps, so a child that waits to start again never holds up another."""
    for keeper in keepers:
        child = keeper.child
        if child.alive():
            keeper.restart_at = None
            if now - child.started > 60:
                child.delay = 5.0
            if keeper.watch is not None and keeper.url is not None:
                watch_child(config, child, keeper.watch, keeper.url, now, ask)
            continue
        if child.proc is None:
            if keeper.restart_at is not None and now < keeper.restart_at:
                continue
            found = ask(keeper.wait_url) if keeper.wait_url else {"status": "ok"}
            if not (found and found.get("status") == "ok"):
                continue
            keeper.restart_at = None
            if not start_child(child, keeper, now):
                continue
            supervisor_say(f"the {child.name} is starting (pid {child.proc.pid if child.proc else 0})")
        else:
            if keeper.restart_at is None:
                keeper.restart_at = now + child.delay
                supervisor_say(f"the {child.name} ended (exit {child.proc.returncode}); "
                               f"starting it again in {child.delay:.0f}s")
                early = ""
                if child.name in QUICK_END_LOGS and child.log is not None and now - child.started < QUICK_END:
                    early = tail(child.log, 5) if child.log.exists() else ""
                keeper.noted = (child.proc.pid, child.proc.returncode,
                                getattr(child, "ended_why", None) or "exited", early)
                child.ended_why = None  # type: ignore[attr-defined]
            if now < keeper.restart_at:
                continue
            keeper.restart_at = None
            child.delay = min(child.delay * 2, 300.0)
            if not start_child(child, keeper, now):
                continue
            if keeper.noted is not None:
                old, code, why, early = keeper.noted
                keeper.noted = None
                supervisor_say(f"restarted the {child.name}: old pid {old} exit {code}, new pid "
                               f"{child.proc.pid if child.proc else 0}; why: {why}")
                for line in early.splitlines():
                    print(f"    {line}", flush=True)
        if keeper.watch is not None:
            keeper.watch.restart(now)


# --- the tools: Node, and the app's Python environment ---------------------------------------------------------------


def registry_paths() -> list[str]:
    """The folders of the PATH that Windows keeps for the machine and for this user, as they are now (a program
    installed after this process started is on them, and not on this process's own PATH). Empty away from Windows."""
    if not WINDOWS:
        return []
    import winreg  # type: ignore[import-not-found]

    folders: list[str] = []
    for hive, sub_key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        folders += [os.path.expandvars(part) for part in str(value).split(";") if part.strip()]
    return folders


def known_node_folders() -> list[str]:
    """Where Node's installer puts it, for the PC whose registry PATH does not name it either."""
    if not WINDOWS:
        return []
    found = []
    for variable, rest in (("ProgramFiles", "nodejs"), ("ProgramW6432", "nodejs"), ("LOCALAPPDATA", "Programs/nodejs")):
        base = os.environ.get(variable)
        if base:
            found.append(str(Path(base) / rest))
    return found


def find_program(name: str) -> str | None:
    """A program by name: on this process's PATH, else on the PATH the registry holds now, else in the folders Node's
    installer uses. None when it is nowhere."""
    found = shutil.which(name)
    if found:
        return found
    folders = [*registry_paths(), *known_node_folders()]
    return shutil.which(name, path=os.pathsep.join(folders)) if folders else None


def with_path_first(env: dict[str, str], folders: list[str]) -> dict[str, str]:
    """A copy of `env` whose PATH starts with these folders (those not already on it)."""
    env = dict(env)
    key = next((name for name in env if name.upper() == "PATH"), "PATH")
    have = [part for part in env.get(key, "").split(os.pathsep) if part]
    seen = {os.path.normcase(part) for part in have}
    add = [folder for folder in folders if os.path.normcase(folder) not in seen]
    if add:
        env[key] = os.pathsep.join([*add, *have])
    return env


def clean_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """What a setup step runs with: the shell's environment without what steers Python, pip and Node away from the
    app's own (PYTHONPATH, PYTHONHOME, VIRTUAL_ENV, PIP_*, NODE_ENV: a NODE_ENV of production leaves the build tools out
    of `npm ci`)."""
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items()
            if name.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NODE_ENV")
            and not name.upper().startswith("PIP_")}


def sqlite_is_safe(version: tuple[int, ...]) -> bool:
    """Whether a SQLite version is clear of the WAL-reset bug: fixed in 3.51.3 and backported to 3.50.7 and 3.44.6.
    A copy of `sqlite_is_safe` in app/backend/app/setup_cli.py (a test holds the two equal): at setup the app is not
    installed in the environment yet, so this file cannot import it."""
    return version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)


SQLITE_REMEDY = ("run  uv python install 3.12 --upgrade, delete the app's .venv, run  stack.py setup  with  "
                 "$(uv python find --managed-python 3.12)")
#: What the environment's Python is asked: pip and sqlite3 import (a venv killed half-made has python.exe and no pip),
#: and which SQLite it carries.
VENV_PROBE = "import pip, sqlite3, sys; print(sqlite3.sqlite_version)"


def venv_state(config: Config) -> tuple[str, str]:
    """How the app's Python environment is: ("missing", "") when there is none; ("broken", why) when its Python does
    not run or lacks pip or sqlite3; ("unsafe", version) when its SQLite has the WAL-reset bug; else ("ok", version)."""
    if not config.python.exists():
        return "missing", ""
    try:
        done = quiet_run([str(config.python), "-c", VENV_PROBE], timeout=60,
                         env={**clean_environment(config.environ), "PYTHONUTF8": "1"})
    except (OSError, subprocess.SubprocessError) as problem:
        return "broken", type(problem).__name__
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        why = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
        return "broken", why
    said = lines[-1].strip()
    try:
        version = tuple(int(part) for part in said.split(".")[:3])
    except ValueError:
        return "broken", f"SQLite version {said!r}"
    return ("ok" if sqlite_is_safe(version) else "unsafe"), said


# --- setup ---------------------------------------------------------------------------------------------------------

STEP_HINT = "Check the network and antivirus, then run setup again: it is safe to repeat"


def run_step(command: list[str], cwd: Path | None = None, label: str = "", env: dict[str, str] | None = None) -> None:
    """One long program, in this console so its own output shows. Its environment is the shell's without what steers
    Python, pip and Node (`clean_environment`) unless `env` is given. A step that fails says which step and that
    setup is safe to run again."""
    name = label or f"{Path(command[0]).name} {' '.join(command[1:3])}"
    try:
        result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                env=clean_environment() if env is None else env)
    except OSError as problem:
        raise Stop(f"{name} could not start ({problem.strerror or type(problem).__name__}). {STEP_HINT}") from None
    if result.returncode != 0:
        raise Stop(f"{name} failed ({result.returncode}); see above. {STEP_HINT}")


def link_node_modules() -> None:
    """The page's build needs app/node_modules to be app/frontend/node_modules: the process-view package lives beside
    the frontend and finds its imports (react, @xyflow/react ...) from its real place, up the folders. On Windows the
    link is a junction, which needs no administrator rights."""
    link, target = APP / "node_modules", FRONTEND / "node_modules"
    if os.path.lexists(link):
        if Path(os.path.realpath(link)) != Path(os.path.realpath(target)):
            say(f"{link} is not a link to {target}: the page's build may not find its imports there")
        return
    if WINDOWS:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
    else:
        os.symlink(target, link, target_is_directory=True)


NODE_REMEDY = "winget install OpenJS.NodeJS.LTS --source winget --accept-source-agreements --accept-package-agreements"


def node_is_new_enough(version: tuple[int, ...]) -> bool:
    """The page's build tools (Vite) need Node 20.19 or newer, or 22.12 or newer on the 22 line (21 never)."""
    major, minor = (*version, 0, 0)[:2]
    return (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major >= 23


def swap_page(built: Path, target: Path = PAGE_DIR) -> None:
    """Put a finished build where the app serves it: the old page is moved aside, the new one moved in, and the old one
    removed; when the move in fails the old page goes back. A build with no index.html is no build."""
    if not (built / "index.html").is_file():
        raise Stop(f"the build made no page ({built / 'index.html'} is missing): the old page is kept. {STEP_HINT}")
    aside = target.with_name(target.name + ".old")
    shutil.rmtree(aside, ignore_errors=True)
    had = target.exists()
    try:
        if had:
            os.replace(target, aside)
        os.replace(built, target)
    except OSError as problem:
        if had and aside.exists() and not target.exists():
            os.replace(aside, target)
        raise Stop(f"could not put the new page in place "
                   f"({problem.strerror or type(problem).__name__}): the old page is kept; close what has it open "
                   "and run setup again") from None
    shutil.rmtree(aside, ignore_errors=True)


def build_page() -> None:
    npm, node = find_program("npm"), find_program("node")
    if not npm or not node:
        raise Stop("Node.js (npm) is not on the PATH: it is needed once to build the page (nodejs.org); "
                   f"or run  {NODE_REMEDY}  and then  stack.py setup  again")
    env = with_path_first(clean_environment(), [str(Path(node).parent), str(Path(npm).parent)])
    try:
        said = quiet_run([node, "--version"], timeout=30, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        said = ""
    found = re.match(r"v(\d+)\.(\d+)", said)
    if not found or not node_is_new_enough((int(found.group(1)), int(found.group(2)))):
        raise Stop(f"Node.js {said or '(of a version it will not say)'} cannot build the page: it needs 20.19 or "
                   f"newer, or 22.12 or newer on the 22 line: run  {NODE_REMEDY}")
    modules, lock = FRONTEND / "node_modules" / ".package-lock.json", FRONTEND / "package-lock.json"
    if not modules.exists() or modules.stat().st_mtime < lock.stat().st_mtime:
        say("installing the page's packages (npm ci)")
        run_step([npm, "ci", "--include=dev", "--no-audit", "--no-fund"], FRONTEND, "installing the page's packages",
                 env)
    link_node_modules()
    say("building the page (npm run build:package-temp), then putting it in place")
    run_step([npm, "run", "build:package-temp"], FRONTEND, "building the page", env)
    swap_page(PAGE_BUILD)


def only_me(path: Path) -> None:
    """Leave this user alone with a folder and what is made in it (Windows: inherited access dropped, as pg.py does for
    the Postgres folder; elsewhere the mode). Best effort: a folder in the user's own profile is private already."""
    try:
        if WINDOWS:
            who = quiet_run(["whoami", "/user", "/fo", "csv", "/nh"], timeout=30, check=True).stdout.strip()
            sid = who.split(",")[1].strip().strip('"')
            quiet_run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"], timeout=60, check=True)
        else:
            path.chmod(0o700)
    except (OSError, subprocess.SubprocessError, IndexError):
        say(f"could not limit {path} to this user: check who can read it")


def next_step(config: Config) -> str:
    """What to do after `setup`: a folder with no database address has not had `resumebot init`; one with it starts."""
    if not config.has("JOBSEARCH_DATABASE_URL"):
        return (f"Next: resumebot init --owner \"<your name>\" --data \"{config.data}\" (the settings, the time zone "
                "and the database), then  stack.py start")
    return "Next: stack.py start"


def site_packages(config: Config) -> Path:
    """The environment's own folder for installed packages: the known place on Windows, else the one its Python says
    (read as UTF-8, whatever this PC's code page is)."""
    known = config.venv / "Lib" / "site-packages"
    if WINDOWS and known.is_dir():
        return known
    try:
        said = quiet_run([str(config.python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                         env={**clean_environment(config.environ), "PYTHONUTF8": "1"}, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as problem:
        raise Stop(f"the app's Python would not say where its packages go ({type(problem).__name__}): run setup again; "
                   f"it is safe to repeat") from None
    return Path(said)


def setup(config: Config, dev: bool, page: bool) -> None:
    """The data folder, the Python environment and the page. It binds no port (so a taken port does not stop it: `start`
    says that), refuses while the app runs (its files are in use), and takes `run/setup.pid` so two setups never work in
    the same folders at once."""
    if sys.version_info < (3, 12):  # the script itself runs on older ones, to say this
        raise Stop("Python 3.12 or newer runs the app: run  stack.py setup  with uv's own Python 3.12, the path that  "
                   "uv python find --managed-python 3.12  prints", 2)
    for folder in (config.data, config.secrets, config.logs, config.run):
        folder.mkdir(parents=True, exist_ok=True)
    running = PidFile(config.run, "supervisor").live()
    if running:
        raise Stop(f"the app is running (supervisor pid {running}): run  stack.py stop  first, then  stack.py setup", 2)
    lock = PidFile(config.run, "setup")
    other = lock.acquire()
    if other:
        raise Stop(f"another setup is running (pid {other}): wait for it to finish, then run it again if need be", 10)
    try:
        make_environment(config, dev, page)
    finally:
        lock.clear()


def make_environment(config: Config, dev: bool, page: bool) -> None:
    only_me(config.secrets)
    for name in SECRET_FILES:
        path = config.secrets / name
        if not path.exists():
            path.write_text(secrets.token_hex(32), encoding="utf-8")
            say(f"made {path}")
    state, detail = venv_state(config)
    if state == "broken":  # a venv killed half-made has python.exe and no pip; it is made again, whole
        if not (config.venv / "pyvenv.cfg").is_file():
            raise Stop(f"{config.venv} has a Python that does not run ({detail}) and is not a virtual environment this "
                       "launcher made: remove it yourself, then run setup again", 2)
        say(f"the Python environment in {config.venv} does not run ({detail}): making it again")
        try:
            shutil.rmtree(config.venv)
        except OSError as problem:
            raise Stop(f"could not remove {config.venv} ({problem.strerror or type(problem).__name__}): close what has "
                       "it open (a terminal inside it, an editor) and run setup again") from None
        state = "missing"
    if state == "missing":
        say(f"making the app's Python environment in {config.venv}")
        run_step([sys.executable, "-m", "venv", str(config.venv)], label="making the app's Python environment")
        state, detail = venv_state(config)
    if state == "broken":
        raise Stop(f"the Python environment in {config.venv} was made and does not run ({detail}). {STEP_HINT}")
    if state == "unsafe":
        raise Stop(f"the app's Python carries SQLite {detail}, which has the WAL-reset bug "
                   f"(3.51.3 or newer is needed): {SQLITE_REMEDY}", 2)
    lock = BACKEND / ("requirements-dev.lock" if dev else "requirements.lock")
    say(f"installing {lock.name} (every file checked against its hash)")
    run_step([str(config.python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "--require-hashes",
              "--only-binary=:all:", "--no-deps", "-r", str(lock)], label="installing the app's packages")
    # The process engine is vendored in this repository (app/packages/process-engine): the environment reads it from
    # there, so it is never a stale copy and the checkout is not left with build folders.
    packages = site_packages(config)
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "resumebot-process-engine.pth").write_text(str(ENGINE_SRC) + "\n", encoding="utf-8")
    # The `resumebot` command (pyproject.toml's [project.scripts]), by the same pip: the app is read from this folder
    # (-e), so after an update the command is the new version; --no-deps, so no package comes from outside the lock.
    say("adding the resumebot command to the environment")
    run_step([str(config.python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-deps",
              "-e", str(BACKEND)], label="adding the resumebot command")
    if page:
        build_page()
    say(f"set up. {next_step(config)}")


# --- the supervisor ------------------------------------------------------------------------------------------------


def problems(config: Config, hold: bool) -> list[str]:
    """What stops a start, as plain sentences (none: it can start)."""
    found = []
    state, detail = venv_state(config)
    if state == "missing":
        found.append(f"the app's Python environment is not at {config.venv}: run  stack.py setup")
    elif state == "broken":
        found.append(f"the app's Python environment at {config.venv} does not run ({detail}): run  stack.py setup  "
                     "(it makes the environment again)")
    elif state == "unsafe":
        found.append(f"the app's Python carries SQLite {detail}, which has the WAL-reset bug: {SQLITE_REMEDY}")
    here = Path(sys.executable)
    if WINDOWS and here.name.lower() == "pythonw.exe" and not here.with_name("python.exe").is_file():
        found.append(f"{here.with_name('python.exe')} is not there beside pythonw.exe: without it every program the "
                     "launcher starts opens a console window; reinstall that Python")
    if not PAGE_INDEX.is_file():
        found.append(f"the page is not built ({PAGE_INDEX} is missing): run  stack.py setup")
    if not config.has("JOBSEARCH_DATABASE_URL"):
        found.append(f"JOBSEARCH_DATABASE_URL is not set in {config.env_file}: run  resumebot init --owner "
                     f"\"<your name>\" --data \"{config.data}\"  (it makes the settings, the time zone and the "
                     "database)")
    for name in ("agent-token", "wake-secret"):
        if not (config.secrets / name).exists():
            found.append(f"{config.secrets / name} is not there: run  stack.py setup")
    if not hold and not find_program("node"):
        found.append("Node.js is not on the PATH: the lanes run under it (use --hold to start without them)")
    if not PidFile(config.run, "supervisor").live():  # a running instance owns its own ports
        found += taken_ports(config, lanes=not hold)
    return found


def program_command(config: Config, log: str, module: str, port: int) -> list[str]:
    """One program's process: `serve` below, which runs the server and follows logs/<log>.log when it is set aside."""
    return [str(config.python), str(Path(__file__).resolve()), "serve", str(config.logs / f"{log}.log"),
            module, "--host", "127.0.0.1", "--port", str(port), "--no-server-header"]


def serve(log: Path, server: list[str]) -> None:
    """What `python -m uvicorn <server>` runs, in the folder it is started in, by a process that starts a fresh log
    when its own was set aside (`keep_following`, where its output or errors are that log; the file is renamed under
    it, and only the process that holds a file open can let go of it). The app's Python runs this file for it."""
    fds = writing(log)
    if fds:
        threading.Thread(target=keep_following, args=(log, fds), name="follow-log", daemon=True).start()
    sys.path[0] = os.getcwd()  # as `python -m` has it; not this file's folder
    from app.log_setup import uvicorn_log_config  # the app's own folder is on the path now

    conf = log.with_suffix(".logconfig.json")
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(json.dumps(uvicorn_log_config()), encoding="utf-8")
    sys.argv = ["uvicorn", *server, "--log-config", str(conf)]
    runpy.run_module("uvicorn", run_name="__main__", alter_sys=True)


#: How long a stop waits for the lanes to finish: their browser workers' items (up to three minutes), then the lanes.
LANES_GRACE = 210


def supervise(config: Config, hold: bool) -> int:
    """The long-running part: Postgres, then the page and the engine, then the lanes; keeps every child up on its own
    turn (`tend`): a restart after a crash, waiting longer each time, and a restart of a program that stopped answering
    /health, with a snapshot of the database written to the logs first (`watch_child`); the page, the engine and the
    lanes never wait for one another, and an engine that never answers does not end the stack; on a stop request (the
    run/stop file, Ctrl+C, Ctrl+Break, SIGTERM) finishes the lanes, the engine, the page and Postgres in that order."""
    for folder in (config.logs, config.run):
        folder.mkdir(parents=True, exist_ok=True)
    mine = PidFile(config.run, "supervisor")
    other = mine.acquire()  # first of all, so a `start` sees the supervisor at once, and two never run together
    if other:
        raise Stop(f"another supervisor is already running (pid {other}): stack.py stop first", 2)
    for old in config.logs.glob("*.log"):
        rotate(old)
    own_log = config.logs / "supervisor.log"
    own = writing(own_log)  # none when this runs in a window (`run`)
    rolled = time.monotonic()
    stop_file = config.run / "stop"
    stop_file.unlink(missing_ok=True)
    wanted = {"stop": False}

    def asked(*_: object) -> None:
        wanted["stop"] = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), asked)
    (config.run / "mode").write_text("held" if hold else "running", encoding="utf-8")
    for stray in ("app", "engine", "page", "lanes"):  # left by a supervisor that was ended outright
        pid = PidFile(config.run, stray).live()
        if pid:
            supervisor_say(f"ending the {stray} an earlier supervisor left (pid {pid})")
            kill_tree(pid)
            PidFile(config.run, stray).clear()
    children: list[Child] = []
    code = 0
    try:
        if config.uses_postgres and postgres(config, "start") != 0:
            raise Stop("Postgres did not start")
        engine = Child("engine", program_command(config, "engine", "app.jobsearch.engine:app", config.engine_port),
                       BACKEND, config.app_environment(hold), config.logs / "engine.log", config.run)
        page = Child("page", program_command(config, "page", "app.jobsearch.page:app", config.port), BACKEND,
                     config.app_environment(False), config.logs / "page.log", config.run)
        # Both start at once. The list is the start order and the stop order is its reverse: the lanes, then the
        # engine, the page last (it goes on answering the owner while the others finish).
        for program in (page, engine):
            try:
                program.start()
            except OSError as problem:  # `tend` starts it again, after a wait of its own
                why = problem.strerror or type(problem).__name__
                supervisor_say(f"the {program.name} could not be started ({why}); trying again in a moment")
        children.extend([page, engine])
        for program, url in ((page, config.app_url), (engine, config.engine_url)):
            if program.proc is not None:
                supervisor_say(f"the {program.name} is starting (pid {program.proc.pid}) on {url}")
        page_watch, engine_watch = Watch(), Watch()
        page_watch.restart(time.monotonic())
        engine_watch.restart(time.monotonic())
        lanes = None
        if not hold:
            node = find_program("node") or "node"
            lanes = Child("lanes", [node, str(SCOUT / "lanes-local.mjs")], SCOUT, config.lanes_environment(),
                          config.logs / "lanes-stdout.log", config.run)
            children.append(lanes)
            supervisor_say("the lanes start once the engine answers")
        else:
            supervisor_say(
                "held: the scheduled searches and the lanes are off; the engine still answers the page's asks")
        keepers = [Keeper(page, config.app_url, page_watch), Keeper(engine, config.engine_url, engine_watch)]
        if lanes is not None:
            keepers.append(Keeper(lanes, wait_url=config.engine_url))
        while not wanted["stop"] and not stop_file.exists():
            time.sleep(0.5)
            if time.monotonic() - rolled >= ROLL_EVERY:
                rolled = time.monotonic()
                roll(config.logs)
                try:
                    follow(own_log, own)
                except OSError:
                    pass  # the next look tries again
            # Each child is looked after on its own turn (`tend`): a program that stopped answering /health is ended
            # (a snapshot of the database first) and started again, a child that ended is started again after its own
            # wait, and none of this waits for another child.
            tend(config, keepers, time.monotonic(), health)
    except Stop as problem:
        supervisor_say(str(problem))
        note_failure(config.logs, "supervise", str(problem))
        code = problem.code
    except KeyboardInterrupt:
        pass
    finally:
        supervisor_say("stopping")
        for child in reversed(children):
            if child.proc is None:  # never started: the lanes, when the engine never answered
                continue
            # The lanes let the browser workers finish the item each holds first, up to about three minutes
            # (lanes-local.mjs `drainBrowser`).
            clean = child.finish(LANES_GRACE if child.name == "lanes" else 60)
            supervisor_say(f"the {child.name} {'finished' if clean else 'did not finish in time and was ended'}")
        if config.uses_postgres:
            postgres(config, "stop")
        stop_file.unlink(missing_ok=True)
        (config.run / "mode").unlink(missing_ok=True)
        mine.clear()
        supervisor_say("stopped")
    return code


# --- start, stop, status ---------------------------------------------------------------------------------------------


def supervisor_command(hold: bool) -> list[str]:
    program = Path(sys.executable)
    if WINDOWS and program.name.lower() == "pythonw.exe":
        # The logon task starts this file with pythonw.exe, which has no console. A supervisor with none makes every
        # child open a visible console of its own; python.exe beside it gets the hidden one `start` creates, and the
        # children share it.
        console = program.with_name("python.exe")
        if console.is_file():
            program = console
    return [str(program), str(Path(__file__).resolve()), "supervise", *(["--hold"] if hold else [])]


#: How long `start` waits for the supervisor it launched to take `run/supervisor.pid` (it does so first of all): a
#: supervisor that has not by then never will, and waiting the whole `--wait` for it would show nothing for minutes.
SUPERVISOR_APPEARS = 30.0


def note_failure(logs: Path, command: str, text: str) -> None:
    """One entry in logs/start.log: the time, the command and why it stopped. Under pythonw (the shortcut, the task) a
    stopped start prints nowhere, so this file is where it can be read. Never raises."""
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "start.log", "a", encoding="utf-8", errors="replace") as out:
            out.write(f"{wall_clock().isoformat(timespec='seconds')} {command}: {text.rstrip()}\n")
    except OSError:
        pass


def why_stopped(config: Config, log: Path) -> str:
    """The end of the supervisor's own log and of start.log, for the sentence of a start that did not come up."""
    start_log = config.logs / "start.log"
    return tail(log) + (f"\nstart.log:\n{tail(start_log, 6)}" if start_log.exists() else "")


def start(config: Config, hold: bool, wait: float) -> int:
    found = problems(config, hold)
    if found:
        raise Stop("not starting:\n  " + "\n  ".join(found), 2)
    existing = PidFile(config.run, "supervisor").live()
    if existing:
        say(f"already running (pid {existing}); stack.py stop first to change how it runs")
        return status(config)
    if health(config.app_url):
        raise Stop(f"something already answers at {config.app_url}: is the app running without this launcher?", 2)
    if health(config.engine_url):
        raise Stop(f"something already answers at {config.engine_url}: is an engine running without this launcher?", 2)
    for folder in (config.logs, config.run):
        folder.mkdir(parents=True, exist_ok=True)
    log = config.logs / "supervisor.log"
    rotate(log)
    kwargs: dict = {}
    if WINDOWS:
        info = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        info.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
        kwargs = {"creationflags": NEW_CONSOLE | NEW_GROUP, "startupinfo": info}
    else:
        kwargs = {"start_new_session": True}
    env = dict(config.environ)
    env[DATA_VARIABLE] = str(config.data)
    env["PYTHONUTF8"] = "1"
    sink = open_log(log)
    try:
        proc = subprocess.Popen(supervisor_command(hold), cwd=BACKEND, env=env, stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.STDOUT, **kwargs)
    except OSError as problem:
        raise Stop(f"the supervisor could not be started ({problem.strerror or type(problem).__name__}): "
                   f"{supervisor_command(hold)[0]}") from None
    finally:
        os.close(sink)
    say(f"starting (supervisor pid {proc.pid}; log {log})")
    began = time.monotonic()
    ours = PidFile(config.run, "supervisor")

    def going() -> bool:
        if proc.poll() is not None:
            return False
        return ours.live() is not None or time.monotonic() - began < SUPERVISOR_APPEARS

    found_health = wait_for_health(config.app_url, wait, going)
    if not found_health:
        if proc.poll() is not None:
            raise Stop(f"it stopped ({proc.returncode}):\n{why_stopped(config, log)}")
        if ours.live() is None:
            kill_tree(proc.pid)
            raise Stop(f"the supervisor did not appear within {SUPERVISOR_APPEARS:.0f}s and was ended:\n"
                       f"{why_stopped(config, log)}")
        raise Stop(f"the app has not answered within {wait:.0f}s; it may still be starting (stack.py status):\n"
                   f"{tail(log)}")
    return status(config)


#: How long a stop waits for the supervisor: the lanes' own grace, and two minutes for the programs after them.
STOP_WAIT = LANES_GRACE + 120


def stop(config: Config, wait: float = STOP_WAIT) -> int:
    pid = PidFile(config.run, "supervisor").live()
    if pid:
        say(f"stopping (supervisor pid {pid})")
        (config.run / "stop").write_text("stop", encoding="utf-8")
        end = time.monotonic() + wait
        while time.monotonic() < end and PidFile(config.run, "supervisor").live():
            time.sleep(0.5)
        if PidFile(config.run, "supervisor").live():
            say("the supervisor did not finish in time: ending it and what it started")
            kill_tree(pid)
    for name in ("lanes", "engine", "page", "app"):  # anything a supervisor ended outright left behind
        left = PidFile(config.run, name)
        stray = left.live()
        if stray:
            say(f"ending the {name} left behind (pid {stray})")
            kill_tree(stray)
        left.clear()
    PidFile(config.run, "supervisor").clear()
    (config.run / "stop").unlink(missing_ok=True)
    if config.uses_postgres:
        postgres(config, "stop")
    say("stopped")
    return 0


def program_line(label: str, url: str, found: dict | None, pidfile: PidFile | None) -> str:
    """One line of `status` for a program: what its /health says, or that its process runs and nothing answers
    yet, or that nothing answers."""
    if found:
        return f"{label}: {url} {found.get('status')}, db {found.get('db')}, version {found.get('version')}"
    if pidfile is not None and pidfile.live():
        return f"{label}: starting (the process runs; nothing answers yet at {url})"
    return f"{label}: nothing answers at {url}"


def lane_health(port: int) -> bool:
    status_code, _, body = fetch(f"http://127.0.0.1:{port}/health", timeout=3)
    try:
        return status_code == 200 and bool(json.loads(body).get("ok"))
    except ValueError:
        return False


#: How long `smoke --lanes` waits for the five lanes: a lane that has just been started answers /health late.
LANES_WAIT = 60


def lanes_answer(config: Config, seconds: float = LANES_WAIT) -> list[bool]:
    """Each lane's /health answer, asked again every two seconds until all five answer or `seconds` have passed."""
    end = time.monotonic() + seconds
    while True:
        up = [lane_health(config.scout_port + index) for index in range(LANE_COUNT)]
        if all(up) or time.monotonic() >= end:
            return up
        time.sleep(2)


def status(config: Config) -> int:
    mode = (config.run / "mode").read_text(encoding="utf-8").strip() if (config.run / "mode").exists() else ""
    supervisor = PidFile(config.run, "supervisor").live()
    say(f"data folder {config.data}")
    if supervisor:
        say(f"supervisor: running, pid {supervisor}" + (f" ({mode})" if mode else ""))
    else:
        say("supervisor: not running")
    if config.uses_postgres:
        postgres(config, "status")
    else:
        say(f"database: SQLite, {config.db_file}")
    found = health(config.app_url)
    say(program_line("page", config.app_url, found, None))
    say(program_line("engine", config.engine_url, health(config.engine_url), PidFile(config.run, "engine")))
    up = sum(lane_health(config.scout_port + index) for index in range(LANE_COUNT))
    say(f"lanes: {up} of {LANE_COUNT} answer" + (" (held: none is started)" if mode == "held" else ""))
    return 0 if found and found.get("status") == "ok" else 1


# --- the smoke test ------------------------------------------------------------------------------------------------


def version_of_checkout() -> str:
    found = re.search(r'^VERSION = "([^"]+)"', (BACKEND / "app" / "settings.py").read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else ""


def rpc(method: str, params: dict | None = None) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()


MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def live_events(base: str, send: Callable[[], None], seconds: float) -> list[str]:
    """Open the owner's live stream (`GET /api/v1/live?topics=task`, the smallest one: its `hello` carries no counts)
    and return the names of the events heard, in order. When `hello` comes `send()` is called once; it stops at the
    first `resync`, when the stream ends, or when nothing comes for `seconds`. Reads only."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    heard: list[str] = []
    try:
        with opener.open(base + "/api/v1/live?topics=task", timeout=seconds) as reply:
            while True:
                line = reply.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("event: "):
                    continue
                heard.append(text[len("event: "):])
                if heard[-1] == "hello":
                    send()
                elif heard[-1] == "resync":
                    break
    except (urllib.error.URLError, OSError, ValueError):
        # a stream that did not open, or went quiet (a timeout is an OSError): what was heard stands
        pass
    return heard


def send_resync(port: int) -> None:
    """One datagram to the page's notice port (UDP, the page's own port) that says `resync`, from a sender the page has
    not seen (a boot id of its own): the page takes that as a restarted engine and tells every open stream to read
    again. It carries no row and changes none; an open page reads again once."""
    body = {"v": 1, "b": "smoke" + secrets.token_hex(4), "n": 1, "o": "smoke", "m": [["resync", "", []]]}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(json.dumps(body, separators=(",", ":")).encode("ascii"), ("127.0.0.1", port))


def smoke(config: Config, lanes: bool) -> int:
    """Read-only checks of the running instance: the page's health, the page and the headers and cache rules the app
    sends with it, the screens' calls, the live stream (`hello`, and a `resync` datagram sent to the page's port) and
    the owner door (the page's); the engine's health (the same version as the page's) and the two lane doors (the
    engine's); the export and (with --lanes) the five lanes. Prints a line for each check; reads nothing personal and
    prints no secret."""
    base = config.app_url
    results: list[tuple[bool, str]] = []

    def check(passed: bool, what: str, why: str = "") -> bool:
        results.append((passed, what))
        note = f" -- {why}" if why and not passed else ""
        print(f"[smoke] {'ok  ' if passed else 'FAIL'} {what}{note}", flush=True)
        return passed

    found = None
    end = time.monotonic() + SMOKE_PAGE_WAIT
    while time.monotonic() < end and not found:
        found = health(base)
        if not found:
            time.sleep(2)
    check(bool(found and found.get("status") == "ok" and found.get("db") == "ok"), "/health: ok, database ok",
          f"got {found}")
    want = version_of_checkout()
    if found:
        check(not want or found.get("version") == want, f"/health: version {want} (this checkout's)",
              f"the app says {found.get('version')}")

    engine = config.engine_url
    ready = wait_for_health(engine, SMOKE_ENGINE_WAIT)
    check(bool(ready and ready.get("db") == "ok"), "the engine's /health: ok, database ok",
          f"nothing ready at {engine} within {SMOKE_ENGINE_WAIT}s (it says {health(engine)})")
    if ready:
        same = found.get("version") if found else want
        check(ready.get("version") == same, f"the engine's /health: version {same} (the page's)",
              f"the engine says {ready.get('version')}")

    status_code, headers, body = fetch(base + "/")
    check(status_code == 200 and b'id="root"' in body, "the page: / serves the app shell", f"status {status_code}")
    check(headers.get("cache-control") == "no-cache", "the shell is revalidated on every load (no-cache)",
          headers.get("cache-control", "none"))
    for url in ("/", "/api/v1/me"):
        _, sent, _ = fetch(base + url)
        missing = [h for h in ("content-security-policy", "x-content-type-options", "referrer-policy") if h not in sent]
        check(not missing, f"security headers on {url}", f"missing {missing}")
    _, sent, _ = fetch(base + "/api/v1/me")
    check(sent.get("cache-control") == "no-store", "the API answers are no-store", sent.get("cache-control", "none"))
    status_code, _, _ = fetch(base + "/assets/not-a-file.js")
    check(status_code == 404, "a missing /assets file is a 404, not the shell", f"status {status_code}")
    asset = re.search(rb'(/assets/[^"\']+\.js)', body)
    if asset:
        status_code, sent, _ = fetch(base + asset.group(1).decode())
        check(status_code == 200 and "immutable" in sent.get("cache-control", ""), "a built /assets file is immutable",
              f"status {status_code}, {sent.get('cache-control', 'none')}")
    else:
        check(False, "the shell names a built script under /assets", "none found: is the page built?")
    status_code, _, _ = fetch(base + "/some/screen/path")
    check(status_code == 200, "a screen's address opens the shell (reload, pasted link)", f"status {status_code}")

    for path in ("/api/v1/me", "/api/v1/board", "/api/v1/profile", "/api/v1/inbox"):
        status_code, _, _ = fetch(base + path)
        check(status_code == 200, f"the screens' call {path}", f"status {status_code}")

    heard = live_events(base, lambda: send_resync(config.port), SMOKE_LIVE_WAIT)
    check("hello" in heard, "/api/v1/live opens with hello", f"heard {heard}")
    check("resync" in heard, "a resync datagram sent to the page's port reaches an open stream as resync",
          f"heard {heard}")

    # /mcp and /mcp-browser are the lane doors, the engine's; /mcp-owner is the page's.
    status_code, _, _ = fetch(engine + "/mcp-browser/", method="POST", headers=MCP_HEADERS, body=rpc("tools/list"))
    check(status_code == 401, "/mcp-browser/ refuses a call with no token (401)", f"status {status_code}")
    status_code, _, _ = fetch(base + "/mcp-owner/", method="POST", headers=MCP_HEADERS, body=rpc("tools/list"))
    check(status_code == 401, "/mcp-owner/ refuses a call with no token (401)", f"status {status_code}")
    status_code, _, reply = fetch(engine + "/mcp/", method="POST", headers=MCP_HEADERS,
                                  body=rpc("tools/call", {"name": "jobs.profile", "arguments": {}}))
    check(status_code == 200 and b"no valid agent token" in reply, "/mcp/ refuses a tool call with no token",
          f"status {status_code}")
    for door, token_file in (("/mcp/", config.get("JOBSEARCH_AGENT_TOKEN_FILE") or str(config.secrets / "agent-token")),
                             ("/mcp-browser/", config.get("JOBSEARCH_BROWSER_TOKEN_FILE")
                              or str(config.secrets / "browser-token")),
                             ("/mcp-owner/", config.get("JOBSEARCH_OWNER_TOKEN_FILE")
                              or str(config.secrets / "owner-token"))):
        try:
            token = Path(token_file).read_text(encoding="utf-8-sig").strip()
        except OSError:
            check(False, f"{door} answers a call with its token (tools/list)", f"no token file at {token_file}")
            continue
        status_code, _, reply = fetch((base if door == "/mcp-owner/" else engine) + door, method="POST",
                                      body=rpc("tools/list"),
                                      headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
        names = {"/mcp-browser/": b"browser.next_item", "/mcp-owner/": b"act.do"}.get(door, b"jobs.profile")
        check(status_code == 200 and names in reply, f"{door} answers a call with its token (tools/list)",
              f"status {status_code}")
    status_code, sent, reply = fetch(base + "/export.xlsx", timeout=120)
    check(status_code == 200 and reply[:2] == b"PK", "/export.xlsx is a workbook", f"status {status_code}")

    if lanes:
        up = lanes_answer(config)
        check(all(up), f"the {LANE_COUNT} lanes answer /health", f"answering: {up}")
    failed = [what for passed, what in results if not passed]
    passed_count = len(results) - len(failed)
    print(f"[smoke] {'PASS' if not failed else 'FAIL'}: {passed_count} of {len(results)} checks", flush=True)
    return 0 if not failed else 1


def owner_header(config: Config) -> int:
    """The action door's header as JSON, for the project's .mcp.json (`headersHelper`), read from the data folder each
    time Claude Code connects: the token is never written into the repository. Only the JSON goes to standard output."""
    path = Path(config.get("JOBSEARCH_OWNER_TOKEN_FILE") or config.secrets / "owner-token")
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise Stop(f"no owner token at {path}: run  stack.py setup", 2) from None
    print(json.dumps({"Authorization": f"Bearer {token}"}), flush=True)
    return 0


# --- the command line ----------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if words[:1] == ["serve"] and len(words) > 2:  # a program's process (`program_command`): the rest is the server's
        serve(Path(words[1]), words[2:])
        return 0
    parser = argparse.ArgumentParser(
        prog="stack.py", description="resumebot on this computer: Postgres (unless SQLite), the app and the lanes.")
    parser.add_argument(
        "--data", help=f"the data folder (default {DATA_VARIABLE}, else this instance's own folder)")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("setup", help="the data folder, the Python environment, the page")
    one.add_argument("--dev", action="store_true", help="install the test tools too (requirements-dev.lock)")
    one.add_argument("--no-page", action="store_true", help="do not build the page")
    for name, text in (("start", "start it in the background"), ("run", "start it in this window")):
        two = commands.add_parser(name, help=text)
        two.add_argument("--hold", action="store_true",
                         help="the scheduled searches off and no lanes (the engine still answers the page)")
        if name == "start":
            two.add_argument("--wait", type=float, default=300, help="seconds to wait for /health (default 300)")
    hidden = commands.add_parser("supervise", help=argparse.SUPPRESS)
    hidden.add_argument("--hold", action="store_true")
    commands.add_parser("stop", help="stop the lanes, the engine, the page and Postgres (if used)")
    commands.add_parser("status", help="what is running")
    commands.add_parser("owner-header", help="print the owner door's Authorization header as JSON (for .mcp.json)")
    three = commands.add_parser("smoke", help="read-only checks of the running app")
    three.add_argument("--lanes", action="store_true", help="check that the five lanes answer too")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    named = Path(args.data).expanduser() if args.data else None
    try:
        config = Config(named)
        if args.command == "setup":
            setup(config, args.dev, not args.no_page)
        elif args.command == "start":
            return start(config, args.hold, args.wait)
        elif args.command in ("run", "supervise"):
            return supervise(config, args.hold)
        elif args.command == "stop":
            return stop(config)
        elif args.command == "status":
            return status(config)
        elif args.command == "smoke":
            return smoke(config, args.lanes)
        elif args.command == "owner-header":
            return owner_header(config)
        return 0
    except Stop as problem:
        print(f"==> {problem}", file=sys.stderr, flush=True)
        note_failure((named or data_folder()) / "logs", args.command, str(problem))
        return problem.code
    except Exception:  # whatever else stops it is read from start.log too: pythonw prints it nowhere
        text = traceback.format_exc()
        print(text, file=sys.stderr, flush=True)
        note_failure((named or data_folder()) / "logs", args.command, text)
        return 1


if __name__ == "__main__":
    sys.exit(main())
