"""Every module of the site project loop (`app/sites`) imports, and the
modules the loop is built from are all there. A module that quietly needs one that is missing fails here by name instead
of first use. (The whole-app check, `test_every_module_imports.py`, imports all of `app`; this one is the loop's
own.)"""

import importlib
import pkgutil

import app.sites

MODULES = sorted(m.name for m in pkgutil.walk_packages(app.sites.__path__, "app.sites."))


def test_the_loops_modules_are_all_there():
    names = {m.removeprefix("app.sites.") for m in MODULES if m.startswith("app.sites.")}
    # generic modules (copied), trimmed modules, and the ones this app writes itself
    expected = {
        "act_summary", "compact", "earlier", "base",
        "round_process", "plans", "loop", "completion", "checkout", "agent_calls", "validation", "plan_check",
        "check_flow", "task_context", "lessons", "resolution", "projects", "records",
        "escalation", "process", "models",
    }
    assert expected <= names, sorted(expected - names)


def test_each_module_imports():
    failures = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as error:  # each module's failure is named; none stops the rest
            failures.append(f"{name}: {type(error).__name__}: {error}")
    assert not failures, "\n".join(failures)


def test_the_loop_imports_no_planengine_module_but_the_two_that_live_here():
    """The upstream `planengine` package here holds only `budget` and `handouts` (the generic pieces the job-search side
    already uses); the loop's own modules are `app.sites`."""
    from pathlib import Path

    allowed = {"app.planengine.budget", "app.planengine.handouts"}
    problems = []
    for path in Path(app.sites.__path__[0]).glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("from app.planengine", "import app.planengine")):
                module = stripped.split()[1]
                if module not in allowed:
                    problems.append(f"{path.name}:{n}: {stripped}")
    assert not problems, "\n".join(problems)
