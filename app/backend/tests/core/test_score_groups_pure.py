"""The arithmetic of a fit imports no database code and runs with no session (docs/PRD.md 30.9): the guarantee that it
works the same on any engine, and that nothing it does waits on a write. Checked on the module's own imports, and by
importing it alone in a fresh interpreter."""

import ast
import subprocess
import sys
from pathlib import Path

from app.jobsearch.fit import score_groups

BACKEND = Path(score_groups.__file__).resolve().parents[3]

#: What an interpreter that imports only the arithmetic must never have loaded.
DATABASE_CODE = ("sqlalchemy", "asyncpg", "app.db", "app.database", "app.jobsearch.models",
                 "app.jobsearch.profile.store")


def test_the_arithmetic_imports_nothing_but_the_standard_library():
    tree = ast.parse(Path(score_groups.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import: it would name a module of the app"
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


def test_importing_the_arithmetic_alone_loads_no_database_code():
    probe = (
        "import sys\n"
        "import app.jobsearch.fit.score_groups\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {DATABASE_CODE!r} or m in {DATABASE_CODE!r})\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n")
    done = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"loaded database code: {done.stdout.strip()} {done.stderr.strip()}"


def test_it_scores_with_no_session_and_no_engine():
    got = score_groups.score(
        (score_groups.Single("a", 4, "met"), score_groups.FirstMatch("l", (
            score_groups.Level("x", 2, False), score_groups.Level("y", 1, True)))))
    assert (got.possible, got.earned, got.percent) == (6, 5.0, 83)
