"""Importing the place check (`gating/stated_place.py`) or the settle (`research/settle.py`) does not load the
re-gate (`gating/regate.py`). The re-gate is the engine's work (tests/split/forbidden.py), and the page's routes reach
these two modules when they are imported, so each imports the re-gate inside the one function that calls it. Each
module goes first in a fresh interpreter: this test process has already imported everything. Everything is made up."""

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REGATE = "app.jobsearch.gating.regate"


@pytest.mark.parametrize("module", ["app.jobsearch.gating.stated_place", "app.jobsearch.research.settle"])
def test_importing_the_module_does_not_load_the_regate(module):
    code = "\n".join([
        "import sys",
        f"import {module}",
        f"assert {REGATE!r} not in sys.modules, 'the re-gate was loaded at import'",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
