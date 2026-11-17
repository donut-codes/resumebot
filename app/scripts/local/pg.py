#!/usr/bin/env python3
r"""A local PostgreSQL 16 for resumebot on a Windows computer: no container, no installer, no Windows service and no
administrator rights. The server is a plain process of the signed-in user, it listens on 127.0.0.1 only, and everything
it owns is under the data folder (RESUMEBOT_DATA_DIR, else the user's own folder, as `default_data_folder` below says).

    py -3 app/scripts/local/pg.py fetch    download the official Postgres 16 programs (373 MB), check, keep what it uses
    py -3 app/scripts/local/pg.py init     make the cluster, the app's role and database, the password and the .env line
    py -3 app/scripts/local/pg.py start    start the server (only this machine can reach it)
    py -3 app/scripts/local/pg.py stop     stop it (a fast shutdown: sessions end, nothing is lost)
    py -3 app/scripts/local/pg.py status   is it up, which build, which address
    py -3 app/scripts/local/pg.py snapshot who is connected, what each waits on and which locks are held (read-only;
                                           the stack's supervisor writes one to the logs before it restarts the app)

`fetch` and `init` are run once, in that order; `start` and `stop` as often as you like. `init` is safe to run again:
it never touches data, it only makes what is missing and rewrites the settings it owns.

    <data folder>\.env                          the line JOBSEARCH_DATABASE_URL=postgresql+asyncpg://...@127.0.0.1:5433/resumebot
    <data folder>\pg\bin, lib, share            the programs (from `fetch`)
    <data folder>\pg\data                       the cluster; its log is data\log (one file per weekday, 7 at most)
    <data folder>\pg\resumebot-password.txt     the password of the role resumebot (made by `init`, read by no other)
    <data folder>\pg\startup.log                what the server said before its own log began

Choices, and why:
  The programs. EnterpriseDB's zip of the community server for Windows: the official build, no installer (an installer
  needs administrator rights and registers a service). It is pinned below by file name, size and the SHA-256 that
  EnterpriseDB publishes beside the file. The pgserver package on PyPI was not used: its newest Windows wheel (2024-06)
  holds Postgres 16.2, thirteen releases behind, and nobody has updated it since.
  The role and database. `resumebot` and `resumebot`, a superuser, so that a dump of
  an existing database restores as it is, and the tests can make the databases they need.
  The locale. UTF8 and the C collation. C sorts by
  code point, as SQLite's default does: 'A,B,Z,_x,a,c,z'. The one difference is SQL-side
  lower() and upper() of non-ASCII letters (C leaves them); the app folds case in Python. Time zone UTC, DateStyle ISO
  MDY and the English text search configuration match an existing database's.
  Light. 32 MB of shared buffers (a database of a few hundred MB; Windows caches the rest), no JIT, and a log that
  rotates by weekday so it never grows. Durability is left at its defaults (fsync on).
  Closed. Only 127.0.0.1 is listened on, and pg_hba.conf allows that address alone, with a password (SCRAM). The
  password and the .env are readable by this user alone.

Settings through the environment: RESUMEBOT_DATA_DIR (the data folder) and RESUMEBOT_PG_PORT (default 5433; 5432 is left
for a Postgres of its own). Python 3.9 or newer; the standard library only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

#: The build this installs: EnterpriseDB's zip of PostgreSQL 16.15 for Windows x64, by name, size and the SHA-256 that
#: EnterpriseDB publishes at ZIP_URL + ".sha256". A newer 16.x is a change of these lines and nothing else.
SERVER_VERSION = "16.15"
ZIP_NAME = "postgresql-16.15-5-windows-x64-binaries.zip"
ZIP_URL = "https://get.enterprisedb.com/postgresql/" + ZIP_NAME
ZIP_SIZE = 373_254_386
ZIP_SHA256 = "43bb45f173a6f08cf1d29a97a6d8deb119e8e8093a24c00d2d1001a0ccaa8281"
#: What of the zip is kept: the programs, their libraries and the server's own data files. The rest of its gigabyte is
#: pgAdmin, the headers and the documentation.
KEEP = ("bin", "lib", "share")

HOST = "127.0.0.1"
DEFAULT_PORT = 5433
#: The role and the database the app expects (JOBSEARCH_DATABASE_URL in the data folder's .env, which `init` writes).
ROLE = "resumebot"
DATABASE = "resumebot"
DATA_DIR_ENV = "RESUMEBOT_DATA_DIR"
PORT_ENV = "RESUMEBOT_PG_PORT"
#: The setting the app reads its database from (app/backend/app/settings.py).
URL_KEY = "JOBSEARCH_DATABASE_URL"
#: No console window for a program run from here, and none shared with the terminal this is run from: closing that
#: terminal must not end a server it started.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Who may connect: this machine, over IPv4, with a password. The file replaces the one initdb writes.
HBA = """# Written by app/scripts/local/pg.py init, which rewrites it on every run.
# This machine only, and always with a password.
host    all    all    127.0.0.1/32    scram-sha-256
"""
#: The line init adds to postgresql.conf, once: every setting this script owns is in the file it names.
INCLUDE = "include = 'resumebot.conf'"


class Stop(Exception):
    """Something to say in one plain sentence before stopping: `main` prints it and exits with `code`."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def say(text: str) -> None:
    print(f"==> {text}", flush=True)


