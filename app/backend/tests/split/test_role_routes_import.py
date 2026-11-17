"""The role routes (`app/jobsearch/routes/role.py`) are loaded by the page program for its reads, so importing them
loads neither Launch in Browser's runner, nor the add-a-link reader, nor the browser's code: the two handlers that
need them import them when they are called. Everything here is made up."""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ENGINE_ONLY = ("app.jobsearch.browser.go", "app.jobsearch.browser.add_link", "app.jobsearch.browser.code")


def test_importing_the_role_routes_loads_no_runner_no_link_reader_and_no_browser_code():
    code = "\n".join([
        "import sys",
        "import app.jobsearch.routes.role",
        f"loaded = [m for m in {ENGINE_ONLY!r} if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
