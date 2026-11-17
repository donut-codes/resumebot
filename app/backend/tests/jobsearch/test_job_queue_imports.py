"""`work/job_queue.py` holds `ready`, which `GET /api/v1/agent/ready` asks, and that route is the page's. Importing it
must not load `session_kinds`, which loads `wake` (the engine's knock on the scout's door): `queued` imports `of_runner`
when it is called. Everything here is made up."""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_importing_the_job_queue_loads_no_session_kinds_and_no_wake():
    code = "\n".join([
        "import sys",
        "import app.jobsearch.work.job_queue",
        "loaded = [m for m in ('app.jobsearch.work.session_kinds', 'app.jobsearch.work.wake') if m in sys.modules]",
        "assert not loaded, loaded",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
