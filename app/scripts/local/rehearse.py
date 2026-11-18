#!/usr/bin/env python3
r"""The install rehearsal: the install script (install.py) run end to end on a scratch copy of a PC, again and again.

    py -3 app/scripts/dev.py rehearse [SCENARIO...] [--slow]      (the whole check runs the fast tier)

What is real: install.py itself, git (cloning from a local bare repository holding two tags), the `resumebot` commands
(init, connect, shortcut, doctor, status and signin's token rules, run in this process), SQLite and the files. What is
replaced, so nothing outside the scenario's own folder is touched: the registry, the scheduled task and the shortcut
writer (one recorder behind `setup_cli.PLATFORM`), Claude Code (`rehearsal/fake_claude.py`, kept in a JSON file in the
scenario's own CLAUDE_CONFIG_DIR), the sign-in page (a local stub), the app itself (a stub answering /health and the
two doors on ports from a free range, never the owner's), `uv`, `node`, the venv and the stack program (stand-ins).
Each scenario has its own HOME, USERPROFILE, LOCALAPPDATA, APPDATA, CLAUDE_CONFIG_DIR and temp folder under the system
temp folder. No window opens (every child is started without one), and a program that is not on the short list
(`REAL_PROGRAMS`) fails the scenario. Every process this starts is kept by its handle and killed at the end, even on
failure.

Scenarios are data: `rehearsal/*.json`, each {name, tier ("fast" or "slow"), about, root, programs, claude, env, steps}.
A step is one of {"run": {...}} (the install script), {"op": ...} (`OPS` below) or {"check": ...} (`CHECKS` below).
Assertions are on files, recorded calls and exit codes, never on timing. A leak scanner fails any scenario whose output,
logs or status file hold a token-shaped string or one of the scenario's own secrets.

Not covered, because it needs the real thing (the release's own rehearsal on a real PC keeps it): the real `claude`, a
real venv and pip, Task Scheduler, winget and UAC, the desktop app's restart, a port clash through stack.py, and two
sign-ins at once.
"""

import argparse
import contextlib
import glob
import hashlib
import importlib.util
import io
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
APP = HERE.parents[1]
BACKEND = APP / "backend"
SCENARIO_FOLDER = HERE / "rehearsal"
FAKE_CLAUDE = SCENARIO_FOLDER / "fake_claude.py"
WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if WINDOWS else 0

#: The owner's own ports: the page, the engine, the five lanes. A scenario never binds or asks any of them.
FORBIDDEN_PORTS = frozenset({8430, 8530, *range(8651, 8656)})
#: The range the stand-in app's ports come from. It is a block no other stage uses: the runner tests (app/scout) draw
#: from 20000 to 39999, the stack-setup tests from 41000 to 47999 (and up to 1004 more above a page port), the app's
#: smoke fakes fall back to 20000 to 49000 only when their own pick fails, and the system's own range starts at 49152.
#: It held 24000 to 29500 once, inside the runner tests' range, which run beside it in `dev.py check`.
PORT_RANGE = (40000, 40899)
PORT_FOLDER = Path(tempfile.gettempdir()) / "resumebot-rehearsal-ports"  #: one lock file per claimed port
ENGINE_OFFSET = 100
OLD_TAG, OLD_VERSION = "app-v0.9.0", "0.9.0"
OWNER_NAME = "Rehearsal Owner"
ZONE = "America/New_York"
#: Programs a scenario may really start. Anything else (the real claude, pythonw, a browser) fails it.
REAL_PROGRAMS = frozenset({"git", "tasklist"})
#: What a snapshot or an end state leaves out: the script's own record, its lock, logs, backups and git's folder.
VOLATILE = ("install", "run", "logs", "backups", ".git")

