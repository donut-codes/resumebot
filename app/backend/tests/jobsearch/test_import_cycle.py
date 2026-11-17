"""A module of the polite fetcher's import ring imports cleanly as the first thing a fresh interpreter does.

Found when `tab_check.py` imported `browser_code` and `tab_register` in a fresh process and got "cannot import name
'public_url' from partially initialized module 'app.jobsearch.polite'": polite -> host_stops -> employer_posting ->
employers -> polite. Importing `app.jobsearch.cli` first hid it, and the test run (which imports everything through
conftest) never saw it. Each ring member and the browser's entry points go first in a process of their own. So does
each module that asks `placement` where a role may sit, and `placement` itself: the gates, the research module and the
pipeline import each other through it. So do the missions' leaf (`record`) and their interface (`core`), which import
no kind until one is asked for. So do the four modules that begin a background task by name (`engine_calls`): the door
must not close a ring of imports through them."""

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

FIRST = [
    "app.jobsearch.ats",
    "app.jobsearch.adapters",
    "app.jobsearch.browser.code",
    "app.jobsearch.browser.tabs",
    "app.jobsearch.fetch.polite",
    "app.jobsearch.fetch.host_stops",
    "app.jobsearch.employers.follow",
    "app.jobsearch.employers.core",
    "app.jobsearch.work.session_kinds",
    "app.jobsearch.work.hand_outs",
    "app.jobsearch.postings.whole",
    "app.jobsearch.postings.this_posting",
    "app.jobsearch.postings.text",
    "app.jobsearch.pipeline.quiet",
    "app.jobsearch.pipeline.placement",
    "app.jobsearch.pipeline.deck",
    "app.jobsearch.pipeline.favorites",
    "app.jobsearch.pipeline.lifecycle",
    "app.jobsearch.pipeline.employer_limit",
    "app.jobsearch.pipeline.dial_hold",
    "app.jobsearch.gating.regate",
    "app.jobsearch.gating.stated_place",
    "app.jobsearch.gating.gate_facts",
    "app.jobsearch.research.analysis",
    "app.jobsearch.missions.record",
    "app.jobsearch.missions.core",
    "app.jobsearch.missions.boards_night",
    "app.jobsearch.postings.sourced",
    "app.sites.wake_ops",
    "app.jobsearch.owner_actions.runner",
    "app.jobsearch.owner_actions.reading",
    "app.jobsearch.owner_actions.mcp",
]


@pytest.mark.parametrize("module", FIRST)
def test_the_module_imports_first_in_a_fresh_interpreter(module):
    done = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=BACKEND, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def test_what_a_link_says_loads_no_reader_until_one_is_asked():
    """`ats` is what about forty modules import for a label, a name or a board's address. Importing it must not bring in
    the readers (the HTTP client, the polite fetcher, every system's code): each of those modules would carry all of
    that, and the polite fetcher's ring of imports would close through it. The first question that needs a system's
    rules loads them."""
    code = "\n".join([
        "import sys",
        "import app.jobsearch.ats as ats",
        "from app.sites import registry",
        "assert 'httpx' not in sys.modules and 'app.jobsearch.adapters' not in sys.modules, 'importing loaded readers'",
        "run = next(r for r in registry.current().runbooks if r.kind == 'job_system' and r.address.get('board_url')"
        " and r.flags.needs == ('slug',))",
        "board = run.address['board_url'][0]['template'].format(slug='acme')",
        "assert ats.label(run.key) == run.name and ats.parse(board)",
        "assert 'app.jobsearch.adapters' in sys.modules",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def test_what_a_link_says_is_the_real_rules_whenever_it_is_first_asked_even_while_a_test_stands_in_for_the_readers():
    """Regression case: `test_db_resilience` rebinds `adapters.ADAPTERS` to a stand-in (as many tests do, for the code
    that reads a board), and its night-read test is the first thing in its pytest worker to ask `ats` anything. The
    table `ats` loads lazily was filled from the stand-in and kept: that test failed (`'_SlowFailingBoard' object has no
    attribute 'needs'`) and every later test in the worker that parsed a link or asked for a label failed with it, so a
    run failed now and then by the order xdist dealt the tests. The first look is the thing under test, so it needs an
    interpreter of its own, and that is why this test takes a second or two."""
    code = "\n".join([
        "from unittest import mock",
        "from app.jobsearch import adapters, ats",
        "from app.sites import registry",
        "run = next(r for r in registry.current().runbooks if r.key in adapters.READERS"
        " and r.address.get('board_url') and r.flags.needs == ('slug',))",
        "board = run.address['board_url'][0]['template'].format(slug='acme')",
        "with mock.patch.object(adapters, 'ADAPTERS', {run.key: object()}):",
        "    link = ats.parse(board)",
        "    assert ats.label(run.key) == run.name and ats.complete(run.key, {'slug': 'acme'})",
        "assert link is not None and link.reader is adapters.READERS[run.key], link",
        "assert list(ats.SYSTEMS) == [*adapters.ADAPTERS, *adapters.POSTING_ONLY, *adapters.UNREAD]",
    ])
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