# --- where things are ---------------------------------------------------------------------------------------------


class Layout:
    """The folders and the port. `root` is the data folder; the server and everything it owns is in `root\\pg`. A test
    hands its own `root`, `port` and, to share the programs, `bin_dir`."""

    def __init__(self, root: Path | None = None, port: int | None = None, bin_dir: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir()
        self.port = port or port_setting()
        self.pg = self.root / "pg"
        self.bin = Path(bin_dir) if bin_dir else self.pg / "bin"
        self.data = self.pg / "data"
        self.download = self.pg / "download"
        self.password_file = self.pg / "resumebot-password.txt"
        self.startup_log = self.pg / "startup.log"
        self.env_file = self.root / ".env"


def data_dir() -> Path:
    """The data folder: RESUMEBOT_DATA_DIR, else the user's own folder."""
    named = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(named) if named else default_data_folder()


def default_data_folder() -> Path:
    """Where an install keeps its data when nothing names a folder: the user's own, per platform. The same folder as
    stack.py's `default_data_folder` and `resumebot init` make."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "resumebot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "resumebot"


def port_setting() -> int:
    raw = os.environ.get(PORT_ENV, "").strip() or str(DEFAULT_PORT)
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise Stop(f"{PORT_ENV} must be a port from 1024 to 65535, not '{raw}'", code=2)
    return int(raw)


def require_windows() -> None:
    if sys.platform != "win32":
        raise Stop("this sets up Postgres on a Windows computer (it is not for other systems)", code=2)


# --- small helpers: processes, files, ports -------------------------------------------------------------------------


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without the PG* variables (a stray PGHOST or PGDATA would aim a program elsewhere)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    env.update(extra or {})
    return env


def program(layout: Layout, name: str) -> str:
    path = layout.bin / f"{name}.exe"
    if not path.exists():
        raise Stop(f"{path} is not there: run  py -3 app/scripts/local/pg.py fetch  first")
    return str(path)


def run(command: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a program to its end and keep what it printed. A failure stops with the end of what it said."""
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        env=env if env is not None else clean_env(), creationflags=NO_WINDOW,
    )
    if check and result.returncode != 0:
        said = (result.stderr or result.stdout).strip()[-1500:]
        raise Stop(f"{Path(command[0]).name} failed ({result.returncode}): {said}")
    return result


def write_text(path: Path, text: str) -> None:
    """Write a file with LF line endings (Postgres and the app both read them; Path.write_text cannot before 3.10)."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def user_sid() -> str:
    """The SID of the signed-in user, as `whoami` gives it: the name a permission is granted by."""
    out = run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout
    return next(csv.reader([out.strip()]))[1]


def only_me(path: Path) -> None:
    """Leave this user alone with a file or folder: inherited permissions are dropped, and a folder's contents get the
    same (new files made in it inherit it)."""
    grant = f"*{user_sid()}:" + ("(OI)(CI)F" if path.is_dir() else "F")
    run(["icacls", str(path), "/inheritance:r", "/grant:r", grant])


def port_problem(port: int) -> str | None:
    """Why a server cannot listen on this port, or None when it can: something answers there, or Windows refuses the
    port (a reserved range)."""
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((HOST, port)) == 0:
            return f"something is already listening on {HOST}:{port} (set {PORT_ENV} to use another port)"
    with socket.socket() as probe:
        try:
            probe.bind((HOST, port))
        except OSError as error:
            return f"{HOST}:{port} cannot be used ({error.strerror or error}); set {PORT_ENV} to use another port"
    return None


def log_tail(layout: Layout, lines: int = 12) -> str:
    """The end of the server's startup log and of its newest log file, for a message about a start that failed."""
    files = [layout.startup_log, *sorted((layout.data / "log").glob("*.log"), key=lambda p: p.stat().st_mtime)[-1:]]
    parts = []
    for file in files:
        if file.exists() and file.stat().st_size:
            text = file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
            parts.append(f"--- {file.name}\n" + "\n".join(text))
    return "\n".join(parts) or "(the server wrote no log)"


