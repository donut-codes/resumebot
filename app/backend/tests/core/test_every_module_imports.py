"""Every module in the package imports: a copied module that quietly needs an upstream module that was left behind fails
here, by name, instead of on first use. (Once a fresh interpreter was started for the lot; the suite already imports
the package in its own process, which finds a missing module just as well and costs nothing.)"""

import importlib
import pkgutil
from pathlib import Path

import app


def module_names() -> list[str]:
    return sorted(m.name for m in pkgutil.walk_packages(app.__path__, "app."))


def test_every_module_imports():
    names = module_names()
    assert "app.jobsearch.main" in names and "app.jobsearch.process" in names and "app.mailmine.message" in names
    failures = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:  # every module's failure is named; none stops the rest
            failures.append(f"{name}: {type(error).__name__}: {error}")
    assert not failures, "\n".join(failures)


def test_nothing_in_the_package_names_an_upstream_module_that_is_not_here():
    here = {m for m in module_names()} | {"app"}
    known = ("app.bench",)  # the upstream bench harness, which stays upstream (PROVENANCE.md)
    missing = []
    for path in Path(app.__path__[0]).rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("from app", "import app")):
                module = stripped.split()[1].rstrip(",")
                if module.startswith(known):
                    continue
                base = module if module in here else module.rsplit(".", 1)[0]
                if module not in here and base not in here:
                    missing.append(f"{path.name}: {stripped}")
    assert not missing, missing
