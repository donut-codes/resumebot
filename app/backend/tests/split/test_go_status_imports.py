"""Where Launch in Browser stands is read by the page program (`GET /api/v1/apply/go`), so the module that reads it
(`browser/go_status.py`) loads none of the runner: not `browser/go.py`, which opens Chrome, nor the browser's code.
The route answers what the owner's `GoRun` row says. Everything here is made up."""

import subprocess
import sys
from pathlib import Path

from app import db
from app.jobsearch.browser import go_status
from app.jobsearch.models import GoRun
from tests.support import OWNER

BACKEND = Path(__file__).resolve().parents[2]
GO = "/api/v1/apply/go"


def test_importing_the_status_read_loads_no_runner_and_no_browser_code():
    code = "\n".join([
        "import sys",
        "import app.jobsearch.browser.go_status",
        "loaded = [m for m in ('app.jobsearch.browser.go', 'app.jobsearch.browser.code') if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def test_the_role_routes_read_the_status_from_this_module():
    from app.jobsearch.routes import role

    assert role.go_status is go_status
    assert not hasattr(sys.modules["app.jobsearch.browser.go"], "stored"), "one place reads the row"


async def test_with_no_run_the_route_says_idle(client):
    assert (await client.get(GO)).json() == {**go_status.fresh(), "phase": "idle", "done": True}


async def test_the_route_answers_what_the_row_says(client):
    state = {**go_status.fresh(), "phase": "done", "done": True, "total": 2, "built": 2,
             "notes": ["Two roles opened."]}
    async with db.session_factory()() as session:
        session.add(GoRun(owner=OWNER, state=state))
        await session.commit()
    assert (await client.get(GO)).json() == state