# --- the password, the address, the .env ------------------------------------------------------------------------


def password(layout: Layout) -> str:
    try:
        return layout.password_file.read_text(encoding="utf-8").strip()
    except OSError:
        raise Stop(f"{layout.password_file} is not there: run  py -3 app/scripts/local/pg.py init  first") from None


def database_url(layout: Layout, secret: str) -> str:
    """The address the app connects with. The password is URL-safe by construction (letters, digits, - and _)."""
    return f"postgresql+asyncpg://{ROLE}:{secret}@{HOST}:{layout.port}/{DATABASE}"


def redacted(url: str, secret: str) -> str:
    return url.replace(f":{secret}@", ":***@")


def set_env_line(path: Path, key: str, value: str) -> None:
    """Put `key=value` in a .env file: its other lines are kept as they are, an earlier line for the key is replaced."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{key}=")]
    write_text(path, "\n".join([*kept, f"{key}={value}"]) + "\n")


def server_conf(port: int) -> str:
    """The settings this script owns, in the file postgresql.conf includes. The values an existing database has, where
    they matter to the data (time zone, DateStyle, text search), light ones where the machine is the user's own."""
    return f"""# Written by app/scripts/local/pg.py init, which rewrites it on every run: change a setting there.
port = {port}
listen_addresses = '{HOST}'
max_connections = 100
shared_buffers = 32MB
maintenance_work_mem = 32MB
effective_cache_size = 1GB
jit = off
timezone = 'UTC'
log_timezone = 'UTC'
datestyle = 'iso, mdy'
default_text_search_config = 'pg_catalog.english'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%a.log'
log_truncate_on_rotation = on
log_rotation_age = 1d
log_rotation_size = 0
log_line_prefix = '%m [%p] %q%u@%d '
"""


