#!/usr/bin/env python3
"""The local development flow: one entry point that checks the app on this machine, writes its lock files again,
photographs the running instance and removes a worktree. It needs Python and, for the page, Node and npm: no bash and no
container, so it runs the same on Windows, macOS and Linux.

    py -3 app/scripts/dev.py check [--backend] [--frontend] [--runner] [--shell] [--scripts] [--shipped-only] [PATH...]
    py -3 app/scripts/dev.py check --copier
    py -3 app/scripts/dev.py shots [--out DIR] ROUTE...
    py -3 app/scripts/dev.py lock [--upgrade]
    py -3 app/scripts/dev.py worktree-remove PATH

`check` is the gate: Ruff, basedpyright (new findings only), pytest on one worker for each core (at most eight), the
process engine's own tests, the scout's node tests, typecheck, ESLint (new findings only), vitest, ShellCheck (new
findings only, over bump-version.sh) and this folder's own lint and tests. It runs every step it can and says at the end
what passed. Its five sides (backend, runner, frontend, shell, scripts) run at the same time, each line marked with
its side's name, with one summary at the end; inside a side the steps go one after another. Paths narrow it
(tests/jobsearch/test_gates.py, src/App.test.tsx, packages/process-engine, packages/process-view).
The backend's tests are two trees, one pytest call: tests/ (they ship) and restricted_runbooks/tests/ (they never do,
and are linted with the rest). `--shipped-only` leaves the second out; it exists to run the exported tree, which has no
such folder.

The backend test modules that do need a Postgres server, the copier's (tests/migrate/test_copy_from_postgres.py and
test_drop_noise.py), are left out of `check` and run only as `check --copier`, against the server DATABASE_URL names;
this script never starts or stops a Postgres. They leave with the copier. The rest of the backend's tests need no
database server and no DATABASE_URL: each worker makes one SQLite file from the models once, and each test works on its
own copy of it (app/backend/tests/support.py, the `js` fixture). Nothing here ever touches the running instance's
database, and nothing `check` starts is told where an install's data folder is (`forget_data_folder`).

The Python environment is built once for each requirements-dev.lock (pip, hashes required) and kept in a per-user
cache that every checkout and worktree shares; ESLint and the page keep their node_modules in the checkout, as npm
does. Nothing is installed system-wide and nothing needs administrator rights. The cache is RESUMEBOT_DEV_HOME, or the
platform's own cache folder.

How the instance is started, backed up and updated is in the local-instance notes and the project rules
("Releasing"): this script does none of it.

Python 3.9 or newer runs this file (it builds a Python 3.12 environment for the app); it uses only the standard library.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# --- where things are ---------------------------------------------------------------------------------------------


class Tree:
    """One checkout of the repository: this one, or another worktree of it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "app"
        self.backend = self.app / "backend"
        self.frontend = self.app / "frontend"
        self.scout = self.app / "scout"
        self.scripts = self.app / "scripts"
        self.engine = self.app / "packages" / "process-engine"

    @property
    def lock(self) -> Path:
        """The lock the test environment is built from: the app's run-time packages and the test tools."""
        return self.backend / "requirements-dev.lock"


HERE = Path(__file__).resolve().parent
THIS = Tree(HERE.parents[1])

#: The Python the app and its lock are made for. The lock is resolved for 3.12, and asyncpg has no wheel for a newer
#: one yet.
PYTHON = (3, 12)
#: Tools that are not part of the app: uv resolves the lock, ShellCheck lints the shell scripts. Pinned (a new release
#: is never a surprise) and installed by pip into an environment of their own, only when a command needs them.
UV_VERSION = "0.12.24"
SHELLCHECK_VERSION = "0.11.0.1"  # ShellCheck 0.11.0
#: What `shots` photographs with. This script does not install them: see `shots`.
SHOTS_PACKAGES = ("playwright@1.48.0", "pixelmatch@5.3.0", "pngjs@7.0.0")
PAGE_PORT = 8430
LOCAL_PAGE = f"http://localhost:{PAGE_PORT}"

#: Bumped when the way an environment is built changes, so every cached one is made again.
ENVIRONMENT_FORMAT = "1"


