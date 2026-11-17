"""One fresh interpreter per import root, shared by the guard tests that read it.

A guard that says "importing X loads none of the engine's modules" must look at an interpreter that has imported nothing
else: this test process has already imported everything. Starting one costs 6 to 8 seconds, and five guards read the
same root (the page program), so each pytest worker starts the child once per distinct chain and every test of that
worker reads the answer. Workers do not share (no file lock is in the dependencies).

A chain is imported in order, and one snapshot of `sys.modules` is taken after each module. A module later in the chain
may be listed after an earlier one only when importing it first would load the earlier one anyway (a package, then its
submodule): the snapshot after the later module is then the same as if it had gone first. Everything here is made up.
"""

import functools
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from app import db

#: The process engine's source folder: the models import it.
PROCESS_ENGINE_SRC = Path(__file__).resolve().parents[3] / "packages" / "process-engine" / "src"
MARK = "@@MODULES@@"


@functools.cache
def loaded_after_importing(*chain: str) -> tuple[frozenset[str], ...]:
    """Import each module of `chain` in a fresh interpreter, in order, and return the names in `sys.modules` after each
    import (a tuple with one set per module)."""
    code = textwrap.dedent(f"""
        import importlib, json, sys
        sys.path.insert(0, {str(db.BACKEND_DIR)!r})
        sys.path.insert(0, {str(PROCESS_ENGINE_SRC)!r})
        snapshots = []
        for name in {list(chain)!r}:
            importlib.import_module(name)
            snapshots.append(sorted(sys.modules))
        sys.stdout.write("\\n{MARK}" + json.dumps(snapshots))
        """)
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
    return tuple(frozenset(names) for names in json.loads(done.stdout.rsplit(MARK, 1)[1]))