# --- fetch: the programs ----------------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(target: Path) -> None:
    """Download the zip to `target`, kept only if it has the pinned size and checksum."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    say(f"downloading {ZIP_URL} ({ZIP_SIZE / 2**20:.0f} MiB)")
    digest, got, shown = hashlib.sha256(), 0, 0
    request = urllib.request.Request(ZIP_URL, headers={"User-Agent": "resumebot-local-pg"})
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            out.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if got * 10 // ZIP_SIZE > shown:
                shown = got * 10 // ZIP_SIZE
                say(f"  {shown * 10}%")
    if got != ZIP_SIZE or digest.hexdigest() != ZIP_SHA256:
        part.unlink()
        raise Stop(
            f"the download is not the file this script pins ({got} bytes, SHA-256 {digest.hexdigest()}); "
            "nothing was kept"
        )
    part.replace(target)


def unpack(archive: Path, layout: Layout) -> None:
    """Unpack the folders in KEEP into pg\\, through a folder of its own: only whole sets of them land in place."""
    staging = layout.pg / "unpack"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    wanted = tuple(f"pgsql/{name}/" for name in KEEP)
    with zipfile.ZipFile(archive) as zipped:
        members = [m for m in zipped.infolist() if m.filename.startswith(wanted)]
        if not members:
            raise Stop(f"{archive.name} holds no pgsql/bin, lib or share: not the file this script expects")
        for member in members:
            zipped.extract(member, staging)  # extract() drops drive letters and ".." parts of a name
    for name in KEEP:
        shutil.move(str(staging / "pgsql" / name), str(layout.pg / name))
    shutil.rmtree(staging)


def installed_version(layout: Layout) -> str:
    """What `postgres --version` says: 'postgres (PostgreSQL) 16.15'."""
    return run([program(layout, "postgres"), "--version"]).stdout.strip()


def fetch(layout: Layout) -> None:
    """Get the programs: download (unless the zip is already there and right), check, unpack, delete the zip."""
    if (layout.bin / "postgres.exe").exists():
        say(f"the programs are already in {layout.bin}: {installed_version(layout)}")
        return
    archive = layout.download / ZIP_NAME
    if archive.exists() and sha256_of(archive) == ZIP_SHA256:
        say(f"{archive.name} is already here and its SHA-256 matches")
    else:
        download(archive)
        say("SHA-256 and size match the ones EnterpriseDB publishes")
    say(f"unpacking {', '.join(KEEP)} into {layout.pg}")
    unpack(archive, layout)
    shutil.rmtree(layout.download)
    try:
        version = installed_version(layout)
    except Stop as problem:
        raise Stop(f"{problem}\nIf Windows names a missing DLL, the Microsoft Visual C++ 2015-2022 x64 runtime is "
                   "missing (it comes with most software).") from None
    if f"PostgreSQL) {SERVER_VERSION}" not in version:
        raise Stop(f"expected PostgreSQL {SERVER_VERSION}, got: {version}")
    say(f"{version} is in {layout.bin}")


# --- init: the cluster, the role and database, the password, the .env ----------------------------------------------


def has_cluster(layout: Layout) -> bool:
    return (layout.data / "PG_VERSION").exists()


def psql(layout: Layout, sql: str, database: str = "postgres", secret: str | None = None) -> str:
    """One statement as the app's role, over the loopback address. Never asks for a password; returns what it printed
    (unaligned, no headers)."""
    # PGCLIENTENCODING: psql answers in the console's code page unless told otherwise, and this reads UTF-8.
    env = clean_env({"PGPASSWORD": secret if secret is not None else password(layout), "PGCLIENTENCODING": "UTF8"})
    command = [program(layout, "psql"), "-h", HOST, "-p", str(layout.port), "-U", ROLE, "-d", database,
               "-X", "-q", "-w", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    return run(command, env=env).stdout.strip()


def init(layout: Layout) -> None:
    initdb = program(layout, "initdb")
    layout.pg.mkdir(parents=True, exist_ok=True)
    only_me(layout.pg)
    fresh = not has_cluster(layout)
    if not layout.password_file.exists():
        if not fresh:
            raise Stop(f"the cluster exists but {layout.password_file} does not, and a password cannot be recovered")
        write_text(layout.password_file, secrets.token_urlsafe(24) + "\n")
    if fresh:
        if layout.data.exists() and any(layout.data.iterdir()):
            raise Stop(f"{layout.data} is not empty and holds no cluster: move it away first")
        say(f"making the cluster in {layout.data}")
        layout.data.mkdir(exist_ok=True)
        run([initdb, "-D", str(layout.data), "-U", ROLE, "-E", "UTF8", "--locale=C",
             "--auth=scram-sha-256", f"--pwfile={layout.password_file}", "--no-instructions"])
    write_text(layout.data / "pg_hba.conf", HBA)
    write_text(layout.data / "resumebot.conf", server_conf(layout.port))
    main_conf = layout.data / "postgresql.conf"
    text = main_conf.read_text(encoding="utf-8")
    if INCLUDE not in text:
        write_text(main_conf, text.rstrip("\n") + "\n\n" + INCLUDE + "\n")
    was_up = running(layout)
    if not was_up:
        start(layout)
    try:
        if psql(layout, f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'") != "1":
            say(f"making the database {DATABASE}")
            psql(layout, f'CREATE DATABASE "{DATABASE}" OWNER "{ROLE}"')
    finally:
        if not was_up:
            stop(layout)
    url = database_url(layout, password(layout))
    set_env_line(layout.env_file, URL_KEY, url)
    only_me(layout.env_file)
    say(f"{URL_KEY} is in {layout.env_file}: {redacted(url, password(layout))}")
    if was_up:
        say("the server is running: its new settings apply at the next start")


# --- start, stop, status ----------------------------------------------------------------------------------------


def running(layout: Layout) -> bool:
    """True when a server runs on this cluster (pg_ctl status: 0 running, 3 not)."""
    return run([program(layout, "pg_ctl"), "status", "-D", str(layout.data)], check=False).returncode == 0


def start(layout: Layout) -> None:
    if not has_cluster(layout):
        raise Stop(f"there is no cluster in {layout.data}: run  py -3 app/scripts/local/pg.py init  first")
    if running(layout):
        say(f"the server is already running on {HOST}:{layout.port}")
        return
    problem = port_problem(layout.port)
    if problem:
        raise Stop(problem)
    # No pipe to the server: it inherits the standard handles, and a pipe it holds would keep this from ever returning.
    started = subprocess.run(
        [program(layout, "pg_ctl"), "start", "-D", str(layout.data), "-w", "-t", "60", "-l", str(layout.startup_log)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env(),
        creationflags=NO_WINDOW,
    )
    if started.returncode != 0:
        raise Stop(f"the server did not start:\n{log_tail(layout)}")
    say(f"the server is running on {HOST}:{layout.port}")


def stop(layout: Layout) -> None:
    if not has_cluster(layout) or not running(layout):
        say("the server is not running")
        return
    run([program(layout, "pg_ctl"), "stop", "-D", str(layout.data), "-m", "fast", "-w", "-t", "60"])
    say("the server is stopped")


def status(layout: Layout) -> int:
    """Print where things stand; the exit status is pg_ctl's: 0 up, 3 stopped (4: no cluster yet)."""
    try:
        say(f"build: {installed_version(layout)}")
    except Stop:
        say("build: not fetched yet (py -3 app/scripts/local/pg.py fetch)")
        return 4
    say(f"data:  {layout.data}")
    if not has_cluster(layout):
        say("no cluster yet (py -3 app/scripts/local/pg.py init)")
        return 4
    if not running(layout):
        say("stopped")
        return 3
    pid = (layout.data / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0]
    ready = run([program(layout, "pg_isready"), "-h", HOST, "-p", str(layout.port)], check=False)
    say(f"running, pid {pid}, {HOST}:{layout.port}: {ready.stdout.strip() or ready.stderr.strip()}")
    return 0 if ready.returncode == 0 else 1


