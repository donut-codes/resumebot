"""The owner's second guard (2026-11-10): the page program starts no engine loop and imports none.

Both checks start the page in a fresh interpreter (`python -I`), because only a fresh one tells what the page itself
loads and runs: this test process has already imported everything.

- By what RUNS (`test_the_page_starts_no_engine_loop`): every starter in `forbidden.STARTERS` is replaced by a function
  that records the call and raises, the page's lifespan is entered, and the test fails if a starter was called, if the
  start failed, or if a task is running that `forbidden.ALLOWED_TASKS` does not name.
- By what is IMPORTED (`test_the_page_imports_no_engine_module_but_the_baselined_ones`): while the page is imported and
  started, every `import` statement that reaches a module of `forbidden.FORBIDDEN` is recorded as "importer -> module".
  The recorded pairs must be exactly the lines of `page_import_baseline.txt`: a pair not listed fails (make the import
  local to the function that needs it, or call the engine through `engine_calls`), and a listed pair that is no longer
  reached fails too (delete its line: the file only shrinks, never re-baseline to hide a new pair). Each module of
  FORBIDDEN that is loaded must also be explained by a pair.

Because the pairs name the module whose import statement does the reaching, the fix is always the line that module
names. The file is empty (`test_the_baseline_holds_no_pair`): there is no pair left to clear.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import db
from tests.split import forbidden

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "page_import_baseline.txt"
MARK = "@@RESULT@@"

#: Runs in the fresh interpreter. argv[1] is the JSON `spec` written by `boot` below. It records every import statement
#: that reaches a forbidden module (`builtins.__import__` is called for each, loaded or not), optionally replaces the
#: starters, imports the entry module, enters its lifespan, and prints the result as JSON after the mark `spec["mark"]`.
BOOT = r'''
import asyncio, builtins, importlib, json, sys

spec = json.loads(sys.argv[1])
sys.path[:0] = spec["paths"]
forbidden = set(spec["forbidden"])
edges = set()
real_import = builtins.__import__


def recording_import(name, globals=None, locals=None, fromlist=(), level=0):
    importer = (globals or {}).get("__name__", "?")
    if level:
        package = (globals or {}).get("__package__") or importer
        base = package.rsplit(".", level - 1)[0] if level > 1 else package
        full = f"{base}.{name}" if name else base
    else:
        full = name
    for wanted in [full, *(f"{full}.{item}" for item in (fromlist or ()) if isinstance(item, str))]:
        if wanted in forbidden and importer not in forbidden:
            edges.add((importer, wanted))
    return real_import(name, globals, locals, fromlist, level)


builtins.__import__ = recording_import
result = {"called": [], "tasks": [], "error": None}


def replace_starter(module_name, dotted):
    owner = importlib.import_module(module_name)
    parts = dotted.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    getattr(owner, parts[-1])

    def boom(*args, **kwargs):
        result["called"].append(f"{module_name}.{dotted}")
        raise RuntimeError(f"the page started {module_name}.{dotted}")

    setattr(owner, parts[-1], boom)


def task_key(task):
    coro = task.get_coro()
    frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
    module = frame.f_globals.get("__name__", "?") if frame is not None else getattr(coro, "__module__", "?")
    return f"{module}:{getattr(coro, '__qualname__', repr(coro))}"


async def main():
    entry = importlib.import_module(spec["entry"])
    application = getattr(entry, spec["attr"])
    async with application.router.lifespan_context(application):
        result["tasks"] = sorted(task_key(t) for t in asyncio.all_tasks() if t is not asyncio.current_task())


if spec["mode"] == "runs":
    for module_name, dotted in spec["starters"]:
        replace_starter(module_name, dotted)
try:
    asyncio.run(main())
except BaseException as error:
    result["error"] = f"{type(error).__name__}: {error}"
result["loaded"] = sorted(n for n in forbidden if n in sys.modules)
result["libs"] = sorted(n for n in spec.get("libs", []) if n in sys.modules)
result["edges"] = sorted(f"{a} -> {b}" for a, b in edges)
sys.stdout.write("\n" + spec["mark"] + json.dumps(result))
'''


def boot(tmp_path, mode, *, entry="app.jobsearch.page", attr="app", forbidden_names=forbidden.FORBIDDEN,
         starters=forbidden.STARTERS, paths=(), libs=()) -> dict:
    """Start `entry` in a fresh interpreter and return what it reported (see BOOT). The database is a file in
    `tmp_path`, so nothing of the install is touched, and the install's data folder is not named. `-I` keeps the
    environment's PYTHONPATH and the current folder out, so the child is handed this process's own `sys.path` (the
    backend and the vendored engine are on it) instead."""
    spec = {"mark": MARK, "mode": mode, "entry": entry, "attr": attr, "forbidden": list(forbidden_names),
            "starters": [list(s) for s in starters], "libs": list(libs),
            "paths": [*map(str, paths), str(db.BACKEND_DIR), *(p for p in sys.path if p)]}
    env = {k: v for k, v in os.environ.items() if k != "RESUMEBOT_DATA_DIR"}
    env["JOBSEARCH_DATABASE_URL"] = db.sqlite_url(tmp_path / "boot.db")
    done = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", BOOT, json.dumps(spec)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    assert MARK in done.stdout, f"the start-up script did not finish:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.rsplit(MARK, 1)[1])


def listed_pairs() -> set[str]:
    pairs = set()
    for raw in BASELINE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            pairs.add(line)
    return pairs


def test_the_page_starts_no_engine_loop(tmp_path):
    got = boot(tmp_path, "runs")
    assert got["error"] is None, f"the page's start failed: {got['error']}"
    assert got["called"] == [], f"the page started what only the engine starts: {got['called']}"
    stray = [t for t in got["tasks"] if t not in forbidden.ALLOWED_TASKS]
    assert not stray, f"tasks running after the page started that ALLOWED_TASKS does not name: {stray}"


def test_the_page_imports_no_engine_module_but_the_baselined_ones(tmp_path):
    got = boot(tmp_path, "imports", libs=forbidden.LAZY_LIBRARIES)
    assert got["error"] is None, f"the page's start failed: {got['error']}"
    assert got["libs"] == [], (f"the page's import must not load {got['libs']}: import it inside the function "
                               "that uses it")
    reached = set(got["edges"])
    listed = listed_pairs()
    new = sorted(reached - listed)
    gone = sorted(listed - reached)
    assert not new, ("the page reaches engine modules the baseline does not list (make each import local to the "
                     "function that needs it, or call the engine through engine_calls; never add a line to hide it):\n"
                     + "\n".join(new))
    assert not gone, ("the baseline lists pairs the page no longer reaches (delete these lines; the file only "
                      "shrinks):\n" + "\n".join(gone))
    explained = {pair.split(" -> ")[1] for pair in reached}
    unexplained = sorted(set(got["loaded"]) - explained)
    assert not unexplained, f"loaded with no import statement that names them (imported by importlib?): {unexplained}"


def test_every_baseline_line_names_the_lane_that_clears_it():
    bare = [raw for raw in BASELINE.read_text(encoding="utf-8").splitlines()
            if raw.split("#", 1)[0].strip() and "# cleared by:" not in raw]
    assert not bare, f"baseline lines with no '# cleared by: <lane key>': {bare}"


def test_the_lists_name_real_modules_and_starters():
    root = Path(db.BACKEND_DIR)
    for name in forbidden.FORBIDDEN:
        stem = root.joinpath(*name.split("."))
        assert stem.with_suffix(".py").is_file() or (stem / "__init__.py").is_file(), f"no module {name}"
    for module_name, dotted in forbidden.STARTERS:
        stem = root.joinpath(*module_name.split("."))
        source = stem.with_suffix(".py").read_text(encoding="utf-8")
        head, _, tail = dotted.rpartition(".")
        assert f"def {tail}(" in source and (not head or f"class {head}" in source), f"no {module_name}.{dotted}"


def test_the_baseline_holds_no_pair():
    """The page reaches no engine module by an import statement, so the file that lists such pairs is empty and stays
    empty: a new pair is fixed at its import, never listed."""
    assert listed_pairs() == set()


# --- the guard is only worth having if it can fail: the same start-up over a throwaway package ------------------


@pytest.fixture
def fake_package(tmp_path):
    package = tmp_path / "fake_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "engine.py").write_text("def start_loop():\n    return None\n", encoding="utf-8")
    (package / "other.py").write_text("", encoding="utf-8")
    (package / "never.py").write_text("", encoding="utf-8")
    (package / "page.py").write_text(textwrap.dedent('''
        import contextlib

        import fake_pkg.other
        from fastapi import FastAPI

        from . import engine


        def later():
            from . import never


        @contextlib.asynccontextmanager
        async def lifespan(_):
            engine.start_loop()
            yield


        app = FastAPI(lifespan=lifespan)
        '''), encoding="utf-8")
    (package / "tasks.py").write_text(textwrap.dedent('''
        import asyncio
        import contextlib

        from fastapi import FastAPI


        async def forever():
            await asyncio.sleep(3600)


        @contextlib.asynccontextmanager
        async def lifespan(_):
            task = asyncio.create_task(forever())
            yield
            task.cancel()


        app = FastAPI(lifespan=lifespan)
        '''), encoding="utf-8")
    return tmp_path


FAKE = {"forbidden_names": ("fake_pkg.engine", "fake_pkg.other", "fake_pkg.never"),
        "starters": (("fake_pkg.engine", "start_loop"),)}


def test_a_page_that_starts_a_loop_is_caught(fake_package):
    got = boot(fake_package, "runs", entry="fake_pkg.page", paths=[fake_package], **FAKE)
    assert got["called"] == ["fake_pkg.engine.start_loop"]
    assert "the page started" in got["error"]


def test_a_page_that_leaves_a_task_running_is_caught(fake_package):
    got = boot(fake_package, "runs", entry="fake_pkg.tasks", paths=[fake_package], **FAKE)
    assert got["error"] is None
    assert got["tasks"] == ["fake_pkg.tasks:forever"]


def test_the_import_record_names_the_importer_of_each_forbidden_module_the_start_reached(fake_package):
    got = boot(fake_package, "imports", entry="fake_pkg.page", paths=[fake_package], **FAKE)
    assert got["error"] is None
    assert got["edges"] == ["fake_pkg.page -> fake_pkg.engine", "fake_pkg.page -> fake_pkg.other"]
    assert got["loaded"] == ["fake_pkg.engine", "fake_pkg.other"]
