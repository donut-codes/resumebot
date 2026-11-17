"""What the page program must not run and must not import (the owner's second guard, 2026-11-10).

The page serves the built page, the owner's reads and clicks, and the live stream. The schedule, the work queue, the
fetchers and the browser belong to the engine program. `test_page_has_no_engine.py` boots the page in a fresh
interpreter and checks both lists below.

The lists are the loops and the starters only. Library modules the page's routes reach by many paths (`fetch.polite`,
`postings.index`, `work.session_kinds`) are not on them: the owner's test is loops, not libraries.

Later lanes change this file on purpose, and only these three ways:
- the lane that adds the helper pool's backend module (compute-boundary) adds it to FORBIDDEN;
- the lane that adds the notice receiver (notice-wire) adds its task to ALLOWED_TASKS, written "module:coroutine";
- a lane that adds a new loop or a new starter to the engine adds it to FORBIDDEN or STARTERS.
Nothing is ever taken off a list to get a page through: make the page stop reaching it.
"""

#: Third-party libraries the page must not load at import or start: each is imported inside the function that uses it.
LAZY_LIBRARIES: tuple[str, ...] = ("fpdf", "docx", "openpyxl")

#: Modules that start loops or do the engine's work. None may be in `sys.modules` after the page has started, except as
#: `page_import_baseline.txt` lists (a pair "importer -> module", with the lane that clears it; the file only shrinks).
FORBIDDEN: tuple[str, ...] = (
    "app.jobsearch.loops.schedule",
    "app.jobsearch.work.queue.loop",
    "app.jobsearch.work.queue.handlers",
    "app.jobsearch.work.dispatcher",
    "app.jobsearch.work.wake",
    "app.jobsearch.browser.go",
    "app.jobsearch.browser.code",
    "app.jobsearch.browser.mcp",
    "app.jobsearch.missions.boards_night",
    "app.jobsearch.doors.scout",
    "app.jobsearch.gating.regate",
    "app.jobsearch.profile.after_save",
    "app.sites.read_probe",
    "app.jobsearch.browser.add_link",
    "app.jobsearch.owner_actions.runner",
    "app.jobsearch.work.engine_asks",
)

#: Functions that start a loop or the engine's start-up work: (module, dotted name). The test replaces each with one
#: that records the call and raises, then starts the page; a call is a failure.
STARTERS: tuple[tuple[str, str], ...] = (
    ("app.jobsearch.loops.schedule", "schedule_loop"),
    ("app.jobsearch.work.queue.loop", "run"),
    ("app.jobsearch.work.queue.loop", "Dispatcher.run"),
    ("app.jobsearch.browser.code", "start"),
    ("app.jobsearch.missions.boards_night", "start_in_background"),
    ("app.jobsearch.browser.tabs", "reconcile_at_app_start"),
    ("app.jobsearch.loops.startup", "run_startup_fixups"),
)

#: Tasks that may be running after the page has started, each "module:coroutine qualified name". None today: the
#: page's server (uvicorn) is not part of this test, and the notice receiver does not exist yet.
ALLOWED_TASKS: tuple[str, ...] = ()