#: What `snapshot` asks: every session (what it runs, how long its transaction and statement have run, what it waits on
#: and which sessions block it), then every lock (the ungranted first), with the table it is on.
SNAPSHOT_SQL = (
    "SELECT pid, usename, application_name, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blocked_by, "
    "date_trunc('second', now() - xact_start) AS in_transaction, "
    "date_trunc('second', now() - query_start) AS in_query, "
    "left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query "
    "FROM pg_stat_activity WHERE datname IS NOT NULL AND pid <> pg_backend_pid() ORDER BY xact_start NULLS LAST",
    "SELECT l.pid, l.locktype, l.mode, l.granted, c.relname, l.page, l.tuple, l.transactionid "
    "FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation WHERE l.pid <> pg_backend_pid() "
    "ORDER BY l.granted, l.pid, c.relname",
)


def snapshot(layout: Layout) -> int:
    """Print who is connected to the app's database, what each session runs and waits on, which sessions block it, and
    every lock held or waited for. Read-only, as the app's role, each query cut off after 10 seconds; what the
    supervisor keeps when the app stops answering, so the cause can be read afterwards (stack.py)."""
    env = clean_env({"PGPASSWORD": password(layout), "PGCLIENTENCODING": "UTF8",
                     "PGOPTIONS": "-c statement_timeout=10000 -c lock_timeout=2000"})
    command = [program(layout, "psql"), "-h", HOST, "-p", str(layout.port), "-U", ROLE, "-d", DATABASE, "-X", "-w",
               "-P", "pager=off"]
    for sql in SNAPSHOT_SQL:
        command += ["-c", sql]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW, timeout=60)
    print(result.stdout, end="", flush=True)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr, flush=True)
    return result.returncode


COMMANDS = {"fetch": fetch, "init": init, "start": start, "stop": stop, "status": status, "snapshot": snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pg.py", description="A local PostgreSQL 16 for resumebot on Windows.")
    parser.add_argument("command", choices=list(COMMANDS), help="fetch, init, start, stop, status or snapshot")
    args = parser.parse_args(argv)
    try:
        require_windows()
        return COMMANDS[args.command](Layout()) or 0
    except Stop as problem:
        print(f"==> {problem}", file=sys.stderr, flush=True)
        return problem.code


if __name__ == "__main__":
    sys.exit(main())