sys.path.insert(0, str(BACKEND))
# the vendored engine, as dev.py's backend_env has it
sys.path.insert(1, str(APP / "packages" / "process-engine" / "src"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inst = load_module("install_script_rehearsal", HERE / "install.py")


#: A database `create_database` made once for the whole run: each scenario's init copies it instead of building the
#: schema again (the seed, the secrets and the settings file are still the real init's).
TEMPLATE: dict[str, Path] = {}  #: "base": the folder to make it in (set by `main`); "file": the finished copy


def setup_cli():
    import app.setup_cli as cli  # not at the top: it needs the backend's packages

    return cli


def app_version() -> str:
    found = re.search(r'^VERSION\s*=\s*"([^"]+)"', (BACKEND / "app" / "settings.py").read_text(encoding="utf-8"), re.M)
    if not found:
        raise RuntimeError("no VERSION in app/settings.py")
    return found.group(1)


# --- the leak scanner -------------------------------------------------------------------------------------------------

TOKEN_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}|\b[0-9a-f]{64}\b")


def leaks(text: str, secrets: list[str]) -> list[str]:
    """What in `text` is shaped like a token or is one of the scenario's own secrets (described, never shown)."""
    found = [f"a token-shaped string ({len(match)} characters)" for match in TOKEN_SHAPE.findall(text)]
    found += [f"a secret's own value ({len(secret)} characters)" for secret in secrets if secret and secret in text]
    return found


# --- ports ------------------------------------------------------------------------------------------------------------


def can_bind(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


#: The ports this process holds a claim on (port -> its open, locked file). A claim lasts until `release_ports`, or
#: until the process ends: the operating system drops a lock with its process, so a killed run leaves none behind.
CLAIMS: dict[int, io.BufferedRandom] = {}


def claim(port: int) -> bool:
    """Take the one lock on `port` that every rehearsal process asks for first. A port is bound some seconds after it is
    picked (the stand-in app starts mid-scenario), so a bind test alone lets two scenarios running at once, in this
    process's tests and in `dev.py rehearse`, pick the same pair; only one of them gets the claim."""
    if port in CLAIMS:
        return False
    PORT_FOLDER.mkdir(parents=True, exist_ok=True)
    handle = open(PORT_FOLDER / f"{port}.lock", "a+b")
    try:
        handle.seek(0)
        if WINDOWS:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    CLAIMS[port] = handle
    return True


def release_ports(page: int) -> None:
    """Give back the claim on a page port and its engine port."""
    for port in (page, page + ENGINE_OFFSET):
        handle = CLAIMS.pop(port, None)
        if handle is not None:
            handle.close()


def free_ports(low: int = PORT_RANGE[0], high: int = PORT_RANGE[1]) -> int:
    """A page port whose engine port (plus 100) is free and claimed by this process alone; neither is one of the
    owner's. The caller gives it back with `release_ports`."""
    for _ in range(500):
        page = random.randint(low, high)
        pair = (page, page + ENGINE_OFFSET)
        if not FORBIDDEN_PORTS.isdisjoint(pair) or not claim(pair[0]):
            continue
        if claim(pair[1]) and all(can_bind(port) for port in pair):
            return page
        release_ports(page)
    raise RuntimeError("no free port pair")


def guard_port(url: str) -> None:
    found = re.search(r":(\d+)(?:/|$)", url)
    if found and int(found.group(1)) in FORBIDDEN_PORTS:
        raise AssertionError(f"a request to the owner's own port: {url}")


# --- the stand-in app and the other local servers --------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    # On Windows a reusable address lets a second server share a port that is in use. Off Windows a port whose listener
    # has just closed stays reserved for a while, and a plain bind of it fails though nothing holds it (as stack.py's
    # `port_is_free` says), so there the address is reusable: a port something still listens on is still refused.
    allow_reuse_address = not WINDOWS


class Stub:
    """Two servers (the page's and the engine's) that answer /health as the app does, and each door's tools/list when
    the right token comes. The data folder's token files are read at each request, so a replaced secret is honoured.
    The same class serves the sign-in page (`/signin`)."""

    APP_NAME = "resumebot"

    def __init__(self, data: Path) -> None:
        self.data = data
        self.servers: list[ThreadingHTTPServer] = []
        self.version = ""
        self.token = "sk-ant-oat01-" + "S1gn1nStub" * 8

    @property
    def up(self) -> bool:
        return bool(self.servers)

    def start(self, page: int, version: str) -> None:
        if self.servers:
            return
        self.version = version
        made: list[ThreadingHTTPServer] = []
        try:
            for port, door, token_file, tool in ((page, "/mcp-owner/", "owner-token", "act.do"),
                                                 (page + ENGINE_OFFSET, "/mcp-browser/", "browser-token",
                                                  "browser.next_item")):
                made.append(self.serve(port, door, token_file, tool))
        except OSError:
            for server in made:
                self.close(server)
            raise
        self.servers = made

    def serve(self, port: int, door: str, token_file: str, tool: str) -> ThreadingHTTPServer:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def reply(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self.reply(200, {"app": stub.APP_NAME, "status": "ok", "db": "ok", "version": stub.version})
                elif self.path == "/signin":  # the local sign-in page: it hands the token over once "approved"
                    self.reply(200, {"token": stub.token})
                else:
                    self.reply(404, {"detail": "not here"})

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    wanted = (stub.data / "secrets" / token_file).read_text(encoding="utf-8-sig").strip()
                except OSError:
                    wanted = None
                if self.path != door:
                    self.reply(404, {"detail": "no such door"})
                elif wanted is None or self.headers.get("Authorization") != f"Bearer {wanted}":
                    self.reply(401, {"detail": "token refused"})
                else:
                    self.reply(200, {"result": {"tools": [{"name": tool}]}})

        server = Server(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def close(server: ThreadingHTTPServer) -> None:
        server.shutdown()
        server.server_close()

    def stop(self) -> None:
        for server in self.servers:
            self.close(server)
        self.servers = []


class Foreign:
    """Another program on the app's port: it answers 200 to everything and is not the app."""

    def __init__(self, port: int) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                raw = b'{"status": "ok", "hello": "not the app"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = Server(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        Stub.close(self.server)


# --- the local repository: two tags -----------------------------------------------------------------------------------


def git_program() -> str:
    found = shutil.which("git")
    if not found:
        raise RuntimeError("git is not on the PATH: the rehearsal clones from a local repository")
    program = Path(found)
    wrapper = None
    if program.parent.name == "bin" and len(program.parents) > 2:
        wrapper = program.parents[2] / "cmd" / "git.exe"
    return str(wrapper) if wrapper and wrapper.is_file() else found  # Git for Windows' own front program sets git up


def git(*words: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run([git_program(), *words], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(words[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def build_repositories(base: Path, newest_version: str) -> dict[str, str]:
    """`both` (the old tag and the newest) and `old` (the old tag alone), as file addresses."""
    work = base / "repo-work"
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=work)
    identity = ["-c", "user.name=rehearsal", "-c", "user.email=rehearsal@example.invalid"]
    old_commit = ""
    for version, tag in ((OLD_VERSION, OLD_TAG), (newest_version, f"app-v{newest_version}")):
        files = {"app/scripts/local/stack.py": "# a stand-in: the rehearsal answers for it\n",
                 "app/backend/requirements.lock": f"locked {version}\n", "app/frontend/package-lock.json": "{}\n",
                 "VERSION": version}
        for name, text in files.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text, encoding="utf-8")
        git("add", "-A", cwd=work)
        git(*identity, "commit", "-q", "-m", f"release {version}", cwd=work)
        git("tag", tag, cwd=work)
        old_commit = old_commit or git("rev-parse", "HEAD", cwd=work).strip()
    both, old = base / "both.git", base / "old.git"
    git("clone", "-q", "--bare", work.as_uri(), str(both))
    git("clone", "-q", "--bare", "--no-tags", work.as_uri(), str(old))
    git("tag", OLD_TAG, old_commit, cwd=old)
    return {"both": both.as_uri(), "old": old.as_uri()}


# --- the platform recorder --------------------------------------------------------------------------------------------


class Recorder:
    """Put in `setup_cli.PLATFORM`: nothing real is asked or changed. The scheduled-task and shortcut scripts are
    recorded as text (never run); the shortcut one is read for its target and arguments, so the later look at the
    shortcut has something to read. The Claude lookup walks the PATH as the real one does and records its answer."""

    def __init__(self, world: "World") -> None:
        self.world = world

    def log(self, kind: str, **fields) -> None:
        with (self.world.recorded / f"{kind}.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(fields) + "\n")

    def zone_name(self) -> str | None:
        return "Eastern Standard Time"

    def set_user_variable(self, name: str, value: str) -> None:
        self.log("registry", name=name, value=value)

    def powershell(self, script: str):
        self.log("powershell", script=script)
        link = self.world.recorded / "shortcut.json"
        text = ""

        def quoted(field: str) -> str:
            found = re.search(r"\$link\." + field + r" = '((?:[^']|'')*)'", script)
            return found.group(1).replace("''", "'") if found else ""

        tasks = self.world.recorded / "tasks.json"
        held_tasks = read_json(tasks)
        asked = re.search(r"-TaskName '((?:[^']|'')*)'", script)
        name = asked.group(1).replace("''", "'") if asked else ""
        if "Register-ScheduledTask" in script:
            argument = re.search(r"-Argument '((?:[^']|'')*)'", script)
            held_tasks[name] = argument.group(1).replace("''", "'") if argument else ""
            tasks.write_text(json.dumps(held_tasks), encoding="utf-8")
        elif "Get-ScheduledTask" in script:
            text = f"TASK {held_tasks[name]}\n" if name in held_tasks else ""
        if "CreateShortcut($new)" in script:
            link.write_text(json.dumps({"target": quoted("TargetPath"), "arguments": quoted("Arguments")}),
                            encoding="utf-8")
            text = f"SHORTCUT {self.world.root / 'Desktop' / 'resumebot.lnk'}\n"
        elif "CreateShortcut($p)" in script:
            try:
                held = json.loads(link.read_text(encoding="utf-8"))
                text = f"LINK {held['target']}\t{held['arguments']}\n"
            except (OSError, ValueError):
                text = "NOLINK\n"
        return subprocess.CompletedProcess(["powershell"], 0, stdout=text, stderr="")

    def find_claude(self) -> str | None:
        cli = setup_cli()
        names = ("claude.cmd", "claude.exe", "claude") if WINDOWS else ("claude",)
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            for name in names:
                candidate = Path(folder) / name
                if folder and candidate.is_file() and cli._claude_runs(str(candidate)):
                    self.log("claude", used=str(candidate))
                    return str(candidate)
        self.log("claude", used=None)
        return None

    def shortcut_problem(self, install) -> str | None:
        return setup_cli()._shortcut_problem(install)


# --- one scenario's world ---------------------------------------------------------------------------------------------


class Killed(BaseException):
    """The runner ends the script after a step, as a killed process would: nothing after it runs."""


def shim(folder: Path, name: str, windows_text: str, posix_text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        path = folder / f"{name}.cmd"
        path.write_text(windows_text, encoding="utf-8")
    else:
        path = folder / name
        path.write_text(posix_text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


ODD_ROOTS = {"plain": "root", "space": "a folder with spaces",
             "unicode": "Пользователь № 1"}


class World:
    def __init__(self, base: Path, spec: dict, repositories: dict[str, str], newest_version: str) -> None:
        self.spec, self.repositories, self.newest_version = spec, repositories, newest_version
        self.base = base
        self.root = base / ODD_ROOTS[spec.get("root", "plain")]
        self.home, self.local, self.roaming = self.root / "home", self.root / "local", self.root / "roaming"
        self.claude_dir, self.tmp = self.root / "claude", self.root / "tmp"
        self.data, self.code = self.root / "data", self.root / "Programs" / "resumebot"
        self.bootstrap, self.recorded = self.root / "boot", self.root / "recorded"
        for folder in (self.home, self.local, self.roaming, self.claude_dir, self.tmp, self.recorded):
            folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            # On a Mac the shortcut step is the app's own and writes into the home's Desktop folder, which every real
            # account has; on Windows the step is PowerShell, which the rehearsal stands in for.
            (self.home / "Desktop").mkdir(exist_ok=True)
        self.page = free_ports()
        self.stub = Stub(self.data)
        self.foreign: Foreign | None = None
        self.calls: list[list[str]] = []
        self.outputs: list[str] = []
        self.lines: list[str] = []
        self.counts = {"signin_runs": 0}
        self.flags = {"stop_fails": False, "venv_broken": False}
        self.snapshots: dict[str, dict[str, str]] = {}
        self.pids: list[subprocess.Popen] = []
        self.connections: list[sqlite3.Connection] = []
        self.registry_dirs: list[Path] = []
        self.path_dirs: list[Path] = []
        self.kill_after: str | None = None
        self.last_rc: int | None = None
        self.failures: list[str] = []
        self.build_programs()
        self.host = Host(self)

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.page}"

    def build_programs(self) -> None:
        placed = {"node": "path", "uv": "path", **self.spec.get("programs", {})}
        bin_dir = self.base / "bin"
        registry_bin = self.base / "ProgramFiles" / "tools"
        for name, where in placed.items():
            if where != "missing":
                said = "v22.4.0" if name == "node" else f"rehearsal {name}"
                shim(registry_bin if where == "registry" else bin_dir, name, f"@echo off\r\necho {said}\r\n",
                     f"#!/bin/sh\necho {said}\n")
        if "registry" in placed.values():
            self.registry_dirs.append(registry_bin)
        for index, copy in enumerate(self.spec.get("claude", ["working"])):
            folder = self.base / f"claude-bin-{index}"
            flag = "--broken " if copy == "broken" else ""
            shim(folder, "claude", f'@"{sys.executable}" "{FAKE_CLAUDE}" {flag}%*\r\n',
                 f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" {flag}"$@"\n')
            self.path_dirs.append(folder)
        system = Path(os.environ.get("SystemRoot", "C:/Windows"))  # git and tasklist need the system's own folders
        self.path_dirs += [bin_dir, Path(git_program()).parent, *([system / "System32", system] if WINDOWS else [])]

    def path_text(self) -> str:
        return os.pathsep.join(str(folder) for folder in self.path_dirs)

    def environment(self) -> dict[str, str | None]:
        env: dict[str, str | None] = {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.local),
            "APPDATA": str(self.roaming), "XDG_DATA_HOME": str(self.local), "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "TEMP": str(self.tmp), "TMP": str(self.tmp), "PATH": self.path_text(), "PYTHONUTF8": "1"}
        for name in os.environ:
            if name.startswith(("RESUMEBOT_", "JOBSEARCH_", "SCOUT_")) or name.upper() in (
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                env[name] = None
        env.update(self.spec.get("env", {}))
        return env

    def say(self, text: str) -> None:
        self.lines.append(text)

    def secrets(self) -> list[str]:
        found = [self.stub.token]
        folder = self.data / "secrets"
        for path in folder.glob("*") if folder.exists() else []:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                text = path.read_text(encoding="utf-8-sig").strip()
                if len(text) >= 32:
                    found.append(text)
        return found

    def close(self) -> None:
        for connection in self.connections:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
        if self.foreign:
            self.foreign.stop()
        self.stub.stop()
        release_ports(self.page)
        for process in self.pids:  # only the processes this started, by their own handles
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.kill()
                process.wait(timeout=10)


class Host(inst.Host):
    """The machine as the script sees it: only git and tasklist are real. Everything else is answered here."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world
        self.path = world.path_text()

    def registry_path(self) -> list[str]:
        return [str(folder) for folder in self.world.registry_dirs]

    def registry_user_variable(self, name: str) -> str | None:
        return None

    def sleep(self, seconds: float) -> None:
        pass

    def health(self, url: str) -> dict | None:
        guard_port(url)
        if not (self.world.stub.up or self.world.foreign):  # nothing listens: Windows takes 2 s to say so
            return None
        return super().health(url)

    # -- programs --

    def run(self, argv, cwd=None, timeout=600):
        argv = [str(part) for part in argv]
        world = self.world
        world.calls.append(argv)
        name = Path(argv[0]).stem.lower()
        if name in REAL_PROGRAMS:
            done = super().run([git_program() if name == "git" else argv[0], *argv[1:]], cwd=cwd, timeout=timeout)
            world.outputs.append(done.out)
            return done
        if name == "node":
            return inst.Result(0, "v22.4.0\n")
        if name == "uv":
            return inst.Result(0, f"{sys.executable}\n" if "find" in argv else "")
        if len(argv) > 1 and argv[1].endswith("stack.py"):
            return self.stack(argv)
        if Path(argv[0]) == inst.venv_python(world.code):
            return inst.Result(1, "No module named pip\n") if world.flags["venv_broken"] else inst.Result(0, "3.51.3\n")
        if Path(argv[0]) == inst.resumebot_program(world.code):
            done = self.app(argv[1:])
            world.outputs.append(done.out)
            return done
        raise AssertionError(f"a program the rehearsal does not allow was started: {argv}")

    def stack(self, argv: list[str]):
        world = self.world
        word = next(part for part in reversed(argv) if not part.startswith("--"))
        if word == "setup":
            for path in (inst.venv_python(world.code), inst.resumebot_program(world.code)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("a stand-in\n", encoding="utf-8")
        elif word == "stop" and not world.flags["stop_fails"]:
            world.stub.stop()
        return inst.Result(0, "")

    def installed_version(self) -> str:
        found = inst.Host.run(self, [git_program(), "-C", str(self.world.code), "describe", "--tags", "--exact-match"],
                              timeout=60)
        return ".".join(re.findall(r"\d+", found.out))

    def sign_in(self) -> "inst.Result":
        """A sign-in that needs the user: the stand-in page (a local server) hands over a token."""
        world = self.world
        world.counts["signin_runs"] += 1
        page = Stub(world.data)
        port = free_ports()
        page.start(port, "signin")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/signin", timeout=10) as reply:
                token = json.loads(reply.read())["token"]
        finally:
            page.stop()
            release_ports(port)
        file = world.data / "secrets" / "claude-oauth-token"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(token, encoding="utf-8")
        return inst.Result(0, "signed in (the rehearsal's sign-in page)\n")

    def app(self, words: list[str]):
        world = self.world
        verb = words[0]
        if verb == "start":
            try:
                world.stub.start(world.page, self.installed_version())
            except OSError as error:
                return inst.Result(1, f"the app could not start: {error.strerror or error}\n")
            return inst.Result(0, f"started at {world.app_url}\n")
        if verb == "smoke":
            return inst.Result(0, "ok\n") if world.stub.up else inst.Result(1, "FAIL nothing answers\n")
        if verb == "signin":
            cli = setup_cli()
            if cli.token_is_stored(cli.Install(world.data)):
                return self.in_process(words)  # the real command skips a good token
            return self.sign_in()
        if verb == "init" and "--port" not in words:
            words = [*words, "--port", str(world.page)]  # never the owner's default port
        return self.in_process(words)

    def in_process(self, words: list[str]):
        cli = setup_cli()  # the commands themselves, not main_cli (which also loads the Postgres copier's packages)
        command = {"init": cli.init, "connect": cli.connect, "shortcut": cli.shortcut, "signin": cli.signin,
                   "doctor": cli.doctor, "status": cli.status}[words[0]]
        os.environ["PATH"] = self.path
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                code = command(words[1:])
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 2
            except Exception:
                traceback.print_exc()
                code = 1
        return inst.Result(int(code or 0), buffer.getvalue())


# --- operations and checks --------------------------------------------------------------------------------------------


def fill(world: World, text: str) -> str:
    return (text.replace("{data}", str(world.data)).replace("{code}", str(world.code))
            .replace("{root}", str(world.root))
            .replace("{port}", str(world.page)).replace("{app}", world.app_url)
            .replace("{engine}", f"http://127.0.0.1:{world.page + ENGINE_OFFSET}")
            .replace("{version}", world.newest_version))


def op_file(world: World, step: dict) -> None:
    path = Path(fill(world, step["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if "hex" in step:
        path.write_bytes(bytes.fromhex(step["hex"]))
    else:
        path.write_bytes(fill(world, step.get("text", "")).encode(step.get("encoding", "utf-8")))


def op_claude_behavior(world: World, step: dict) -> None:
    behavior = json.loads(fill(world, json.dumps(step["behavior"])))
    (world.claude_dir / "fake-claude-behavior.json").write_text(json.dumps(behavior), encoding="utf-8")


def op_claude_door(world: World, step: dict) -> None:
    config = world.claude_dir / ".claude.json"
    try:
        held = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = {}
    token = step.get("token", "")
    if token.startswith("secret:"):
        token = (world.data / "secrets" / token[7:]).read_text(encoding="utf-8-sig").strip()
    held.setdefault("mcpServers", {})[step["name"]] = {"url": fill(world, step["url"]),
                                                       "authorization": f"Bearer {token}"}
    config.write_text(json.dumps(held), encoding="utf-8")


def op_empty_database(world: World, step: dict) -> None:
    cli = setup_cli()
    world.data.mkdir(parents=True, exist_ok=True)
    cli.create_database(cli.database_url(world.data))


def op_old_install(world: World, step: dict) -> None:
    """An install of the older release already in place: its code, its settings, its data, and (unless `running` is
    false) the app answering."""
    git("clone", "-q", "--depth", "1", "--branch", OLD_TAG, world.repositories["old"], str(world.code))
    world.host.stack(["x", "stack.py", "--data", str(world.data), "setup"])
    done = world.host.app(["init", "--owner", OWNER_NAME, "--timezone", ZONE, "--data", str(world.data)])
    if done.rc != 0:
        raise AssertionError(f"the old install's init failed: {done.out[-300:]}")
    if step.get("running", True):
        world.stub.start(world.page, OLD_VERSION)


def op_hold_wal(world: World, step: dict) -> None:
    """A live-style database: WAL mode, a row written and left in the log by a connection that stays open."""
    connection = sqlite3.connect(world.data / "resumebot.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS rh_probe (n INTEGER)")
    connection.execute("INSERT INTO rh_probe VALUES (1)")
    connection.commit()
    world.connections.append(connection)


def op_stop_app(world: World, step: dict) -> None:
    world.stub.stop()


def op_foreign(world: World, step: dict) -> None:
    world.foreign = Foreign(world.page)


def op_flag(world: World, step: dict) -> None:
    world.flags[step["flag"]] = step.get("value", True)


def op_live_lock(world: World, step: dict) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], creationflags=NO_WINDOW,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    world.pids.append(sleeper)
    lock = world.data / "run" / "install.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(sleeper.pid), encoding="utf-8")


def tree_hashes(world: World, skip: tuple[str, ...] = VOLATILE) -> dict[str, str]:
    seen = {}
    for folder in (world.code, world.data):
        for path in sorted(folder.rglob("*")) if folder.exists() else []:
            relative = path.relative_to(world.root).as_posix()
            if path.is_file() and not any(f"/{part}/" in f"/{relative}" for part in skip):
                seen[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def op_start_task(world: World, step: dict) -> None:
    """What `resumebot start` does to the machine-wide start task for an install whose data folder is `data` (the
    scenario's own when omitted): the task is registered through the recorder, under the name the install picks."""
    cli = setup_cli()
    data = Path(fill(world, step.get("data", "{data}")))
    program, arguments = cli.start_command(data)
    cli._register_task(program, arguments, cli.start_task_name(data))


def op_snapshot(world: World, step: dict) -> None:
    world.snapshots[step["label"]] = tree_hashes(world)


OPS: dict[str, Callable[[World, dict], None]] = {
    "file": op_file, "claude_behavior": op_claude_behavior, "claude_door": op_claude_door,
    "empty_database": op_empty_database, "old_install": op_old_install, "hold_wal": op_hold_wal,
    "stop_app": op_stop_app, "foreign": op_foreign, "flag": op_flag, "live_lock": op_live_lock,
    "snapshot": op_snapshot, "start_task": op_start_task,
}


def journal(world: World) -> dict:
    try:
        return json.loads((world.data / "install" / "install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def doors(world: World) -> dict[str, dict]:
    try:
        return json.loads((world.claude_dir / ".claude.json").read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError):
        return {}


def matches(world: World, text: str) -> list[Path]:
    path = fill(world, text)
    if "*" in path:
        return [Path(found) for found in sorted(glob.glob(path))]
    return [Path(path)] if Path(path).exists() else []


def check_exists(world, step):
    return [] if matches(world, step["path"]) else [f"{step['path']} is not there"]


def check_absent(world, step):
    return [f"{step['path']} is there"] if matches(world, step["path"]) else []


def check_steps(world, step):
    held = journal(world).get("steps", {})
    return [f"step {name} is {held.get(name, {}).get('state')} (wanted {state})"
            for name, state in step["expect"].items() if held.get(name, {}).get("state") != state]


def check_state(world, step):
    state = journal(world).get("state")
    return [] if state == step["equals"] else [f"install.json state is {state} (wanted {step['equals']})"]


def check_door(world, step):
    held = doors(world).get(step["name"])
    if held is None:
        return [f"{step['name']} is not registered"]
    problems = []
    if "url" in step and held["url"].rstrip("/") != fill(world, step["url"]).rstrip("/"):
        problems.append(f"{step['name']} is registered at {held['url']}")
    if "token" in step:
        secret = (world.data / "secrets" / step["token"]).read_text(encoding="utf-8-sig").strip()
        if held["authorization"] != f"Bearer {secret}":
            problems.append(f"{step['name']} holds a token that is not this install's {step['token']}")
    return problems


def check_sql(world, step):
    files = matches(world, step["db"])
    if not files:
        return [f"{step['db']} is not there"]
    connection = sqlite3.connect(f"{files[0].resolve().as_uri()}?mode=ro", uri=True)
    try:
        value = connection.execute(step["query"]).fetchone()[0]
    finally:
        connection.close()
    return [] if value == step["equals"] else [f"{step['query']} gave {value!r} (wanted {step['equals']!r})"]


def check_secrets(world, step):
    cli = setup_cli()
    whole = [name for name in cli.SECRET_FILES if cli.secret_is_whole(world.data / "secrets" / name)]
    return [] if len(whole) == step["whole"] else [f"{len(whole)} whole secrets (wanted {step['whole']})"]


def check_text(world, step):
    problems = []
    for path in matches(world, step["path"]) or [Path(fill(world, step["path"]))]:
        text = read_text(path).lower()
        problems += [f"{path.name} lacks {word!r}" for word in step.get("has", [])
                     if fill(world, word).lower() not in text]
        problems += [f"{path.name} holds {word!r}" for word in step.get("lacks", [])
                     if fill(world, word).lower() in text]
    return problems


def check_output(world, step):
    text = "\n".join(world.lines).lower()
    problems = [f"the output lacks {word!r}" for word in step.get("has", []) if fill(world, word).lower() not in text]
    return problems + [f"the output holds {word!r}" for word in step.get("lacks", [])
                       if fill(world, word).lower() in text]


def check_unchanged(world, step):
    before, now = world.snapshots[step["label"]], tree_hashes(world)
    changed = sorted(name for name in before.keys() | now.keys() if before.get(name) != now.get(name))
    return [f"changed since {step['label']}: {', '.join(changed[:6])}"] if changed else []


def recorded_scripts(world: World, kind: str) -> str:
    scripts = []
    for line in read_text(world.recorded / f"{kind}.jsonl").splitlines():
        with contextlib.suppress(ValueError):
            scripts.append(json.loads(line).get("script", ""))
    return "\n".join(scripts)


def check_recorded(world, step):
    text = recorded_scripts(world, step["kind"])
    return [f"no recorded {step['kind']} call holds {word!r}" for word in step["has"] if fill(world, word) not in text]


def check_task(world, step):
    """The recorded scheduled tasks: with `name`, that task's arguments must hold each of `has` and none of `lacks`
    (a missing task fails); with `count`, that many tasks are recorded."""
    tasks = read_json(world.recorded / "tasks.json")
    problems = []
    if "count" in step and len(tasks) != step["count"]:
        problems.append(f"{len(tasks)} tasks recorded (wanted {step['count']})")
    if "name" in step:
        if step["name"] not in tasks:
            return problems + [f"no task {step['name']!r} recorded"]
        text = tasks[step["name"]]
        problems += [f"task {step['name']} lacks {word!r}" for word in step.get("has", [])
                     if fill(world, word) not in text]
        problems += [f"task {step['name']} holds {word!r}" for word in step.get("lacks", [])
                     if fill(world, word) in text]
    return problems


def check_count(world, step):
    value = world.counts[step["what"]]
    return [] if value == step["equals"] else [f"{step['what']} is {value} (wanted {step['equals']})"]


def check_rc(world, step):
    return [] if world.last_rc == step["equals"] else [f"exit code {world.last_rc} (wanted {step['equals']})"]


CHECKS: dict[str, Callable[[World, dict], list[str]]] = {
    "exists": check_exists, "absent": check_absent, "steps": check_steps, "state": check_state, "door": check_door,
    "sql": check_sql, "secrets": check_secrets, "text": check_text, "output": check_output,
    "unchanged": check_unchanged,
    "recorded": check_recorded, "count": check_count, "task": check_task, "rc": check_rc,
}


def run_install(world: World, step: dict) -> None:
    owner = [] if step.get("owner", True) is None else ["--owner", OWNER_NAME]
    repository = world.repositories[step.get("repo", "both")]
    args = ["--data", str(world.data), "--code", str(world.code), "--repo", repository,
            "--bootstrap", str(world.bootstrap), "--no-reexec", "--timezone", ZONE, *owner,
            *[fill(world, word) for word in step.get("args", [])]]
    world.lines = []
    original = inst.Journal.step

    def step_then_maybe_die(self, name, state, text):
        original(self, name, state, text)
        if world.kill_after == name and state in ("done", "already", "skipped"):
            raise Killed(name)

    try:
        with mock.patch.object(inst.Journal, "step", step_then_maybe_die):
            world.last_rc = inst.main(args, host=world.host, out=world.say)
    except Killed:
        world.last_rc = None
        (world.data / "run").mkdir(parents=True, exist_ok=True)
        (world.data / "run" / "install.lock").write_text("2147483646", encoding="utf-8")  # a killed run leaves its lock
    world.outputs.append("\n".join(world.lines))
    expected = step.get("expect_rc")
    if expected is not None and world.last_rc != expected:
        world.failures.append(f"exit code {world.last_rc} (wanted {expected}); the end of the output: "
                              f"{' | '.join(world.lines[-4:])[:500]}")


def always_checks(world: World) -> list[str]:
    """What every scenario must satisfy: no traceback, no token in any output, log or record, the data folder kept."""
    problems = []
    texts = [*world.outputs, read_text(world.data / "install" / "install.log"),
             read_text(world.data / "install" / "install.json"),
             *[read_text(path) for path in world.recorded.glob("*.jsonl")]]
    secrets = world.secrets()
    for text in texts:
        if "Traceback (most recent call last)" in text:
            problems.append("a traceback reached an output or a log")
        problems += [f"leak: {found}" for found in leaks(text, secrets)]
    if world.calls and not world.data.exists():
        problems.append("the data folder is gone")
    return problems


@contextlib.contextmanager
def isolated(world: World):
    """This process, made into the scenario's PC: its environment, its temp folder, its platform, and the guards."""
    cli = setup_cli()
    wanted = world.environment()
    saved = {name: os.environ.get(name) for name in wanted}
    saved_tmp = tempfile.tempdir
    saved_cli = (cli.PLATFORM, cli._client, cli._quiet_popen, cli._launch, cli.create_database)

    def client():
        import httpx

        def refuse_owner_ports(request):
            guard_port(str(request.url))

        return httpx.Client(timeout=cli.HTTP_SECONDS, trust_env=False, event_hooks={"request": [refuse_owner_ports]})

    real_create = cli.create_database

    def create_database(url: str) -> bool:
        path = cli.database_file(url)
        if path is None or path.exists() or "base" not in TEMPLATE:
            return real_create(url)
        if "file" not in TEMPLATE:  # the first scenario that needs a database makes the one every other copies
            made = TEMPLATE["base"] / "resumebot.db"
            real_create(cli.database_url(TEMPLATE["base"]))
            TEMPLATE["file"] = made
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE["file"], path)
        return True

    def no_popen(*args, **kwargs):
        raise AssertionError("a program that outlives the command was started: the rehearsal starts none")

    try:
        for name, value in wanted.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
        tempfile.tempdir = str(world.tmp)
        cli.PLATFORM, cli._client, cli._quiet_popen, cli._launch = Recorder(world), client, no_popen, no_popen
        cli.create_database = create_database
        yield
    finally:
        cli.PLATFORM, cli._client, cli._quiet_popen, cli._launch, cli.create_database = saved_cli
        tempfile.tempdir = saved_tmp
        for name, value in saved.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)


def run_steps(world: World, steps: list[dict]) -> list[str]:
    problems: list[str] = []
    for step in steps:
        if "run" in step:
            run_install(world, step["run"])
        elif "op" in step:
            OPS[step["op"]](world, step)
        elif "check" in step:
            problems += [f"{step['check']}: {text}" for text in CHECKS[step["check"]](world, step)]
        else:
            problems.append(f"a step with no run, op or check: {step}")
    return problems + world.failures


def end_state(world: World) -> dict:
    return {"files": sorted(tree_hashes(world, (*VOLATILE, "-old-")).keys()),
            "doors": sorted((name, entry["url"].replace(str(world.page + ENGINE_OFFSET), "{engine}").replace(
                str(world.page), "{page}")) for name, entry in doors(world).items()),  # each world has its own ports
            "journal": journal(world).get("state")}


def run_interrupted(spec: dict, base: Path, repositories: dict[str, str], newest: str) -> list[str]:
    """The script is ended after each step in turn; the next run must reach the end state of a run that was never
    interrupted."""
    problems: list[str] = []
    reference_world = World(base / "reference", spec, repositories, newest)
    try:
        with isolated(reference_world):
            run_install(reference_world, {"expect_rc": 0})
            reference = end_state(reference_world)
            problems += reference_world.failures
    finally:
        reference_world.close()
    for name in inst.STEPS:
        world = World(base / f"after-{name}", spec, repositories, newest)
        try:
            with isolated(world):
                world.kill_after = name
                run_install(world, {})
                world.kill_after = None
                run_install(world, {"expect_rc": 0})
                got = end_state(world)
                problems += [f"after {name}: {text}" for text in [*world.failures, *always_checks(world)]]
                if got["files"] != reference["files"] or got["doors"] != reference["doors"] or got["journal"] != "done":
                    differ = sorted(set(got["files"]) ^ set(reference["files"]))[:4]
                    problems.append(f"after {name}: another end state (files that differ: {differ}; doors "
                                    f"{got['doors']}; journal {got['journal']})")
        finally:
            world.close()
    return problems


def remove_tree(path: Path) -> None:
    def writable(function, name, _error):
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if path.exists():
        shutil.rmtree(path, onexc=writable)


# --- the run ----------------------------------------------------------------------------------------------------------


def scenarios(slow: bool, names: list[str]) -> list[dict]:
    found = []
    for path in sorted(SCENARIO_FOLDER.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["file"] = path.name
        found.append(spec)
    if names:
        chosen = [spec for spec in found if spec["name"] in names or spec["file"] in names]
        unknown = set(names) - {spec["name"] for spec in chosen} - {spec["file"] for spec in chosen}
        if unknown:
            raise SystemExit(f"no such scenario: {', '.join(sorted(unknown))}")
        return chosen
    return [spec for spec in found if spec["tier"] == "fast" or slow]


def run_scenario(spec: dict, base: Path, repositories: dict[str, str], newest: str) -> list[str]:
    folder = base / re.sub(r"[^a-z0-9]+", "-", spec["name"].lower())
    if spec.get("interrupt_each_step"):
        return run_interrupted(spec, folder, repositories, newest)
    world = World(folder, spec, repositories, newest)
    try:
        with isolated(world):
            problems = run_steps(world, spec["steps"])
            problems += always_checks(world)
        return problems
    finally:
        world.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ask = argparse.ArgumentParser(prog="rehearse.py", description="Run the install script on a scratch PC, scenario by "
                                  "scenario.")
    ask.add_argument("names", nargs="*", help="scenario names or file names (default: the fast tier)")
    ask.add_argument("--slow", action="store_true", help="the slow tier as well (the release runs it)")
    ask.add_argument("--list", action="store_true", help="name the scenarios and stop")
    ask.add_argument("--keep", action="store_true", help="leave the scenarios' folders for a look")
    args = ask.parse_args(argv)
    chosen = scenarios(args.slow, args.names)
    if args.list:
        for spec in chosen:
            print(f"{spec['tier']:<5} {spec['name']}")
        return 0
    base = Path(tempfile.mkdtemp(prefix="resumebot-rehearsal-"))
    failed = 0
    began = time.monotonic()
    try:
        newest = app_version()
        repositories = build_repositories(base / "repositories", newest)
        TEMPLATE.clear()
        TEMPLATE["base"] = base / "template"
        for spec in chosen:
            started = time.monotonic()
            try:
                problems = run_scenario(spec, base / "worlds", repositories, newest)
            except Exception:
                problems = ["the rehearsal itself broke:\n" + traceback.format_exc()]
            seconds = time.monotonic() - started
            if problems:
                failed += 1
                print(f"FAILED {spec['name']} ({seconds:.1f}s)")
                for text in problems:
                    print(f"    {text}")
            else:
                print(f"ok     {spec['name']} ({seconds:.1f}s)")
    finally:
        if not args.keep:
            remove_tree(base)
    print(f"rehearsal: {len(chosen) - failed} of {len(chosen)} scenarios passed in {time.monotonic() - began:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