class Stop(Exception):
    """Something to say in one plain sentence before stopping: `main` prints it and exits with `code`."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


#: What a thread that runs one side of a check at the same time as the others puts before each line it prints, so the
#: interleaved output stays readable (`say`, `warn` and `run` read it; the main thread has none).
_LINES = threading.local()
_PRINT_LOCK = threading.Lock()


def _prefix() -> str:
    return getattr(_LINES, "prefix", "")


def say(text: str) -> None:
    with _PRINT_LOCK:
        print(f"{_prefix()}==> {text}", flush=True)


def warn(text: str) -> None:
    with _PRINT_LOCK:
        print(f"{_prefix()}==> {text}", file=sys.stderr, flush=True)


# --- small helpers: processes, files, locks -----------------------------------------------------------------------


def run(
    cmd: Sequence[object], *, cwd: Path | None = None, env: dict[str, str] | None = None, quiet: bool = False
) -> int:
    """Run a command and return its exit status. Its output goes straight to the screen; when `quiet` it is kept and
    shown only if the command failed."""
    argv = [str(part) for part in cmd]
    try:
        if quiet:
            done = subprocess.run(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            if done.returncode != 0:
                print(done.stdout, end="", flush=True)
            return done.returncode
        prefix = _prefix()
        if not prefix:
            return subprocess.run(argv, cwd=cwd, env=env).returncode
        # One of several sides running at once: its output is read line by line and printed with the side's name in
        # front, so two sides' lines do not run into each other.
        with subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        ) as child:
            assert child.stdout is not None
            for line in child.stdout:
                with _PRINT_LOCK:
                    print(f"{prefix}{line.rstrip()}", flush=True)
        return child.returncode
    except FileNotFoundError as exc:
        raise Stop(f"cannot run {argv[0]}: {exc.strerror or exc}") from exc


def output(cmd: Sequence[object]) -> str:
    """What a command prints (standard output only), or '' when it fails or is not there."""
    try:
        done = subprocess.run(
            [str(part) for part in cmd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def tool(name: str, hint: str) -> str:
    """The full path of a program on the PATH, or a plain sentence saying what to install."""
    found = shutil.which(name)
    if not found:
        raise Stop(f"{name} was not found on this machine. {hint}")
    return found


def sha256_of(*parts: str) -> str:
    """A hash of texts, whatever line endings each was saved with (a Windows checkout may differ from the repo's)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.replace("\r\n", "\n").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def is_link(path: Path) -> bool:
    """A symbolic link or, on Windows, a junction (which `Path.is_symlink` does not see)."""
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
    return False


def remove_link(path: Path) -> None:
    if os.name == "nt":
        os.rmdir(path)  # a junction, or a link to a folder: it goes, and what it points to stays
    else:
        os.unlink(path)


def long_path(path: Path) -> Path:
    """The path in the form that lets Windows go past its 260-character limit (a checkout in a temporary folder, with
    the page's node_modules in it, goes past it); the same path elsewhere."""
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):  # a network share
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def remove_tree(path: Path) -> None:
    """Delete a folder and what is in it. Windows refuses to delete read-only files (git's object files are some) and
    paths over 260 characters (npm's deepest folders are); the first are made writable, the second are reached by their
    long form. A link is removed without touching what it points to."""
    if is_link(path):
        remove_link(path)
        return

    def writable(func: Callable[[str], object], target: str, _error: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(long_path(path), onexc=writable)
    else:
        shutil.rmtree(long_path(path), onerror=writable)


def link_directory(link: Path, target: Path) -> None:
    """Make `link` a link to the folder `target`, with no privilege needed: a junction on Windows (a symbolic link
    there wants developer mode or administrator rights; a junction wants neither and reaches any folder on this
    machine), a symbolic link elsewhere. An existing link that points elsewhere is replaced; a real folder in the way
    is a mistake."""
    if is_link(link):
        if Path(os.path.realpath(link)) == target.resolve():
            return
        remove_link(link)
    elif link.exists():
        raise Stop(f"{link} is in the way and is not a link; move it aside")
    if os.name == "nt":
        if run(["cmd", "/c", "mklink", "/J", link, target], quiet=True) != 0:
            raise Stop(f"could not link {link} to {target}")
    else:
        os.symlink(target, link, target_is_directory=True)


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running. (os.kill(pid, 0) is the usual test, but on Windows it ends the
    process.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return False  # a child of this process that has ended: a finished process nobody collected still has an id
    except OSError:
        pass  # not a child of this process (started by another run of this script)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_tree(pid: int) -> None:
    """End a process and what it started: a server has children, and a Windows process does not take them with it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


#: Where the holder of a lock writes its process id: inside the lock file, past the one byte that the operating system's
#: lock covers (Windows refuses a read of a locked byte).
LOCK_NOTE_OFFSET = 16


def take_lock(handle) -> bool:
    """Try, without waiting, to take the operating system's exclusive lock on an open file (its first byte)."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def lock_holder(path: Path) -> str:
    """Who holds a lock, as its holder wrote it, for the message of someone waiting."""
    try:
        with open(path, "rb") as handle:
            handle.seek(LOCK_NOTE_OFFSET)
            return f"process {int(handle.read(32).split()[0])}"
    except (OSError, ValueError, IndexError):
        return "another run"


@contextlib.contextmanager
def exclusive(path: Path, what: str, *, wait: float = 600.0) -> Iterator[None]:
    """Hold a lock on this machine: the operating system's lock on a file, so a process that dies, however it dies, lets
    go of it. (A lock made of a folder or a pid file is left behind by a crash or, on Windows, by a waiting process
    that has the file open at the moment its owner deletes it.) Waits up to `wait` seconds for the holder, saying who
    it is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o666), "r+b")
    try:
        deadline = time.monotonic() + wait
        told = False
        while not take_lock(handle):
            if time.monotonic() > deadline:
                raise Stop(f"{what} is held by {lock_holder(path)}; waited {seconds_text(wait)}")
            if not told:
                say(f"{what} is busy ({lock_holder(path)}); waiting for it")
                told = True
            time.sleep(0.25)
        handle.seek(LOCK_NOTE_OFFSET)
        handle.write(f"{os.getpid()}\n".encode())
        handle.flush()
        yield
    finally:
        handle.close()  # closing the file lets go of the lock


# --- the environments ---------------------------------------------------------------------------------------------


def dev_home() -> Path:
    """Where this flow keeps what it builds for a person, outside every checkout: the Python environments, the tools,
    the pictures' program. RESUMEBOT_DEV_HOME, or the platform's own cache folder."""
    override = os.environ.get("RESUMEBOT_DEV_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "resumebot-dev"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "resumebot-dev"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "resumebot-dev"


def venv_executable(venv: Path, name: str) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def find_python() -> str:
    """A Python 3.12 to build the environments with: uv's own build when uv has one (its SQLite is a current one), else
    the one running this script if it is 3.12, else the launcher's or the PATH's. A newer Python is no use: the lock
    holds wheels for 3.12."""
    tag = f"{PYTHON[0]}.{PYTHON[1]}"
    uv = shutil.which("uv")
    if uv:
        managed = output([uv, "python", "find", "--managed-python", tag]).strip()
        if managed and Path(managed).is_file():
            return managed
    if sys.version_info[:2] == PYTHON and sys.executable:
        return sys.executable
    candidates: list[list[str]] = []
    if os.name == "nt" and shutil.which("py"):
        candidates.append(["py", f"-{tag}"])
    candidates.append([f"python{tag}"])
    probe = "import sys; print(sys.executable); print(*sys.version_info[:2])"
    for candidate in candidates:
        exe = shutil.which(candidate[0])
        if not exe:
            continue
        found = output([exe, *candidate[1:], "-c", probe]).splitlines()
        if len(found) == 2 and tuple(int(n) for n in found[1].split()) == PYTHON:
            return found[0]
    raise Stop(
        f"Python {tag} was not found. The app's dependencies are locked for it. Install it (python.org, or "
        f"`uv python install {tag} --upgrade`) and run this again; it is used only to build a private environment."
    )


def build_environment(path: Path, install: Sequence[str], what: str) -> None:
    """A virtual environment at `path` with `install` (pip arguments) run in it, or a plain sentence saying why not.
    A half-built one is never left looking finished: the `ready` file is written last."""
    if path.exists():
        remove_tree(path)
    say(f"Building {what} (once, about a minute): {path}")
    if run([find_python(), "-m", "venv", path], quiet=True) != 0:
        raise Stop(f"could not create a Python environment at {path}")
    pip = [venv_executable(path, "python"), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if run([*pip, *install], env=dict(os.environ, PYTHONUTF8="1"), quiet=True) != 0:
        remove_tree(path)
        raise Stop(f"could not install {what}")
    (path / "ready").write_text("ok\n", encoding="utf-8")


def environment_works(path: Path) -> bool:
    ready = (path / "ready").exists()
    return ready and output([venv_executable(path, "python"), "-c", "print(1)"]).strip() == "1"


def environment(path: Path, install: Sequence[str], what: str) -> Path:
    """The Python of a ready environment at `path`, built first if it is not there (or its Python has been
    uninstalled). Two runs at once build it once."""
    if not environment_works(path):
        with exclusive(path.with_name(path.name + ".lock"), f"the {what}"):
            if not environment_works(path):
                build_environment(path, install, what)
    with contextlib.suppress(OSError):
        os.utime(path / "ready")  # last used: old environments are pruned by it
    return venv_executable(path, "python")


def prune_environments(parent: Path, keep: Path, days: int = 30) -> None:
    """Remove the cached environments in `parent` that nothing has used for `days` days (a changed lock leaves the
    old one)."""
    now = time.time()
    for old in parent.glob("*"):
        ready = old / "ready"
        if old == keep or old.suffix == ".lock" or not ready.exists():
            continue
        with contextlib.suppress(OSError):
            if now - ready.stat().st_mtime > days * 86400:
                remove_tree(old)


def app_python(tree: Tree) -> Path:
    """The Python the app's checks run in: built from this tree's requirements-dev.lock, once for each lock file, and
    shared by every checkout and worktree that has the same one. pip checks every file's hash and installs nothing
    the lock does not name."""
    if not tree.lock.exists():
        raise Stop(f"{tree.lock} is missing")
    version = f"python{PYTHON[0]}.{PYTHON[1]}"
    key = sha256_of(version, ENVIRONMENT_FORMAT, tree.lock.read_text(encoding="utf-8"))[:12]
    parent = dev_home() / "venvs"
    path = parent / key
    install = ["--only-binary=:all:", "--require-hashes", "--no-deps", "-r", str(tree.lock)]
    python = environment(path, install, "the app's Python environment")
    prune_environments(parent, keep=path)
    return python


@dataclass(frozen=True)
class Tools:
    uv: str
    shellcheck: str


def tools() -> Tools:
    """uv and ShellCheck, at the versions pinned above, in an environment of their own."""
    pins = [f"uv=={UV_VERSION}", f"shellcheck-py=={SHELLCHECK_VERSION}"]
    key = sha256_of(f"python{PYTHON[0]}.{PYTHON[1]}", ENVIRONMENT_FORMAT, *pins)[:12]
    parent = dev_home() / "tools"
    path = parent / key
    environment(path, ["--only-binary=:all:", *pins], "the tools (uv and ShellCheck)")
    prune_environments(parent, keep=path)
    return Tools(uv=str(venv_executable(path, "uv")), shellcheck=str(venv_executable(path, "shellcheck")))


def node_supported(version: tuple[int, int, int]) -> bool:
    """Node versions the page's packages run on: the strictest engines range among them is jsdom's,
    ^22.22.2 || ^24.15.0 || >=26."""
    major = version[0]
    if major == 22:
        return version >= (22, 22, 2)
    if major == 24:
        return version >= (24, 15, 0)
    return major >= 26


def npm_executable() -> str:
    """npm, after checking that the node beside it is one the page's packages run on."""
    node = tool("node", "The page needs Node 22.22.2 or newer, or 24.15 or newer (nodejs.org).")
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", output([node, "--version"]).strip())
    if not match:
        raise Stop("could not read the version of node")
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if not node_supported(version):
        raise Stop(
            f"node {'.'.join(map(str, version))} is too old for the page's packages; "
            "use 22.22.2 or newer, or 24.15 or newer"
        )
    return tool("npm", "It comes with Node (nodejs.org).")


def lock_stamp(lockfile: Path) -> str:
    """A hash of a package-lock.json that ignores the app's own version, which every release changes (a release should
    not reinstall every package)."""
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    data["version"] = "0.0.0"
    if isinstance(data.get("packages"), dict) and "" in data["packages"]:
        data["packages"][""]["version"] = "0.0.0"
    return sha256_of(json.dumps(data, sort_keys=True))


def ensure_node_modules(folder: Path) -> None:
    """`npm ci` in a folder, unless node_modules already holds what its package-lock.json says."""
    stamp = folder / "node_modules" / ".resumebot-lock"
    want = lock_stamp(folder / "package-lock.json")
    with contextlib.suppress(OSError):
        if stamp.read_text(encoding="utf-8").strip() == want:
            return
    say(f"npm ci in {folder}")
    if run([npm_executable(), "ci", "--no-audit", "--no-fund"], cwd=folder) != 0:
        raise Stop(f"npm ci failed in {folder}")
    stamp.write_text(want + "\n", encoding="utf-8")


def ensure_frontend(tree: Tree) -> str:
    """The page's packages installed, and the link the vendored process-view package needs: its sources are found by
    their real path, which looks for node_modules in app/ (a junction on Windows, so no symbolic-link privilege and no
    administrator rights). Returns npm."""
    npm = npm_executable()
    ensure_node_modules(tree.frontend)
    link_directory(tree.app / "node_modules", tree.frontend / "node_modules")
    return npm


def drop_build_info(tree: Tree) -> None:
    """tsc -b leaves its build info beside the config, even with --noEmit. It is removed so that nothing of this
    machine stays in the working tree."""
    (tree.frontend / "tsconfig.tsbuildinfo").unlink(missing_ok=True)


def backend_env(tree: Tree, extra_paths: Sequence[Path] = ()) -> dict[str, str]:
    """The environment the app's code runs in here: the backend and the vendored engine on the path (a shared
    environment cannot point at one checkout's copy), UTF-8 everywhere."""
    env = dict(os.environ)
    paths = [str(tree.backend), str(tree.engine / "src"), *(str(p) for p in extra_paths)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def once(func):
    """`func`, called once however many steps ask: each gets the same result, or the same Stop (a step that cannot
    get its environment is not a reason to try again, for a minute, in each step after it)."""
    cache = []

    def wrapper():
        if not cache:
            try:
                cache.append((func(), None))
            except Stop as exc:
                cache.append((None, exc))
        value, error = cache[0]
        if error is not None:
            raise error
        return value

    return wrapper


# --- check --------------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What a `check` runs: the sides of the gate, and the paths that narrow the backend's and the page's tests."""

    backend: bool = False
    runner: bool = False
    frontend: bool = False
    shell: bool = False
    scripts: bool = False
    app_tests: bool = False  # Ruff, basedpyright and pytest on the app
    shipped_only: bool = False  # leave out the restricted folder's tests (the exported tree has none)
    engine_tests: bool = False  # the process engine package's own tests
    py_paths: list[str] = field(default_factory=list)
    js_paths: list[str] = field(default_factory=list)


#: The restricted runbooks' folder, under the backend, and its own tests. Never shipped: the exported tree has neither.
RESTRICTED_TESTS = "restricted_runbooks/tests"


def plan_check(sides: Sequence[str], paths: Sequence[str], shipped_only: bool = False) -> Plan:
    """Turn the options and paths of `check` into a Plan: paths narrow the run to the sides they name (and leave out the
    runner); a path for a side that was switched off, or of no known kind, is a mistake. `shipped_only` runs the tests
    that ship and nothing of the restricted folder: it is built to run the exported tree, which has no such folder."""
    plan = Plan(shipped_only=shipped_only)
    chosen = set(sides)
    engine_only = False
    for raw in paths:
        arg = raw.replace("\\", "/")
        for prefix in ("app/", "backend/", "frontend/"):
            arg = arg.removeprefix(prefix)
        if arg.startswith("tests/") or arg.startswith(RESTRICTED_TESTS):
            if shipped_only and arg.startswith(RESTRICTED_TESTS):
                raise Stop(f"'{raw}' is a restricted test; drop --shipped-only to run it")
            plan.py_paths.append(arg)
        elif arg.startswith("src/"):
            plan.js_paths.append(arg)
        elif arg.startswith("packages/process-engine"):
            engine_only = True
        elif arg.startswith("packages/process-view"):
            plan.js_paths.append(arg)
        else:
            raise Stop(
                f"'{raw}' is neither a backend tests/ path, a frontend src/ path nor packages/process-engine "
                "or packages/process-view"
            )
    named_python = bool(plan.py_paths) or engine_only
    if paths:
        if "runner" in chosen:
            raise Stop("paths name python or frontend tests; drop --runner")
        if not chosen:
            named = (("backend", named_python), ("frontend", bool(plan.js_paths)))
            chosen = {side for side, is_named in named if is_named}
        elif (named_python and "backend" not in chosen) or (plan.js_paths and "frontend" not in chosen):
            raise Stop("the sides you chose do not run the paths you named; drop the flag to run them")
    elif not chosen:
        chosen = {"backend", "runner", "frontend", "shell", "scripts"}
    plan.backend = "backend" in chosen
    plan.runner = "runner" in chosen
    plan.frontend = "frontend" in chosen
    plan.shell = "shell" in chosen
    plan.scripts = "scripts" in chosen
    if plan.backend:
        # `packages/process-engine` among the paths means the package's tests; with no path at all both suites run.
        plan.app_tests = bool(plan.py_paths) or not paths
        plan.engine_tests = engine_only or not paths
    return plan


@dataclass
class Result:
    name: str
    status: str  # "ok", "FAILED" or "skipped"
    seconds: float
    note: str = ""


class Gate:
    """Runs the steps of a check one after another, keeps going after a failure so one run shows every problem, and
    says at the end what passed."""

    def __init__(self) -> None:
        self.results: list[Result] = []
        self.started = time.monotonic()

    def step(self, name: str, func: Callable[[], int], note: str = "") -> None:
        """Run one step. A `note` is said again in the summary, beside the result: what a passing step did not cover."""
        say(name)
        began = time.monotonic()
        try:
            code = func()
        except Stop as exc:
            warn(str(exc))
            code = 1
        except Exception:  # a bug in this script is one failed step, not the end of the others
            traceback.print_exc()
            code = 1
        self.results.append(Result(name, "ok" if code == 0 else "FAILED", time.monotonic() - began, note))

    def skip(self, name: str, why: str) -> None:
        say(f"{name}: skipped, {why}")
        self.results.append(Result(name, "skipped", 0.0, why))

    @property
    def failed(self) -> bool:
        return any(r.status == "FAILED" for r in self.results)

    def summary(self) -> str:
        width = max((len(r.name) for r in self.results), default=0)
        lines = [f"check finished in {seconds_text(time.monotonic() - self.started)}"]
        for r in self.results:
            tail = f"  ({r.note})" if r.note else ""
            lines.append(f"    {r.status:<8} {r.name:<{width}}  {seconds_text(r.seconds)}{tail}")
        return "\n".join(lines)


def seconds_text(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}m {whole % 60:02d}s" if whole >= 60 else f"{whole}s"


#: The most workers the backend's suite is given (more were no faster).
MAX_TEST_WORKERS = 8

#: How the workers are dealt their tests. xdist's default (`load`) hands each worker one block of the tests in file
#: order at the start and one test at a time after that. The migration tests sit side by side in `tests/core`, so one
#: worker got the block that held most of them and the run waited for it with the other workers idle. `worksteal` starts
#: the same way and then lets a worker that has run out take half of what another still has queued.
TEST_DISTRIBUTION = "worksteal"


def worker_count(cores: int | None = None) -> int:
    """How many pytest workers the backend's suite runs on: one for each core, at most eight."""
    return max(1, min(MAX_TEST_WORKERS, cores if cores is not None else (os.cpu_count() or 1)))


def pytest_command(python: Path, extra: Sequence[str], *, workers: bool) -> list[str]:
    """pytest as the check runs it (quiet, no cache folder, `worker_count()` workers dealt their tests as
    `TEST_DISTRIBUTION` says, and the 15 slowest listed for the app's suite)."""
    command = [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if workers:
        command += ["-n", str(worker_count()), "--dist", TEST_DISTRIBUTION, "--durations=15"]
    return command + list(extra)


#: The copier's tests, which need a Postgres server (they make a database on the one DATABASE_URL names). `check`
#: leaves them out by name, so the gate needs no server; `check --copier` is their named run. They leave with
#: the copier.
COPIER_TESTS = ("tests/migrate/test_copy_from_postgres.py", "tests/migrate/test_drop_noise.py")


def check_backend(gate: Gate, tree: Tree, plan: Plan) -> None:
    py = once(lambda: app_python(tree))
    env = backend_env(tree)

    restricted = (tree.backend / RESTRICTED_TESTS).is_dir() and not plan.shipped_only

    def ruff() -> int:
        # The restricted folder's tests and hooks are linted with the rest when the folder is there (and wanted).
        folders = ["app", "tests", *(["restricted_runbooks"] if restricted else [])]
        return run([py(), "-m", "ruff", "check", *folders], cwd=tree.backend, env=env)

    def basedpyright() -> int:
        # --pythonpath names the environment the packages are read from; by default it would use whichever python is
        # first on the PATH, which is not this one.
        return run([py(), "-m", "basedpyright", "--pythonpath", py()], cwd=tree.backend, env=env)

    if plan.app_tests:
        gate.step("backend: ruff", ruff)
        gate.step("backend: basedpyright", basedpyright)

    def tests(name: str, folder: Path, extra: Sequence[str], *, workers: bool) -> None:
        def go() -> int:
            command = pytest_command(py(), extra, workers=workers)
            return run(command, cwd=folder, env=backend_env(tree, [tree.scripts]))

        gate.step(name, go)

    if plan.app_tests:
        # The copier's tests need a Postgres server this gate no longer starts: `dev.py check --copier` runs them.
        left_out = [f"--ignore={path}" for path in COPIER_TESTS]
        # Both trees go to one pytest call (one set of workers, one list of the slowest); with no restricted tests
        # (the exported tree, or --shipped-only) pytest's own default, `tests`, stands.
        trees = ["tests", RESTRICTED_TESTS] if restricted and not plan.py_paths else []
        tests("backend: pytest", tree.backend, [*left_out, *plan.py_paths, *trees], workers=True)
    if plan.engine_tests:
        tests("backend: the process engine's own tests", tree.engine, [], workers=False)


def check_runner(gate: Gate, tree: Tree) -> None:
    name = "runner: the scout's node tests"

    def node_tests() -> int:
        node = tool("node", "The scout's tests need Node (nodejs.org).")
        return run([node, "--test"], cwd=tree.scout)

    gate.step(name, node_tests)


def check_frontend(gate: Gate, tree: Tree, plan: Plan) -> None:
    def prepare() -> str:
        npm = ensure_frontend(tree)
        ensure_node_modules(tree.frontend / "lint")  # ESLint has its own folder and lockfile
        return npm

    npm = once(prepare)

    def typecheck() -> int:
        code = run([npm(), "run", "typecheck"], cwd=tree.frontend)
        drop_build_info(tree)
        return code

    def vitest() -> int:
        return run([npm(), "test", *(["--", *plan.js_paths] if plan.js_paths else [])], cwd=tree.frontend)

    # Typecheck and ESLint always cover the whole page; paths narrow vitest to the files named.
    gate.step("frontend: typecheck", typecheck)
    gate.step("frontend: eslint", lambda: run([npm(), "--prefix", "lint", "run", "lint"], cwd=tree.frontend))
    gate.step("frontend: vitest", vitest)


SHELLCHECK_FINDING = re.compile(r"^([^:]+):(\d+):\d+: [a-z]+: .*\[(SC\d+)\]$")


def shellcheck_entries(report: str, read_line: Callable[[str, int], str]) -> list[str]:
    """The findings in `shellcheck -f gcc` output as 'file | SCnnnn | the offending line', without line numbers (so
    editing elsewhere in a script does not turn old findings into new ones), sorted. The format of
    shellcheck-baseline.txt."""
    entries = []
    for line in report.splitlines():
        match = SHELLCHECK_FINDING.match(line)
        if match:
            file, number, code = match.group(1), int(match.group(2)), match.group(3)
            entries.append(f"{file} | {code} | {read_line(file, number).strip()}")
    return sorted(entries)


def new_findings(current: Sequence[str], baseline: Sequence[str]) -> tuple[list[str], list[str]]:
    """(new, gone): findings not in the baseline, and baseline entries that no longer occur. A second copy of an old
    finding is new."""
    now, before = collections.Counter(current), collections.Counter(baseline)
    return sorted((now - before).elements()), sorted((before - now).elements())


def check_shell(gate: Gate, tree: Tree) -> None:
    def go() -> int:
        shellcheck = tools().shellcheck
        found = (p.relative_to(tree.app).as_posix() for p in tree.app.rglob("*.sh") if "node_modules" not in p.parts)
        files = sorted(found)
        if not files:
            raise Stop(f"no shell scripts found under {tree.app}")
        # -x follows `source`d files; -f gcc prints one finding a line. ShellCheck exits 1 when it finds anything,
        # which is the normal case here: the old findings are in the baseline, so its status is not the answer.
        done = subprocess.run(
            [shellcheck, "-x", "-f", "gcc", *files], cwd=tree.app, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if done.returncode not in (0, 1):
            raise Stop(f"shellcheck failed: {done.stderr.strip()}")

        def read_line(file: str, number: int) -> str:
            lines = (tree.app / file).read_text(encoding="utf-8").splitlines()
            return lines[number - 1] if 0 < number <= len(lines) else ""

        current = shellcheck_entries(done.stdout, read_line)
        recorded = (tree.scripts / "shellcheck-baseline.txt").read_text(encoding="utf-8").splitlines()
        baseline = sorted(line for line in recorded if line.strip())
        new, gone = new_findings(current, baseline)
        if gone:
            say(f"{len(gone)} baseline finding(s) no longer occur; drop them from app/scripts/shellcheck-baseline.txt")
        if new:
            warn("SHELLCHECK FOUND NEW PROBLEMS (file | rule | line); fix them, or see shellcheck.net/wiki/<rule>:")
            for line in new:
                print(line, file=sys.stderr)
            return 1
        say(f"{len(files)} scripts, {len(current)} findings, none new (baseline {len(baseline)})")
        return 0

    gate.step("shell: ShellCheck (new findings only)", go)


def check_scripts(gate: Gate, tree: Tree) -> None:
    """This folder's own checks: Ruff under the app's rules but for Python 3.9 (this file runs on whatever Python a
    person has), and its tests, which need nothing installed and run with the Python that runs this script. The
    repository-level scripts/ folder's tests (check_no_one, check_migrations, backup, restore, export) run here too:
    this is their one home."""

    def lint() -> int:
        py39 = ["--config", "pyproject.toml", "--target-version", "py39"]
        scripts = os.path.relpath(tree.scripts, tree.backend)
        return run([app_python(tree), "-m", "ruff", "check", *py39, scripts], cwd=tree.backend)

    def tests() -> int:
        command = [sys.executable, "-m", "unittest", "discover", "-s", tree.scripts, "-p", "test_*.py"]
        return run(command, cwd=tree.scripts, env=dict(os.environ, PYTHONUTF8="1"))

    def repo_tests() -> int:
        command = [sys.executable, "-m", "unittest", "discover", "-s", tree.root / "scripts", "-p", "test_*.py"]
        return run(command, cwd=tree.root, env=dict(os.environ, PYTHONUTF8="1"))

    gate.step("scripts: ruff", lint)
    gate.step("scripts: this folder's own tests", tests)
    gate.step("scripts: the repository's scripts/ tests", repo_tests)


def check_copier(tree: Tree, environ: Mapping[str, str] | None = None) -> int:
    """The copier's tests, on the Postgres server DATABASE_URL names. Without one they cannot run, so this says so in
    plain words and fails; it never starts or stops a Postgres."""
    url = (environ if environ is not None else os.environ).get("DATABASE_URL", "")
    if not url.split(":", 1)[0].lower().startswith("postgres"):
        raise Stop(
            "check --copier needs DATABASE_URL to name a running Postgres server (postgresql://...); "
            "this script does not start one"
        )
    gate = Gate()
    py = once(lambda: app_python(tree))

    def go() -> int:
        command = pytest_command(py(), [*COPIER_TESTS], workers=False)
        return run(command, cwd=tree.backend, env=backend_env(tree, [tree.scripts]))

    gate.step("copier: the Postgres-to-SQLite copy tests", go)
    print()
    print(gate.summary(), flush=True)
    return 1 if gate.failed else 0


#: The variable that names an install's data folder (`DATA_DIR_VARIABLE` in app/backend/app/settings.py; the lanes'
#: launcher and stack.py read it too).
DATA_DIR_VARIABLE = "RESUMEBOT_DATA_DIR"


def forget_data_folder() -> None:
    """A check reads and writes no install's data folder. Where resumebot is installed the variable that names the
    folder is set for the whole user, so the shell a check is started from has it. It is taken out of this process's
    environment, which is where everything a check starts gets its own: pytest and its workers, the engine's tests,
    node, npm and this folder's tests. A pytest started by hand does the same for itself
    (app/backend/tests/conftest.py)."""
    os.environ.pop(DATA_DIR_VARIABLE, None)


def check(tree: Tree, sides: Sequence[str], paths: Sequence[str], shipped_only: bool = False) -> int:
    """Run the sides the plan names. The sides share no state (each has its own tools, folders and test databases), so
    they run at the same time, each in a thread of its own with its lines marked by its name; inside a side the steps
    still go one after another. One summary at the end lists every step, in the order the sides are named here."""
    plan = plan_check(sides, paths, shipped_only)
    gate = Gate()
    sides_to_run: list[tuple[str, Callable[[Gate], None]]] = []
    if plan.backend:
        sides_to_run.append(("backend", lambda g: check_backend(g, tree, plan)))
    if plan.runner:
        sides_to_run.append(("runner", lambda g: check_runner(g, tree)))
    if plan.frontend:
        sides_to_run.append(("frontend", lambda g: check_frontend(g, tree, plan)))
    if plan.shell:
        sides_to_run.append(("shell", lambda g: check_shell(g, tree)))
    if plan.scripts:
        sides_to_run.append(("scripts", lambda g: check_scripts(g, tree)))
    gates = [Gate() for _ in sides_to_run]
    if len(sides_to_run) == 1:
        sides_to_run[0][1](gates[0])  # one side: its output goes straight to the screen, as it always did
    else:
        width = max((len(name) for name, _ in sides_to_run), default=0)

        def work(name: str, func: Callable[[Gate], None], side_gate: Gate) -> None:
            _LINES.prefix = f"[{name:<{width}}] "
            try:
                func(side_gate)
            except Stop as exc:
                warn(str(exc))
                side_gate.results.append(Result(name, "FAILED", 0.0, "could not start"))
            except Exception:  # a bug in this script fails its side, not the other sides
                traceback.print_exc()
                side_gate.results.append(Result(name, "FAILED", 0.0, "script error"))

        threads = [
            threading.Thread(target=work, args=(name, func, side_gate), name=f"check-{name}")
            for (name, func), side_gate in zip(sides_to_run, gates)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    for side_gate in gates:
        gate.results.extend(side_gate.results)
    print()
    print(gate.summary(), flush=True)
    return 1 if gate.failed else 0


# --- lock ---------------------------------------------------------------------------------------------------------

#: What each lock file is made from: the app's packages, and those plus the test tools.
LOCKS = (("requirements.lock", ()), ("requirements-dev.lock", ("--extra", "dev")))
#: The command the lock files' header names, so whoever reads one knows how to make it again.
LOCK_COMMAND = "py -3 app/scripts/dev.py lock"


def lock(tree: Tree, upgrade: bool) -> int:
    """Write requirements.lock and requirements-dev.lock again from pyproject.toml. They are universal: one resolution
    for every platform (Linux, Windows, macOS), a marker on each package only some of them need, and the hashes of
    every file. The native environment installs them with pip (--require-hashes).
    Without --upgrade every version already locked is kept (uv starts from the file it is about to replace)."""
    uv = tools().uv
    for name, extra in LOCKS:
        command = [
            uv, "pip", "compile", "pyproject.toml", *extra, "--universal", "--generate-hashes",
            "--python-version", f"{PYTHON[0]}.{PYTHON[1]}", "--no-config", "--no-python-downloads", "-q",
            "--custom-compile-command", LOCK_COMMAND, "-o", name,
        ]
        if upgrade:
            command.append("--upgrade")
        if run(command, cwd=tree.backend) != 0:
            raise Stop(f"uv could not resolve {name}")
        lines = (tree.backend / name).read_text(encoding="utf-8").splitlines()
        say(f"{name}: {sum(1 for line in lines if re.match(r'^[A-Za-z0-9]', line))} packages")
    say("Run `py -3 app/scripts/dev.py check`, and commit the two lock files with the pyproject change.")
    return 0


# --- worktrees ----------------------------------------------------------------------------------------------------


def git(root: Path, *args: str, check: bool = True) -> str:
    """Run git in a checkout and return what it printed. A failure is a Stop saying what git said, unless `check` is
    off."""
    # core.longpaths: removing a checkout that has npm's deepest folders in it must not stop at Windows' 260 characters.
    command = ["git", "-C", str(root), "-c", "core.longpaths=true", *args]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 and check:
        raise Stop(f"git {' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def unlink_worktree(tree: Path) -> None:
    """Remove the link `ensure_frontend` made in a worktree (app/node_modules, a junction on Windows), without
    following it: `git worktree remove` stops at that junction and leaves a half-deleted folder behind. What the link
    points to stays."""
    with contextlib.suppress(OSError):
        link = tree / "app" / "node_modules"
        if is_link(link):
            remove_link(link)


def remove_worktree(repo: Path, tree: Path) -> None:
    """Remove a worktree of `repo` cleanly: its link first, then git's own removal, then whatever git left."""
    unlink_worktree(tree)
    git(repo, "worktree", "remove", "--force", str(tree), check=False)
    if tree.exists():
        remove_tree(tree)
    git(repo, "worktree", "prune", check=False)


# --- shots --------------------------------------------------------------------------------------------------------


def shots_folder() -> Path:
    return dev_home() / "shots"


def shots(routes: Sequence[str], out: str) -> int:
    """Photograph the running app (each route at 1400px and 375px) with Playwright, and compare each picture with the
    one from the last release. Navigate only: it never clicks anything. The pictures land in `out` (./shots by
    default).

    This script does not install Playwright: it is a download of about 150 MB (the package and a Chromium), made
    once on this machine with the owner's yes, in the folder named in the message below. Until then this stops and
    says how."""
    folder = shots_folder()
    node = tool("node", "The pictures are taken with Node (nodejs.org).")
    if any(not (folder / "node_modules" / pkg).is_dir() for pkg in ("playwright", "pixelmatch", "pngjs")):
        raise Stop(
            "Playwright is not installed on this machine. It is installed once, with the owner's yes for the "
            f"download (about 150 MB):\n    npm install --prefix {folder} --no-save {' '.join(SHOTS_PACKAGES)}"
            f"\n    node {folder / 'node_modules' / 'playwright' / 'cli.js'} install chromium\nThen run this again."
        )
    base = os.environ.get("SHOTS_BASE") or LOCAL_PAGE
    # The last release's pictures are only replaced by a run against the running app: another address leaves them be.
    update_last = not os.environ.get("SHOTS_BASE")
    target = Path(out).resolve()
    target.mkdir(parents=True, exist_ok=True)
    (folder / "last").mkdir(exist_ok=True)
    script = folder / "shots.mjs"
    shutil.copy2(HERE / "shots.mjs", script)  # next to node_modules, where its imports are found
    env = dict(
        os.environ,
        SHOTS_SCHEME=os.environ.get("SHOTS_SCHEME", "light"),
        SHOTS_FIRST_SCREEN=os.environ.get("SHOTS_FIRST_SCREEN", "0"),
        SHOTS_LAST=str(folder / "last"),
        SHOTS_UPDATE_LAST="1" if update_last else "0",
    )
    code = run([node, script, base, target, *routes], cwd=folder, env=env)
    if code == 0:
        say(f"Pictures are in {target}")
    return code


# --- the command line ---------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="dev.py", description="The local development flow (the top of this file says what each command does)."
    )
    commands = top.add_subparsers(dest="command", required=True, metavar="COMMAND")

    one = commands.add_parser("check", help="the gate: lint, types, tests, ShellCheck")
    one.add_argument("--backend", action="store_true", help="the Python side only")
    one.add_argument("--frontend", action="store_true", help="the page only (typecheck and ESLint cover all of it)")
    one.add_argument("--runner", "--scout", dest="runner", action="store_true", help="the scout's node tests")
    one.add_argument("--shell", action="store_true", help="ShellCheck over the shell scripts only")
    one.add_argument("--scripts", action="store_true", help="this folder's own lint and tests only")
    one.add_argument(
        "--shipped-only", action="store_true",
        help="leave out the restricted folder's tests and lint: the run the exported tree needs "
             "(it has no such folder)",
    )
    one.add_argument(
        "--copier", action="store_true",
        help="the copier's tests only; they need DATABASE_URL to name a running Postgres server "
             "(not part of a plain check)",
    )
    one.add_argument(
        "paths", nargs="*",
        help="tests/... for pytest, src/... for vitest, packages/process-engine, packages/process-view",
    )

    one = commands.add_parser("shots", help="photograph the running app at 1400px and 375px")
    one.add_argument("--out", default="shots", help="where the pictures go (default ./shots)")
    one.add_argument("routes", nargs="+", metavar="ROUTE", help="the app's own routes, e.g. '#/board'")

    one = commands.add_parser("lock", help="write the two lock files again from pyproject.toml (universal, hashes)")
    one.add_argument("--upgrade", action="store_true", help="move every package to the newest version pyproject allows")

    one = commands.add_parser(
        "worktree-remove", help="remove a worktree of this repository, its node_modules link first (git stops at it)"
    )
    one.add_argument("path", help="the worktree's folder")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        if args.command == "check":
            forget_data_folder()
            if args.copier:
                sides = ("backend", "frontend", "runner", "shell", "scripts")
                if args.paths or any(getattr(args, side) for side in sides):
                    raise Stop("--copier runs the copier's tests alone; drop the other options and paths")
                return check_copier(THIS)
            sides = [side for side in ("backend", "frontend", "runner", "shell", "scripts") if getattr(args, side)]
            return check(THIS, sides, args.paths, args.shipped_only)
        if args.command == "shots":
            return shots(args.routes, args.out)
        if args.command == "lock":
            return lock(THIS, args.upgrade)
        if args.command == "worktree-remove":
            remove_worktree(THIS.root, Path(args.path).resolve())
            return 0
    except Stop as exc:
        warn(str(exc))
        return exc.code
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
