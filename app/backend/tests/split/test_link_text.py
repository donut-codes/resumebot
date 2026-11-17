"""The page checks a pasted link without loading the engine's link reader (`browser/add_link.py`): the check is its own
module, `browser/link_text.py`. Every name here is made up."""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_the_link_check_loads_no_link_reader():
    code = "\n".join([
        "import sys",
        "import app.jobsearch.routes.role",
        "from app.jobsearch.browser import link_text",
        "assert 'app.jobsearch.browser.add_link' not in sys.modules",
        "assert link_text.read_link('https://jobs.example.test/jobs/7001')[1] == 'https://jobs.example.test/jobs/7001'",
        "try:",
        "    link_text.read_link('not a link')",
        "except link_text.LinkError:",
        "    pass",
        "else:",
        "    raise AssertionError('a text that is no link was taken')",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
